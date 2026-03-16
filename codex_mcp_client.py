#!/usr/bin/env python3
"""
CodexMCPClient - Codex CLI の MCP サーバーモードを使った Python SDK

codex mcp-server を常駐プロセスとして起動し、JSON-RPC 2.0 (newline-delimited) で通信する。
Copilot SDK の CopilotClient に近い API を提供する。

Usage:
    import asyncio
import os
    from codex_mcp_client import CodexMCPClient

    async def main():
        async with CodexMCPClient() as client:
            # 新規セッション
            result = await client.run("echo hello")
            print(result.text)
            print(result.thread_id)

            # セッション継続
            result2 = await client.reply(result.thread_id, "前のコマンドの結果は？")
            print(result2.text)

    asyncio.run(main())
"""

import asyncio
import os
import json
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# デフォルト設定
DEFAULT_CWD = os.environ.get("PROJECT_ROOT", os.getcwd())
DEFAULT_SANDBOX = "danger-full-access"
DEFAULT_MODEL = None  # None = codex のデフォルト設定を使用


@dataclass
class CodexResult:
    """codex / codex-reply ツール呼び出しの結果"""
    text: str
    thread_id: Optional[str] = None
    is_error: bool = False

    def __str__(self):
        return self.text


class CodexMCPError(Exception):
    """MCP 通信エラー"""
    pass


class CodexMCPClient:
    """
    Codex CLI の MCP サーバーモードに接続する常駐クライアント。

    `codex mcp-server` を subprocess として起動し、JSON-RPC 2.0 で通信する。
    プロセスは start() で起動し stop() または async context manager で終了する。
    """

    def __init__(
        self,
        cwd: str = DEFAULT_CWD,
        sandbox: str = DEFAULT_SANDBOX,
        model: Optional[str] = DEFAULT_MODEL,
        timeout: int = 600,
    ):
        self.cwd = cwd
        self.sandbox = sandbox
        self.model = model
        self.timeout = timeout

        self._proc: Optional[subprocess.Popen] = None
        self._responses: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._next_id = 2  # 1 は initialize で使用
        self._running = False
        # 現 MCP プロセスが開始したスレッドのみ codex-reply で処理できる
        self._known_threads: set = set()

    # ── async context manager ────────────────────────────────────
    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    # ── ライフサイクル ─────────────────────────────────────────
    async def start(self):
        """MCP サーバープロセスを起動して初期化する"""
        if self._running:
            return

        self._proc = subprocess.Popen(
            ["codex", "mcp-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._running = True
        self._known_threads.clear()  # 新プロセスなのでスレッド情報をリセット

        # 標準出力を別スレッドで読み続ける
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        # initialize ハンドシェイク
        await self._initialize()

    async def stop(self):
        """プロセスを終了する"""
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def is_running(self) -> bool:
        return self._running and self._proc is not None and self._proc.poll() is None

    # ── 公開 API ──────────────────────────────────────────────
    async def run(
        self,
        prompt: str,
        cwd: Optional[str] = None,
        sandbox: Optional[str] = None,
        model: Optional[str] = None,
    ) -> CodexResult:
        """新規 Codex セッションでプロンプトを実行する"""
        if not self.is_running():
            await self.start()

        args: dict = {
            "prompt": prompt,
            "cwd": cwd or self.cwd,
            "sandbox": sandbox or self.sandbox,
        }
        if model or self.model:
            args["model"] = model or self.model

        result = await self._call_tool("codex", args)
        # 成功したら thread_id を現プロセスの既知スレッドに登録
        if result.thread_id and not result.is_error:
            self._known_threads.add(result.thread_id)
        return result

    async def reply(
        self,
        thread_id: str,
        prompt: str,
    ) -> CodexResult:
        """既存セッション (thread_id) に続けてプロンプトを送る。
        
        現 MCP プロセスが知らないスレッド（再起動後など）は
        codex exec resume --json にフォールバックして DB から復元する。
        """
        if not self.is_running():
            await self.start()

        # 現 MCP プロセスが知らない thread_id → subprocess resume にフォールバック
        if thread_id not in self._known_threads:
            return await self._subprocess_resume(thread_id, prompt)

        # conversationId は非推奨 → threadId のみ使用
        args = {
            "prompt": prompt,
            "threadId": thread_id,
        }
        result = await self._call_tool("codex-reply", args)

        # "Session not found" が返ってきた場合もフォールバック
        if result.is_error and "session not found" in result.text.lower():
            self._known_threads.discard(thread_id)
            return await self._subprocess_resume(thread_id, prompt)

        return result

    async def _subprocess_resume(self, thread_id: str, prompt: str) -> CodexResult:
        """codex exec resume --json で DB から既存セッションを復元して実行する。
        
        MCP プロセスが知らない thread_id を引き継ぐためのフォールバック手段。
        """
        cmd = ["codex", "exec", "resume", thread_id, prompt, "--json"]
        try:
            proc = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                ),
            )
            output = proc.stdout.strip()
            text_parts = []
            found_thread_id = thread_id
            for line in output.splitlines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    t = obj.get("type", "")
                    if t == "thread.started":
                        found_thread_id = obj.get("thread_id", thread_id)
                    elif t == "item.completed":
                        item = obj.get("item", {})
                        if item.get("type") == "agent_message":
                            text_parts.append(item.get("text", ""))
                except json.JSONDecodeError:
                    pass

            if not text_parts and proc.returncode != 0:
                err = proc.stderr.strip()
                return CodexResult(text=f"❌ codex resume 失敗: {err}", thread_id=thread_id, is_error=True)

            # 成功した thread_id を known_threads に登録（次回は MCP で処理できる）
            # ※ ただし subprocess resume では MCP には登録されないため継続して subprocess を使う
            self._known_threads.discard(thread_id)  # 敢えて known に入れない
            return CodexResult(
                text="\n".join(text_parts) or "（出力なし）",
                thread_id=found_thread_id,
                is_error=False,
            )
        except Exception as e:
            return CodexResult(text=f"❌ codex resume エラー: {e}", thread_id=thread_id, is_error=True)

    # ── 内部実装 ──────────────────────────────────────────────
    def _send(self, msg: dict):
        """JSON-RPC メッセージを送信（newline-delimited）"""
        if not self._proc or not self._proc.stdin:
            raise CodexMCPError("Process not running")
        data = json.dumps(msg) + "\n"
        try:
            self._proc.stdin.write(data.encode())
            self._proc.stdin.flush()
        except BrokenPipeError as e:
            raise CodexMCPError(f"Broken pipe: {e}")

    def _read_loop(self):
        """stdout を監視して応答を responses dict に格納するスレッド"""
        try:
            for raw in self._proc.stdout:
                line = raw.decode().strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    if "id" in msg:
                        with self._lock:
                            self._responses[msg["id"]] = msg
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass

    async def _wait_for(self, req_id: int) -> dict:
        """指定 ID の応答が来るまで非同期で待つ"""
        deadline = asyncio.get_event_loop().time() + self.timeout
        while asyncio.get_event_loop().time() < deadline:
            with self._lock:
                if req_id in self._responses:
                    return self._responses.pop(req_id)
            await asyncio.sleep(0.1)
        raise CodexMCPError(f"Timeout waiting for response id={req_id}")

    def _next_req_id(self) -> int:
        req_id = self._next_id
        self._next_id += 1
        return req_id

    async def _initialize(self):
        """MCP initialize ハンドシェイク"""
        self._send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "codex-mcp-client", "version": "1.0"},
            },
        })
        await self._wait_for(1)
        # initialized 通知を送る
        self._send({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        })

    async def _call_tool(self, tool_name: str, arguments: dict) -> CodexResult:
        """tools/call リクエストを送って CodexResult を返す"""
        req_id = self._next_req_id()
        self._send({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        })
        response = await self._wait_for(req_id)

        result = response.get("result", {})
        is_error = bool(result.get("isError"))
        content_list = result.get("content", [])
        text = "\n".join(c.get("text", "") for c in content_list if c.get("type") == "text")
        structured = result.get("structuredContent", {})
        thread_id = structured.get("threadId")

        if "error" in response:
            err = response["error"]
            raise CodexMCPError(f"JSON-RPC error {err.get('code')}: {err.get('message')}")

        return CodexResult(text=text, thread_id=thread_id, is_error=is_error)


# ── 同期ラッパー（シンプルな用途向け）──────────────────────────
class SyncCodexClient:
    """
    CodexMCPClient の同期ラッパー。asyncio を使わずに呼べる。

    Usage:
        client = SyncCodexClient()
        result = client.run("ls -la")
        print(result.text)
        result2 = client.reply(result.thread_id, "合計ファイル数は？")
        client.close()
    """

    def __init__(self, **kwargs):
        self._client = CodexMCPClient(**kwargs)
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._client.start())

    def is_running(self) -> bool:
        return self._client.is_running()

    def run(self, prompt: str, **kwargs) -> CodexResult:
        return self._loop.run_until_complete(self._client.run(prompt, **kwargs))

    def reply(self, thread_id: str, prompt: str) -> CodexResult:
        return self._loop.run_until_complete(self._client.reply(thread_id, prompt))

    def close(self):
        self._loop.run_until_complete(self._client.stop())
        self._loop.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ── CLI テスト ─────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    async def test():
        print("🚀 CodexMCPClient テスト開始")
        async with CodexMCPClient() as client:
            prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "echo 'hello from codex sdk' && pwd"
            print(f"  prompt: {prompt}")

            result = await client.run(prompt)
            print(f"  thread_id: {result.thread_id}")
            print(f"  is_error: {result.is_error}")
            print(f"  text:\n{result.text}")

            if result.thread_id and not result.is_error:
                print("\n  [続きのテスト]")
                r2 = await client.reply(result.thread_id, "上のコマンドをもう一度実行して")
                print(f"  text:\n{r2.text}")

        print("✅ テスト完了")

    asyncio.run(test())
