"""CDP 客户端：RFC 6455 分帧 + JSON-RPC 调用 + 事件订阅，只用标准库。

设计要点：
- **后台读线程**：镜像要持续收 `Page.screencastFrame`，同时 AI 还要发命令，
  所以读与写必须解耦。调用方按 id 等 Event，事件交给订阅者。
- **写锁**：一个 socket 不能并发写帧，所有发送串行化。
- **失败关闭**：连接断了要把所有在等的调用一次性唤醒并报错，不能挂死。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
from collections.abc import Callable
from typing import Any


class CdpError(RuntimeError):
    """CDP 调用失败（协议错误、连接断开、超时）。"""


# 高频事件不进缓存，否则帧会吃掉内存
_NO_CACHE = {"Page.screencastFrame"}


class CdpConnection:
    """一条到浏览器 DevTools 端点的 WebSocket 连接。"""

    def __init__(self, url: str, timeout: float = 20.0):
        if not url.startswith("ws://"):
            raise CdpError(f"仅支持 ws:// 端点: {url}")
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        self._path = "/" + path
        host, _, port = hostport.partition(":")
        try:
            self._sock = socket.create_connection((host, int(port)), timeout=timeout)
        except OSError as exc:
            raise CdpError(f"连接 DevTools 端点失败 {hostport}: {exc}") from exc
        self._sock.settimeout(timeout)
        self._buf = b""
        self._frag = bytearray()
        self._frag_opcode = 0
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}
        self._handlers: dict[str, list[Callable[[dict], None]]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._next_id = 0
        self._closed = False
        self._error: str = ""
        self._default_timeout = timeout
        self._handshake()
        self._reader = threading.Thread(
            target=self._read_loop, name="cdp-reader", daemon=True
        )
        self._reader.start()

    # ------------------------------------------------------------------ 握手
    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CdpError("WebSocket 握手被对端关闭")
            head += chunk
        head, _, self._buf = head.partition(b"\r\n\r\n")
        status = head.split(b"\r\n")[0]
        if b"101" not in status:
            raise CdpError(f"WebSocket 握手失败: {status.decode(errors='replace')}")
        expect = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        )
        if b"sec-websocket-accept" in head.lower() and expect not in head:
            raise CdpError("WebSocket 握手校验失败（accept 不匹配）")

    # -------------------------------------------------------------------- 帧
    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        mask = os.urandom(4)
        size = len(payload)
        frame = bytearray([0x80 | opcode])
        if size < 126:
            frame.append(0x80 | size)
        elif size < (1 << 16):
            frame.append(0x80 | 126)
            frame += struct.pack(">H", size)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack(">Q", size)
        frame += mask
        frame += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._write_lock:
            self._sock.sendall(frame)

    def _read_exact(self, count: int) -> bytes:
        while len(self._buf) < count:
            try:
                chunk = self._sock.recv(65536)
            except (socket.timeout, TimeoutError):
                # 空闲不是错误。页面静止时浏览器一分钟也不推一条消息，
                # 把读超时当断线会连带毁掉整条连接（真机现场：用户想几秒
                # 问题，之后的每次调用都报「连接已关闭」）。半帧数据在
                # _buf 里，接着等下一批字节即可。
                continue
            if not chunk:
                raise CdpError("连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:count], self._buf[count:]
        return out

    def _read_frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = self._read_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        return fin, opcode, (self._read_exact(length) if length else b"")

    # ------------------------------------------------------------- 读线程
    def _read_loop(self) -> None:
        try:
            while not self._closed:
                fin, opcode, data = self._read_frame()
                if opcode == 0x9:  # ping
                    self._send_frame(data, opcode=0xA)
                    continue
                if opcode == 0xA:  # pong
                    continue
                if opcode == 0x8:  # close
                    break
                if opcode == 0x0:  # continuation
                    self._frag += data
                    if fin:
                        payload, self._frag = bytes(self._frag), bytearray()
                        self._handle_message(payload)
                    continue
                if not fin:
                    self._frag = bytearray(data)
                    self._frag_opcode = opcode
                    continue
                self._handle_message(data)
        except Exception as exc:  # 包括 socket 关闭与解析错误
            self._fail_all(f"读线程退出: {exc}")
        else:
            self._fail_all("连接已被对端关闭")

    def _handle_message(self, payload: bytes) -> None:
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            return
        message_id = message.get("id")
        if message_id is not None:
            with self._state_lock:
                slot = self._pending.pop(message_id, None)
            if slot is not None:
                slot["reply"] = message
                slot["event"].set()
            return
        self._dispatch(message)

    def _dispatch(self, message: dict) -> None:
        method = message.get("method", "")
        if not method:
            return
        handlers = self._handlers.get(method)
        if handlers:
            for handler in list(handlers):
                try:
                    handler(message)
                except Exception:  # 订阅者异常不能拖垮读线程
                    continue
        if method in _NO_CACHE:
            return
        # 缓存给 wait_event 用，避免订阅晚到丢事件
        with self._state_lock:
            bucket = self._events.setdefault(method, [])
            bucket.append(message)
            if len(bucket) > 64:
                del bucket[:-64]

    def _fail_all(self, reason: str) -> None:
        with self._state_lock:
            pending, self._pending = self._pending, {}
            self._closed = True
            if not self._error:
                self._error = reason
        for slot in pending.values():
            slot["error"] = reason
            slot["event"].set()

    # ------------------------------------------------------------- 公开接口
    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """同步调用一个 CDP 方法，返回 result。"""
        if self._closed:
            raise CdpError(f"连接已关闭: {self._error or '未知原因'}")
        with self._state_lock:
            self._next_id += 1
            request_id = self._next_id
            slot: dict[str, Any] = {"event": threading.Event(), "reply": None}
            self._pending[request_id] = slot
        message: dict[str, Any] = {
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            message["sessionId"] = session_id
        try:
            self._send_frame(json.dumps(message).encode())
        except Exception as exc:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise CdpError(f"{method} 发送失败: {exc}") from exc

        wait = self._default_timeout if timeout is None else timeout
        if not slot["event"].wait(wait):
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise CdpError(f"{method} 超时 {wait}s")
        if slot.get("error"):
            raise CdpError(f"{method} 失败: {slot['error']}")
        reply = slot["reply"] or {}
        if "error" in reply:
            detail = reply["error"]
            raise CdpError(f"{method} 报错: {detail.get('message', detail)}")
        return reply.get("result", {})

    def on(self, method: str, handler: Callable[[dict], None]) -> None:
        """订阅一个 CDP 事件。"""
        with self._state_lock:
            self._handlers.setdefault(method, []).append(handler)

    def post(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        """只发送、不等待响应。

        专供**在读线程里**执行的调用（如 `Page.screencastFrameAck`）：
        读线程若在此同步等待响应，谁来置位 Event？会直接死锁。
        """
        if self._closed:
            raise CdpError("连接已关闭")
        with self._state_lock:
            self._next_id += 1
            request_id = self._next_id
        message: dict[str, Any] = {
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            message["sessionId"] = session_id
        self._send_frame(json.dumps(message).encode())

    def off(self, method: str, handler: Callable[[dict], None]) -> None:
        with self._state_lock:
            bucket = self._handlers.get(method)
            if bucket and handler in bucket:
                bucket.remove(handler)

    def wait_event(
        self, method: str, predicate: Callable[[dict], bool] | None = None,
        timeout: float = 10.0,
    ) -> dict | None:
        """等待一个事件（先查缓存，再轮询）。用于 take 后续等场景。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._state_lock:
                bucket = self._events.get(method, [])
                for message in list(bucket):
                    if predicate is None or predicate(message):
                        bucket.remove(message)
                        return message
            time.sleep(0.02)
        return None

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send_frame(b"", opcode=0x8)
        except Exception:
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
        reader = getattr(self, "_reader", None)
        if reader is not None and reader.is_alive():
            reader.join(timeout=timeout)
        self._fail_all("连接已主动关闭")
