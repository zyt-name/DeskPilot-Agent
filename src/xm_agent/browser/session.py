"""浏览器会话：一个内核进程 + 一个 profile + 多个标签页。

对外只暴露 `BrowserSession` 与 `Page`，调用方不接触 CDP 细节。
关闭必须优雅：先 `Browser.close`，再等进程退出，最后释放 profile 锁。
"""

from __future__ import annotations

import base64
import threading
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
from .stealth import STEALTH_JS


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
    # 空串 = 按内核自动生成（无头模式下替换掉 HeadlessChrome）
    user_agent: str = ""
    # 抹掉 navigator.webdriver 等自动化特征，否则很多站点直接拦截
    hide_automation: bool = True


class _PageLink:
    """把一个标签页包成「像 CdpConnection 一样」的对象。

    `snapshot` / `input` 只用到 `call(method, params, session_id, timeout)`，
    传这个代理进去，它们就顺带获得了断线重连与 session 失效重挂的能力，
    不用在每个辅助模块里各写一遍重试。
    """

    __slots__ = ("_page",)

    def __init__(self, page: "Page") -> None:
        self._page = page

    def call(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        return self._page.call(method, params or {}, timeout=timeout or 20.0)


# CDP 在「浏览器重启过 / 页面被重新 attach」后的典型报错
_STALE_SESSION = (
    "Session with given id not found",
    "No session with given id",
    "Session closed",
    "Not attached to an active page",
)

# 导航过程中会短暂出现的错误：目标正在换文档，重试一次就好
_TRANSIENT = (
    "Inspected target navigated or closed",
    "Target closed",
)


class Page:
    """一个标签页。所有坐标都是视口坐标。"""

    def __init__(self, session: BrowserSession, target_id: str, cdp_session: str):
        self._session = session
        self.target_id = target_id
        self.cdp_session = cdp_session
        self._screencast: ScreencastStream | None = None
        self._link = _PageLink(self)

    # ---------------------------------------------------------------- 基础
    @property
    def conn(self) -> CdpConnection:
        """拿一条活着的连接（镜像与关闭需要真的连接对象）。

        断了就先重连，内核没了就重启——所以断线自愈对所有调用点都生效。
        """
        return self._session.live_connection()

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        conn = self._session.live_connection()
        try:
            return conn.call(method, params, self.cdp_session, timeout=timeout)
        except CdpError as exc:
            detail = str(exc)
            if "连接已关闭" in detail:
                # 连接断了（socket 被回收、内核重启）：重连一次再试。
                # 不重试的话一次抖动就让整个任务后半段全部失败。
                conn = self._session.live_connection()
                return conn.call(method, params, self.cdp_session, timeout=timeout)
            if any(mark in detail for mark in _TRANSIENT):
                # 文档正在换：CDP 会临时拒绝求值。等一下再来，不是错误。
                time.sleep(0.15)
                return conn.call(method, params, self.cdp_session, timeout=timeout)
            if any(mark in detail for mark in _STALE_SESSION):
                # 换进程导航（跨站跳转、退回 about:blank）会让旧 session 失效，
                # 稍等一下再挂通常就好了；连续两次不行才认定页面真的没了。
                for _ in range(2):
                    if self.reattach():
                        conn = self._session.live_connection()
                        return conn.call(method, params, self.cdp_session, timeout=timeout)
                    time.sleep(0.2)
                raise BrowserError("页面已失效（无法重新挂上标签页）") from None
            raise

    def reattach(self) -> bool:
        """用 target_id 重新 attach 一次，换一个有效的 session id。"""
        conn = self._session.live_connection()
        try:
            attached = conn.call(
                "Target.attachToTarget",
                {"targetId": self.target_id, "flatten": True},
            )
            self.cdp_session = str(attached.get("sessionId") or "")
            conn.call("Page.enable", {}, self.cdp_session)
            conn.call("Runtime.enable", {}, self.cdp_session)
            conn.call(
                "Page.setLifecycleEventsEnabled", {"enabled": True}, self.cdp_session
            )
        except CdpError:
            return False
        self._session._pages[self.target_id] = self
        # 新 session 不继承旧 session 注册过的脚本，得重新挂一遍
        self.harden()
        return True

    @property
    def url(self) -> str:
        return str(snap.evaluate(self._link, self.cdp_session, "location.href") or "")

    @property
    def title(self) -> str:
        return str(snap.evaluate(self._link, self.cdp_session, "document.title") or "")

    def evaluate(self, expression: str, timeout: float = 20.0) -> Any:
        return snap.evaluate(self._link, self.cdp_session, expression, timeout=timeout)

    # ---------------------------------------------------------------- 观测
    def screenshot(self, quality: int = 70, max_width: int | None = None,
                   full_page: bool = False) -> bytes:
        return snap.capture_screenshot(
            self._link,
            self.cdp_session,
            quality=quality,
            max_width=max_width or self._session.settings.screenshot_max_width,
            full_page=full_page,
        )

    def elements(self, limit: int = 80) -> list[dict]:
        return snap.interactive_elements(self._link, self.cdp_session, limit)

    def meta(self) -> dict:
        return snap.page_meta(self._link, self.cdp_session)

    def text(self, limit: int = 1500) -> str:
        return snap.visible_text(self._link, self.cdp_session, limit)

    def ax(self, limit: int = 120) -> list[dict]:
        return snap.ax_summary(self._link, self.cdp_session, limit)

    def snapshot(self, include_screenshot: bool = False, limit: int = 80) -> dict:
        data = snap.page_snapshot(
            self._link,
            self.cdp_session,
            include_screenshot=include_screenshot,
            max_width=self._session.settings.screenshot_max_width,
            element_limit=limit,
        )
        data["engine"] = self._session.engine.name
        return data

    # ---------------------------------------------------------------- 动作
    def navigate(self, url: str, timeout: float = 20.0) -> None:
        input_ops.open_url(self._link, self.cdp_session, url, timeout)

    def reload(self, ignore_cache: bool = False, timeout: float = 20.0) -> bool:
        """重新加载当前页。

        走 `Page.reload` 而不是 `location.reload()`：后者会让当前文档的执行上下文
        立即销毁，`Runtime.evaluate` 的返回值还没回到 CDP 就没了，真机上表现为
        「页面求值失败」。另外 `history.reload` 这个方法根本不存在——模型按直觉
        调过一次，直接报 `TypeError: history.reload is not a function`。
        """
        self.call("Page.reload", {"ignoreCache": bool(ignore_cache)},
                  timeout=timeout + 10)
        return self.wait_loaded(timeout)

    def go(self, delta: int, timeout: float = 20.0) -> bool:
        """在会话历史里前后移动；越界返回 False（不当成错误）。"""
        history = self.call("Page.getNavigationHistory")
        entries = history.get("entries") or []
        index = int(history.get("currentIndex") or 0) + int(delta)
        if not 0 <= index < len(entries):
            return False
        self.call(
            "Page.navigateToHistoryEntry",
            {"entryId": entries[index].get("id")},
            timeout=timeout + 10,
        )
        self.wait_loaded(timeout)
        return True

    def back(self, timeout: float = 20.0) -> bool:
        return self.go(-1, timeout)

    def forward(self, timeout: float = 20.0) -> bool:
        return self.go(1, timeout)

    def harden(self) -> None:
        """注册反自动化指纹脚本：对后续每个文档生效（含刷新、跳转、新开页）。"""
        if not self._session.settings.hide_automation:
            return
        try:
            self.call("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
            # 当前文档已经加载过了，补一次立即生效，否则要等下一次导航
            self.call(
                "Runtime.evaluate",
                {"expression": STEALTH_JS, "returnByValue": True},
            )
        except (CdpError, BrowserError):
            # 伪装失败不该让页面用不了：宁可被拦，也不能崩
            pass

    def click(self, x: float, y: float, button: str = "left", clicks: int = 1) -> None:
        input_ops.click(self._link, self.cdp_session, x, y, button=button, clicks=clicks)

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
        input_ops.type_text(self._link, self.cdp_session, text)

    def type_into(self, selector: str, text: str) -> bool:
        return input_ops.type_into(self._link, self.cdp_session, selector, text)

    def press(self, key: str, modifiers: str = "") -> None:
        input_ops.press_key(self._link, self.cdp_session, key, modifiers)

    def scroll(self, delta_y: float = 400, x: float | None = None,
               y: float | None = None) -> None:
        if x is None or y is None:
            size = snap.viewport(self._link, self.cdp_session)
            x = (size["width"] or 800) / 2
            y = (size["height"] or 600) / 2
        input_ops.scroll(self._link, self.cdp_session, x, y, delta_y)

    def wait_text(self, needle: str, timeout: float = 15.0) -> bool:
        return input_ops.wait_text(self._link, self.cdp_session, needle, timeout)

    def wait_loaded(self, timeout: float = 20.0) -> bool:
        return input_ops.wait_loaded(self._link, self.cdp_session, timeout)

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
        self._conn_lock = threading.RLock()

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
        with self._conn_lock:
            self._start_locked()

    def _start_locked(self) -> None:
        if self.started:
            if self.conn is not None and not self.conn.closed:
                return
            # 内核还活着，只是 CDP 连接断了：先原地重连，比重启便宜得多，
            # 也不会丢掉已经打开、已经登录的标签页。
            if self.reconnect_locked():
                return
            if self.process is not None:
                self.process.kill()
                self.process = None
        self._closed = False
        profile_root = Path(self.settings.profile_root)
        bundled_root = bundled_root_for(profile_root)
        self.engine = resolve_engine(self.settings.engine, bundled_root)
        # 必须是绝对路径：相对路径下 Chromium 会把 DevToolsActivePort 写到别处，
        # 启动探针永远等不到端口（真机踩过，默认配置就是相对路径）。
        profile_dir = (profile_root / self.engine.name).resolve()
        self.process = BrowserProcess(
            self.engine,
            profile_dir,
            headless=self.headless,
            user_agent=self.settings.user_agent,
            hide_automation=self.settings.hide_automation,
        )
        ws_url = self.process.start(timeout=self.settings.connect_timeout)
        self.conn = CdpConnection(ws_url, timeout=self.settings.connect_timeout)
        self.conn.call("Target.setDiscoverTargets", {"discover": True})
        # 新进程里旧的 target id 全部失效，页面对象不能再复用
        self._pages.clear()
        self._started_at = time.time()

    def live_connection(self) -> CdpConnection:
        """返回一条可用的 CDP 连接；断了就重连，内核没了就整套重启。"""
        with self._conn_lock:
            conn = self.conn
            if conn is not None and not conn.closed:
                return conn
            if self._closed:
                raise BrowserError("会话已关闭")
            if self.reconnect_locked():
                assert self.conn is not None
                return self.conn
            self._start_locked()
            assert self.conn is not None
            return self.conn

    def reconnect_locked(self) -> bool:
        """用同一个内核重建 CDP 连接，并把已有标签页接回去。"""
        process = self.process
        if process is None or not process.running or not process.ws_url:
            return False
        old = self.conn
        # 先停镜像：再往死连接上写帧没有任何意义；重连后按原参数拉起
        mirrors: dict[str, tuple] = {}
        for target_id, page in list(self._pages.items()):
            stream = page.mirror
            if stream is not None:
                mirrors[target_id] = (stream.on_frame, stream.fps)
            page.stop_mirror()
        try:
            conn = CdpConnection(process.ws_url, timeout=self.settings.connect_timeout)
            conn.call("Target.setDiscoverTargets", {"discover": True})
        except CdpError:
            return False
        if old is not None and old is not conn:
            try:
                old.close()
            except Exception:  # noqa: BLE001 - 旧连接收尾失败不影响新连接
                pass
        self.conn = conn
        self._reattach_locked()
        for target_id, (on_frame, fps) in mirrors.items():
            page = self._pages.get(target_id)
            if page is None:
                continue
            try:
                page.start_mirror(on_frame=on_frame, fps=fps)
            except Exception:  # noqa: BLE001 - 镜像起不来不该影响调用
                pass
        return True

    def _reattach_locked(self) -> None:
        """重连后把已知标签页重新 attach；接不上的丢掉，下次观测会新开。"""
        conn = self.conn
        if conn is None:
            return
        for target_id, page in list(self._pages.items()):
            try:
                attached = conn.call(
                    "Target.attachToTarget",
                    {"targetId": target_id, "flatten": True},
                )
                page.cdp_session = str(attached.get("sessionId") or "")
                conn.call("Page.enable", {}, page.cdp_session)
                conn.call("Runtime.enable", {}, page.cdp_session)
                conn.call(
                    "Page.setLifecycleEventsEnabled",
                    {"enabled": True},
                    page.cdp_session,
                )
                page.harden()
            except CdpError:
                self._pages.pop(target_id, None)

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
        conn = self.live_connection()
        # 先开空白页再导航：`Target.createTarget` 直接带 URL 的话，页面在我们
        # 注入反自动化脚本之前就已经开始加载了，第一跳仍然暴露无头特征。
        target = conn.call("Target.createTarget", {"url": "about:blank"})
        target_id = target.get("targetId", "")
        attached = conn.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        cdp_session = attached.get("sessionId", "")
        conn.call("Page.enable", {}, cdp_session)
        conn.call("Runtime.enable", {}, cdp_session)
        conn.call("Page.setLifecycleEventsEnabled", {"enabled": True}, cdp_session)
        page = Page(self, target_id, cdp_session)
        self._pages[target_id] = page
        page.harden()
        if url and url != "about:blank":
            page.navigate(url)
        return page

    def open(self, url: str, timeout: float = 20.0) -> Page:
        page = self.new_page("about:blank")
        page.navigate(url, timeout=timeout)
        return page

    def page_urls(self) -> list[str]:
        try:
            conn = self.live_connection()
            targets = conn.call("Target.getTargets").get("targetInfos", [])
        except (BrowserError, CdpError):
            return []
        return [
            str(t.get("url", ""))
            for t in targets
            if t.get("type") == "page" and str(t.get("url", "")).startswith("http")
        ]

    @property
    def pages(self) -> list[Page]:
        return list(self._pages.values())

    def owns(self, page: Page) -> bool:
        """这个页面对象还属于当前会话吗（内核重启后旧对象就失效了）。"""
        return self._pages.get(page.target_id) is page

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
        with self._conn_lock:
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
        with self._conn_lock:
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
        self.live_connection()

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
