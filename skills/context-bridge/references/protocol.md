# Protocol and deployment

## Runtime

Python 3.10+; standard library only. Persistent data lives in `~/.context-bridge` (override `CONTEXT_BRIDGE_HOME` for isolated profiles). Files are owner-readable/writable. SQLite stores host history; local membership files store access credentials. Usernames are display labels, not verified real-world identities. Each membership has an independent random credential, so identical names do not share credentials.

The API exposes context, membership metadata, and context-management operations. It never evaluates commands, reads arbitrary paths, or offers filesystem browsing. It does not provide distributed consensus or automatic host failover. Each session has one authoritative host; anyone can host another session. Members can save the history with `pull SESSION --output history.json`.

## Network modes

- Local: `start --local` creates a loopback-only test session; another computer cannot use its code.
- Tunnel: install cloudflared from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/ (on macOS, `brew install cloudflared`), then `start --tunnel`. No account/domain/payment is needed for Quick Tunnels. Cloudflare relays and terminates TLS, so this mode is not end-to-end encrypted or independent of third-party infrastructure. It is intended for temporary collaboration and has no uptime guarantee. Port exposure is outbound; the database stays local.
- Direct: `start --public-url https://your-host.example --port 8765` behind a configured HTTPS reverse proxy to `127.0.0.1:8765`. You manage DNS/addressing, firewall and inbound reachability. It requires no Context Bridge relay or directory. A public IP alone does not bypass carrier NAT; port forwarding may be unavailable. Domains and ISP connectivity may have their own costs. TLS certificates and reverse-proxy setup are not provisioned by this skill.
- Trusted LAN only: `start --public-url http://192.168.1.10:8765 --bind 0.0.0.0 --port 8765 --allow-insecure`; join with `--allow-insecure`. This transmits credentials and chat unencrypted and is unsuitable for the public internet.

Host/tunnel processes must remain running. Quick Tunnel addresses change on resume. Join the new code to update an existing membership without losing its identity. This implementation polls ordinary JSON, avoiding Quick Tunnel's lack of SSE support.

## Transcript format

```json
[
  {"message_id":"chat-123:user:1","role":"user","text":"Build the importer."},
  {"message_id":"chat-123:assistant:2","role":"assistant","text":"The importer is complete; CSV and TSV checks passed."}
]
```

`import SESSION transcript.json --source chat-123` publishes in array order. IDs supplied by adapters are preserved. When IDs are absent, they are deterministically derived from source ID and array index: use a stable source ID and an append-only transcript. Reordering or editing previously published messages causes a conflict; add a correction as a new message. Roles are `user`, `assistant`, `tool`, `summary`. Text is UTF-8, at most 64 KiB per message. Source strings are at most 500 characters; IDs at most 160. Import splits large files into bounded requests; a later batch failure may leave earlier batches committed, and retries deduplicate them.

`created_at` is host receipt time; imported messages retain their array order, not necessarily original wall-clock chronology across users. Original timestamp information can be included in the text. Previous messages from a participant's chat are attributed to that participant with role and source preserved; there is no proof of authorship of pasted content.

## HTTP API v1

Session codes are `cb1_` plus unpadded base64url JSON `{v:1,url,session,invite}`. Session IDs are 32 lowercase hex characters; invite secrets are 64. Codes are long on purpose: human-short global lookup requires a directory or discovery network.

All calls use `/v1/sessions/{session}` at the decoded origin. HTTPS is required outside loopback unless explicitly allowing insecure LAN. Clients must not forward credentials through redirects. JSON bodies are limited to 1 MiB. Credentials go in `Authorization: Bearer SECRET`, never URL parameters. No cookies or browser CORS permissions are used.

| Method/path | Credential | Body/result |
| --- | --- | --- |
| POST `/join` | Invite | `{username}` → `{member_id,username,token,session,title}` |
| GET `/context?after=0&limit=200` | Member | `{session,title,events,next_cursor,through,has_more}` |
| POST `/events` | Member | `{events:[{message_id,role,text,source}]}` → `{sequences}` |
| GET `/members` | Member | `{members:[{id,username,revoked}]}` |
| POST `/admin/rotate` | Host admin | `{}` → `{code}` |
| POST `/admin/revoke` | Host admin | `{member_id}` → `{revoked}` |
| POST `/admin/stop` | Host admin | `{}` → `{stopped}` |

Each returned event has `seq,member_id,username,message_id,role,text,source,created_at`. Always use host-assigned username/member ID, not names embedded in message text. Posting the same member/message ID with identical content returns the original sequence; different content is rejected. Each batch is transactional. All members can read all context and append only under their own membership; no member can edit/delete earlier events through the API. Revocation preserves historical attribution.

Pagination: use `next_cursor` as `after` on the next request, preserving the first response's `through` value to read a finite snapshot while other users append. `limit` is 1–200. Empty results at or beyond the high-water mark have `has_more:false`. Save the last cursor only after consuming the messages. There are no subscriptions; poll at sensible intervals if the agent runtime supports a persistent integration.

No agent-specific chat adapter is bundled: the invoking agent reads its own accessible messages and produces the transcript. Agents without tool/network access can use an external integration implementing this protocol, but cannot execute this skill on their own.
