# Claude Code ↔ Telegram

Chat with **Claude Code from Telegram** — the full agent (bash, files, tools),
plus model selection and session resume. Same brain as the CLI, on your phone.

```
Telegram  →  bot.py  →  claude-agent-sdk  →  claude CLI  →  your provider
                ↑                                              │
                └────────── streamed replies ◄─────────────────┘
```

## What you get

- **Plain messages** are sent to the agent — it runs real commands, edits real
  files, in `AGENT_CWD`.
- **Live progress**: the reply message is edited as the agent works, showing
  which tools it used.
- **`/model <id>`** — switch model per chat (sonnet / opus / haiku / any id)
- **`/models`** — list models (pulls from your gateway's `/v1/models` if configured)
- **`/new`** — fresh session, **`/status`**, **`/stop`**, **`/help`**
- **Sessions persist** — follow-up messages continue the same conversation
- **Long replies** are split across messages automatically
- **Locked to your chat** — the first chat that talks to the bot is allow-listed
  (or set `TELEGRAM_ALLOWED_CHATS` explicitly)

## 1. Create the bot

1. Open Telegram → **@BotFather** → `/newbot`
2. Pick a name + username → you get a token like `8123456:AAH...`
3. (optional) `/setcommands` with:
   ```
   new - new session
   model - set model
   models - list models
   status - current state
   stop - stop
   help - help
   ```

## 2. Install (recommended — into your existing container)

The bot must run where `claude` lives. If you already run the
[claude-code-web](https://github.com/deepuyrf-blip/claude-code-web) container:

```bash
git clone https://github.com/deepuyrf-blip/claude-telegram-bot
cd claude-telegram-bot

TELEGRAM_BOT_TOKEN='8123456:AAH...' ./install-into-container.sh
```

That script installs the Python deps, copies `bot.py` into the container, and
starts it detached. Logs: `docker exec claude-code-web tail -f /home/agent/tgbot/bot.log`

Then message your bot → `/help`.

### Manual equivalent

```bash
docker exec -u root claude-code-web sh -lc \
  'pip install --break-system-packages claude-agent-sdk httpx'
docker exec claude-code-web sh -lc 'mkdir -p /home/agent/tgbot'
docker cp bot.py claude-code-web:/home/agent/tgbot/bot.py
docker exec -d claude-code-web sh -lc \
  "TELEGRAM_BOT_TOKEN='8123456:AAH...' nohup python3 /home/agent/tgbot/bot.py > /home/agent/tgbot/bot.log 2>&1"
```

## 3. Or run it standalone (Docker)

```bash
cp .env.example .env      # fill TELEGRAM_BOT_TOKEN + provider vars
docker compose up -d --build
```

The standalone image installs Node + the `claude` CLI itself.

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | **required**, from @BotFather |
| `TELEGRAM_ALLOWED_CHATS` | *(first chat)* | comma-separated chat ids allowed |
| `AGENT_CWD` | `/home/agent/workspace` | where the agent works |
| `AGENT_MODEL` | `sonnet` | default model |
| `AGENT_PERMISSION_MODE` | `bypassPermissions` | `default` / `acceptEdits` / `bypassPermissions` / `plan` |
| `AGENT_MAX_TURNS` | `30` | max agent turns per message |
| `MODELS_URL` | — | e.g. `http://127.0.0.1:3001/v1/models` for `/models` |
| `MODELS_KEY` | — | bearer key for `MODELS_URL` |
| `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` | — | provider for Claude Code |

## ⚠️ Security — read this

`AGENT_PERMISSION_MODE=bypassPermissions` means **the agent runs commands
without asking**. That is what makes it usable from a chat window, but it also
means anyone who can message the bot can run anything the container user can.

- Keep `TELEGRAM_ALLOWED_CHATS` set to **your** chat id.
- Prefer a throwaway container / dedicated workspace.
- If you want approvals, switch to `acceptEdits` and note that un-approved
  commands will stall (there is no UI to approve them from Telegram).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `getMe failed` | wrong token — re-copy from BotFather |
| Bot silent | `docker exec claude-code-web tail -50 /home/agent/tgbot/bot.log` |
| `claude not found` | the bot must run where the `claude` CLI is (inside your container) |
| Auth errors from the agent | set `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`, or fix `~/.claude/settings.json` |
| Replies cut off | they're split at ~3900 chars; very long output arrives as several messages |
| Bot replies to nobody else | expected — the first chat gets locked in |

## License

MIT
