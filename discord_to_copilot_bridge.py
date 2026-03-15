#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import websockets
from copilot import CopilotClient, PermissionHandler

UA = "DiscordBot (https://adultok.jp, 1.0)"
DEFAULT_PROJECT_ROOT = Path("/root/projects/adultok-v2")
DEFAULT_USER_ID = os.environ.get("DISCORD_DEFAULT_USER_ID", "")
DEFAULT_CHANNEL_ID = os.environ.get("DISCORD_DEFAULT_CHANNEL_ID", "")
STATE_FILE = Path("/root/.copilot/discord_to_copilot_bridge_state.json")
LOCK_FILE = Path("/root/.copilot/discord_to_copilot_bridge.lock")
HEARTBEAT_FILE = Path("/root/projects/persistent_agent/logs/discord_to_copilot_bridge.heartbeat.json")
SESSION_STATE_DIR = Path.home() / ".copilot" / "session-state"
MAX_CONTEXT_EXCHANGES = 20
PROCESSED_LIMIT = 500
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_REASONING = "high"
HEARTBEAT_INTERVAL = 15
PROGRESS_UPDATE_INTERVAL = 2
MAX_REPLY_LEN = 1800
MAX_TASK_TIMEOUT = 600  # 10 minutes - kill hung tasks before they freeze the bridge
AVAILABLE_MODELS = (
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-sonnet-4.6",
    "claude-opus-4-5",
    "claude-opus-4.6",
    "gpt-5.4",
    "gpt-5.1",
    "gpt-5-mini",
)
LOCK_HANDLES: list[object] = []
current_task: asyncio.Task[Any] | None = None


def log(message: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[copilot-bridge {level} {ts}] {message}", flush=True)


def find_project_root() -> Path:
    start = Path(__file__).resolve().parent
    for candidate in [start, *start.parents]:
        if (candidate / ".env").exists() and (candidate / "server").exists():
            return candidate
    return DEFAULT_PROJECT_ROOT


PROJECT_ROOT = find_project_root()


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return env
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


ENV = load_env()
DISCORD_TOKEN = ENV.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = ENV.get("DISCORD_CHANNEL_INSTRUCTIONS") or os.environ.get(
    "DISCORD_CHANNEL_INSTRUCTIONS", DEFAULT_CHANNEL_ID
)
_raw_user_ids = ENV.get("DISCORD_INSTRUCTION_USER_ID") or os.environ.get(
    "DISCORD_INSTRUCTION_USER_ID", DEFAULT_USER_ID
)
DISCORD_USER_IDS: set[str] = {uid.strip() for uid in _raw_user_ids.split(",") if uid.strip()}
DISCORD_USER_ID = next(iter(DISCORD_USER_IDS))  # primary (for legacy compat)
COPILOT_MODEL = ENV.get("DISCORD_COPILOT_MODEL") or os.environ.get(
    "DISCORD_COPILOT_MODEL", DEFAULT_MODEL
)
COPILOT_REASONING = ENV.get("DISCORD_COPILOT_REASONING") or os.environ.get(
    "DISCORD_COPILOT_REASONING", DEFAULT_REASONING
)
DISCORD_APPLICATION_ID = ENV.get("DISCORD_APPLICATION_ID") or os.environ.get(
    "DISCORD_APPLICATION_ID", ""
)


def is_discord_message_id(value: Any) -> bool:
    return isinstance(value, str) and value.isdigit()


def load_state() -> dict[str, Any]:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return {"session_id": None, "last_user_message_id": None, "processed": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state root must be object")
        data.setdefault("session_id", None)
        data.setdefault("last_user_message_id", None)
        data.setdefault("processed", {})
        if not is_discord_message_id(data.get("last_user_message_id")):
            data["last_user_message_id"] = None
        if not isinstance(data.get("processed"), dict):
            data["processed"] = {}
        return data
    except Exception:
        return {"session_id": None, "last_user_message_id": None, "processed": {}}


def save_state(state: dict[str, Any]) -> None:
    processed = state.get("processed", {})
    if len(processed) > PROCESSED_LIMIT:
        sorted_items = sorted(processed.items(), key=lambda item: item[1].get("processed_at", ""))
        state["processed"] = dict(sorted_items[-PROCESSED_LIMIT:])
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def acquire_instance_lock() -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as err:
        raise RuntimeError("another discord_to_copilot_bridge watch process is already running") from err
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    LOCK_HANDLES.append(handle)


def write_heartbeat(session_id: str, status: str) -> None:
    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_FILE.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "session_id": session_id,
                "status": status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


async def heartbeat_while_processing(session_id: str, status: str, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        write_heartbeat(session_id, status)
        await asyncio.sleep(HEARTBEAT_INTERVAL)


def build_session_summary(session_id: str) -> str:
    """Read events.jsonl for a session and return a compact conversation summary."""
    events_path = SESSION_STATE_DIR / session_id / "events.jsonl"
    if not events_path.exists():
        return ""
    try:
        exchanges: list[tuple[str, str]] = []
        role_map = {"user.message": "User", "assistant.message": "Assistant"}
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            etype = event.get("type", "")
            if etype not in role_map:
                continue
            content = (event.get("data") or {}).get("content") or ""
            content = content.strip()
            if not content:
                continue
            # Strip the Discord bridge header from user messages
            if etype == "user.message" and "[User message]" in content:
                content = content.split("[User message]", 1)[-1].strip()
            exchanges.append((role_map[etype], content[:300]))

        if not exchanges:
            return ""

        # Keep only the last MAX_CONTEXT_EXCHANGES turns
        exchanges = exchanges[-MAX_CONTEXT_EXCHANGES:]
        lines = [
            "---",
            f"[前セッション引き継ぎコンテキスト (session: {session_id[:8]})]",
            "以下は前のセッションの会話履歴の要約です。参考にして作業を継続してください。",
            "",
        ]
        for role, text in exchanges:
            lines.append(f"{role}: {text}")
        lines.append("---")
        return "\n".join(lines)
    except Exception as err:
        log(f"build_session_summary error for {session_id}: {err}", "WARN")
        return ""


def discord_api(method: str, path: str, payload: dict[str, Any] | None = None):
    url = f"https://discord.com/api/v10{path}"
    headers = {"User-Agent": UA}
    if DISCORD_TOKEN:
        headers["Authorization"] = f"Bot {DISCORD_TOKEN}"
    return request_json(method, url, payload=payload, headers=headers)


def request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
    _retry: int = 3,
):
    data = None
    request_headers = {"User-Agent": UA, **(headers or {})}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    for attempt in range(_retry):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
            if not raw:
                return None
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < _retry - 1:
                retry_after = float(e.headers.get("Retry-After") or 5)
                retry_after = min(retry_after, 60)
                log(f"Discord API rate-limited (429); retrying in {retry_after}s (attempt {attempt + 1}/{_retry})", "WARN")
                time.sleep(retry_after)
                continue
            raise
    return None


def discover_discord_application_id() -> str:
    if DISCORD_APPLICATION_ID:
        return DISCORD_APPLICATION_ID
    try:
        application = discord_api("GET", "/oauth2/applications/@me") or {}
        app_id = str(application.get("id") or "").strip()
        if app_id:
            return app_id
    except Exception as err:
        log(f"failed to discover application id via /oauth2/applications/@me: {err}", "WARN")
    try:
        me = discord_api("GET", "/users/@me") or {}
        app_id = str(me.get("id") or "").strip()
        if app_id:
            return app_id
    except Exception as err:
        log(f"failed to discover application id via /users/@me: {err}", "WARN")
    if DISCORD_TOKEN:
        try:
            token_prefix = DISCORD_TOKEN.split(".", 1)[0]
            padding = "=" * (-len(token_prefix) % 4)
            decoded = base64.urlsafe_b64decode(token_prefix + padding).decode("utf-8")
            if decoded.isdigit():
                return decoded
        except Exception:
            pass
    raise RuntimeError("DISCORD_APPLICATION_ID is not configured and could not be discovered")


def build_discord_commands_payload() -> list[dict[str, Any]]:
    model_choices = [{"name": "list", "value": "list"}]
    model_choices.extend({"name": model, "value": model} for model in AVAILABLE_MODELS)
    return [
        {
            "name": "cancel",
            "description": "Cancel the current Copilot task",
            "type": 1,
        },
        {
            "name": "model",
            "description": "Show or switch the Copilot model",
            "type": 1,
            "options": [
                {
                    "type": 3,
                    "name": "model",
                    "description": "Model name. Leave empty to show the current model. Use 'list' to list models.",
                    "required": False,
                    "choices": model_choices,
                }
            ],
        },
    ]


def register_slash_commands(application_id: str) -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured")
    commands = build_discord_commands_payload()
    discord_api("PUT", f"/applications/{application_id}/commands", payload=commands)


def create_interaction_response(
    interaction_id: str,
    interaction_token: str,
    response_type: int = 5,
) -> None:
    discord_api(
        "POST",
        f"/interactions/{interaction_id}/{interaction_token}/callback",
        payload={"type": response_type},
    )


def edit_interaction_response(application_id: str, interaction_token: str, content: str) -> dict[str, Any] | None:
    url = f"https://discord.com/api/v10/webhooks/{application_id}/{interaction_token}/messages/@original"
    return request_json("PATCH", url, payload={"content": content[:MAX_REPLY_LEN]})


def fetch_messages(after: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    path = f"/channels/{DISCORD_CHANNEL_ID}/messages?limit={limit}"
    if is_discord_message_id(after):
        path += f"&after={after}"
    messages = discord_api("GET", path) or []
    messages = [m for m in messages if m.get("author", {}).get("id") in DISCORD_USER_IDS]
    messages.sort(key=lambda msg: int(msg["id"]))
    return messages


def fetch_message(message_id: str) -> dict[str, Any]:
    message = discord_api("GET", f"/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}") or {}
    if message.get("author", {}).get("id") not in DISCORD_USER_IDS:
        raise RuntimeError(f"message {message_id} is not from configured Discord user")
    return message


def put_reaction(message_id: str, emoji: str) -> None:
    encoded = urllib.parse.quote(emoji, safe="")
    discord_api("PUT", f"/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}/reactions/{encoded}/@me")


def reply_to_discord(message_id: str, content: str) -> dict[str, Any] | None:
    body = {
        "content": content[:MAX_REPLY_LEN],
        "message_reference": {"message_id": message_id},
        "allowed_mentions": {"replied_user": False},
    }
    return discord_api("POST", f"/channels/{DISCORD_CHANNEL_ID}/messages?wait=true", payload=body)


def edit_discord_message(message_id: str, content: str) -> dict[str, Any] | None:
    return discord_api(
        "PATCH",
        f"/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}",
        payload={"content": content[:MAX_REPLY_LEN]},
    )


def delete_discord_message(message_id: str) -> None:
    discord_api("DELETE", f"/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}")


def summarize_text(value: Any, limit: int = 120) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def extract_filename(arguments: Any) -> str:
    if isinstance(arguments, dict):
        for key in ("file_path", "filepath", "path", "filename", "file", "target_file"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return Path(value.strip()).name or value.strip()
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                return extract_filename(parsed)
        except Exception:
            pass
        return Path(arguments.strip()).name or arguments.strip()
    return "ファイル"


def extract_command(arguments: Any) -> str:
    if isinstance(arguments, dict):
        for key in ("cmd", "command", "input", "script"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(arguments, str):
        return arguments.strip()
    return summarize_text(arguments)


def format_tool_action(tool_name: str, arguments: Any) -> str:
    normalized = (tool_name or "").strip().lower()
    if normalized == "report_intent":
        if isinstance(arguments, dict):
            intent = str(arguments.get("intent") or "").strip()
            if intent:
                return intent
        return summarize_text(arguments) or "意図を報告中"
    if normalized in {"bash", "run_command"}:
        command = extract_command(arguments)
        return f"コマンド実行中: {command[:60]}" if command else "コマンド実行中"
    if normalized in {"view", "read_file"}:
        return f"{extract_filename(arguments)} を読んでいます"
    if normalized in {"edit", "edit_file", "create"}:
        return f"{extract_filename(arguments)} を編集中"
    return tool_name or "処理中"


def format_progress_message(elapsed_seconds: int, current_action: str, prev_action: str) -> str:
    return (
        f"⏳ 処理中 ({elapsed_seconds}s経過)\n"
        f"現在: {current_action or '待機中'}\n"
        f"直前: {prev_action or '-'}"
    )


class DiscordProgressUpdater:
    def __init__(self, reply_message_id: str):
        self.reply_message_id = reply_message_id
        self.started_at = time.monotonic()
        self.current_action = "受付しました"
        self.prev_action = "-"
        self._dirty = True
        self._last_sent_at = 0.0
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        self._task = asyncio.create_task(self._run())
        return self._task

    def update(self, action: str) -> None:
        action = action.strip() or "処理中"
        if action == self.current_action:
            return
        self.prev_action = self.current_action
        self.current_action = action
        self._dirty = True

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            try:
                await self._task
            except Exception:
                pass

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            if self._dirty:
                now = time.monotonic()
                if self._last_sent_at == 0.0 or now - self._last_sent_at >= PROGRESS_UPDATE_INTERVAL:
                    try:
                        edit_discord_message(
                            self.reply_message_id,
                            format_progress_message(
                                int(now - self.started_at),
                                self.current_action,
                                self.prev_action,
                            ),
                        )
                        self._last_sent_at = now
                        self._dirty = False
                    except Exception:
                        pass
            await asyncio.sleep(0.5)


def build_prompt(message: dict[str, Any]) -> str:
    content = (message.get("content") or "").strip()
    lines = [
        "Discordからの新しい指示です。/root/projects/adultok-v2 を優先して必要な作業を進めてください。",
        "この実行は固定の Copilot CLI セッションに対する継続プロンプトです。過去の文脈が役立つなら利用してください。",
        "返信はそのまま Discord に返されるので、日本語で簡潔に、結果を先に書いてください。",
        "",
        "[Discord metadata]",
        f"message_id: {message['id']}",
        f"timestamp: {message.get('timestamp', '')}",
        f"channel_id: {DISCORD_CHANNEL_ID}",
        "",
        "[User message]",
        content or "(empty message)",
    ]
    return "\n".join(lines)


def is_broken_session_error(err: Exception) -> bool:
    message = str(err).lower()
    markers = (
        "session error",
        "unknown session",
        "session not found",
        "session file is corrupted",
        "no tool output found for function call",
        "no tool call found for function call output",
    )
    return any(marker in message for marker in markers)


def is_transient_session_error(err: Exception) -> bool:
    message = str(err).lower()
    markers = (
        "session is busy",
        "already running",
        "already executing",
        "please wait for the current turn",
    )
    return any(marker in message for marker in markers)


class CopilotSessionManager:
    def __init__(
        self,
        state: dict[str, Any],
        *,
        model: str,
        reasoning_effort: str,
        requested_session_id: str | None = None,
    ):
        self.state = state
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.requested_session_id = requested_session_id
        self.client: CopilotClient | None = None
        self.session = None
        self._lock = asyncio.Lock()

    @property
    def session_id(self) -> str:
        current = getattr(self.session, "session_id", None)
        if current:
            return current
        saved = self.state.get("session_id")
        if isinstance(saved, str) and saved:
            return saved
        if self.requested_session_id:
            return self.requested_session_id
        return ""

    async def start(self) -> None:
        async with self._lock:
            await self._ensure_client_locked()
            await self._ensure_session_locked()

    async def stop(self) -> None:
        async with self._lock:
            if self.session is not None:
                try:
                    await self.session.disconnect()
                except Exception:
                    pass
                self.session = None
            if self.client is not None:
                try:
                    await self.client.stop()
                except Exception:
                    pass
                self.client = None

    async def reset_session(self) -> str:
        async with self._lock:
            if self.session is not None:
                try:
                    await self.session.disconnect()
                except Exception:
                    pass
                self.session = None
            await self._ensure_client_locked()
            await self._create_session_locked(session_id=None)
            return self.session_id

    async def send_and_wait(
        self,
        prompt: str,
        *,
        on_progress: Callable[[str, Any], None] | None = None,
    ) -> str:
        async with self._lock:
            await self._ensure_client_locked()
            await self._ensure_session_locked()
            session = self.session
            if session is None:
                raise RuntimeError("failed to create Copilot session")

            idle_event = asyncio.Event()
            error_event: Exception | None = None
            last_assistant_message: Any | None = None

            def handle_event(event: Any) -> None:
                nonlocal error_event, last_assistant_message
                event_type = getattr(getattr(event, "type", None), "value", "")
                data = getattr(event, "data", None)
                if event_type == "assistant.turn_start" and on_progress:
                    on_progress("assistant.turn_start", None)
                    return
                if event_type == "tool.execution_start" and on_progress:
                    tool_name = getattr(data, "tool_name", None) or getattr(data, "tool_title", None) or ""
                    arguments = getattr(data, "arguments", None)
                    on_progress(str(tool_name), arguments)
                    return
                if event_type == "assistant.message":
                    last_assistant_message = event
                    return
                if event_type == "session.error":
                    error_event = Exception(
                        f"Session error: {getattr(data, 'message', str(data))}"
                    )
                    idle_event.set()
                    return
                if event_type == "session.idle":
                    idle_event.set()

            unsubscribe = session.on(handle_event)
            try:
                await session.send({"prompt": prompt})
                await idle_event.wait()
            finally:
                unsubscribe()

            if error_event is not None:
                raise error_event

            content = getattr(getattr(last_assistant_message, "data", None), "content", None)
            # Return empty string when no text content (Copilot may have replied via tools directly)
            return str(content).strip() if content else ""

    async def _ensure_client_locked(self) -> None:
        if self.client is not None:
            return
        self.client = CopilotClient({"cwd": str(PROJECT_ROOT)})
        await self.client.start()

    async def _ensure_session_locked(self) -> None:
        if self.session is not None:
            return
        await self._create_session_locked(session_id=self.requested_session_id or self.state.get("session_id"))

    async def _create_session_locked(self, session_id: str | None) -> None:
        if self.client is None:
            raise RuntimeError("client is not started")
        # reasoning_effort is only supported by some models (e.g. gpt-5.x, claude-opus)
        REASONING_EFFORT_MODELS = ("gpt-5", "o1", "o3", "claude-opus")
        supports_reasoning = any(self.model.startswith(p) for p in REASONING_EFFORT_MODELS)
        config: dict[str, Any] = {
            "model": self.model,
            "working_directory": str(PROJECT_ROOT),
            "client_name": "adultok-discord-bridge",
            "on_permission_request": PermissionHandler.approve_all,
        }
        if supports_reasoning and self.reasoning_effort:
            config["reasoning_effort"] = self.reasoning_effort
        if session_id:
            config["session_id"] = session_id
        session = await self.client.create_session(config)
        self.session = session
        self.state["session_id"] = session.session_id
        save_state(self.state)


async def invoke_copilot(
    manager: CopilotSessionManager,
    state: dict[str, Any],
    prompt: str,
    on_progress: Callable[[str, Any], None] | None = None,
) -> str:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return await asyncio.wait_for(
                manager.send_and_wait(prompt, on_progress=on_progress),
                timeout=MAX_TASK_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log(f"task timed out after {MAX_TASK_TIMEOUT}s — resetting session", "WARN")
            try:
                old_session_id = manager.session_id
                new_session_id = await manager.reset_session()
                state["session_id"] = new_session_id
                save_state(state)
                log(f"session reset after timeout: {old_session_id} -> {new_session_id}", "WARN")
            except Exception as reset_err:
                log(f"session reset after timeout failed: {reset_err}", "WARN")
            raise RuntimeError(f"タスクが {MAX_TASK_TIMEOUT // 60} 分でタイムアウトしました。セッションをリセットしました。")
        except Exception as err:
            last_error = err
            if is_transient_session_error(err) and attempt < 2:
                log(f"copilot session busy (attempt {attempt + 1}/3): {err}", "WARN")
                await asyncio.sleep(min(5, attempt + 1))
                continue
            if is_broken_session_error(err) and attempt < 2:
                old_session_id = manager.session_id
                new_session_id = await manager.reset_session()
                state["session_id"] = new_session_id
                save_state(state)
                log(f"rotated broken Copilot session {old_session_id} -> {new_session_id}", "WARN")
                continue
            if attempt < 2:
                log(f"copilot transport/session error (attempt {attempt + 1}/3): {err}", "WARN")
                try:
                    await manager.stop()
                    await manager.start()
                    continue
                except Exception as restart_err:
                    last_error = restart_err
                    break
            break
    raise RuntimeError(f"Copilot bridge retry budget exhausted: {last_error}") from last_error


def seed_to_latest_message(state: dict[str, Any]) -> None:
    latest = fetch_messages(limit=20)
    state["last_user_message_id"] = latest[-1]["id"] if latest else None
    save_state(state)
    log(f"seeded last_user_message_id={state['last_user_message_id']}")


def is_cancel_command(message: dict[str, Any]) -> bool:
    return (message.get("content") or "").strip() == "!cancel"


def is_model_command(message: dict[str, Any]) -> bool:
    return (message.get("content") or "").strip().startswith("!model")


def is_status_command(message: dict[str, Any]) -> bool:
    return (message.get("content") or "").strip() in ("!status", "!ping")


async def handle_model_command(
    message: dict[str, Any],
    state: dict[str, Any],
    manager: CopilotSessionManager | None,
    *,
    advance_cursor: bool,
    reply_func: Callable[[str, str], dict[str, Any] | None] | None = None,
) -> bool:
    message_id = message["id"]
    existing_record = state.get("processed", {}).get(message_id)
    if existing_record:
        if advance_cursor:
            state["last_user_message_id"] = message_id
            save_state(state)
        return False

    content = (message.get("content") or "").strip()
    parts = content.split(maxsplit=1)
    requested_model = parts[1].strip() if len(parts) > 1 else ""
    current_model = (manager.model if manager else None) or state.get("model") or COPILOT_MODEL
    record: dict[str, Any] = {
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "preview": content[:120] or "!model",
        "session_id": manager.session_id if manager else (state.get("session_id") or ""),
        "model": current_model,
    }

    if not requested_model:
        available_models_text = "\n".join(f"- `{model}`" for model in AVAILABLE_MODELS)
        response_text = (
            f"現在のモデル: `{current_model}`\n"
            f"利用可能なモデル:\n{available_models_text}"
        )
        record["status"] = "model-list"
    elif requested_model not in AVAILABLE_MODELS:
        available_models_text = ", ".join(AVAILABLE_MODELS)
        response_text = (
            f"❌ 不明なモデルです: `{requested_model}`\n"
            f"利用可能: {available_models_text}"
        )
        record["status"] = "model-invalid"
        record["requested_model"] = requested_model
    elif requested_model == current_model:
        response_text = f"ℹ️ すでに `{requested_model}` を使用中です"
        record["status"] = "model-unchanged"
        record["requested_model"] = requested_model
    else:
        if manager is None:
            response_text = "❌ モデル切替には有効な Copilot セッションが必要です"
            record["status"] = "model-error"
            record["requested_model"] = requested_model
        else:
            old_model = current_model
            old_session_id = manager.session_id
            manager.model = requested_model
            new_session_id = await manager.reset_session()
            state["model"] = requested_model
            state["session_id"] = new_session_id
            # Build and store context summary from old session
            summary = build_session_summary(old_session_id)
            if summary:
                state["pending_context"] = summary
                log(f"stored session context from {old_session_id[:8]} -> {new_session_id[:8]}")
            save_state(state)
            response_text = f"✅ モデルを `{old_model}` から `{requested_model}` に切り替えました"
            if summary:
                response_text += "\n📋 前セッションの会話履歴を引き継ぎました（次のメッセージに注入）"
            record.update(
                {
                    "status": "model-switched",
                    "requested_model": requested_model,
                    "model": requested_model,
                    "session_id": new_session_id,
                    "reset_from_session_id": old_session_id,
                }
            )

    if reply_func is None:
        reply_func = reply_to_discord

    try:
        reply = reply_func(message_id, response_text)
        record["reply_message_id"] = (reply or {}).get("id")
    except Exception as err:
        record["reply_error"] = str(err)

    if advance_cursor:
        state["last_user_message_id"] = message_id
    state.setdefault("processed", {})[message_id] = record
    save_state(state)
    log(f"processed model command {message_id}: status={record['status']}")
    return True


async def handle_cancel_command(
    message: dict[str, Any],
    state: dict[str, Any],
    manager: CopilotSessionManager | None,
    processing_task: asyncio.Task[Any] | None,
    *,
    advance_cursor: bool,
    reply_func: Callable[[str, str], dict[str, Any] | None] | None = None,
) -> bool:
    message_id = message["id"]
    existing_record = state.get("processed", {}).get(message_id)
    if existing_record:
        if advance_cursor:
            state["last_user_message_id"] = message_id
            save_state(state)
        return False

    record: dict[str, Any] = {
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "preview": "!cancel",
        "session_id": manager.session_id if manager else (state.get("session_id") or ""),
    }
    cancelled = False

    if processing_task and not processing_task.done():
        processing_task.cancel()
        try:
            await processing_task
        except asyncio.CancelledError:
            cancelled = True
        except Exception as err:
            record["cancel_wait_error"] = str(err)

        if manager:
            try:
                old_session_id = manager.session_id
                new_session_id = await manager.reset_session()
                state["session_id"] = new_session_id
                record["session_id"] = new_session_id
                record["reset_from_session_id"] = old_session_id
            except Exception as err:
                record["session_reset_error"] = str(err)

        response_text = "⛔ キャンセルしました"
        record["status"] = "cancelled"
    else:
        response_text = "⛔ キャンセルするものがありません"
        record["status"] = "cancel-noop"

    if reply_func is None:
        reply_func = reply_to_discord

    try:
        reply = reply_func(message_id, response_text)
        record["reply_message_id"] = (reply or {}).get("id")
    except Exception as err:
        record["reply_error"] = str(err)

    if advance_cursor:
        state["last_user_message_id"] = message_id
    state.setdefault("processed", {})[message_id] = record
    save_state(state)
    log(f"processed cancel command {message_id}: status={record['status']}")
    return cancelled


async def handle_status_command(
    message: dict[str, Any],
    state: dict[str, Any],
    manager: "CopilotSessionManager | None",
    processing_task: "asyncio.Task[Any] | None",
    *,
    advance_cursor: bool,
    reply_func: "Callable[[str, str], dict[str, Any] | None] | None" = None,
) -> bool:
    message_id = message["id"]
    existing_record = state.get("processed", {}).get(message_id)
    if existing_record:
        if advance_cursor:
            state["last_user_message_id"] = message_id
            save_state(state)
        return False

    now = datetime.now(timezone.utc)
    lines: list[str] = ["🤖 **dangerbot ステータス**"]

    # Heartbeat file
    hb_status = "不明"
    hb_age_str = "不明"
    try:
        hb = json.loads(HEARTBEAT_FILE.read_text())
        hb_ts = datetime.fromisoformat(hb["timestamp"])
        age_sec = (now - hb_ts).total_seconds()
        hb_age_str = f"{int(age_sec)}秒前"
        hb_status = hb.get("status", "不明")
    except Exception:
        pass

    is_processing = processing_task is not None and not processing_task.done()
    if is_processing:
        lines.append("⚙️ 状態: **タスク処理中**")
    else:
        lines.append("✅ 状態: 待機中 (watching)")

    lines.append(f"🕐 最終ハートビート: {hb_age_str} (`{hb_status}`)")

    current_model = (manager.model if manager else None) or state.get("model") or COPILOT_MODEL
    session_id = (manager.session_id if manager else None) or state.get("session_id") or "?"
    lines.append(f"🧠 モデル: `{current_model}`")
    lines.append(f"🔑 セッション: `{session_id[:16]}...`")
    lines.append(f"🔧 PID: `{os.getpid()}`")
    lines.append("")
    lines.append("コマンド: `!status` `!model` `!cancel`")

    response_text = "\n".join(lines)

    if reply_func is None:
        reply_func = reply_to_discord

    record: dict[str, Any] = {
        "processed_at": now.isoformat(),
        "preview": "!status",
        "status": "status-replied",
        "session_id": session_id,
        "model": current_model,
    }
    try:
        reply = reply_func(message_id, response_text)
        record["reply_message_id"] = (reply or {}).get("id")
    except Exception as err:
        record["reply_error"] = str(err)

    if advance_cursor:
        state["last_user_message_id"] = message_id
    state.setdefault("processed", {})[message_id] = record
    save_state(state)
    log(f"processed status command {message_id}")
    return True


def parse_interaction_model_value(data: dict[str, Any]) -> str:
    options = data.get("options") or []
    if not isinstance(options, list):
        return ""
    for option in options:
        if not isinstance(option, dict):
            continue
        if option.get("name") == "model":
            value = option.get("value")
            return str(value).strip() if value is not None else ""
        if option.get("name") == "list":
            return "list"
    return ""


async def process_interaction_command(
    interaction: dict[str, Any],
    state: dict[str, Any],
    manager: CopilotSessionManager | None,
) -> None:
    data = interaction.get("data") or {}
    command_name = str(data.get("name") or "").strip()
    interaction_id = str(interaction.get("id") or "").strip()
    application_id = str(interaction.get("application_id") or "").strip()
    interaction_token = str(interaction.get("token") or "").strip()

    if not interaction_id or not application_id or not interaction_token:
        raise RuntimeError("interaction payload is missing id, application_id, or token")

    synthetic_message = {
        "id": interaction_id,
        "content": "",
        "timestamp": interaction.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "author": {"id": DISCORD_USER_ID},
        "interaction": {"type": "application_command", "name": command_name},
    }

    def edit_original(_: str, content: str) -> dict[str, Any] | None:
        return edit_interaction_response(application_id, interaction_token, content)

    if command_name == "cancel":
        await handle_cancel_command(
            synthetic_message,
            state,
            manager,
            current_task,
            advance_cursor=False,
            reply_func=edit_original,
        )
        return

    if command_name == "model":
        requested_model = parse_interaction_model_value(data)
        if requested_model.lower() == "list":
            requested_model = ""
        synthetic_message["content"] = "!model" if not requested_model else f"!model {requested_model}"
        await handle_model_command(
            synthetic_message,
            state,
            manager,
            advance_cursor=False,
            reply_func=edit_original,
        )
        return

    edit_interaction_response(application_id, interaction_token, f"❌ 未対応のコマンドです: `{command_name}`")


class DiscordGatewayClient:
    """Connect to Discord Gateway and receive INTERACTION_CREATE events."""

    def __init__(self):
        self.ws: Any | None = None
        self.sequence: int | None = None
        self.session_id: str | None = None
        self.heartbeat_interval: float | None = None
        self.heartbeat_task: asyncio.Task[Any] | None = None
        self.gateway_url: str | None = None

    async def start(self) -> None:
        gateway_url = self.gateway_url or await asyncio.to_thread(self.get_gateway_url)
        self.gateway_url = gateway_url
        ws_url = f"{gateway_url}?v=10&encoding=json"
        # 30s timeout on connect + handshake to avoid silent hangs on reconnect
        self.ws = await asyncio.wait_for(
            websockets.connect(ws_url, ping_interval=None, max_size=None), timeout=30
        )
        hello = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=30))
        self.sequence = hello.get("s")
        await self.handle_hello(hello)
        await self.send_identify()
        log(f"connected to Discord Gateway: {ws_url}")

    async def listen(self):
        while self.ws is not None:
            raw_message = await self.ws.recv()
            payload = json.loads(raw_message)
            if payload.get("s") is not None:
                self.sequence = payload["s"]

            op = payload.get("op")
            if op == 0:
                interaction = await self.handle_dispatch(payload)
                if interaction is not None:
                    yield interaction
                continue
            if op == 1:
                await self.send_heartbeat()
                continue
            if op == 7:
                raise RuntimeError("Discord Gateway requested reconnect")
            if op == 9:
                resumable = bool(payload.get("d"))
                raise RuntimeError(f"Discord Gateway invalid session (resumable={resumable})")
            if op == 10:
                await self.handle_hello(payload)
                continue
            if op == 11:
                continue

    async def stop(self) -> None:
        heartbeat_task = self.heartbeat_task
        ws = self.ws
        self.heartbeat_task = None
        self.ws = None
        if heartbeat_task:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    def get_gateway_url(self) -> str:
        payload = discord_api("GET", "/gateway") or {}
        gateway_url = str(payload.get("url") or "").strip()
        if not gateway_url:
            raise RuntimeError("Discord Gateway URL was not returned by /gateway")
        return gateway_url

    async def send_identify(self) -> None:
        if self.ws is None:
            raise RuntimeError("Discord Gateway is not connected")
        identify = {
            "op": 2,
            "d": {
                "token": DISCORD_TOKEN,
                "intents": 0,
                "properties": {
                    "os": sys.platform,
                    "browser": "adultok-discord-bridge",
                    "device": "adultok-discord-bridge",
                },
            },
        }
        await self.ws.send(json.dumps(identify))

    async def send_heartbeat(self) -> None:
        if self.ws is None:
            raise RuntimeError("Discord Gateway is not connected")
        await self.ws.send(json.dumps({"op": 1, "d": self.sequence}))

    async def handle_hello(self, payload: dict[str, Any]) -> None:
        data = payload.get("d") or {}
        interval_ms = data.get("heartbeat_interval")
        if not isinstance(interval_ms, (int, float)) or interval_ms <= 0:
            raise RuntimeError("Discord Gateway HELLO payload missing heartbeat_interval")
        self.heartbeat_interval = float(interval_ms) / 1000.0
        if self.heartbeat_task is None or self.heartbeat_task.done():
            self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def handle_ready(self, payload: dict[str, Any]) -> None:
        data = payload.get("d") or {}
        session_id = str(data.get("session_id") or "").strip()
        if session_id:
            self.session_id = session_id
        user = data.get("user") or {}
        log(
            f"Discord Gateway READY session={self.session_id or '-'} user={user.get('username') or '-'}",
        )

    async def handle_dispatch(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        event_type = payload.get("t")
        if event_type == "READY":
            await self.handle_ready(payload)
            return None
        if event_type == "INTERACTION_CREATE":
            interaction = payload.get("d")
            if isinstance(interaction, dict):
                return interaction
        return None

    async def _heartbeat_loop(self) -> None:
        if self.heartbeat_interval is None:
            return
        try:
            while self.ws is not None:
                await asyncio.sleep(self.heartbeat_interval)
                await self.send_heartbeat()
        except asyncio.CancelledError:
            raise


async def process_message(
    message: dict[str, Any],
    state: dict[str, Any],
    manager: CopilotSessionManager | None,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> bool:
    global current_task
    message_id = message["id"]
    existing_record = state.get("processed", {}).get(message_id)
    if not force and existing_record:
        state["last_user_message_id"] = message_id
        save_state(state)
        return True

    if is_model_command(message):
        return await handle_model_command(message, state, manager, advance_cursor=True)

    if is_status_command(message):
        return await handle_status_command(message, state, manager, current_task, advance_cursor=True)

    if is_cancel_command(message):
        await handle_cancel_command(message, state, manager, None, advance_cursor=True)
        return True

    content = (message.get("content") or "").strip()
    if not content and not message.get("attachments"):
        state.setdefault("processed", {})[message_id] = {
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "status": "skipped",
            "note": "empty message",
        }
        state["last_user_message_id"] = message_id
        save_state(state)
        log(f"skipped empty message {message_id}")
        return True

    session_id = manager.session_id if manager else (state.get("session_id") or "")
    prompt = build_prompt(message)

    # Inject pending context from a previous session (e.g. after !model switch)
    pending_context = state.pop("pending_context", None)
    if pending_context:
        prompt = pending_context + "\n\n" + prompt
        save_state(state)
        log(f"injected pending context ({len(pending_context)} chars) into message {message_id}")

    if dry_run:
        state.setdefault("processed", {})[message_id] = {
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "status": "dry-run",
            "session_id": session_id,
            "preview": content[:120],
        }
        state["last_user_message_id"] = message_id
        save_state(state)
        log(f"dry-run processed {message_id} in session {session_id}")
        return True

    if manager is None:
        raise RuntimeError("Copilot session manager is required unless --dry-run is used")

    put_reaction(message_id, "👀")
    progress_reply = None
    progress_updater = None
    heartbeat_stop = asyncio.Event()
    record: dict[str, Any] = {
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "session_id": manager.session_id,
        "preview": content[:120],
        "status": "processing",
    }
    # Save "processing" status immediately so restarts don't re-process this message
    state.setdefault("processed", {})[message_id] = record
    save_state(state)

    try:
        progress_reply = reply_to_discord(message_id, format_progress_message(0, "受付しました", "-"))
        if progress_reply and progress_reply.get("id"):
            progress_updater = DiscordProgressUpdater(progress_reply["id"])
            progress_updater.start()
            record["progress_reply_message_id"] = progress_reply["id"]
    except Exception as err:
        record["progress_reply_error"] = str(err)

    heartbeat_task = asyncio.create_task(
        heartbeat_while_processing(manager.session_id, f"processing:{message_id}", heartbeat_stop)
    )
    current_task = asyncio.current_task()

    def handle_progress(tool_name: str, arguments: Any) -> None:
        if not progress_updater:
            return
        if tool_name == "assistant.turn_start":
            progress_updater.update("考えています")
            return
        progress_updater.update(format_tool_action(tool_name, arguments))

    try:
        response_text = await invoke_copilot(manager, state, prompt, on_progress=handle_progress)
        record["session_id"] = manager.session_id
        if progress_reply and progress_reply.get("id"):
            try:
                delete_discord_message(progress_reply["id"])
            except Exception as err:
                record["progress_delete_error"] = str(err)
        # Copilotがテキスト返答を返さなかった場合は投稿しない（直接Discord投稿済みの場合など）
        # Also skip responses that look like "no response" placeholders in any form
        reply = None
        _normalized = (response_text or "").strip().strip("（）()").strip()
        _is_empty = not response_text or _normalized in {"応答なし", "no response", ""}
        if not _is_empty:
            reply = reply_to_discord(message_id, response_text)
        put_reaction(message_id, "✅")
        record.update(
            {
                "status": "done",
                "reply_preview": response_text[:200],
                "reply_message_id": (reply or {}).get("id"),
            }
        )
        log(f"processed Discord {message_id} via SDK session {manager.session_id}")
    except asyncio.CancelledError:
        record.update({"status": "cancelled", "session_id": manager.session_id})
        if progress_reply and progress_reply.get("id"):
            try:
                delete_discord_message(progress_reply["id"])
            except Exception as err:
                record["progress_delete_error"] = str(err)
        log(f"cancelled Discord {message_id}", "WARN")
        raise
    except Exception as err:
        error_text = f"❌ Discord bridge error: {str(err)[:1600]}"
        try:
            if progress_reply and progress_reply.get("id"):
                edit_discord_message(progress_reply["id"], error_text)
            else:
                reply = reply_to_discord(message_id, error_text)
                record["reply_message_id"] = (reply or {}).get("id")
        except Exception as reply_err:
            record["reply_error"] = str(reply_err)
        try:
            put_reaction(message_id, "❌")
        except Exception as reaction_err:
            record["reaction_error"] = str(reaction_err)
        record.update({"status": "error", "error": str(err), "session_id": manager.session_id})
        log(f"failed Discord {message_id}: {err}", "WARN")
    finally:
        heartbeat_stop.set()
        try:
            await heartbeat_task
        except Exception:
            pass
        if progress_updater:
            await progress_updater.stop()
        if current_task is asyncio.current_task():
            current_task = None

    state.setdefault("processed", {})[message_id] = record
    state["last_user_message_id"] = message_id
    save_state(state)
    return True


async def run_once(
    state: dict[str, Any],
    manager: CopilotSessionManager | None,
    *,
    dry_run: bool = False,
) -> int:
    processed = 0
    while True:
        messages = fetch_messages(after=state.get("last_user_message_id"), limit=50)
        if not messages:
            break
        for message in messages:
            if is_model_command(message):
                await handle_model_command(message, state, manager, advance_cursor=True)
            elif is_status_command(message):
                await handle_status_command(message, state, manager, current_task, advance_cursor=True)
            elif is_cancel_command(message):
                await handle_cancel_command(message, state, manager, None, advance_cursor=True)
            else:
                await process_message(message, state, manager, dry_run=dry_run)
            processed += 1
    return processed


async def watch_gateway_interactions(
    gateway_client: DiscordGatewayClient,
    state: dict[str, Any],
    manager: CopilotSessionManager | None,
    stop_event: asyncio.Event,
) -> None:
    retry_delay = 5
    while not stop_event.is_set():
        try:
            await gateway_client.start()
            retry_delay = 5  # reset on successful connect
            async for interaction in gateway_client.listen():
                if stop_event.is_set():
                    break
                if interaction.get("type") != 2:
                    continue
                interaction_id = str(interaction.get("id") or "").strip()
                interaction_token = str(interaction.get("token") or "").strip()
                if not interaction_id or not interaction_token:
                    log("skipping interaction without id or token", "WARN")
                    continue
                try:
                    # Run in thread so blocking HTTP call doesn't delay event loop (3s deadline)
                    await asyncio.to_thread(create_interaction_response, interaction_id, interaction_token)
                except Exception as err:
                    log(f"failed to defer interaction {interaction_id}: {err}", "WARN")
                    continue
                task = asyncio.create_task(process_interaction_command(interaction, state, manager))

                def _log_result(done: asyncio.Task[Any], *, current_interaction_id: str = interaction_id) -> None:
                    try:
                        done.result()
                    except Exception as err:
                        log(f"interaction command failed {current_interaction_id}: {err}", "WARN")

                task.add_done_callback(_log_result)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            log(f"Discord Gateway loop error: {err}", "WARN")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)  # exponential backoff, max 60s
        finally:
            await gateway_client.stop()


def ensure_sdk_available() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from copilot import CopilotClient; print('ok')"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "copilot SDK import failed")


async def async_main() -> int:
    parser = argparse.ArgumentParser(description="Bridge Discord instructions into a persistent Copilot SDK session")
    parser.add_argument("--watch", action="store_true", help="continue polling")
    parser.add_argument("--once", action="store_true", help="run a single polling cycle")
    parser.add_argument("--interval", type=int, default=10, help="polling interval seconds")
    parser.add_argument("--catch-up", action="store_true", help="process unseen messages on first run instead of seeding to latest")
    parser.add_argument("--dry-run", action="store_true", help="do not call Copilot or reply to Discord")
    parser.add_argument("--replay-message-id", help="re-run a specific Discord message ID immediately")
    parser.add_argument("--session-id", help="override the persistent Copilot session ID")
    parser.add_argument("--channel-id", help="Discord channel ID to monitor (overrides DISCORD_CHANNEL_INSTRUCTIONS env var)")
    parser.add_argument("--user-id", help="Discord user ID whose messages are treated as instructions (overrides default)")
    parser.add_argument("--model", help=f"Copilot model to use (overrides DISCORD_COPILOT_MODEL env var). Available: {', '.join(AVAILABLE_MODELS)}")
    args = parser.parse_args()

    # Allow per-instance overrides for multi-bridge setups
    global DISCORD_CHANNEL_ID, STATE_FILE, LOCK_FILE, HEARTBEAT_FILE, DEFAULT_USER_ID, COPILOT_MODEL
    if args.channel_id:
        DISCORD_CHANNEL_ID = args.channel_id
        suffix = args.channel_id
        STATE_FILE = Path(f"/root/.copilot/discord_to_copilot_bridge_{suffix}_state.json")
        LOCK_FILE = Path(f"/root/.copilot/discord_to_copilot_bridge_{suffix}.lock")
        HEARTBEAT_FILE = Path(
            f"/root/projects/persistent_agent/logs/discord_to_copilot_bridge_{suffix}.heartbeat.json"
        )
    if args.user_id:
        global DISCORD_USER_IDS
        DISCORD_USER_IDS = {uid.strip() for uid in args.user_id.split(",") if uid.strip()}
    if args.model:
        COPILOT_MODEL = args.model

    if not DISCORD_TOKEN:
        log("DISCORD_BOT_TOKEN missing", "ERROR")
        return 1

    try:
        if not args.dry_run:
            ensure_sdk_available()
    except Exception as err:
        log(f"copilot SDK unavailable: {err}", "ERROR")
        return 1

    state = load_state()
    if args.session_id:
        state["session_id"] = args.session_id
        save_state(state)

    # Mark any in-flight messages as interrupted (prevents re-processing on restart)
    interrupted = [mid for mid, rec in state.get("processed", {}).items() if rec.get("status") == "processing"]
    if interrupted:
        for mid in interrupted:
            state["processed"][mid]["status"] = "interrupted"
        save_state(state)
        log(f"marked {len(interrupted)} interrupted message(s) on startup: {interrupted}")

    stop_event = asyncio.Event()

    def request_shutdown() -> None:
        if not stop_event.is_set():
            log("shutdown requested")
            stop_event.set()

    if args.watch:
        try:
            acquire_instance_lock()
            write_heartbeat(state.get("session_id") or "", "locked")
        except Exception as err:
            log(str(err), "ERROR")
            return 1

    current_model = state.get("model") or COPILOT_MODEL
    manager = None
    gateway_client: DiscordGatewayClient | None = None
    gateway_task: asyncio.Task[Any] | None = None
    if not args.dry_run:
        manager = CopilotSessionManager(
            state,
            model=current_model,
            reasoning_effort=COPILOT_REASONING,
            requested_session_id=args.session_id,
        )

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_shutdown)
            except NotImplementedError:
                pass

        if manager:
            await manager.start()
            write_heartbeat(manager.session_id, "connected")

        if args.watch and not args.dry_run:
            application_id = discover_discord_application_id()
            try:
                register_slash_commands(application_id)
                log(f"registered Discord slash commands for application {application_id}")
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    log(f"Discord slash command registration rate-limited (429); skipping (commands already registered)", "WARN")
                else:
                    raise
            gateway_client = DiscordGatewayClient()
            gateway_task = asyncio.create_task(
                watch_gateway_interactions(gateway_client, state, manager, stop_event)
            )

        if args.replay_message_id:
            try:
                await process_message(
                    fetch_message(args.replay_message_id),
                    state,
                    manager,
                    dry_run=args.dry_run,
                    force=True,
                )
                log(f"replayed message {args.replay_message_id}")
                return 0
            except Exception as err:
                log(f"replay failed: {err}", "ERROR")
                return 1

        if not state.get("last_user_message_id") and not args.catch_up:
            seed_to_latest_message(state)

        if args.once or not args.watch:
            processed = await run_once(state, manager, dry_run=args.dry_run)
            log(f"done once: processed={processed}")
            return 0

        log(
            f"watching Discord channel {DISCORD_CHANNEL_ID} -> SDK session {manager.session_id if manager else state.get('session_id')} "
            f"(interval={args.interval}s, model={current_model})"
        )
        processing_task: asyncio.Task[Any] | None = None
        while True:
            if stop_event.is_set():
                break
            active_session_id = manager.session_id if manager else (state.get("session_id") or "")
            write_heartbeat(active_session_id, "watching")
            try:
                if processing_task and processing_task.done():
                    try:
                        await processing_task
                    except asyncio.CancelledError:
                        pass
                    finally:
                        processing_task = None
                    active_session_id = manager.session_id if manager else (state.get("session_id") or "")
                    write_heartbeat(active_session_id, "processed")

                messages = fetch_messages(after=state.get("last_user_message_id"), limit=50)
                if processing_task:
                    model_message = next(
                        (
                            message
                            for message in messages
                            if is_model_command(message) and not state.get("processed", {}).get(message["id"])
                        ),
                        None,
                    )
                    if model_message:
                        await handle_model_command(
                            model_message,
                            state,
                            manager,
                            advance_cursor=False,
                        )
                        active_session_id = manager.session_id if manager else (state.get("session_id") or "")
                        write_heartbeat(active_session_id, "model-switched")
                        continue
                    cancel_message = next(
                        (
                            message
                            for message in messages
                            if is_cancel_command(message) and not state.get("processed", {}).get(message["id"])
                        ),
                        None,
                    )
                    if cancel_message:
                        cancelled = await handle_cancel_command(
                            cancel_message,
                            state,
                            manager,
                            processing_task,
                            advance_cursor=False,
                        )
                        if cancelled:
                            processing_task = None
                            active_session_id = manager.session_id if manager else (state.get("session_id") or "")
                            write_heartbeat(active_session_id, "cancelled")
                elif messages:
                    next_message = messages[0]
                    if is_model_command(next_message):
                        await handle_model_command(next_message, state, manager, advance_cursor=True)
                        active_session_id = manager.session_id if manager else (state.get("session_id") or "")
                        write_heartbeat(active_session_id, "model-switched")
                    elif is_cancel_command(next_message):
                        await handle_cancel_command(next_message, state, manager, None, advance_cursor=True)
                        write_heartbeat(active_session_id, "processed")
                    else:
                        processing_task = asyncio.create_task(
                            process_message(next_message, state, manager, dry_run=args.dry_run)
                        )
            except urllib.error.HTTPError as err:
                log(f"http error: {err}", "WARN")
                write_heartbeat(active_session_id, "http-error")
            except Exception as err:
                log(f"loop error: {err}", "WARN")
                write_heartbeat(active_session_id, "loop-error")
                if manager:
                    try:
                        await manager.stop()
                        await manager.start()
                        write_heartbeat(manager.session_id, "restarted")
                    except Exception as restart_err:
                        log(f"session manager restart failed: {restart_err}", "WARN")
            await asyncio.sleep(max(2, args.interval))
        if processing_task:
            try:
                await processing_task
            except asyncio.CancelledError:
                pass
        return 0
    finally:
        if gateway_task:
            gateway_task.cancel()
            try:
                await gateway_task
            except asyncio.CancelledError:
                pass
        elif gateway_client:
            await gateway_client.stop()
        if manager:
            await manager.stop()


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    sys.exit(main())
