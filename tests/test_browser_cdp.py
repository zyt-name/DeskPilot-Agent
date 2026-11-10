"""CDP 连接的纯单元测试（不需要真浏览器）。

核心回归：**空闲不是错误**。socket 上的读超时曾经被读线程当成致命错误，
用户想几秒问题，连接就被判死，之后每次调用都报「连接已关闭」，
整个任务每况愈下（真机现场见 TECH_DESIGN 4.21.16）。
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from xm_agent.browser.cdp import CdpConnection


class _FlakySock:
    """按脚本吐出字节或抛异常；脚本用完就抛 SystemExit（只停读线程）。"""

    def __init__(self, chunks: list[object]) -> None:
        self.chunks = list(chunks)
        self.sent = bytearray()

    def recv(self, size: int) -> bytes:
        if not self.chunks:
            raise SystemExit
        item = self.chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[return-value]

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, value: float) -> None:
        pass


def _bare_conn(chunks: list[object]) -> CdpConnection:
    """绕过握手，直接造一个「已连上」的连接对象。"""
    conn = object.__new__(CdpConnection)
    conn._sock = _FlakySock(chunks)
    conn._buf = b""
    conn._frag = bytearray()
    conn._frag_opcode = 0
    conn._write_lock = threading.Lock()
    conn._state_lock = threading.Lock()
    conn._pending = {}
    conn._handlers = {}
    conn._events = {}
    conn._next_id = 0
    conn._closed = False
    conn._error = ""
    conn._default_timeout = 5.0
    return conn


def _frame(payload: bytes, opcode: int = 0x1) -> bytes:
    assert len(payload) < 126
    return bytes([0x80 | opcode, len(payload)]) + payload


def test_read_exact_waits_through_idle_timeouts():
    conn = _bare_conn([socket.timeout("timed out"), socket.timeout("timed out"), b"ab"])

    assert conn._read_exact(2) == b"ab"


def test_read_frame_keeps_partial_header_across_timeout():
    body = b"abc"
    conn = _bare_conn([b"\x81", socket.timeout("timed out"), b"\x03" + body])

    fin, opcode, data = conn._read_frame()

    assert fin is True
    assert opcode == 0x1
    assert data == body


def test_read_loop_survives_idle_timeout_and_still_answers():
    """读线程在超时之后仍要处理后续消息，不能把连接判死。"""
    conn = _bare_conn(
        [
            socket.timeout("timed out"),
            _frame(json.dumps({"id": 1, "result": {"ok": 1}}).encode()),
        ]
    )
    slot = {"event": threading.Event(), "reply": None}
    conn._pending[1] = slot

    # 假 socket 的脚本用完后抛 SystemExit，正好只停循环、不触发 _fail_all
    with pytest.raises(SystemExit):
        conn._read_loop()

    assert slot["event"].is_set() is True
    assert slot["reply"] == {"id": 1, "result": {"ok": 1}}
    assert conn.closed is False
    assert conn._error == ""


def test_closed_connection_reports_reason():
    conn = _bare_conn([socket.timeout("timed out")])
    conn._fail_all("读线程退出: timed out")

    assert conn.closed is True
    try:
        conn.call("Page.enable")
    except Exception as exc:  # noqa: BLE001 - 只关心报错文案
        assert "连接已关闭" in str(exc)
    else:  # pragma: no cover - 不可达
        raise AssertionError("已关闭的连接不该允许调用")
