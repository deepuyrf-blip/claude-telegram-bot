#!/usr/bin/env python3
"""
Claude Code ↔ Telegram bridge.

Chat with Claude Code from Telegram: full agent (bash, files, tools), model
selection via tap-buttons, session resume — everything the CLI does, on your phone.

Env:
  TELEGRAM_BOT_TOKEN      required — from @BotFather
  TELEGRAM_ALLOWED_CHATS  comma-separated chat ids allowed (empty = lock to first chat)
  AGENT_CWD               working dir for the agent      (default /home/agent/workspace)
  AGENT_MODEL             default model                  (default: sonnet)
  AGENT_PERMISSION_MODE   default | acceptEdits | bypassPermissions | plan
  AGENT_MAX_TURNS         default 30
  MODELS_URL              optional, e.g. http://localhost:7860/v1/models
  MODELS_KEY              bearer key for MODELS_URL (defaults to ANTHROPIC_AUTH_TOKEN)
  ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN   provider for Claude Code

Run:  python3 bot.py
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
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
MODELS_KEY = os.environ.get("MODELS_KEY", "") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")


def _provider_from_settings() -> tuple[str, str]:
    """Fall back to ~/.claude/settings.json (what /admin writes) for base URL + key."""
    try:
        p = os.path.expanduser("~/.claude/settings.json")
        env = (json.load(open(p)) or {}).get("env", {}) or {}
        return (env.get("ANTHROPIC_BASE_URL", "") or "",
                env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or "")
    except Exception:
        return "", ""


if not BASE_URL or not MODELS_KEY:
    _b, _k = _provider_from_settings()
    BASE_URL = BASE_URL or _b.rstrip("/")
    MODELS_KEY = MODELS_KEY or _k

TG_LIMIT = 3900
PAGE = 8  # model buttons per page
UPLOAD_DIR = os.path.join(AGENT_CWD, "uploads")

STATE: dict[int, dict[str, Any]] = {}
MODELS: dict[str, Any] = {"list": [], "ts": 0.0, "page": {}}

HELP = """*Claude Code bot*

Send any message — the agent runs it in `{cwd}` with full tools.

Tap the buttons below, or use the commands.
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
        while len(line) > size:
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) > size:
            out.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        out.append(cur)
    return out or [""]


def kb(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


MAIN_KB = kb([
    [("🆕 New session", "act:new"), ("🤖 Models", "act:models")],
    [("📊 Status", "act:status"), ("⏹ Stop", "act:stop")],
])


async def send(client, chat: int, text: str, reply_to: int | None = None,
               markup: dict | None = None) -> int | None:
    last_id = None
    for i, part in enumerate(chunks(text)):
        params: dict[str, Any] = {"chat_id": chat, "text": part, "parse_mode": "Markdown",
                                  "disable_web_page_preview": True}
        if reply_to and i == 0:
            params["reply_to_message_id"] = reply_to
        if markup and i == len(chunks(text)) - 1:
            params["reply_markup"] = markup
        res = await tg(client, "sendMessage", **params)
        if not res.get("ok"):
            params.pop("parse_mode", None)
            res = await tg(client, "sendMessage", **params)
        if res.get("ok"):
            last_id = res["result"]["message_id"]
    return last_id


async def edit(client, chat: int, msg_id: int, text: str, markup: dict | None = None) -> None:
    body: dict[str, Any] = {"chat_id": chat, "message_id": msg_id, "text": text[:TG_LIMIT],
                            "disable_web_page_preview": True, "parse_mode": "Markdown"}
    if markup is not None:
        body["reply_markup"] = markup
    res = await tg(client, "editMessageText", **body)
    if not res.get("ok"):
        body.pop("parse_mode", None)
        await tg(client, "editMessageText", **body)


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
async def fetch_models(force: bool = False) -> list[str]:
    if not force and MODELS["list"] and time.time() - MODELS["ts"] < 300:
        return MODELS["list"]

    urls = []
    if MODELS_URL:
        urls.append(MODELS_URL)
    if BASE_URL:
        urls.append(BASE_URL + "/v1/models")
    for url in urls:
        try:
            headers = {"Authorization": f"Bearer {MODELS_KEY}"} if MODELS_KEY else {}
            async with httpx.AsyncClient(timeout=20) as cx:
                r = await cx.get(url, headers=headers)
                data = r.json()
                items = data.get("data") if isinstance(data, dict) else None
                ids = [m.get("id") for m in (items or []) if isinstance(m, dict) and m.get("id")]
                if ids:
                    MODELS["list"] = ids
                    MODELS["ts"] = time.time()
                    print(f"[models] loaded {len(ids)} from {url}", flush=True)
                    return ids
        except Exception as e:
            print("[models]", url, "->", e, flush=True)

    if not MODELS["list"]:
        MODELS["list"] = ["sonnet", "opus", "haiku", "claude-sonnet-4-5", "claude-opus-4-1"]
        MODELS["ts"] = time.time()
    return MODELS["list"]


def model_keyboard(page: int) -> tuple[str, dict]:
    models = MODELS["list"]
    pages = max(1, (len(models) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    MODELS["page"][page] = models[page * PAGE:(page + 1) * PAGE]

    rows: list[list[tuple[str, str]]] = []
    for i in range(0, len(MODELS["page"][page]), 2):
        pair = []
        for j in (i, i + 1):
            if j < len(MODELS["page"][page]):
                mid = MODELS["page"][page][j]
                label = mid if len(mid) <= 28 else mid[:26] + "…"
                pair.append((label, f"m:{page}:{j - i}"))
        rows.append(pair)

    nav = []
    if page > 0:
        nav.append(("« Prev", f"mp:{page-1}"))
    nav.append((f"{page+1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("Next »", f"mp:{page+1}"))
    rows.append(nav)
    rows.append([("🔄 Reload list", "act:models"), ("✖ Close", "noop_close")])

    text = (f"*Models* ({len(models)} available)\n"
            f"Current: `{STATE.get(next(iter(STATE), 0), {}).get('model', DEFAULT_MODEL)}`\n\n"
            "Tap one to use it.")
    return text, kb(rows)


# --------------------------------------------------------------------------- #
# agent
# --------------------------------------------------------------------------- #
async def run_agent(client, chat: int, prompt: str, status_id: int) -> None:
    st = STATE.setdefault(chat, {"session": None, "model": DEFAULT_MODEL, "busy": False})
    st["busy"] = True

    opts = ClaudeAgentOptions(
        cwd=AGENT_CWD,
        model=st.get("model") or DEFAULT_MODEL,
        permission_mode=PERMISSION_MODE,
        max_turns=MAX_TURNS,
        allowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep",
                       "WebFetch", "WebSearch", "TodoWrite"],
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
                if now - last_edit > 1.6:
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
# actions (shared by commands and buttons)
# --------------------------------------------------------------------------- #
async def do_action(client, chat: int, action: str, msg_id: int | None, reply_to: int | None = None):
    st = STATE.setdefault(chat, {"session": None, "model": DEFAULT_MODEL, "busy": False})

    if action == "new":
        st["session"] = None
        text, markup = "🆕 New session started.", MAIN_KB
    elif action == "status":
        text, markup = (f"*model:* `{st.get('model')}`\n"
                        f"*session:* `{st.get('session') or 'new'}`\n"
                        f"*cwd:* `{AGENT_CWD}`\n"
                        f"*permissions:* `{PERMISSION_MODE}`"), MAIN_KB
    elif action == "stop":
        st["busy"] = False
        text, markup = "⏹ Noted — the current turn will finish; next message starts fresh.", MAIN_KB
    elif action == "models":
        await fetch_models(force=True)
        text, markup = model_keyboard(0)
    else:
        return

    if msg_id:
        await edit(client, chat, msg_id, text, markup)
    else:
        await send(client, chat, text, reply_to, markup)


async def download_tg_file(client, file_id: str, suffix: str) -> str | None:
    """Fetch a Telegram file into AGENT_CWD/uploads and return its path."""
    r = await tg(client, "getFile", file_id=file_id)
    if not r.get("ok"):
        print("[dl] getFile failed:", r, flush=True)
        return None
    file_path = r["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    try:
        async with httpx.AsyncClient(timeout=180) as cx:
            resp = await cx.get(url)
        resp.raise_for_status()
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        dest = os.path.join(UPLOAD_DIR, f"{int(time.time())}_{secrets.token_hex(3)}{suffix}")
        with open(dest, "wb") as f:
            f.write(resp.content)
        print(f"[dl] saved {dest} ({len(resp.content)} bytes)", flush=True)
        return dest
    except Exception as e:
        print("[dl] failed:", e, flush=True)
        return None


async def handle_media(client, chat: int, file_id: str, suffix: str, caption: str,
                       reply_to: int, kind: str = "image") -> None:
    st = STATE.setdefault(chat, {"session": None, "model": DEFAULT_MODEL, "busy": False})
    if st.get("busy"):
        await send(client, chat, "⏳ Still working on the previous message…", reply_to)
        return

    status = await send(client, chat, "📥 downloading…", reply_to)
    path = await download_tg_file(client, file_id, suffix)
    if not path:
        await edit(client, chat, status, "⚠ Could not download that file. Try again?")
        return

    if kind == "image":
        prompt = ((caption.strip() + "\n\n") if caption.strip() else "") + (
            f"I sent you an image. It is saved at: {path}\n"
            "Use the Read tool to open it, then tell me what you see / do what I asked."
        )
    else:  # voice / audio
        prompt = ((caption.strip() + "\n\n") if caption.strip() else "") + (
            f"I sent you an audio file at: {path}\n"
            "Transcribe it (faster-whisper is installed: `python3 -c \"from faster_whisper import "
            "WhisperModel as W; m=W('small'); print(''.join(s.text for s in m.transcribe('" + path +
            "')[0]))\"`), then do what I asked."
        )

    await edit(client, chat, status, "⏳ working…")
    await run_agent(client, chat, prompt, status)


async def handle_message(client, chat: int, text: str, reply_to: int) -> None:
    st = STATE.setdefault(chat, {"session": None, "model": DEFAULT_MODEL, "busy": False})
    low = text.strip().lower()

    if low in ("/start", "/help", "/h"):
        await send(client, chat, HELP.format(cwd=AGENT_CWD), reply_to, MAIN_KB)
        return

    if low == "/status":
        return await do_action(client, chat, "status", None, reply_to)
    if low in ("/new", "/reset"):
        return await do_action(client, chat, "new", None, reply_to)
    if low in ("/stop",):
        return await do_action(client, chat, "stop", None, reply_to)
    if low in ("/models", "/model"):
        return await do_action(client, chat, "models", None, reply_to)

    if low.startswith("/model "):
        st["model"] = text.split(maxsplit=1)[1].strip()
        await send(client, chat, f"✅ Model set to `{st['model']}`", reply_to, MAIN_KB)
        return

    if st.get("busy"):
        await send(client, chat, "⏳ Still working on the previous message…", reply_to)
        return

    status = await send(client, chat, "⏳ thinking…", reply_to)
    if status:
        await run_agent(client, chat, text, status)


async def handle_callback(client, chat: int, data: str, msg_id: int, cq_id: str) -> None:
    await tg(client, "answerCallbackQuery", callback_query_id=cq_id)

    if data.startswith("m:"):
        _, page, idx = data.split(":")
        try:
            mid = MODELS["page"][int(page)][int(idx)]
        except Exception:
            return
        st = STATE.setdefault(chat, {"session": None, "model": DEFAULT_MODEL, "busy": False})
        st["model"] = mid
        await edit(client, chat, msg_id, f"✅ Model set to `{mid}`", MAIN_KB)
        return

    if data.startswith("mp:"):
        page = int(data.split(":")[1])
        text, markup = model_keyboard(page)
        await edit(client, chat, msg_id, text, markup)
        return

    if data == "noop":
        return
    if data == "noop_close":
        await edit(client, chat, msg_id, "✖ Closed.", None)
        return

    if data.startswith("act:"):
        await do_action(client, chat, data.split(":", 1)[1], msg_id)


# --------------------------------------------------------------------------- #
# main loop
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
            return

        await fetch_models(force=True)

        while True:
            try:
                r = await client.get(f"{API}/getUpdates",
                                     params={"offset": offset, "timeout": 50,
                                             "allowed_updates": '["message","callback_query"]'},
                                     timeout=70)
                data = r.json()
            except Exception as e:
                print("[poll]", e, flush=True)
                await asyncio.sleep(3)
                continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1

                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    msg = cq.get("message") or {}
                    chat = (msg.get("chat") or {}).get("id")
                    if not chat:
                        continue
                    if not ALLOWED:
                        ALLOWED.add(chat)
                    if chat not in ALLOWED:
                        continue
                    asyncio.create_task(handle_callback(client, chat, cq.get("data", ""),
                                                        msg.get("message_id"), cq.get("id")))
                    continue

                msg = upd.get("message") or {}
                chat = (msg.get("chat") or {}).get("id")
                if not chat:
                    continue

                if not ALLOWED:
                    ALLOWED.add(chat)
                    print(f"[bot] locked to first chat: {chat}", flush=True)
                if chat not in ALLOWED:
                    await send(client, chat, "⛔ Not authorised. Add this chat id to TELEGRAM_ALLOWED_CHATS.")
                    continue

                caption = msg.get("caption") or ""
                photos = msg.get("photo") or []
                doc = msg.get("document") or {}
                voice = msg.get("voice") or msg.get("audio") or {}

                if photos:  # pick the largest size
                    asyncio.create_task(handle_media(client, chat, photos[-1]["file_id"],
                                                     ".jpg", caption, msg.get("message_id"), "image"))
                    continue
                if doc and str(doc.get("mime_type", "")).startswith("image/"):
                    suffix = os.path.splitext(doc.get("file_name") or "img.jpg")[1] or ".jpg"
                    asyncio.create_task(handle_media(client, chat, doc["file_id"],
                                                     suffix, caption, msg.get("message_id"), "image"))
                    continue
                if voice:
                    suffix = ".ogg" if msg.get("voice") else ".mp3"
                    asyncio.create_task(handle_media(client, chat, voice["file_id"],
                                                     suffix, caption, msg.get("message_id"), "audio"))
                    continue

                text = msg.get("text") or ""
                if not text:
                    continue

                asyncio.create_task(handle_message(client, chat, text, msg.get("message_id")))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
