from __future__ import annotations

import json
import subprocess
import threading
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from typing import Any


class MCPError(RuntimeError):
    pass


class StdioMCPClient:
    """Minimal MCP stdio client (newline-delimited JSON-RPC 2.0).

    Production should migrate to the official MCP SDK. This implementation is
    dependency-free so the runtime can be tested on a clean Windows machine.
    """

    def __init__(
        self,
        command: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        default_timeout: float = 30.0,
        protocol_version: str = "2024-11-05",
    ) -> None:
        if not command:
            raise ValueError("MCP command must not be empty")
        self.command = list(command)
        self.default_timeout = default_timeout
        self.protocol_version = protocol_version
        self._closed = False
        self._next_id = 0
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, Future[dict[str, Any]]] = {}
        self.stderr_tail: deque[str] = deque(maxlen=80)
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            cwd=cwd,
        )
        self._reader = threading.Thread(
            target=self._read_loop, name="mcp-stdout", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._stderr_loop, name="mcp-stderr", daemon=True
        )
        self._stderr_reader.start()
        try:
            self.initialize()
        except MCPError as exc:
            self._stderr_reader.join(timeout=1)
            code = self._proc.poll()
            if code is None:
                try:
                    code = self._proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    code = None
            detail = str(exc)
            if code is not None:
                detail += f"（退出码 {code}）"
            tail = " | ".join(self.stderr_tail)
            if tail:
                detail += f"：{tail}"
            self.close()
            raise MCPError(detail) from None

    @property
    def pid(self) -> int | None:
        return self._proc.pid

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "xm-agent", "version": "0.1.0"},
            },
        )
        self.notify("notifications/initialized", {})
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return [tool for tool in tools if isinstance(tool, dict)]

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result = self.request(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        return result if isinstance(result, dict) else {}

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise MCPError("MCP client is closed")
        request_id = self._next_request_id()
        future: Future[dict[str, Any]] = Future()
        with self._pending_lock:
            self._pending[request_id] = future
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                }
            )
            message = future.result(timeout=timeout or self.default_timeout)
        except FutureTimeoutError:
            raise MCPError(
                f"timeout after {timeout or self.default_timeout}s: {method}"
            ) from None
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

        if "error" in message:
            raise MCPError(f"{method} failed: {message['error']}")
        result = message.get("result", {})
        return result if isinstance(result, dict) else {"value": result}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=3)
        self._reader.join(timeout=1)
        self._stderr_reader.join(timeout=1)
        for stream in (self._proc.stdout, self._proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def __enter__(self) -> "StdioMCPClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _next_request_id(self) -> int:
        with self._pending_lock:
            self._next_id += 1
            return self._next_id

    def _send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False) + "\n"
        with self._write_lock:
            if self._proc.poll() is not None:
                raise MCPError(
                    "MCP server exited: " + " | ".join(self.stderr_tail)
                )
            if self._proc.stdin is None:
                raise MCPError("MCP stdin is not available")
            try:
                self._proc.stdin.write(payload)
                self._proc.stdin.flush()
            except OSError as exc:
                raise MCPError(f"failed to write to MCP server: {exc}") from exc

    def _read_loop(self) -> None:
        stdout = self._proc.stdout
        if stdout is None:
            return
        try:
            for line in stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if message_id is None:
                    continue
                with self._pending_lock:
                    future = self._pending.get(int(message_id))
                if future is not None and not future.done():
                    future.set_result(message)
        finally:
            with self._pending_lock:
                pending = list(self._pending.values())
            for future in pending:
                if not future.done():
                    future.set_exception(MCPError("MCP server closed stdout"))

    def _stderr_loop(self) -> None:
        stderr = self._proc.stderr
        if stderr is None:
            return
        for line in stderr:
            text = line.rstrip()
            if text:
                self.stderr_tail.append(text)
