# Context Bridge

Free, MIT-licensed context sharing between users and agents. Each session runs on its host's computer, stores history locally, and provides a context-only HTTP API. Share a session code, join from another agent, and read previous contributions organized by username.

Includes a portable `SKILL.md`, a dependency-free Python CLI/server, and an installer. Requires Python 3.10+ and an agent that can run commands or HTTP requests. macOS/Linux are supported; Windows users can use WSL. It does not include a hosted directory, paid backend, or model subscription.

## Install

From this checkout:

```sh
python3 install.py
```

This installs `~/.local/bin/context-bridge` and copies the self-contained skill to both `~/.agents/skills/context-bridge` and `~/.codex/skills/context-bridge`. Reload your agent's skills if required. Other agents can load the same `SKILL.md` from their own skill folder (`--skill-dir PATH`), or implement the HTTP protocol. The installer never edits your shell configuration. If `~/.local/bin` is absent from PATH, invoke the printed absolute CLI path.

Install from GitHub with one command (requires Git and Python 3.10+):

```sh
git clone https://github.com/Ephrem-Adugna/context-bridge.git && python3 context-bridge/install.py
```

For repeat installs, run the installer from the existing checkout instead of cloning again. Users should review code from the repository they choose to install.

## Use with an agent

Tell your agent: **“Use context-bridge to start a shared session.”** It asks for a display name on first use, saves it locally, starts the host, and returns a session code. Tell another agent: **“Use context-bridge to join CODE.”** It fetches shared history and explains progress by username.

A skill cannot access hidden reasoning, recover inaccessible older chats, intercept every future message, or wake idle agents. It instructs the agent to publish accessible user-visible history, check for updates while working, and record progress. Automatic background chat capture needs a separate adapter for the chosen agent runtime. Only published context is visible to members.

## CLI example

```sh
context-bridge profile --name "Alex"
# Install cloudflared first for internet sharing: brew install cloudflared
context-bridge start --tunnel --title "Build the importer"
# On another device, omit CODE to paste it privately at a prompt:
context-bridge join CODE
context-bridge pull SESSION_ID
context-bridge append SESSION_ID --role summary --text "Importer finished; CSV checks passed." --id importer-progress-1
context-bridge import SESSION_ID transcript.json --source importer-chat
context-bridge pull SESSION_ID --output history.json
context-bridge members SESSION_ID
# Host-only controls:
context-bridge rotate SESSION_ID
context-bridge revoke SESSION_ID MEMBER_ID
context-bridge stop SESSION_ID
context-bridge resume SESSION_ID
```

`SESSION_ID` is returned by start/join. `sessions` lists local sessions. A joined membership retains its original username if you later change your device profile. Full import format and API details: [protocol](skills/context-bridge/references/protocol.md).

## Connectivity and privacy

- **Fully self-hosted:** `start --public-url https://YOUR_HOST --port 8765` behind your HTTPS reverse proxy. You supply a reachable address, TLS and router/firewall configuration. No Context Bridge infrastructure is involved.
- **Home Wi-Fi:** `start --tunnel` manages an installed `cloudflared` process. [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) provide a free temporary address without an account, but depend on Cloudflare and have service limits. Cloudflare can see the relayed traffic; this is transport encryption, not end-to-end encryption. Chat storage remains local.
- **Same computer test:** `start --local`. Its loopback code will not connect from a different computer.

The host must stay online and awake. A resumed temporary tunnel gets a new address and code. Participants run `join NEW_CODE` to update their saved address. There is one authoritative host per session, no consensus network or host failover. Any member can export a copy of the history, but exports are not automatic replicas.

A session code contains the host address and a 256-bit invite secret. Anyone with it can join and read the shared history. Rotating it prevents new joins with the old code; revoke member IDs to remove existing access. Revocation cannot erase copies already downloaded. Names are self-selected labels, not verified identities. Public unauthenticated requests cannot read the chat. Local profile/credentials are stored with private file permissions under `~/.context-bridge`; back up that directory to preserve hosted history. Keep credentials and unrelated private chats out of shared transcripts.

The API only handles context and session membership. It does not execute tools or browse the host filesystem. This is a small collaboration implementation, not an internet-scale service with abuse prevention or guaranteed uptime; keep invitations within the intended group.

## Development

```sh
python3 -m unittest discover -s tests -v
```

No external Python packages or paid services are required for the test suite. Tunnel reachability must be tested separately on a real network with cloudflared installed.
