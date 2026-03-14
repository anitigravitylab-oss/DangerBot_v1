# Dangerbot — Discord × GitHub Copilot CLI Bridge

A Discord Gateway bot that bridges Discord messages into a persistent **GitHub Copilot CLI** session.  
Send instructions from Discord; get Copilot's response posted back as a reply.

---

## Features

- **Discord Gateway WebSocket** — no polling; uses real-time events
- **Slash commands**: `/cancel`, `/model` — cancel in-flight tasks or switch Copilot model on-the-fly
- **Multi-channel support** — run multiple bridge instances per channel with `--channel-id`
- **Stale-bridge takeover** — heartbeat detection auto-restarts crashed instances
- **Session continuity** — resumes a fixed Copilot session across bot restarts
- **Progress updates** — live "⏳ 処理中" status updates every few seconds while Copilot works

---

## Requirements

- Python 3.11+
- [GitHub Copilot CLI](https://githubnext.com/projects/copilot-cli/) (`copilot` command in PATH)
- A Discord Bot token with **Gateway Intents**: `MESSAGE_CONTENT`, `GUILD_MESSAGES`

```bash
pip install -r requirements.txt
```

---

## Setup

### 1. Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications) → New Application
2. Bot tab → Reset Token → copy token
3. Enable **Privileged Gateway Intents**: `SERVER MEMBERS`, `MESSAGE CONTENT`
4. OAuth2 → URL Generator → `bot` + `applications.commands` scopes, `Send Messages` + `Read Message History` permissions
5. Invite bot to your server

### 2. Register Slash Commands

The bridge automatically registers `/cancel` and `/model` slash commands on startup via the Discord REST API.  
No manual registration needed.

### 3. Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
# Edit .env with your Discord bot token, channel ID, etc.
```

| Variable | Required | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | Discord bot token |
| `DISCORD_DEFAULT_CHANNEL_ID` | ✅ | Channel ID to monitor for instructions |
| `DISCORD_DEFAULT_USER_ID` | ✅ | Discord user ID whose messages are treated as instructions |
| `COPILOT_PROJECT_ROOT` | ✅ | Absolute path to the project Copilot should work in |
| `DISCORD_COPILOT_MODEL` | | Default Copilot model (default: `gpt-5.4`) |
| `DISCORD_COPILOT_SESSION_ID` | | Fixed Copilot session ID for continuity across restarts |
| `BRIDGE_PROMPT_PREFIX` | | Custom prefix injected before each Discord message sent to Copilot |
| `BRIDGE_STATE_FILE` | | Override path for bridge state file |
| `BRIDGE_LOCK_FILE` | | Override path for bridge lock file |
| `BRIDGE_HEARTBEAT_FILE` | | Override path for heartbeat file |

### 4. Run

```bash
# Simple
python3 discord_to_copilot_bridge.py --watch --channel-id YOUR_CHANNEL_ID

# With the wrapper script (handles stale-bridge takeover)
bash run.sh

# Multiple bridges (different channels)
DISCORD_COPILOT_SESSION_ID=session-a bash run.sh &
DISCORD_DEFAULT_CHANNEL_ID=other-channel-id DISCORD_COPILOT_SESSION_ID=session-b \
  python3 discord_to_copilot_bridge.py --watch --channel-id other-channel-id
```

### 5. Systemd (optional)

```bash
cp deploy/copilot-bridge.service.example /etc/systemd/system/copilot-bridge.service
# Edit the service file: update WorkingDirectory, EnvironmentFile, ExecStart, log paths
systemctl daemon-reload
systemctl enable --now copilot-bridge
```

---

## Discord Commands

Once the bot is running in a channel, the following slash commands are available:

| Command | Description |
|---|---|
| `/cancel` | Cancel the currently running Copilot task |
| `/model <name>` | Switch the Copilot model (e.g. `gpt-5.4`, `claude-sonnet-4.6`) |

### Available Models

`claude-haiku-4-5` · `claude-sonnet-4-5` · `claude-sonnet-4.6` · `claude-opus-4-5` · `claude-opus-4.6` · `gpt-5.4` · `gpt-5.1` · `gpt-5-mini`

---

## Architecture

```
Discord user
    │  (message)
    ▼
Discord Gateway WebSocket
    │  (INTERACTION_CREATE / MESSAGE_CREATE events)
    ▼
discord_to_copilot_bridge.py
    │  (JSON-RPC over stdin/stdout)
    ▼
copilot --headless --stdio
    │  (tool calls, file edits, bash commands)
    ▼
Your project files
    │  (response text)
    ▼
Discord REST API → reply in channel
```

---

## License

MIT
