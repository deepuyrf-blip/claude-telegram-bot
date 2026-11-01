#!/usr/bin/env bash
# Install the Telegram bot INTO an existing claude-code-web container.
# Usage:  TELEGRAM_BOT_TOKEN=123:abc ./install-into-container.sh
set -e
C="${CONTAINER:-claude-code-web}"
TOKEN="${TELEGRAM_BOT_TOKEN:?set TELEGRAM_BOT_TOKEN (from @BotFather)}"
CWD_AGENT="${AGENT_CWD:-/home/agent/workspace}"
MODEL="${AGENT_MODEL:-sonnet}"
CHATS="${TELEGRAM_ALLOWED_CHATS:-}"

echo "==> installing python deps in $C"
docker exec -u root "$C" sh -lc 'pip install --break-system-packages claude-agent-sdk httpx >/dev/null 2>&1 || true'
docker exec "$C" sh -lc 'python3 -c "import claude_agent_sdk, httpx" && echo deps-ok'

echo "==> copying bot"
docker exec "$C" sh -lc 'mkdir -p /home/agent/tgbot'
docker cp bot.py "$C":/home/agent/tgbot/bot.py

echo "==> stopping any old instance"
docker exec "$C" sh -lc 'pkill -f /home/agent/tgbot/bot.py 2>/dev/null || true'

echo "==> starting bot (detached)"
docker exec -d "$C" sh -lc "TELEGRAM_BOT_TOKEN='$TOKEN' TELEGRAM_ALLOWED_CHATS='$CHATS' \
  AGENT_CWD='$CWD_AGENT' AGENT_MODEL='$MODEL' AGENT_PERMISSION_MODE='bypassPermissions' \
  MODELS_URL='http://127.0.0.1:3001/v1/models' \
  nohup python3 /home/agent/tgbot/bot.py > /home/agent/tgbot/bot.log 2>&1"

sleep 3
echo "==> log:"
docker exec "$C" sh -lc 'tail -20 /home/agent/tgbot/bot.log || true'
echo
echo "Open Telegram, message your bot, send /help"
