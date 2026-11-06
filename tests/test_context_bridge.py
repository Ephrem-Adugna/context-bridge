import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / 'skills/context-bridge/scripts/context_bridge.py'
spec = importlib.util.spec_from_file_location('bridge', SCRIPT)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class APITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.invite, self.owner, self.admin = (secrets.token_hex(32) for _ in range(3))
        self.sid = 'a' * 32
        self.db = self.path / 'history.sqlite3'
        self.owner_id = b.init_db(self.db, 'Host', self.owner)
        self.cfg = {'session': self.sid, 'title': 'Testing', 'invite': self.invite, 'invite_hash': b.digest(self.invite), 'admin_hash': b.digest(self.admin)}
        self.server = b.Server(('127.0.0.1', 0), self.cfg, self.db)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.cfg['url'] = self.url
        self.server.cfgpath = self.path / 'host.json'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tmp.cleanup()

    def api(self, suffix, token=None, body=None):
        return b.request(self.url, '/v1/sessions/' + self.sid + suffix, token, body)

    def join(self, name='Guest'):
        return self.api('/join', self.invite, {'username': name})

    def event(self, mid='one', text='Hello'):
        return {'message_id': mid, 'role': 'assistant', 'text': text, 'source': 'test-chat'}

    def test_join_history_attribution_and_no_anonymous_read(self):
        self.api('/events', self.owner, {'events': [self.event()]})
        guest = self.join()
        history = self.api('/context', guest['token'])
        self.assertEqual(history['events'][0]['username'], 'Host')
        self.api('/events', guest['token'], {'events': [{**self.event('two'), 'username': 'Host'}]})
        self.assertEqual(self.api('/context', self.owner)['events'][1]['username'], 'Guest')
        with self.assertRaisesRegex(ValueError, '401'):
            self.api('/context')
        with self.assertRaisesRegex(ValueError, '401'):
            self.api('/join', 'wrong', {'username': 'Intruder'})

    def test_idempotency_and_atomic_rollback(self):
        payload = {'events': [self.event()]}
        first = self.api('/events', self.owner, payload)
        self.assertEqual(self.api('/events', self.owner, payload), first)
        with self.assertRaisesRegex(ValueError, '400'):
            self.api('/events', self.owner, {'events': [self.event('new'), self.event(text='Changed')]})
        self.assertEqual(len(self.api('/context', self.owner)['events']), 1)
        with self.assertRaisesRegex(ValueError, '400'):
            self.api('/events', self.owner, {'events': [self.event('new'), self.event('huge', 'a' * 65537)]})
        self.assertEqual(len(self.api('/context', self.owner)['events']), 1)

    def test_concurrent_writers_and_finite_pagination(self):
        guests = [self.join('Same name') for _ in range(3)]
        def write(i):
            return self.api('/events', guests[i]['token'], {'events': [self.event(str(n)) for n in range(80)]})
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            list(pool.map(write, range(3)))
        first = self.api('/context?limit=200', self.owner)
        self.assertEqual(len(first['events']), 200)
        self.assertTrue(first['has_more'])
        self.api('/events', self.owner, {'events': [self.event('later')]})
        second = self.api('/context?after=200&through=240', self.owner)
        self.assertEqual(len(second['events']), 40)
        self.assertFalse(second['has_more'])
        self.assertEqual(len(self.api('/context?after=240', self.owner)['events']), 1)
        self.assertEqual(len({g['member_id'] for g in guests}), 3)

    def test_rotate_and_revoke(self):
        guest = self.join()
        with self.assertRaisesRegex(ValueError, '401'):
            self.api('/admin/rotate', guest['token'], {})
        code = self.api('/admin/rotate', self.admin, {})['code']
        new = b.decode_code(code)
        with self.assertRaisesRegex(ValueError, '401'):
            self.join('Old invite')
        self.api('/context', guest['token'])
        self.api('/admin/revoke', self.admin, {'member_id': guest['member_id']})
        with self.assertRaisesRegex(ValueError, '401'):
            self.api('/context', guest['token'])
        self.api('/join', new['invite'], {'username': 'New invite'})

    def test_validation_and_bounded_payloads(self):
        for query in ('?limit=201', '?limit=-1', '?after=-1', '?through=-1'):
            with self.assertRaisesRegex(ValueError, '400'):
                self.api('/context' + query, self.owner)
        with self.assertRaisesRegex(ValueError, '400'):
            self.api('/join', self.invite, {'username': 'bad\nname'})
        with self.assertRaisesRegex(ValueError, '400'):
            self.api('/events', self.owner, {'events': [self.event(str(i)) for i in range(201)]})
        with self.assertRaisesRegex(ValueError, '404'):
            b.request(self.url, '/etc/passwd', self.owner)
        for code in ('bad', 'cb1_%%%%', b.encode_code('http://example.com', self.sid, self.invite)):
            with self.assertRaises(ValueError):
                b.decode_code(code)
        for url in ('https://user:pass@example.com', 'https://example.com/path', 'file:///tmp/data', 'https://example.com?secret=1'):
            with self.assertRaises(ValueError):
                b.valid_url(url)


class CLITest(unittest.TestCase):
    def cli(self, home, *args, ok=True):
        env = dict(os.environ, CONTEXT_BRIDGE_HOME=str(home))
        result = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, input='', text=True, capture_output=True, timeout=60)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def test_full_lifecycle_separate_users_resume_and_import(self):
        with tempfile.TemporaryDirectory() as td:
            host, guest = Path(td) / 'host', Path(td) / 'guest'
            self.assertIn('Ask the user', self.cli(host, 'profile', ok=False).stderr)
            self.cli(host, 'profile', '--name', 'Alex')
            self.cli(guest, 'profile', '--name', 'Sam')
            session = self.cli(host, 'start', '--local', '--title', 'CLI session')
            sid = session['session']
            try:
                self.cli(host, 'append', sid, '--text', 'Before Sam joined', '--id', 'start')
                member = self.cli(guest, 'join', session['code'])
                joined_again = self.cli(guest, 'join', session['code'])
                self.assertEqual(member['member_id'], joined_again['member_id'])
                self.assertEqual(self.cli(guest, 'pull', sid)['events'][0]['username'], 'Alex')
                transcript = Path(td) / 'transcript.json'
                transcript.write_text(json.dumps([{'role': 'user' if i % 2 == 0 else 'assistant', 'text': 'こんにちは' * 40 + str(i)} for i in range(230)]))
                self.cli(guest, 'import', sid, str(transcript), '--source', 'sam-chat')
                self.cli(guest, 'import', sid, str(transcript), '--source', 'sam-chat')
                history = self.cli(host, 'pull', sid)
                self.assertEqual(len(history['events']), 231)
                self.assertEqual(history['events'][-1]['username'], 'Sam')
                exported = Path(td) / 'history.json'
                self.cli(guest, 'pull', sid, '--output', str(exported))
                self.assertEqual(len(json.loads(exported.read_text())['events']), 231)
                self.assertEqual(exported.stat().st_mode & 0o777, 0o600)
                self.assertEqual((guest / 'profile.json').stat().st_mode & 0o777, 0o600)
                self.cli(host, 'stop', sid)
                for _ in range(50):
                    if not (host / 'hosts' / sid / 'ready.json').exists():
                        break
                    time.sleep(0.1)
                self.cli(host, 'resume', sid)
                self.assertEqual(len(self.cli(guest, 'pull', sid)['events']), 231)
                self.cli(host, 'revoke', sid, member['member_id'])
                self.assertIn('401', self.cli(guest, 'pull', sid, ok=False).stderr)
                self.assertEqual(self.cli(host, 'sessions')[0]['session'], sid)
            finally:
                self.cli(host, 'stop', sid, ok=True)

    def test_installer_portability(self):
        with tempfile.TemporaryDirectory() as td:
            prefix, skill = Path(td) / 'prefix', Path(td) / 'other-agent'
            result = subprocess.run([sys.executable, str(PROJECT / 'install.py'), '--prefix', str(prefix), '--skill-dir', str(skill)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((skill / 'context-bridge/SKILL.md').exists())
            self.assertTrue((skill / 'context-bridge/references/protocol.md').exists())
            version = subprocess.check_output([str(prefix / 'bin/context-bridge'), '--version'], text=True)
            self.assertEqual(version.strip(), '1.0.0')


if __name__ == '__main__':
    unittest.main()
