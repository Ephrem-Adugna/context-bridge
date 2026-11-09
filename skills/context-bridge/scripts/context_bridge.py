#!/usr/bin/env python3
"""Context Bridge: local-first shared context, Python 3.10+, no dependencies."""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

VERSION = '1.0.0'
MAX_BODY = 1024 * 1024
PAGE_SIZE = 200
ROLES = {'user', 'assistant', 'tool', 'summary'}


def root():
    p = Path(os.environ.get('CONTEXT_BRIDGE_HOME', str(Path.home() / '.context-bridge'))).expanduser()
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(6))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, path)


def read(path):
    return json.loads(path.read_text())


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def username(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80 or any(ord(c) < 32 for c in value):
        raise ValueError('Username must contain 1–80 characters without control characters.')
    return value.strip()


def profile():
    path = root() / 'profile.json'
    if not path.exists():
        if not sys.stdin.isatty():
            raise ValueError('No saved username. Ask the user for their name, then run: profile --name NAME')
        save(path, {'username': username(input('What name should other session members see? '))})
    return read(path)


def valid_url(value, insecure=False):
    p = urllib.parse.urlsplit(value)
    if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in {'', '/'}:
        raise ValueError('Use an HTTP(S) origin with no credentials, path, query, or fragment.')
    if p.hostname.rstrip('.') == 'api.trycloudflare.com':
        raise ValueError('Cloudflare provisioning API is not a session address.')
    if p.port == 0:
        raise ValueError('URL port must be 1–65535.')
    local = p.hostname == 'localhost'
    try:
        local = ipaddress.ip_address(p.hostname).is_loopback
    except ValueError:
        pass
    if p.scheme == 'http' and not local and not insecure:
        raise ValueError('Public connections require HTTPS. --allow-insecure permits trusted LAN HTTP only.')
    return value.rstrip('/')


def encode_code(url, session, invite):
    raw = json.dumps({'v': 1, 'url': url, 'session': session, 'invite': invite}, separators=(',', ':')).encode()
    return 'cb1_' + base64.urlsafe_b64encode(raw).decode().rstrip('=')


def decode_code(code, insecure=False):
    if len(code) > 4096 or not code.startswith('cb1_'):
        raise ValueError('Invalid Context Bridge code.')
    try:
        value = json.loads(base64.b64decode(code[4:] + '=' * (-len(code[4:]) % 4), altchars=b'-_', validate=True))
        if value['v'] != 1 or not re.fullmatch('[a-f0-9]{32}', value['session']) or not re.fullmatch('[a-f0-9]{64}', value['invite']):
            raise ValueError()
        value['url'] = valid_url(value['url'], insecure)
        return value
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError('Invalid session code or unsafe connection address.') from e


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, path, token=None, body=None):
    # Also reject a provisioning address saved by an older version before
    # constructing any authenticated request. LAN transport was opted into earlier.
    url = valid_url(url, insecure=True)
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        if len(data) > MAX_BODY:
            raise ValueError('Request is too large; split the transcript into smaller batches.')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url + path, data=data, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
            raw = response.read(32 * MAX_BODY + 1)
            if len(raw) > 32 * MAX_BODY:
                raise ValueError('Server response too large.')
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raise ValueError('Host returned HTTP %s: %s' % (e.code, e.read(2000).decode(errors='replace'))) from None
    except urllib.error.URLError as e:
        raise ValueError('Cannot reach the host: %s' % e.reason) from None


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


@contextmanager
def database(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys=ON')
        with db:
            yield db
    finally:
        db.close()


def init_db(path, name, token):
    with database(path) as db:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE members(id TEXT PRIMARY KEY, username TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, member_id TEXT NOT NULL REFERENCES members(id), message_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), UNIQUE(member_id,message_id));
        ''')
        member = uuid.uuid4().hex
        db.execute('INSERT INTO members(id,username,token_hash) VALUES(?,?,?)', (member, name, digest(token)))
    return member


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, cfg, dbpath):
        super().__init__(address, Handler)
        self.cfg = cfg
        self.dbpath = dbpath
        self.gate = threading.BoundedSemaphore(24)

    def process_request(self, request_socket, client_address):
        if not self.gate.acquire(blocking=False):
            request_socket.close()
            return
        try:
            super().process_request(request_socket, client_address)
        except BaseException:
            self.gate.release()
            raise

    def process_request_thread(self, request_socket, client_address):
        try:
            super().process_request_thread(request_socket, client_address)
        finally:
            self.gate.release()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'ContextBridge/1'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass  # Never log invite codes, credentials, or chat bodies.

    def reply(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def payload(self):
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('Transfer encoding is unsupported.')
        if self.headers.get_content_type() != 'application/json':
            raise ValueError('Expected application/json.')
        n = int(self.headers.get('Content-Length', '0'))
        if n < 1 or n > MAX_BODY:
            raise ValueError('Body must be 1 byte to 1 MiB.')
        value = json.loads(self.rfile.read(n))
        if not isinstance(value, dict):
            raise ValueError('Expected a JSON object.')
        return value

    def do_GET(self):
        self.handle_api('GET')

    def do_POST(self):
        self.handle_api('POST')

    def handle_api(self, method):
        try:
            self.route(method)
        except (ValueError, TypeError, KeyError, UnicodeError) as e:
            self.reply(400, {'error': str(e)[:300]})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self.reply(500, {'error': 'Host could not process request.'})

    def route(self, method):
        cfg = self.server.cfg
        p = urllib.parse.urlsplit(self.path)
        prefix = '/v1/sessions/' + cfg['session']
        if p.path != prefix and not p.path.startswith(prefix + '/'):
            self.reply(404, {'error': 'Unknown endpoint.'})
            return
        endpoint = p.path[len(prefix):]
        token = self.headers.get('Authorization', '').removeprefix('Bearer ')
        admin = hmac.compare_digest(digest(token), cfg['admin_hash'])
        with database(self.server.dbpath) as db:
            member = db.execute('SELECT * FROM members WHERE token_hash=? AND revoked=0', (digest(token),)).fetchone()
            if endpoint == '/join' and method == 'POST':
                if not hmac.compare_digest(digest(token), cfg['invite_hash']):
                    self.reply(401, {'error': 'Invalid invite.'})
                    return
                name = username(self.payload()['username'])
                new_token, mid = secrets.token_hex(32), uuid.uuid4().hex
                db.execute('INSERT INTO members(id,username,token_hash) VALUES(?,?,?)', (mid, name, digest(new_token)))
                db.commit()
                self.reply(201, {'member_id': mid, 'username': name, 'token': new_token, 'session': cfg['session'], 'title': cfg['title']})
                return
            if endpoint.startswith('/admin/'):
                if not admin:
                    self.reply(401, {'error': 'Host credential required.'})
                    return
                if method != 'POST':
                    self.reply(405, {'error': 'Use POST.'})
                    return
                body = self.payload()
                if endpoint == '/admin/rotate':
                    invite = secrets.token_hex(32)
                    cfg['invite_hash'] = digest(invite)
                    cfg['invite'] = invite
                    save(self.server.cfgpath, cfg)
                    self.reply(200, {'code': encode_code(cfg['url'], cfg['session'], invite)})
                elif endpoint == '/admin/revoke':
                    changed = db.execute('UPDATE members SET revoked=1 WHERE id=?', (body['member_id'],)).rowcount
                    db.commit()
                    self.reply(200 if changed else 404, {'revoked': bool(changed)})
                elif endpoint == '/admin/stop':
                    self.reply(200, {'stopped': True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    self.reply(404, {'error': 'Unknown endpoint.'})
                return
            if not member:
                self.reply(401, {'error': 'Membership required.'})
                return
            if endpoint == '/context' and method == 'GET':
                q = urllib.parse.parse_qs(p.query)
                after = int(q.get('after', ['0'])[0])
                limit = int(q.get('limit', [str(PAGE_SIZE)])[0])
                high = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
                through = min(int(q.get('through', [str(high)])[0]), high)
                if after < 0 or through < 0 or not 1 <= limit <= PAGE_SIZE:
                    raise ValueError('Invalid pagination.')
                rows = db.execute('SELECT e.seq,e.member_id,m.username,e.message_id,e.role,e.text,e.source,e.created_at FROM events e JOIN members m ON e.member_id=m.id WHERE e.seq>? AND e.seq<=? ORDER BY e.seq LIMIT ?', (after, through, limit)).fetchall()
                events = [dict(r) for r in rows]
                cursor = events[-1]['seq'] if events else after
                self.reply(200, {'session': cfg['session'], 'title': cfg['title'], 'events': events, 'next_cursor': cursor, 'through': through, 'has_more': cursor < through})
            elif endpoint == '/members' and method == 'GET':
                self.reply(200, {'members': [dict(r) for r in db.execute('SELECT id,username,revoked FROM members ORDER BY rowid')]})
            elif endpoint == '/events' and method == 'POST':
                events = self.payload()['events']
                if not isinstance(events, list) or not 1 <= len(events) <= PAGE_SIZE:
                    raise ValueError('Send 1–200 events.')
                result = []
                for event in events:
                    if not isinstance(event, dict):
                        raise ValueError('Each event must be an object.')
                    role, text = event['role'], event['text']
                    mid, source = event['message_id'], event.get('source', '')
                    if not isinstance(role, str) or role not in ROLES or not isinstance(text, str) or not text.strip() or len(text.encode()) > 65536:
                        raise ValueError('Invalid role or text; maximum text is 64 KiB.')
                    if not isinstance(mid, str) or not 1 <= len(mid) <= 160 or not isinstance(source, str) or len(source) > 500:
                        raise ValueError('Invalid message_id or source.')
                    existing = db.execute('SELECT seq,role,text,source FROM events WHERE member_id=? AND message_id=?', (member['id'], mid)).fetchone()
                    if existing:
                        if (existing['role'], existing['text'], existing['source']) != (role, text, source):
                            raise ValueError('message_id already exists with different content.')
                        result.append(existing['seq'])
                    else:
                        cur = db.execute('INSERT INTO events(member_id,message_id,role,text,source) VALUES(?,?,?,?,?)', (member['id'], mid, role, text, source))
                        result.append(cur.lastrowid)
                db.commit()
                self.reply(200, {'sequences': result})
            else:
                self.reply(404, {'error': 'Unknown endpoint.'})


def membership(sid):
    if not re.fullmatch('[a-f0-9]{32}', sid):
        raise ValueError('Use the session ID returned by start or join.')
    path = root() / 'memberships' / (sid + '.json')
    if not path.exists():
        raise ValueError('No membership for this session on this device.')
    return read(path)


def remote(m, endpoint, body=None, admin=False):
    token = m.get('admin') if admin else m['token']
    if not token:
        raise ValueError('Only the host can perform this action.')
    return request(m['url'], '/v1/sessions/' + m['session'] + endpoint, token, body)


def serve(cfgpath):
    cfg = read(cfgpath)
    os.umask(0o077)
    server = Server((cfg['bind'], cfg['port']), cfg, cfgpath.parent / 'history.sqlite3')
    server.cfgpath = cfgpath
    cfg['port'] = server.server_port
    tunnel = None
    try:
        if cfg.get('tunnel'):
            binary = shutil.which('cloudflared')
            if not binary:
                raise ValueError('Install cloudflared first, or use --public-url or --local.')
            tunnel = subprocess.Popen([binary, 'tunnel', '--url', 'http://127.0.0.1:' + str(server.server_port), '--no-autoupdate'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            found = threading.Event()
            addresses = []
            def collect():
                try:
                    for line in tunnel.stdout:
                        for match in re.finditer(r'https://([a-z0-9-]+)\.trycloudflare\.com(?=[\s/\"\'<>|]|$)', line):
                            # The provisioning API also appears in failure logs.
                            # It is never a session origin and must not receive credentials.
                            if match.group(1) != 'api' and not addresses:
                                addresses.append(match.group())
                                found.set()
                finally:
                    # EOF wakes startup immediately when cloudflared fails.
                    found.set()
            threading.Thread(target=collect, daemon=True).start()
            if not found.wait(45):
                raise ValueError('Tunnel did not provide an address within 45 seconds.')
            if not addresses or tunnel.poll() is not None:
                raise ValueError('Tunnel exited without providing a usable session address.')
            cfg['url'] = addresses[0]
        elif not cfg.get('url'):
            cfg['url'] = 'http://127.0.0.1:' + str(server.server_port)
        save(cfgpath, cfg)
        mpath = root() / 'memberships' / (cfg['session'] + '.json')
        m = read(mpath)
        m['url'] = cfg['url']
        save(mpath, m)
        save(cfgpath.parent / 'ready.json', {'url': cfg['url'], 'pid': os.getpid()})
        def shutdown(*_):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        if tunnel:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()
                tunnel.wait()
        (cfgpath.parent / 'ready.json').unlink(missing_ok=True)


def start(args):
    name = profile()['username']
    if args.tunnel and not shutil.which('cloudflared'):
        raise ValueError('cloudflared is missing. Install it or use --public-url / --local.')
    sid = uuid.uuid4().hex
    folder = root() / 'hosts' / sid
    folder.mkdir(parents=True, mode=0o700)
    invite, token, admin = (secrets.token_hex(32) for _ in range(3))
    cfg = {'session': sid, 'title': args.title, 'invite': invite, 'invite_hash': digest(invite), 'admin_hash': digest(admin), 'bind': args.bind, 'port': args.port, 'tunnel': args.tunnel, 'url': valid_url(args.public_url, args.allow_insecure) if args.public_url else ''}
    mid = init_db(folder / 'history.sqlite3', name, token)
    save(folder / 'host.json', cfg)
    save(root() / 'memberships' / (sid + '.json'), {'session': sid, 'member_id': mid, 'username': name, 'token': token, 'admin': admin, 'url': cfg['url']})
    launch(folder, sid)


def launch(folder, sid):
    with open(folder / 'service.log', 'ab') as log:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_serve', str(folder / 'host.json')], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(500):
        if (folder / 'ready.json').exists():
            cfg = read(folder / 'host.json')
            output({'session': sid, 'username': membership(sid)['username'], 'url': cfg['url'], 'code': encode_code(cfg['url'], sid, cfg['invite']), 'note': 'Keep the host awake. The code grants access to shared context.'})
            return
        if proc.poll() is not None:
            raise ValueError('Host failed to start. See ' + str(folder / 'service.log'))
        time.sleep(0.1)
    proc.terminate()
    raise ValueError('Host startup timed out. See ' + str(folder / 'service.log'))


def run():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('profile', help='Show or save your device username')
    p.add_argument('--name')
    p = sub.add_parser('start', help='Start a persistent host process')
    p.add_argument('--title', default='Shared context')
    modes = p.add_mutually_exclusive_group(required=True)
    modes.add_argument('--local', action='store_true')
    modes.add_argument('--tunnel', action='store_true')
    modes.add_argument('--public-url')
    p.add_argument('--bind', default='127.0.0.1')
    p.add_argument('--port', type=int, default=0)
    p.add_argument('--allow-insecure', action='store_true')
    p = sub.add_parser('_serve', help=argparse.SUPPRESS)
    p.add_argument('config')
    p = sub.add_parser('join')
    p.add_argument('code', nargs='?', help='Omit to paste code privately at a prompt')
    p.add_argument('--allow-insecure', action='store_true')
    sub.add_parser('sessions')
    p = sub.add_parser('resume', help='Restart a stopped host, keeping its history and members')
    p.add_argument('session')
    for name in ('pull', 'append', 'import', 'members', 'rotate', 'revoke', 'stop', 'status'):
        p = sub.add_parser(name)
        p.add_argument('session')
        if name == 'pull':
            p.add_argument('--after', type=int, default=0)
            p.add_argument('--output', type=Path)
        if name == 'append':
            p.add_argument('--role', choices=sorted(ROLES), default='summary')
            p.add_argument('--text')
            p.add_argument('--file', type=Path)
            p.add_argument('--id', default=None, help='Stable source message ID for retries')
            p.add_argument('--source', default='')
        if name == 'import':
            p.add_argument('file', type=Path, help='JSON list of {role,text,message_id}; max 64 KiB per message')
            p.add_argument('--source', required=True, help='Stable identifier for the originating chat')
        if name == 'revoke':
            p.add_argument('member_id')
    args = parser.parse_args()
    if args.command == 'profile':
        if args.name is not None:
            save(root() / 'profile.json', {'username': username(args.name)})
        output(profile())
    elif args.command == 'start':
        start(args)
    elif args.command == '_serve':
        serve(Path(args.config))
    elif args.command == 'join':
        name = profile()['username']
        if args.code is None:
            import getpass
            args.code = getpass.getpass('Session code: ')
        code = decode_code(args.code, args.allow_insecure)
        mpath = root() / 'memberships' / (code['session'] + '.json')
        if mpath.exists():
            m = read(mpath)
            # A new tunnel address can be adopted without replacing existing credentials.
            m['url'] = code['url']
            remote(m, '/context?limit=1')
            save(mpath, m)
        else:
            result = request(code['url'], '/v1/sessions/' + code['session'] + '/join', code['invite'], {'username': name})
            m = {**result, 'url': code['url']}
            save(mpath, m)
        output({'session': code['session'], 'username': m['username'], 'member_id': m['member_id'], 'next': 'Run pull with this session ID to read all shared history.'})
    elif args.command == 'resume':
        m = membership(args.session)
        if 'admin' not in m:
            raise ValueError('Only the host can resume a session.')
        folder = root() / 'hosts' / args.session
        if (folder / 'ready.json').exists():
            pid = read(folder / 'ready.json')['pid']
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                (folder / 'ready.json').unlink(missing_ok=True)
            else:
                raise ValueError('The host process is still running. Use status or stop first.')
        launch(folder, args.session)
    elif args.command == 'sessions':
        output([{'session': m['session'], 'username': m['username'], 'url': m['url'], 'host': 'admin' in m} for p in sorted((root() / 'memberships').glob('*.json')) for m in [read(p)]])
    else:
        m = membership(args.session)
        if args.command == 'pull':
            cursor, through, events = args.after, None, []
            if cursor < 0:
                raise ValueError('--after must be nonnegative.')
            while True:
                path = '/context?after=%s&limit=%s' % (cursor, PAGE_SIZE)
                if through is not None:
                    path += '&through=' + str(through)
                result = remote(m, path)
                through = result['through']
                events.extend(result['events'])
                nxt = result['next_cursor']
                if result['has_more'] and nxt <= cursor:
                    raise ValueError('Host returned an invalid pagination cursor.')
                cursor = nxt
                if not result['has_more']:
                    break
            result = {'session': args.session, 'title': result['title'], 'events': events, 'next_cursor': cursor}
            if args.output:
                save(args.output, result)
                output({'saved': str(args.output), 'events': len(events), 'next_cursor': cursor})
            else:
                output(result)
        elif args.command in {'append', 'import'}:
            if args.command == 'append':
                if (args.text is None) == (args.file is None):
                    raise ValueError('Provide exactly one of --text or --file.')
                events = [{'role': args.role, 'text': args.text if args.text is not None else args.file.read_text(), 'message_id': args.id or uuid.uuid4().hex, 'source': args.source}]
            else:
                events = read(args.file)
                if not isinstance(events, list) or not events:
                    raise ValueError('Import must be a nonempty JSON list.')
                if any(not isinstance(e, dict) for e in events):
                    raise ValueError('Every imported event must be an object.')
                # Stable per-chat IDs make re-import safe. Preserve IDs supplied by adapters.
                events = [{**e, 'source': args.source, 'message_id': str(e.get('message_id') or hashlib.sha256((args.source + ':' + str(i)).encode()).hexdigest())} for i, e in enumerate(events)]
            batches, batch = [], []
            for event in events:
                if len(json.dumps({'events': [event]}).encode()) > MAX_BODY:
                    raise ValueError('An event is too large.')
                if batch and (len(batch) >= PAGE_SIZE or len(json.dumps({'events': batch + [event]}).encode()) > MAX_BODY):
                    batches.append(batch)
                    batch = []
                batch.append(event)
            if batch:
                batches.append(batch)
            seqs = []
            for batch in batches:
                seqs.extend(remote(m, '/events', {'events': batch})['sequences'])
            output({'sequences': seqs, 'message_ids': [e['message_id'] for e in events]})
        elif args.command == 'members':
            output(remote(m, '/members'))
        elif args.command == 'status':
            result = remote(m, '/context?limit=1')
            output({'online': True, 'session': result['session'], 'title': result['title'], 'events': result['through']})
        elif args.command == 'rotate':
            output(remote(m, '/admin/rotate', {}, admin=True))
        elif args.command == 'revoke':
            output(remote(m, '/admin/revoke', {'member_id': args.member_id}, admin=True))
        elif args.command == 'stop':
            output(remote(m, '/admin/stop', {}, admin=True))


if __name__ == '__main__':
    try:
        run()
    except (ValueError, OSError, sqlite3.Error) as error:
        print('Context Bridge: ' + str(error), file=sys.stderr)
        sys.exit(1)
