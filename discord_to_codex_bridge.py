#!/usr/bin/env python3
"""
Discord-to-Codex Bridge
DiscordチャンネルからCodex CLIを呼び出すブリッジ

- 指定チャンネルをポーリング（デフォルト10秒）
- codex mcp-server を常駐プロセスとして起動し MCP/JSON-RPC で通信
- thread_id を state file に保存してセッション継続
- 返信はDiscord Bot APIで元メッセージへreply

Usage:
  python3 discord_to_codex_bridge.py [--channel <id>] [--interval <sec>] [--cwd <dir>]
"""

import argparse
import fcntl
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# CodexMCPClient を同ディレクトリからインポート
sys.path.insert(0, str(Path(__file__).parent))
from codex_mcp_client import SyncCodexClient, CodexMCPError

# ── 設定 ──────────────────────────────────────────────────────────
ENV_FILE = os.environ.get("ENV_FILE", str(Path(os.path.dirname(os.path.abspath(__file__))) / ".env"))
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = Path.home() / ".codex"
STATE_FILE = STATE_DIR / "discord_to_codex_bridge_state.json"
LOCK_FILE = STATE_DIR / "discord_to_codex_bridge.lock"
LOG_DIR = Path(os.environ.get("LOG_DIR", str(Path.home() / ".dangerbot" / "logs")))

DISCORD_API = "https://discord.com/api/v10"
DEFAULT_CHANNEL = os.environ.get("CODEX_DISCORD_CHANNEL", "")
DEFAULT_POLL_INTERVAL = 10  # seconds
AUTHORIZED_USER_IDS = {uid.strip() for uid in os.environ.get("AUTHORIZED_USER_IDS", "").split(",") if uid.strip()}
MAX_REPLY_LEN = 1900
CODEX_TIMEOUT = 600  # 10分


# ── .env 読み込み ──────────────────────────────────────────────────
def load_env() -> dict:
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return env


ENV = load_env()
BOT_TOKEN = ENV.get("DISCORD_BOT_TOKEN", os.environ.get("DISCORD_BOT_TOKEN", ""))


# ── ロギング ──────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)  # systemd が stdout をログファイルに転送する


# ── State 管理 ────────────────────────────────────────────────────
def load_state(channel_id: str) -> dict:
    try:
        with open(STATE_FILE) as f:
            all_state = json.load(f)
        return all_state.get(channel_id, {})
    except Exception:
        return {}


def save_state(channel_id: str, state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE) as f:
            all_state = json.load(f)
    except Exception:
        all_state = {}
    # processed は最新500件に絞る
    processed = state.get("processed", {})
    if len(processed) > 500:
        keys = sorted(processed.keys())[-500:]
        state["processed"] = {k: processed[k] for k in keys}
    all_state[channel_id] = state
    tmp = str(STATE_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(all_state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ── シングルインスタンスロック ─────────────────────────────────────
_lock_fd = None


def acquire_lock(channel_id: str) -> bool:
    global _lock_fd
    lock_path = STATE_DIR / f"discord_to_codex_bridge_{channel_id}.lock"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _lock_fd = open(lock_path, "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        return True
    except OSError:
        return False


# ── Discord API ───────────────────────────────────────────────────
def discord_request(method: str, path: str, data: dict = None, retries: int = 3):
    url = f"{DISCORD_API}{path}"
    headers = {
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com/anitigravitylab-oss/dangerbot, 1.0)",
    }
    body = json.dumps(data).encode() if data else None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = float(e.headers.get("Retry-After", 5))
                log(f"Rate limited, waiting {retry_after}s")
                time.sleep(retry_after)
                continue
            if e.code in (404, 403):
                raise
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def fetch_messages(channel_id: str, after: str = None) -> list:
    path = f"/channels/{channel_id}/messages?limit=50"
    if after:
        path += f"&after={after}"
    try:
        msgs = discord_request("GET", path) or []
        return sorted(msgs, key=lambda m: m["id"])
    except Exception as e:
        log(f"fetch_messages error: {e}")
        return []


def add_reaction(channel_id: str, message_id: str, emoji: str):
    try:
        discord_request(
            "PUT",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me",
        )
    except Exception:
        pass


def post_message(channel_id: str, content: str, reply_to: str = None) -> dict:
    """Send a message, splitting into multiple chunks if it exceeds Discord's limit."""
    # Split into chunks at paragraph/newline boundaries where possible
    chunks = _split_message(content, MAX_REPLY_LEN)
    last_msg = {}
    for i, chunk in enumerate(chunks):
        data = {"content": chunk}
        if reply_to and i == 0:
            data["message_reference"] = {"message_id": reply_to}
        try:
            last_msg = discord_request("POST", f"/channels/{channel_id}/messages", data) or {}
        except Exception as e:
            log(f"post_message error (chunk {i+1}/{len(chunks)}): {e}")
    return last_msg


def _split_message(text: str, limit: int) -> list:
    """Split text into chunks not exceeding limit characters each."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Try to split at a newline near the limit
        split_at = text.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def edit_message(channel_id: str, message_id: str, content: str):
    try:
        discord_request(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            {"content": content[:MAX_REPLY_LEN]},
        )
    except Exception as e:
        log(f"edit_message error: {e}")


# ── Codex 実行（MCP常駐接続）────────────────────────────────────────
def run_codex_mcp(
    client: SyncCodexClient,
    prompt: str,
    thread_id: str = None,
    cwd: str = PROJECT_ROOT,
    model: str = None,
) -> dict:
    """
    SyncCodexClient (MCP常駐接続) でプロンプトを実行し結果を返す。

    Returns:
        {"success": bool, "thread_id": str, "message": str}
    """
    log(f"  mcp call (thread={thread_id or 'new'})")
    try:
        if thread_id:
            result = client.reply(thread_id, prompt)
        else:
            result = client.run(prompt, cwd=cwd, model=model)

        if result.thread_id:
            log(f"  thread_id: {result.thread_id}")

        return {
            "success": not result.is_error,
            "thread_id": result.thread_id or thread_id,
            "message": result.text or "（出力なし）",
        }
    except CodexMCPError as e:
        return {"success": False, "thread_id": thread_id, "message": f"❌ MCP エラー: {e}"}
    except Exception as e:
        return {"success": False, "thread_id": thread_id, "message": f"❌ Codex実行エラー: {e}"}


# ── プログレス更新 ────────────────────────────────────────────────
class ProgressUpdater:
    """Codex実行中にDiscordメッセージを定期更新するスレッド"""

    def __init__(self, channel_id: str, message_id: str, start_time: float):
        self.channel_id = channel_id
        self.message_id = message_id
        self.start_time = start_time
        self.current_action = "処理中..."
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def set_action(self, action: str):
        self.current_action = action

    def _run(self):
        while not self._stop.wait(20):
            elapsed = int(time.time() - self.start_time)
            m, s = divmod(elapsed, 60)
            elapsed_str = f"{m}分{s}秒" if m else f"{s}秒"
            edit_message(
                self.channel_id,
                self.message_id,
                f"⏳ Codex実行中... ({elapsed_str})\n`{self.current_action}`",
            )


# ── メッセージ処理 ────────────────────────────────────────────────
def process_message(
    channel_id: str,
    msg: dict,
    state: dict,
    client: SyncCodexClient,
    cwd: str = PROJECT_ROOT,
) -> dict:
    """1件のDiscordメッセージをCodexで処理して返信する"""
    msg_id = msg["id"]
    content = msg.get("content", "").strip()
    start_time = time.time()

    log(f"📨 [{msg_id}] {content[:80]}")

    # 👀 受付リアクション
    add_reaction(channel_id, msg_id, "%F0%9F%91%80")

    # !model コマンド
    if content.startswith("!model"):
        parts = content.split()
        if len(parts) >= 2:
            new_model = parts[1]
            state["model"] = new_model
            post_message(channel_id, f"✅ モデルを `{new_model}` に切り替えました", reply_to=msg_id)
            add_reaction(channel_id, msg_id, "%E2%9C%85")
        else:
            current = state.get("model", "（デフォルト）")
            post_message(
                channel_id,
                f"現在のモデル: `{current}`\n使用法: `!model <model_name>`",
                reply_to=msg_id,
            )
        return state

    # !reset コマンド: セッションをリセット
    if content.strip() in ("!reset", "!new"):
        state.pop("thread_id", None)
        post_message(channel_id, "🔄 Codexセッションをリセットしました（次回から新規セッション）", reply_to=msg_id)
        add_reaction(channel_id, msg_id, "%E2%9C%85")
        return state

    # !status コマンド
    if content.strip() in ("!status", "!ping"):
        tid = state.get("thread_id", "なし")
        model = state.get("model", "デフォルト")
        mcp_ok = "🟢" if client.is_running() else "🔴"
        post_message(
            channel_id,
            f"✅ Bridge稼働中 {mcp_ok} MCP\nthread_id: `{tid}`\nmodel: `{model}`",
            reply_to=msg_id,
        )
        add_reaction(channel_id, msg_id, "%E2%9C%85")
        return state

    # 進捗メッセージを投稿
    progress_msg = post_message(
        channel_id,
        "⏳ 受付しました。Codex実行中...",
        reply_to=msg_id,
    )
    progress_msg_id = progress_msg.get("id") if progress_msg else None

    # プログレス更新スレッド起動
    updater = None
    if progress_msg_id:
        updater = ProgressUpdater(channel_id, progress_msg_id, start_time)
        updater.start()

    try:
        result = run_codex_mcp(
            client=client,
            prompt=content,
            thread_id=state.get("thread_id"),
            cwd=cwd,
            model=state.get("model"),
        )
        # "Session not found" エラー → thread_id をリセットして新規セッションで再試行
        if not result["success"] and "session not found" in result["message"].lower():
            log(f"  ⚠️ Session not found, resetting thread_id and retrying as new session")
            state.pop("thread_id", None)
            result = run_codex_mcp(
                client=client,
                prompt=content,
                thread_id=None,
                cwd=cwd,
                model=state.get("model"),
            )
    finally:
        if updater:
            updater.stop()

    # プログレスメッセージを削除
    if progress_msg_id:
        try:
            discord_request("DELETE", f"/channels/{channel_id}/messages/{progress_msg_id}")
        except Exception:
            pass

    # thread_id を更新
    if result.get("thread_id"):
        state["thread_id"] = result["thread_id"]

    # 応答を返信
    elapsed = int(time.time() - start_time)
    reply_text = result["message"]

    suffix = f"\n\n_thread: `{result['thread_id']}` | {elapsed}秒_" if result.get("thread_id") else ""
    reply_msg = post_message(
        channel_id,
        reply_text + suffix,
        reply_to=msg_id,
    )

    # リアクション
    emoji = "%E2%9C%85" if result["success"] else "%E2%9D%8C"
    add_reaction(channel_id, msg_id, emoji)

    # state 記録
    if "processed" not in state:
        state["processed"] = {}
    state["processed"][msg_id] = {
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "status": "done" if result["success"] else "error",
        "thread_id": result.get("thread_id"),
        "preview": content[:100],
        "reply_id": reply_msg.get("id") if reply_msg else None,
        "duration_sec": elapsed,
        "tokens": result.get("tokens", 0),
    }

    log(f"  ✅ done in {elapsed}s | thread={result.get('thread_id')} | tokens={result.get('tokens')}")
    return state


# ── メインループ ──────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Discord-to-Codex Bridge")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, help="Discord channel ID")
    parser.add_argument("--interval", type=int, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--cwd", default=PROJECT_ROOT, help="Working directory for Codex")
    parser.add_argument("--no-lock", action="store_true", help="Skip single-instance lock")
    args = parser.parse_args()

    channel_id = args.channel

    if not BOT_TOKEN:
        log("❌ DISCORD_BOT_TOKEN が設定されていません")
        sys.exit(1)

    if not args.no_lock:
        if not acquire_lock(channel_id):
            log(f"❌ 別のインスタンスが実行中です (channel={channel_id})")
            sys.exit(1)

    log(f"🤖 Discord-Codex Bridge 起動 (MCP常駐接続)")
    log(f"   channel={channel_id}")
    log(f"   cwd={args.cwd}")
    log(f"   interval={args.interval}s")

    state = load_state(channel_id)
    last_message_id = state.get("last_user_message_id")
    log(f"   thread_id={state.get('thread_id', '新規')}")
    log(f"   last_msg={last_message_id or 'なし'}")

    # MCP 常駐プロセスを起動
    log("   MCP サーバー起動中...")
    client = SyncCodexClient(cwd=args.cwd)
    log("   MCP サーバー接続完了 ✅")

    try:
        while True:
            try:
                # MCP プロセスが死んでいたら再起動
                if not client.is_running():
                    log("⚠️  MCP プロセス再起動中...")
                    try:
                        client.close()
                    except Exception:
                        pass
                    client = SyncCodexClient(cwd=args.cwd)
                    log("   MCP 再接続完了 ✅")

                messages = fetch_messages(channel_id, after=last_message_id)

                for msg in messages:
                    msg_id = msg["id"]
                    author = msg.get("author", {})
                    content = msg.get("content", "").strip()

                    last_message_id = msg_id
                    state["last_user_message_id"] = last_message_id

                    # ボット・未認証ユーザー・空メッセージをスキップ
                    if author.get("bot"):
                        continue
                    if author.get("id") not in AUTHORIZED_USER_IDS:
                        continue
                    if not content:
                        continue

                    # 処理済みチェック
                    if msg_id in state.get("processed", {}):
                        continue

                    state = process_message(channel_id, msg, state, client=client, cwd=args.cwd)
                    save_state(channel_id, state)

                # メッセージがなくても last_message_id を更新
                if messages:
                    save_state(channel_id, state)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"⚠️  Loop error: {e}")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        log("🛑 停止")
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
