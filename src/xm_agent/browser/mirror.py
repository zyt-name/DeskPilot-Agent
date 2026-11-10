"""实时镜像：把页面画面以帧流形式推给界面。

- 订阅 `Page.screencastFrame`，在**读线程**里用 `post` 回 ack（同步调用会死锁）。
- 只保留「最新一帧」，用户看画面不需要历史队列，积压只会让延迟越来越大。
- 面板隐藏时 stop，成本归零；不需要重启页面。
"""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Callable

from .cdp import CdpConnection


class ScreencastStream:
    """一条页面的镜像流。"""

    def __init__(
        self,
        conn: CdpConnection,
        session_id: str,
        on_frame: Callable[[bytes, dict], None] | None = None,
        quality: int = 60,
        max_width: int = 1280,
        fps: int = 15,
    ):
        self.conn = conn
        self.session_id = session_id
        self.on_frame = on_frame
        self.quality = quality
        self.max_width = max_width
        self.fps = max(1, min(60, fps))
        self._latest: bytes | None = None
        self._lock = threading.Lock()
        self._running = False
        self._frames = 0
        self._bytes = 0
        self._last_at = 0.0
        self._handler = self._on_event

    # ---------------------------------------------------------------- 生命周期
    def start(self, prime: bool = True) -> None:
        if self._running:
            return
        self.conn.on("Page.screencastFrame", self._handler)
        every_nth = max(1, round(60 / self.fps))
        self.conn.call(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": self.quality,
                "maxWidth": self.max_width,
                "everyNthFrame": every_nth,
            },
            self.session_id,
        )
        self._running = True
        if prime:
            self._prime()

    def _prime(self) -> None:
        """先塞一张静态截图。

        浏览器只在画面**有变化**时才推帧，页面静止时新建的镜像会一直空白。
        先截一张兜底，用户一展开面板就能看到内容。
        """
        try:
            from .snapshot import capture_screenshot

            data = capture_screenshot(
                self.conn,
                self.session_id,
                quality=self.quality,
                max_width=self.max_width,
            )
        except Exception:
            return
        with self._lock:
            if self._latest is None:
                self._latest = data
                self._last_at = time.time()
        if self.on_frame is not None and data:
            try:
                self.on_frame(data, {"primed": True})
            except Exception:
                pass

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self.conn.call("Page.stopScreencast", {}, self.session_id, timeout=5.0)
        except Exception:
            pass
        self.conn.off("Page.screencastFrame", self._handler)

    # ---------------------------------------------------------------- 帧处理
    def _on_event(self, message: dict) -> None:
        params = message.get("params") or {}
        if params.get("sessionId") is None:
            return
        # 关键：这里在读线程里，必须用 post（只发不等）
        try:
            self.conn.post(
                "Page.screencastFrameAck",
                {"sessionId": params["sessionId"]},
                self.session_id,
            )
        except Exception:
            return
        data = params.get("data") or ""
        try:
            raw = base64.b64decode(data)
        except Exception:
            return
        with self._lock:
            self._latest = raw
            self._frames += 1
            self._bytes += len(raw)
            self._last_at = time.time()
        if self.on_frame is not None:
            try:
                self.on_frame(raw, params.get("metadata") or {})
            except Exception:
                pass

    # ---------------------------------------------------------------- 状态
    @property
    def latest(self) -> bytes | None:
        with self._lock:
            return self._latest

    @property
    def running(self) -> bool:
        return self._running

    def stats(self) -> dict:
        with self._lock:
            return {
                "frames": self._frames,
                "bytes": self._bytes,
                "last_at": self._last_at,
                "running": self._running,
                "fps": self.fps,
            }

    def as_data_url(self) -> str:
        """给前端直接塞进 `<img src>` 的形式。"""
        frame = self.latest
        if not frame:
            return ""
        return "data:image/jpeg;base64," + base64.b64encode(frame).decode()
