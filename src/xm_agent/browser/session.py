"""浏览器会话：一个内核进程 + 一个 profile + 多个标签页。

对外只暴露 `BrowserSession` 与 `Page`，调用方不接触 CDP 细节。
关闭必须优雅：先 `Browser.close`，再等进程退出，最后释放 profile 锁。
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import input as input_ops
from . import snapshot as snap
from .cdp import CdpConnection, CdpError
from .engine import (
    BrowserError,
    BrowserProcess,
    EngineInfo,
    bundled_root_for,
    resolve_engine,
)
from .mirror import ScreencastStream


@dataclass
class BrowserSettings:
    """会话参数，来自 `[runtime.browser]`。"""

    engine: str = "auto"
    profile_root: str = ".xm-agent/browser/profiles"
    headless: str = "auto"  # auto | always | never
    keep_alive_seconds: int = 300
    mirror_fps: int = 15
    screenshot_max_width: int = 1280
    connect_timeout: float = 20.0


class Page:
    """一个标签页。所有坐标都是视口坐标。"""

    def __init__(self, session: BrowserSession, target_id: str, cdp_session: str):
        self._session = session
        self.target_id = target_id
        self.cdp_session = cdp_session
        self._screencast: ScreencastStream | None = None

    # ---------------------------------------------------------------- 基础
    @property
    def conn(self) -> CdpConnection:
        return self._session.conn

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        return self.conn.call(method, params, self.cdp_session, timeout=timeout)

    @property
    def url(self) -> str:
        return str(snap.evaluate(self.conn, self.cdp_session, "location.href") or "")

    @property
    def title(self) -> str:
        return str(snap.evaluate(self.conn, self.cdp_session, "document.title") or "")

    def evaluate(self, expression: str, timeout: float = 20.0) -> Any:
        return snap.evaluate(self.conn, self.cdp_session, expression, timeout=timeout)

    # ---------------------------------------------------------------- 观测
    def screenshot(self, quality: int = 70, max_width: int | None = None,
                   full_page: bool = False) -> bytes:
        return snap.capture_screenshot(
            self.conn,
            self.cdp_session,
            quality=quality,
            max_width=max_width or self._session.settings.screenshot_max_width,
            full_page=full_page,
        )

    def elements(self, limit: int = 80) -> list[dict]:
        return snap.interactive_elements(self.conn, self.cdp_session, limit)

    def meta(self) -> dict:
        return snap.page_meta(self.conn, self.cdp_session)

    def text(self, limit: int = 1500) -> str:
        return snap.visible_text(self.conn, self.cdp_session, limit)

    def ax(self, limit: int = 120) -> list[dict]:
        return snap.ax_summary(self.conn, self.cdp_session, limit)

    def snapshot(self, include_screenshot: bool = False, limit: int = 80) -> dict:
        data = snap.page_snapshot(
            self.conn,
            self.cdp_session,
            include_screenshot=include_screenshot,
            max_width=self._session.settings.screenshot_max_width,
            element_limit=limit,
        )
        data["engine"] = self._session.engine.name
        return data

    # ---------------------------------------------------------------- 动作
    def navigate(self, url: str, timeout: float = 20.0) -> None:
        input_ops.open_url(self.conn, self.cdp_session, url, timeout)

    def click(self, x: float, y: float, button: str = "left", clicks: int = 1) -> None:
        input_ops.click(self.conn, self.cdp_session, x, y, button=button, clicks=clicks)

    def bring_to_front(self) -> None:
        """把可见窗口前置到桌面最上层。

        只在 headful 下有意义；headless 没有窗口可前置，调用会被忽略。
        """
        try:
            self.call("Page.bringToFront", {})
        except CdpError:
            pass

    def click_element(self, index: int, elements: list[dict] | None = None,
                      limit: int = 80) -> dict:
        """按元素序号点击（坐标来自刚取的元素清单，避免模型算错）。"""
        items = elements if elements is not None else self.elements(limit)
        if not 0 <= index < len(items):
            raise BrowserError(f"元素序号 {index} 越界（当前 {len(items)} 个）")
        item = items[index]
        self.click(item["x"], item["y"])
        return item

    def type_text(self, text: str) -> None:
        input_ops.type_text(self.conn, self.cdp_session, text)

    def type_into(self, selector: str, text: str) -> bool:
        return input_ops.type_into(self.conn, self.cdp_session, selector, text)

    def press(self, key: str, modifiers: str = "") -> None:
        input_ops.press_key(self.conn, self.cdp_session, key, modifiers)

    def scroll(self, delta_y: float = 400, x: float | None = None,
               y: float | None = None) -> None:
        if x is None or y is None:
            size = snap.viewport(self.conn, self.cdp_session)
            x = (size["width"] or 800) / 2
            y = (size["height"] or 600) / 2
        input_ops.scroll(self.conn, self.cdp_session, x, y, delta_y)

    def wait_text(self, needle: str, timeout: float = 15.0) -> bool:
        return input_ops.wait_text(self.conn, self.cdp_session, needle, timeout)

    def wait_loaded(self, timeout: float = 20.0) -> bool:
        return input_ops.wait_loaded(self.conn, self.cdp_session, timeout)

    # ---------------------------------------------------------------- 镜像
    def start_mirror(self, on_frame=None, fps: int | None = None,
                     quality: int = 60) -> ScreencastStream:
        if self._screencast is not None and self._screencast.running:
            return self._screencast
        self._screencast = ScreencastStream(
            self.conn,
            self.cdp_session,
            on_frame=on_frame,
            quality=quality,
            max_width=self._session.settings.screenshot_max_width,
            fps=fps or self._session.settings.mirror_fps,
        )
        self._screencast.start()
        return self._screencast

    def stop_mirror(self) -> None:
        if self._screencast is not None:
            self._screencast.stop()
            self._screencast = None

    @property
    def mirror(self) -> ScreencastStream | None:
        return self._screencast

    def frame_data_url(self) -> str:
        if self._screencast is None:
            return ""
        return self._screencast.as_data_url()

    # ---------------------------------------------------------------- 关闭
    def close(self) -> None:
        self.stop_mirror()
        try:
            self.conn.call("Target.closeTarget", {"targetId": self.target_id}, timeout=5.0)
        except Exception:
            pass


@dataclass
class SessionStats:
    pages: int = 0
    engine: str = ""
    version: str = ""
    headless: bool = True
    started_at: float = 0.0
    fallback_note: str = ""
    closed: bool = False


class BrowserSession:
    """浏览器会话门面。非线程安全，由上层串行调用。"""

    def __init__(self, settings: BrowserSettings | None = None, headless: bool | None = None):
        self.settings = settings or BrowserSettings()
        self._headless = headless
        self.engine: EngineInfo | None = None
        self.process: BrowserProcess | None = None
        self.conn: CdpConnection | None = None
        self._pages: dict[str, Page] = {}
        self._started_at = 0.0
        self._closed = False

    # ---------------------------------------------------------------- 生命周期
    @property
    def headless(self) -> bool:
        if self._headless is not None:
            return self._headless
        mode = (self.settings.headless or "auto").lower()
        if mode == "always":
            return True
        if mode == "never":
            return False
        return True  # auto 默认无头；登录需要可见时由 ensure_visible 重开

    @property
    def started(self) -> bool:
        return self.process is not None and self.process.running and not self._closed

    def start(self) -> None:
        if self.started:
            return
        self._closed = False
        profile_root = Path(self.settings.profile_root)
        bundled_root = bundled_root_for(profile_root)
        self.engine = resolve_engine(self.settings.engine, bundled_root)
        # 必须是绝对路径：相对路径下 Chromium 会把 DevToolsActivePort 写到别处，
        # 启动探针永远等不到端口（真机踩过，默认配置就是相对路径）。
        profile_dir = (profile_root / self.engine.name).resolve()
        self.process = BrowserProcess(
            self.engine, profile_dir, headless=self.headless
        )
        ws_url = self.process.start(timeout=self.settings.connect_timeout)
        self.conn = CdpConnection(ws_url, timeout=self.settings.connect_timeout)
        self.conn.call("Target.setDiscoverTargets", {"discover": True})
        self._started_at = time.time()

    def ensure_visible(self) -> bool:
        """登录需要看真窗口时切到可见模式。返回是否发生了重启。"""
        if not self.headless:
            return False
        targets = self.page_urls()
        self._headless = False
        self.close(reason="切换到可见模式")
        self.start()
        note = ""
        if targets:
            # 尽量恢复到原来的目标页，避免用户重新找
            self.new_page(targets[-1])
            note = "已切换到可见窗口"
        self._visible_note = note
        return True

    # ---------------------------------------------------------------- 标签页
    def new_page(self, url: str = "about:blank") -> Page:
        self._ensure_conn()
        assert self.conn is not None
        target = self.conn.call("Target.createTarget", {"url": url})
        target_id = target.get("targetId", "")
        attached = self.conn.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        cdp_session = attached.get("sessionId", "")
        self.conn.call("Page.enable", {}, cdp_session)
        self.conn.call("Runtime.enable", {}, cdp_session)
        self.conn.call("Page.setLifecycleEventsEnabled", {"enabled": True}, cdp_session)
        page = Page(self, target_id, cdp_session)
        self._pages[target_id] = page
        return page

    def open(self, url: str, timeout: float = 20.0) -> Page:
        page = self.new_page("about:blank")
        page.navigate(url, timeout=timeout)
        return page

    def page_urls(self) -> list[str]:
        assert self.conn is not None
        try:
            targets = self.conn.call("Target.getTargets").get("targetInfos", [])
        except CdpError:
            return []
        return [
            str(t.get("url", ""))
            for t in targets
            if t.get("type") == "page" and str(t.get("url", "")).startswith("http")
        ]

    @property
    def pages(self) -> list[Page]:
        return list(self._pages.values())

    def first_page(self) -> Page:
        if self._pages:
            return next(iter(self._pages.values()))
        return self.new_page()

    def close_page(self, page: Page) -> None:
        page.close()
        self._pages.pop(page.target_id, None)

    # ---------------------------------------------------------------- 关闭
    def close(self, reason: str = "用户关闭") -> None:
        """优雅关闭：`Browser.close` → 等进程退出 → 释放锁。"""
        if self._closed:
            return
        for page in list(self._pages.values()):
            page.stop_mirror()
        self._pages.clear()
        if self.conn is not None and not self.conn.closed:
            try:
                self.conn.call("Browser.close", {}, timeout=10.0)
            except Exception:
                pass
            self.conn.close()
        self.conn = None
        if self.process is not None:
            self.process.close(graceful=True, timeout=15.0)
            self.process = None
        self._closed = True

    def kill(self) -> None:
        """强制收尾，只在优雅关闭失败时用（会丢未落盘的登录态）。"""
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        if self.process is not None:
            self.process.kill()
            self.process = None
        self._pages.clear()
        self._closed = True

    def _ensure_conn(self) -> None:
        if self._closed:
            raise BrowserError("会话已关闭")
        if self.conn is None or self.conn.closed:
            self.start()

    # ---------------------------------------------------------------- 状态
    def stats(self) -> SessionStats:
        return SessionStats(
            pages=len(self._pages),
            engine=self.engine.name if self.engine else "",
            version=self.engine.version if self.engine else "",
            headless=self.headless,
            started_at=self._started_at,
            fallback_note=self.engine.note if self.engine else "",
            closed=self._closed,
        )

    def description(self) -> str:
        """给界面/日志用的一句话。"""
        if not self.engine:
            return "浏览器未启动"
        mode = "无头" if self.headless else "可见窗口"
        note = f"（{self.engine.note}）" if self.engine.note else ""
        return f"{self.engine.label} {self.engine.version} · {mode}{note}"

    def screenshot_data_url(self, quality: int = 60) -> str:
        page = self.first_page()
        data = page.screenshot(quality=quality)
        return "data:image/jpeg;base64," + base64.b64encode(data).decode()
