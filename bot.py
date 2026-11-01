#!/usr/bin/env python3
"""
Claude Code ↔ Telegram bridge.

Chat with Claude Code from Telegram: full agent (bash, files, tools), model
selection, session resume — everything the CLI does, on your phone.

Env:
  TELEGRAM_BOT_TOKEN      required — from @BotFather
  TELEGRAM_ALLOWED_CHATS  comma-separated chat ids allowed to use the bot
                          (empty = first chat that talks to it gets locked in)
  AGENT_CWD               working dir for the agent      (default /home/agent/workspace)
  AGENT_MODEL             default model                  (default: sonnet)
  AGENT_PERMISSION_MODE   default | acceptEdits | bypassPermissions | plan
                                                          (default bypassPermissions)
  AGENT_MAX_TURNS         default 30
  MODELS_URL              optional, e.g. http://localhost:7860/v1/models
  MODELS_KEY              bearer key for MODELS_URL
  ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN   provider for Claude Code

Run:  python3 bot.py
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
        query,
    )
except Exception as e:  # pragma: no cover
    raise SystemExit(f"claude-agent-sdk is required (pip install claude-agent-sdk): {e}")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
ALLOWED = {int(x) for x in os.environ.get("TELEGRAM_ALLOWED_CHATS", "").split(",") if x.strip()}
AGENT_CWD = os.environ.get("AGENT_CWD", "/home/agent/workspace")
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "sonnet")
PERMISSION_MODE = os.environ.get("AGENT_PERMISSION_MODE", "bypassPermissions")
MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "30"))
MODELS_URL = os.environ.get("MODELS_URL", "")
MODELS_KEY = os.environ.get("MODELS_KEY", "")
TG_LIMIT = 3900

# chat_id -> {"session": str|None, "model": str, "busy": bool}
STATE: dict[int, dict[str, Any]] = {}

HELP = """*Claude Code bot*

Just send a message — the agent runs it in `{cwd}` with full tools.

/new — start a fresh session
/model `<id>` — set the model (e.g. `sonnet`, `opus`, `haiku`)
/models — list models
/status — current model + session
/stop — cancel the running task
/help — this message
""".strip()


# --------------------------------------------------------------------------- #
# telegram helpers
# --------------------------------------------------------------------------- #
async def tg(client: httpx.AsyncClient, method: str, **params: Any) -> dict:
    try:
        r = await client.post(f"{API}/{method}", json=params, timeout=70)
        return r.json()
    except Exception as e:
        print("[tg]", method, "failed:", e, flush=True)
        return {"ok": False}


def chunks(text: str, size: int = TG_LIMIT) -> list[str]:
    if not text:
        return [""]
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > size:
            out.append(cur)
            cur = line[:size]
        else:
            cur += line
    if cur:
        out.append(cur)
    return out or [""]


async def send(client: httpx.AsyncClient, chat: int, text: str, reply_to: int | None = None) -> int | None:
    last_id = None
    for i, part in enumerate(chunks(text)):
        params = {"chat_id": chat, "text": part, "parse_mode": "Markdown",
                  "disable_web_page_preview": True}
        if reply_to and i == 0:
            params["reply_to_message_id"] = reply_to
        res = await tg(client, "sendMessage", **params)
        if not res.get("ok"):  # retry without markdown (unbalanced chars break parsing)
            params.pop("parse_mode", None)
            res = await tg(client, "sendMessage", **params)
        if res.get("ok"):
            last_id = res["result"]["message_id"]
    return last_id


async def edit(client: httpx.AsyncClient, chat: int, msg_id: int, text: str) -> None:
    body = {"chat_id": chat, "message_id": msg_id, "text": text[:TG_LIMIT],
            "disable_web_page_preview": True}
    res = await tg(client, "editMessageText", **body)
    if not res.get("ok"):
        body.pop("parse_mode", None)
        await tg(client, "editMessageText", **body)


# --------------------------------------------------------------------------- #
# model list
# --------------------------------------------------------------------------- #
async def fetch_models() -> list[str]:
    if MODELS_URL:
        try:
            headers = {"Authorization": f"Bearer {MODELS_KEY}"} if MODELS_KEY else {}
            async with httpx.AsyncClient(timeout=15) as cx:
                r = await cx.get(MODELS_URL, headers=headers)
                data = r.json().get("data") or []
                ids = [m.get("id") for m in data if m.get("id")]
                if ids:
                    return ids
        except Exception as e:
            print("[models]", e, flush=True)
    return ["sonnet", "opus", "haiku", "claude-sonnet-4-5", "claude-opus-4-1"]


# --------------------------------------------------------------------------- #
# the agent
# --------------------------------------------------------------------------- #
async def run_agent(client: httpx.AsyncClient, chat: int, prompt: str, status_id: int) -> None:
    st = STATE.setdefault(chat, {"session": None, "model": DEFAULT_MODEL, "busy": False})
    st["busy"] = True

    opts = ClaudeAgentOptions(
        cwd=AGENT_CWD,
        model=st.get("model") or DEFAULT_MODEL,
        permission_mode=PERMISSION_MODE,
        max_turns=MAX_TURNS,
        allowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite"],
    )
    if st.get("session"):
        opts.resume = st["session"]

    text_acc: list[str] = []
    tools: list[str] = []
    last_edit = 0.0

    try:
        async for msg in query(prompt=prompt, options=opts):
            sid = getattr(msg, "session_id", None)
            if sid:
                st["session"] = sid

            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_acc.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tools.append(block.name)
                now = time.time()
                if now - last_edit > 1.6:  # keep it under Telegram's edit rate limit
                    last_edit = now
                    preview = "\n".join(text_acc).strip() or "⏳ working…"
                    if tools:
                        preview = f"🔧 {', '.join(dict.fromkeys(tools[-4:]))}\n\n{preview}"
                    await edit(client, chat, status_id, preview)

            elif isinstance(msg, ResultMessage):
                if getattr(msg, "is_error", False):
                    err = getattr(msg, "result", "") or getattr(msg, "subtype", "error")
                    text_acc.append(f"\n\n⚠ {err}")

        final = "\n".join(text_acc).strip() or "(no text output)"
        if tools:
            final = f"🔧 {', '.join(dict.fromkeys(tools))}\n\n{final}"
        await edit(client, chat, status_id, final[:TG_LIMIT])
        if len(final) > TG_LIMIT:
            await send(client, chat, final[TG_LIMIT:])
    except Exception as e:
        await edit(client, chat, status_id, f"⚠ Error: {type(e).__name__}: {e}"[:TG_LIMIT])
    finally:
        st["busy"] = False


# --------------------------------------------------------------------------- #
# command handling
# --------------------------------------------------------------------------- #
async def handle(client: httpx.AsyncClient, chat: int, text: str, reply_to: int) -> None:
    st = STATE.setdefault(chat, {"session": None, "model": DEFAULT_MODEL, "busy": False})
    low = text.strip().lower()

    if low in ("/start", "/help", "/h"):
        await send(client, chat, HELP.format(cwd=AGENT_CWD), reply_to)
        return

    if low == "/status":
        await send(client, chat,
                   f"*model:* `{st.get('model')}`\n"
                   f"*session:* `{st.get('session') or 'new'}`\n"
                   f"*cwd:* `{AGENT_CWD}`\n"
                   f"*permissions:* `{PERMISSION_MODE}`", reply_to)
        return

    if low in ("/new", "/reset"):
        st["session"] = None
        await send(client, chat, "🆕 New session started.", reply_to)
        return

    if low == "/models":
        models = await fetch_models()
        listing = "\n".join(f"• `{m}`" for m in models[:40])
        await send(client, chat, f"*Available models*\n{listing}\n\nSet one with `/model <id>`", reply_to)
        return

    if low.startswith("/model"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await send(client, chat, f"Current model: `{st.get('model')}`\nUse `/model <id>`", reply_to)
            return
        st["model"] = parts[1].strip()
        await send(client, chat, f"✅ Model set to `{st['model']}`", reply_to)
        return

    if low == "/stop":
        st["busy"] = False
        await send(client, chat, "⏹ (the current turn will finish; next message starts fresh)", reply_to)
        return

    if st.get("busy"):
        await send(client, chat, "⏳ Still working on the previous message…", reply_to)
        return

    status = await send(client, chat, "⏳ thinking…", reply_to)
    if not status:
        return
    await run_agent(client, chat, text, status)


# --------------------------------------------------------------------------- #
# main loop (long polling — works behind NAT, no public URL needed)
# --------------------------------------------------------------------------- #
async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    print(f"[bot] starting · cwd={AGENT_CWD} model={DEFAULT_MODEL} mode={PERMISSION_MODE}", flush=True)

    offset = 0
    async with httpx.AsyncClient(timeout=80) as client:
        me = await tg(client, "getMe")
        if me.get("ok"):
            print(f"[bot] connected as @{me['result'].get('username')}", flush=True)
        else:
            print("[bot] getMe failed — check the token", flush=True)

        while True:
            try:
                r = await client.get(f"{API}/getUpdates",
                                     params={"offset": offset, "timeout": 50,
                                             "allowed_updates": '["message"]'},
                                     timeout=70)
                data = r.json()
            except Exception as e:
                print("[poll]", e, flush=True)
                await asyncio.sleep(3)
                continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat = (msg.get("chat") or {}).get("id")
                text = msg.get("text") or ""
                if not chat or not text:
                    continue

                if not ALLOWED:
                    ALLOWED.add(chat)
                    print(f"[bot] locked to first chat: {chat}", flush=True)
                if chat not in ALLOWED:
                    await send(client, chat, "⛔ Not authorised. Add this chat id to TELEGRAM_ALLOWED_CHATS.")
                    continue

                asyncio.create_task(handle(client, chat, text, msg.get("message_id")))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
