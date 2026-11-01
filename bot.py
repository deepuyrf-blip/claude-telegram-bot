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
import re
import secrets
import shutil
import tempfile
import time
import zipfile
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
SKILLS_DIR = os.path.expanduser("~/.claude/skills")

STATE: dict[int, dict[str, Any]] = {}
MODELS: dict[str, Any] = {"list": [], "ts": 0.0, "page": {}}

HELP = """*Claude Code bot*

Send any message — the agent runs it in `{cwd}` with full tools.

Tap the buttons below, or use the commands.

/new · /model · /models · /agent · /status · /stop
/ls · /get <path> · /skills · /help

Send a `.md` (SKILL.md) or `.zip` to install a skill.
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
    [("🧠 Agent", "act:agent"), ("📊 Status", "act:status")],
    [("⏹ Stop", "act:stop")],
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


# vendor groups — models are bucketed by id prefix, so any provider works
GROUP_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("🟠 Anthropic Claude", ("claude",)),
    ("🟢 OpenAI", ("gpt", "o1", "o3", "o4", "chatgpt", "codex", "davinci")),
    ("🔵 Google Gemini", ("gemini", "gemma", "imagen", "veo")),
    ("🦙 Meta Llama", ("llama",)),
    ("🐉 Qwen", ("qwen",)),
    ("🐋 DeepSeek", ("deepseek",)),
    ("🌙 Kimi / Moonshot", ("kimi", "moonshot")),
    ("✨ GLM / Z.ai", ("glm", "zai", "zhipu")),
    ("✖ xAI Grok", ("grok",)),
    ("🌬 Mistral", ("mistral", "magistral", "codestral", "devstral")),
    ("🖱 Cursor", ("cursor",)),
    ("⌨ OpenCode / Zen", ("opencode", "zen")),
    ("🤗 Hugging Face", ("hf", "huggingface")),
]
OTHER_GROUP = "📦 Other"


def build_groups(models: list[str]) -> list[tuple[str, list[str]]]:
    buckets: dict[str, list[str]] = {}
    for mid in models:
        low = mid.lower()
        label = OTHER_GROUP
        for glabel, prefixes in GROUP_RULES:
            if low.startswith(prefixes) or any(p in low for p in prefixes if len(p) > 3):
                label = glabel
                break
        buckets.setdefault(label, []).append(mid)
    order = [g for g, _ in GROUP_RULES] + [OTHER_GROUP]
    return [(g, buckets[g]) for g in order if g in buckets]


def current_model() -> str:
    if not STATE:
        return DEFAULT_MODEL
    return next(iter(STATE.values())).get("model", DEFAULT_MODEL)


def model_menu() -> tuple[str, dict]:
    groups = build_groups(MODELS["list"])
    MODELS["groups"] = groups
    rows, pair = [], []
    for i, (glabel, items) in enumerate(groups):
        pair.append((f"{glabel} ({len(items)})", f"g:{i}:0"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([("🔄 Reload", "act:models"), ("✖ Close", "noop_close")])
    text = (f"*Models* — {len(MODELS['list'])} available, {len(groups)} groups\n"
            f"Current: `{current_model()}`\n\nPick a provider:")
    return text, kb(rows)


def group_page(gi: int, page: int) -> tuple[str, dict]:
    groups = MODELS.get("groups") or build_groups(MODELS["list"])
    MODELS["groups"] = groups
    if not (0 <= gi < len(groups)):
        return model_menu()
    glabel, items = groups[gi]
    pages = max(1, (len(items) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * PAGE:(page + 1) * PAGE]

    rows: list[list[tuple[str, str]]] = []
    pair: list[tuple[str, str]] = []
    for j, mid in enumerate(chunk):
        label = mid if len(mid) <= 30 else mid[:28] + "…"
        pair.append((label, f"m:{gi}:{page}:{j}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    nav: list[tuple[str, str]] = [("« Back", "mm")]
    if page > 0:
        nav.append(("‹", f"g:{gi}:{page-1}"))
    nav.append((f"{page+1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("›", f"g:{gi}:{page+1}"))
    rows.append(nav)
    rows.append([("✖ Close", "noop_close")])

    text = (f"*{glabel}* — {len(items)} models\n"
            f"Current: `{current_model()}`\n\nTap one to use it.")
    return text, kb(rows)


# --------------------------------------------------------------------------- #
# agent
# --------------------------------------------------------------------------- #
# ---- agents ----------------------------------------------------------------
AGENT_DEFS: dict[str, dict[str, Any]] = {
    "claude": {"label": "🟠 Claude Code", "bin": None},
    "codex": {"label": "🟢 Codex CLI", "bin": "codex",
              "args": ["exec", "{prompt}"]},
    "cursor": {"label": "🖱 Cursor Agent", "bin": "cursor-agent",
               "args": ["-p", "{prompt}"]},
    "opencode": {"label": "⌨ OpenCode", "bin": "opencode",
                 "args": ["run", "{prompt}"]},
}


def agent_available(key: str) -> bool:
    d = AGENT_DEFS.get(key)
    if not d:
        return False
    if d["bin"] is None:
        return True
    return shutil.which(d["bin"]) is not None


def agent_menu() -> tuple[str, dict]:
    rows, pair = [], []
    for key, d in AGENT_DEFS.items():
        ok = agent_available(key)
        pair.append((f"{d['label']}{'' if ok else ' (not installed)'}", f"ag:{key}" if ok else "noop"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([("✖ Close", "noop_close")])
    cur = STATE.get(next(iter(STATE), 0), {}).get("agent", "claude")
    text = (f"*Agents*\nCurrent: `{cur}`\n\n"
            "Claude uses the Agent SDK (tools, sessions). The others run their CLI "
            "non-interactively in the same workspace.")
    return text, kb(rows)


async def run_cli_agent(client, chat: int, prompt: str, status_id: int, key: str) -> None:
    d = AGENT_DEFS[key]
    bin_path = shutil.which(d["bin"])
    if not bin_path:
        await edit(client, chat, status_id, f"⚠ `{d['bin']}` is not installed in this container.")
        return

    cmd = [bin_path] + [a.replace("{prompt}", prompt) for a in d["args"]]
    out: list[str] = []
    last = 0.0
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=AGENT_CWD,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ},
        )
        assert proc.stdout is not None
        async for raw in proc.stdout:
            out.append(raw.decode("utf-8", "replace"))
            now = time.time()
            if now - last > 2.0:
                last = now
                await edit(client, chat, status_id, "".join(out)[-TG_LIMIT:]
                           or f"⏳ {d['label']} working…")
        await proc.wait()
        text = "".join(out).strip() or "(no output)"
        if proc.returncode:
            text = f"⚠ exit {proc.returncode}\n\n{text}"
        await edit(client, chat, status_id, text[:TG_LIMIT])
        if len(text) > TG_LIMIT:
            await send(client, chat, text[TG_LIMIT:])
    except Exception as e:
        await edit(client, chat, status_id, f"⚠ {type(e).__name__}: {e}"[:TG_LIMIT])


SKIP_DIRS = {".git", "node_modules", "__pycache__", ".cache", ".venv", "venv",
             ".npm", ".local", "dist", "build"}
MAX_UPLOAD = 45 * 1024 * 1024  # Telegram bot upload limit is 50MB


async def send_document(client, chat: int, path: str, caption: str = "") -> bool:
    name = os.path.basename(path)
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > MAX_UPLOAD:
            return False
        with open(path, "rb") as fh:
            files = {"document": (name, fh, "application/octet-stream")}
            data = {"chat_id": str(chat)}
            if caption:
                data["caption"] = caption[:1000]
            r = await client.post(f"{API}/sendDocument", data=data, files=files, timeout=300)
        ok = r.json().get("ok", False)
        print(f"[file] {name}: {'sent' if ok else r.text[:120]}", flush=True)
        return ok
    except Exception as e:
        print("[file] failed:", name, e, flush=True)
        return False


def collect_new_files(since: float, limit: int = 6) -> list[str]:
    """Files under AGENT_CWD modified after `since` (newest first)."""
    found: list[tuple[float, str]] = []
    for root, dirs, files in os.walk(AGENT_CWD):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime >= since and st.st_size <= MAX_UPLOAD and st.st_size > 0:
                found.append((st.st_mtime, p))
    found.sort(reverse=True)
    return [p for _t, p in found[:limit]]


async def attach_new_files(client, chat: int, since: float) -> None:
    paths = collect_new_files(since)
    if not paths:
        return
    await send(client, chat, f"📎 {len(paths)} file(s) created/updated:")
    for p in paths:
        rel = os.path.relpath(p, AGENT_CWD)
        await send_document(client, chat, p, f"{rel} · {os.path.getsize(p)//1024} KB")


async def run_agent(client, chat: int, prompt: str, status_id: int) -> None:
    st = STATE.setdefault(chat, {"session": None, "model": DEFAULT_MODEL, "busy": False})
    key = st.get("agent", "claude")
    t0 = time.time() - 1
    if key != "claude":
        st["busy"] = True
        try:
            await run_cli_agent(client, chat, prompt, status_id, key)
        finally:
            st["busy"] = False
    else:
        await run_claude_agent(client, chat, prompt, status_id)
    try:
        await attach_new_files(client, chat, t0)
    except Exception as e:
        print("[attach]", e, flush=True)


async def run_claude_agent(client, chat: int, prompt: str, status_id: int) -> None:
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
        text, markup = (f"*agent:* `{st.get('agent', 'claude')}`\n"
                        f"*model:* `{st.get('model')}`\n"
                        f"*session:* `{st.get('session') or 'new'}`\n"
                        f"*cwd:* `{AGENT_CWD}`\n"
                        f"*permissions:* `{PERMISSION_MODE}`"), MAIN_KB
    elif action == "stop":
        st["busy"] = False
        text, markup = "⏹ Noted — the current turn will finish; next message starts fresh.", MAIN_KB
    elif action == "models":
        await fetch_models(force=True)
        text, markup = model_menu()
    elif action == "agent":
        text, markup = agent_menu()
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


# --------------------------------------------------------------------------- #
# skills (.md / .zip upload)
# --------------------------------------------------------------------------- #
def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (s or "").strip()).strip("-.").lower()
    return s or "skill"


def _frontmatter_name(text: str) -> str | None:
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return None
    for line in m.group(1).splitlines():
        if line.lower().startswith("name:"):
            return line.split(":", 1)[1].strip().strip('"\'')
    return None


def looks_like_skill_md(text: str) -> bool:
    head = text[:1200]
    return head.lstrip().startswith("---") and "name:" in head and "description:" in head


def _safe_extract(zf: zipfile.ZipFile, dest: str) -> None:
    base = os.path.realpath(dest)
    for member in zf.namelist():
        target = os.path.realpath(os.path.join(dest, member))
        if target != base and not target.startswith(base + os.sep):
            raise ValueError(f"unsafe path in zip: {member}")
    zf.extractall(dest)


def list_skills() -> list[str]:
    if not os.path.isdir(SKILLS_DIR):
        return []
    return sorted(d for d in os.listdir(SKILLS_DIR)
                  if os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md")))


def install_skill(path: str) -> tuple[bool, str]:
    """Install a SKILL.md file or a zip containing one. Returns (ok, name|error)."""
    name = _slug(os.path.splitext(os.path.basename(path))[0])
    os.makedirs(SKILLS_DIR, exist_ok=True)
    try:
        if path.lower().endswith(".zip"):
            tmp = tempfile.mkdtemp()
            try:
                with zipfile.ZipFile(path) as zf:
                    _safe_extract(zf, tmp)
                found = None
                for root, _dirs, files in os.walk(tmp):
                    if "SKILL.md" in files:
                        found = root
                        break
                if found:
                    md = open(os.path.join(found, "SKILL.md"), encoding="utf-8", errors="ignore").read()
                    name = _slug(_frontmatter_name(md) or os.path.basename(found) or name)
                    dest = os.path.join(SKILLS_DIR, name)
                    shutil.rmtree(dest, ignore_errors=True)
                    shutil.copytree(found, dest)
                else:
                    dest = os.path.join(SKILLS_DIR, name)
                    shutil.rmtree(dest, ignore_errors=True)
                    shutil.copytree(tmp, dest)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return True, name

        md = open(path, encoding="utf-8", errors="ignore").read()
        name = _slug(_frontmatter_name(md) or name)
        dest = os.path.join(SKILLS_DIR, name)
        os.makedirs(dest, exist_ok=True)
        shutil.copyfile(path, os.path.join(dest, "SKILL.md"))
        return True, name
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def handle_skill_upload(client, chat: int, file_id: str, filename: str,
                              caption: str, reply_to: int) -> None:
    status = await send(client, chat, "📥 downloading…", reply_to)
    suffix = os.path.splitext(filename)[1] or ".md"
    path = await download_tg_file(client, file_id, suffix)
    if not path:
        await edit(client, chat, status, "⚠ Could not download that file.")
        return

    # a .md that isn't a SKILL.md → let the agent handle it as a normal file
    if path.lower().endswith(".md"):
        text = open(path, encoding="utf-8", errors="ignore").read()
        if not looks_like_skill_md(text):
            await edit(client, chat, status, "⏳ working…")
            prompt = ((caption.strip() + "\n\n") if caption.strip() else "") + (
                f"I sent you a markdown file: {path}\nRead it and do what I asked.")
            await run_agent(client, chat, prompt, status)
            return

    ok, info = install_skill(path)
    if ok:
        skills = list_skills()
        await edit(client, chat, status,
                   f"✅ Skill installed: `{info}`\n\n"
                   f"*Installed skills:* {', '.join('`'+s+'`' for s in skills) or '(none)'}\n\n"
                   "Naya Claude Code session isse load kar lega — "
                   f"`/{info}` se bhi chala sakte ho.", MAIN_KB)
    else:
        await edit(client, chat, status, f"⚠ Skill install failed:\n`{info}`", MAIN_KB)


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

    head = (caption.strip() + "\n\n") if caption.strip() else ""
    if kind == "image":
        prompt = head + (
            f"I sent you an image. It is saved at: {path}\n"
            "Use the Read tool to open it, then tell me what you see / do what I asked."
        )
    elif kind == "video":
        prompt = head + (
            f"I sent you a video. It is saved at: {path}\n"
            "Inspect it yourself with ffprobe/ffmpeg (extract keyframes to a temp dir, read them "
            "with the Read tool, pull the audio track and transcribe it with faster-whisper), "
            "then tell me what happens / do what I asked. Clean up temp files when done."
        )
    elif kind == "audio":
        prompt = head + (
            f"I sent you an audio file at: {path}\n"
            "Transcribe it with faster-whisper (it is installed), then do what I asked:\n"
            f"  python3 -c \"from faster_whisper import WhisperModel as W; "
            f"m=W('small'); print(''.join(s.text for s in m.transcribe('{path}')[0]))\""
        )
    else:  # any other file
        prompt = head + (
            f"I sent you a file: {path}\n"
            "Look at it (file/ffprobe/unzip/tesseract/Read as appropriate), then do what I asked."
        )

    await edit(client, chat, status, "⏳ working…")
    await run_agent(client, chat, prompt, status)


async def handle_message(client, chat: int, text: str, reply_to: int) -> None:
    st = STATE.setdefault(chat, {"session": None, "model": DEFAULT_MODEL, "busy": False})
    low = text.strip().lower()

    if low in ("/start", "/help", "/h"):
        await send(client, chat, HELP.format(cwd=AGENT_CWD), reply_to, MAIN_KB)
        return

    if low in ("/agent", "/agents"):
        return await do_action(client, chat, "agent", None, reply_to)

    if low in ("/ls", "/files"):
        items = collect_new_files(0, limit=40)
        if not items:
            await send(client, chat, f"(no files in `{AGENT_CWD}`)", reply_to)
            return
        listing = "\n".join(f"• `{os.path.relpath(p, AGENT_CWD)}`" for p in items)
        await send(client, chat, f"*Files* in `{AGENT_CWD}`\n{listing}\n\n"
                                 "Send `/get <path>` to download one.", reply_to, MAIN_KB)
        return

    if low.startswith("/get "):
        rel = text.split(maxsplit=1)[1].strip().strip("`")
        path = rel if os.path.isabs(rel) else os.path.join(AGENT_CWD, rel)
        path = os.path.realpath(path)
        if not path.startswith(os.path.realpath(AGENT_CWD)) or not os.path.isfile(path):
            await send(client, chat, f"⚠ Not found (or outside the workspace): `{rel}`", reply_to)
            return
        ok = await send_document(client, chat, path, rel)
        if not ok:
            await send(client, chat, "⚠ Could not send (too large? >45 MB)", reply_to)
        return

    if low in ("/skills", "/skill"):
        skills = list_skills()
        body = "\n".join(f"• `{s}`" for s in skills) or "(none yet)"
        await send(client, chat,
                   f"*Skills* (`{SKILLS_DIR}`)\n{body}\n\n"
                   "Send a `.md` (SKILL.md) or a `.zip` containing one to install it.",
                   reply_to, MAIN_KB)
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

    if data == "mm":
        text, markup = model_menu()
        await edit(client, chat, msg_id, text, markup)
        return

    if data.startswith("g:"):
        _, gi, page = data.split(":")
        text, markup = group_page(int(gi), int(page))
        await edit(client, chat, msg_id, text, markup)
        return

    if data.startswith("m:"):
        _, gi, page, idx = data.split(":")
        try:
            mid = MODELS["groups"][int(gi)][1][int(page) * PAGE + int(idx)]
        except Exception:
            return
        st = STATE.setdefault(chat, {"session": None, "model": DEFAULT_MODEL, "busy": False})
        st["model"] = mid
        await edit(client, chat, msg_id, f"✅ Model set to `{mid}`", MAIN_KB)
        return

    if data.startswith("ag:"):
        key = data.split(":", 1)[1]
        if key not in AGENT_DEFS or not agent_available(key):
            return
        st = STATE.setdefault(chat, {"session": None, "model": DEFAULT_MODEL, "busy": False})
        st["agent"] = key
        await edit(client, chat, msg_id, f"✅ Agent set to *{AGENT_DEFS[key]['label']}*", MAIN_KB)
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
                video = msg.get("video") or msg.get("video_note") or {}
                sticker = msg.get("sticker") or {}

                if video:
                    suffix = ".mp4"
                    asyncio.create_task(handle_media(client, chat, video["file_id"],
                                                     suffix, caption, msg.get("message_id"), "video"))
                    continue
                if sticker:
                    asyncio.create_task(handle_media(client, chat, sticker["file_id"],
                                                     ".webp", caption, msg.get("message_id"), "image"))
                    continue
                if doc:
                    fname = (doc.get("file_name") or "").lower()
                    if fname.endswith((".zip", ".md")):
                        asyncio.create_task(handle_skill_upload(
                            client, chat, doc["file_id"], doc.get("file_name") or "skill.md",
                            caption, msg.get("message_id")))
                        continue
                    mime = str(doc.get("mime_type", ""))
                    name = doc.get("file_name") or "file.bin"
                    suffix = os.path.splitext(name)[1] or ".bin"
                    if mime.startswith("image/"):
                        kind = "image"
                    elif mime.startswith("video/"):
                        kind = "video"
                    elif mime.startswith("audio/"):
                        kind = "audio"
                    else:
                        kind = "file"
                    asyncio.create_task(handle_media(client, chat, doc["file_id"],
                                                     suffix, caption, msg.get("message_id"), kind))
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
