"""浏览器通道：把 BrowserSession 适配成 Runtime 能路由的能力宿主。

对外只有五个能力：open / observe / act / verify / session。模型看到的东西
（元素清单、可见文本、截图）与桌面通道保持同一种形状，于是 Runtime 的
决策、审计、校验逻辑不用为浏览器写第二套。

启动是懒的：只有真正调用 open/observe 才拉起内核进程，任务没用到浏览器
就不会多一个进程、多占一份内存。
"""

from __future__ import annotations

import base64
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..channels import BROWSER_KIND
from ..types import Element, Observation, Receipt, Verification
from .engine import BrowserError
from .session import BrowserSession, BrowserSettings, Page

FrameCallback = Callable[[str, dict[str, Any]], None]

CAPABILITIES = frozenset(
    {
        "browser.open",
        "browser.observe",
        "browser.act",
        "browser.verify",
        "browser.session",
    }
)

_LOGIN_URL_HINTS = (
    "login",
    "signin",
    "sign_in",
    "passport",
    "sso",
    "auth",
    "oauth",
    "account",
)
_LOGIN_TEXT_HINTS = (
    "扫码登录",
    "密码登录",
    "登录后",
    "请登录",
    "请先登录",
    "sign in to continue",
    "log in to continue",
    "login required",
)
_PASSWORD_ROLES = ("password",)

# 站点安全防护（WAF）拦截页的特征。真机：自如的 EdgeOne 会返回「请求已被拦截」，
# 页面几乎没内容、也没有可交互元素——和「页面还没渲染完」长得一样，
# 但重试同一地址永远过不去，得让模型知道换条路，而不是反复刷新。
_BLOCK_HINTS = (
    "请求已被拦截",
    "安全策略拦截",
    "已启用安全防护",
    "edgeone 提供防护",
    "访问被拒绝",
    "access denied",
    "attention required",
    "unusual traffic",
    "验证您不是机器人",
)


def _as_index(value: Any) -> int:
    """把 b3 / 3 / "3" 统一成整数序号。"""
    text = str(value if value is not None else "").strip().lower()
    if text.startswith("b"):
        text = text[1:]
    try:
        return int(text)
    except (TypeError, ValueError):
        raise ValueError(f"无法识别的元素序号: {value!r}") from None


class BrowserChannel:
    """浏览器能力的唯一入口。"""

    name = "browser"
    kind = BROWSER_KIND
    capabilities = CAPABILITIES
    vision_only = False

    def __init__(
        self,
        settings: BrowserSettings | None = None,
        *,
        screenshot_dir: str = ".xm-agent/screenshots",
        headless: bool | None = None,
        keep_alive_seconds: int | None = None,
        session: BrowserSession | None = None,
        page: Page | None = None,
    ) -> None:
        self.settings = settings or BrowserSettings()
        self.screenshot_dir = screenshot_dir
        self._headless_override = headless
        self._session: BrowserSession | None = session
        self._page: Page | None = page
        self._last_used = 0.0
        self.keep_alive_seconds = (
            self.settings.keep_alive_seconds
            if keep_alive_seconds is None
            else int(keep_alive_seconds)
        )
        self._frame_lock = threading.Lock()
        self._frame_seq = 0
        self._frame_subscribers: dict[int, FrameCallback] = {}
        self._frames_sent = 0

    # ------------------------------------------------------------ 镜像订阅
    def subscribe(self, callback: FrameCallback) -> int:
        """订阅画面帧。回调收到 (data_url, meta)，在 CDP 读线程里调用。

        面板打开才订阅，关掉就退订并停流——镜像不该在没人看的时候烧 CPU。
        """
        with self._frame_lock:
            self._frame_seq += 1
            sid = self._frame_seq
            self._frame_subscribers[sid] = callback
        self._sync_mirror()
        return sid

    def unsubscribe(self, subscriber_id: int) -> None:
        with self._frame_lock:
            self._frame_subscribers.pop(subscriber_id, None)
            idle = not self._frame_subscribers
        if idle:
            self._stop_mirror()

    @property
    def mirror_active(self) -> bool:
        with self._frame_lock:
            subscribed = bool(self._frame_subscribers)
        return subscribed and self.started

    @property
    def frames_sent(self) -> int:
        return self._frames_sent

    def _sync_mirror(self) -> None:
        with self._frame_lock:
            wanted = bool(self._frame_subscribers)
        if not wanted or not self.started:
            return
        try:
            self.page().start_mirror(
                on_frame=self._on_frame,
                fps=self.settings.mirror_fps,
                quality=55,
            )
        except Exception:  # noqa: BLE001 - 镜像失败不该影响任务
            pass

    def _stop_mirror(self) -> None:
        page = self._page
        if page is None:
            return
        try:
            page.stop_mirror()
        except Exception:  # noqa: BLE001
            pass

    def _on_frame(self, data: bytes, meta: dict[str, Any]) -> None:
        if not data:
            return
        try:
            url = "data:image/jpeg;base64," + base64.b64encode(data).decode()
        except Exception:  # noqa: BLE001
            return
        with self._frame_lock:
            subscribers = list(self._frame_subscribers.values())
        self._frames_sent += 1
        for callback in subscribers:
            try:
                callback(url, meta or {})
            except Exception:  # noqa: BLE001 - 单个订阅者出错不影响其他
                pass

    # ------------------------------------------------------------ 会话
    def session(self) -> BrowserSession:
        if self._session is None or getattr(self._session, "_closed", False):
            self._session = BrowserSession(
                self.settings, headless=self._headless_override
            )
        return self._session

    def page(self) -> Page:
        session = self.session()
        owns = getattr(session, "owns", None)
        if self._page is not None and callable(owns) and not owns(self._page):
            # 内核重启过，旧页面对象已经失效；重新取一个，否则后面每次
            # 调用都会报「Session with given id not found」。
            self._page = None
        if self._page is not None and getattr(self._page, "dead", False):
            # 用户在他那边的窗口里把标签页关了：换一个，别对着空气操作
            self._page = None
        if self._page is None:
            first = getattr(session, "first_page", None)
            self._page = first() if callable(first) else session.new_page()
        return self._page

    @property
    def started(self) -> bool:
        return self._session is not None and self._session.started

    def available(self) -> bool:
        """已启动的通道才能被隐式观测；未启动时由 Hub 回落到桌面。"""
        return self.started

    def activate(self) -> None:
        """唤醒内核并确保有一个标签页。

        只有「没有别的通道可用」时 Hub 才会调用它——正常混合模式下，
        没 open 过的浏览器不该被一次观测顺手拉起来。
        """
        self.page()

    def status(self) -> dict[str, Any]:
        if not self.started:
            return {"started": False, "description": "浏览器未启动"}
        assert self._session is not None
        stats = self._session.stats()
        page = self._page
        # 面板每 2.5s 轮询一次：这里任何异常都会让「浏览器」按钮整块消失，
        # 所以取不到的字段宁可空着，也不能把状态接口打成 500。
        url = title = ""
        if page is not None:
            try:
                url = page.url
            except Exception:  # noqa: BLE001
                url = ""
            try:
                title = page.title
            except Exception:  # noqa: BLE001
                title = ""
        return {
            "started": True,
            "description": self._session.description(),
            "engine": stats.engine,
            "version": stats.version,
            "headless": stats.headless,
            "pages": stats.pages,
            "url": url,
            "title": title,
            "last_used": round(time.time() - self._last_used, 1)
            if self._last_used
            else None,
            "mirror": self.mirror_active,
            "frames": self._frames_sent,
        }

    # ------------------------------------------------------------ 观测
    def observe(
        self, step: int, include_screenshot: bool = False
    ) -> Observation:
        try:
            return self._observe(step, include_screenshot)
        except BrowserError:
            # 页面对象随内核重启失效：换一个页面再来一次，别把任务判死
            self._page = None
            return self._observe(step, include_screenshot)

    def _observe(
        self, step: int, include_screenshot: bool = False
    ) -> Observation:
        page = self.page()
        self._last_used = time.time()
        snap = page.snapshot(include_screenshot=include_screenshot)
        meta = snap.get("meta") or {}
        items = snap.get("elements") or []
        text = str(snap.get("text") or "")

        elements = [
            Element(
                id=f"b{item.get('i', index)}",
                role=str(item.get("role") or item.get("tag") or ""),
                name=str(item.get("text") or "")[:80],
                enabled=not bool(item.get("disabled")),
                bounds=(
                    int(item.get("x", 0)) - int(item.get("w", 0)) // 2,
                    int(item.get("y", 0)) - int(item.get("h", 0)) // 2,
                    int(item.get("w", 0)),
                    int(item.get("h", 0)),
                ),
            )
            for index, item in enumerate(items)
        ]

        url = str(meta.get("url") or "")
        title = str(meta.get("title") or "")
        screenshot_path = None
        if include_screenshot and snap.get("_screenshot"):
            screenshot_path = self._save_shot(step, snap["_screenshot"])

        login = self._login_wall(url, text, items)
        blocked = self._blocked_page(text, items)
        summary = f"浏览器页面「{title or '无标题'}」{url}，{len(elements)} 个可交互元素"
        if login:
            summary += "（检测到登录/扫码页）"
        if blocked:
            summary += (
                "（这是站点安全防护的拦截页，不是内容页：重复请求同一地址"
                "或刷新都不会放行，请换站内其它入口/换搜索引擎结果，"
                "或告知用户该站点拦截了自动访问）"
            )

        card = self._screen_card(title, url, text, elements)
        return Observation(
            step=step,
            source=BROWSER_KIND,
            summary=summary,
            elements=elements,
            active_window=title or url or None,
            screenshot_path=screenshot_path,
            screen_card=card,
            raw={
                "channel": BROWSER_KIND,
                "url": url,
                "title": title,
                "ready_state": meta.get("readyState"),
                "scroll_y": meta.get("scrollY"),
                "scroll_height": meta.get("scrollHeight"),
                "engine": snap.get("engine"),
                "login_wall": login,
                "blocked": blocked,
                "element_count": len(items),
            },
        )

    @staticmethod
    def _screen_card(
        title: str, url: str, text: str, elements: list[Element]
    ) -> str:
        lines = [f"页面：{title}", f"地址：{url}"]
        if text:
            lines.append(f"正文：{text[:800]}")
        if elements:
            listed = "；".join(
                f"[{element.id}]{element.name}" for element in elements[:24]
            )
            lines.append(f"可交互元素：{listed}")
        return "\n".join(lines)

    @staticmethod
    def _login_wall(url: str, text: str, items: list[dict[str, Any]]) -> bool:
        lowered = (url or "").lower()
        has_pwd = any(
            str(item.get("type", "")).lower() in _PASSWORD_ROLES for item in items
        )
        if not has_pwd and not any(hint in lowered for hint in _LOGIN_URL_HINTS):
            return False
        body = (text or "").lower()
        return has_pwd or any(hint in body for hint in _LOGIN_TEXT_HINTS)

    @staticmethod
    def _blocked_page(text: str, items: list[dict[str, Any]]) -> bool:
        """是不是 WAF 的拦截页。

        两个条件同时成立才算：几乎没有可交互元素（真内容页不会这样），
        且正文里出现拦截特征词。只看文本会误判——正常页面的帮助中心里
        也可能写到「人机验证」。
        """
        if len(items) > 3:
            return False
        body = (text or "")[:1200].lower()
        return any(hint in body for hint in _BLOCK_HINTS)

    def _save_shot(self, step: int, data: bytes) -> str:
        root = Path(self.screenshot_dir)
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"browser-step-{step}-{int(time.time() * 1000)}.png"
        path.write_bytes(data)
        return str(path)

    def capture_screenshot(self) -> str | None:
        """审计用截图：当前页面，不启动新会话。"""
        if not self.started:
            return None
        try:
            data = self.page().screenshot(quality=60)
        except Exception:  # noqa: BLE001 - 审计截图失败不该中断任务
            return None
        return self._save_shot(0, data) if data else None

    # ------------------------------------------------------------ 执行
    def execute(self, capability: str, args: dict[str, Any]) -> Receipt:
        try:
            return self._execute(capability, args)
        except BrowserError:
            # 页面失效（内核重启过）：放下旧页面重来一次
            self._page = None
            try:
                return self._execute(capability, args)
            except Exception as exc:  # noqa: BLE001
                return Receipt(False, error=f"{exc.__class__.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - 统一转成回执，不抛给循环
            return Receipt(False, error=f"{exc.__class__.__name__}: {exc}")

    def _execute(self, capability: str, args: dict[str, Any]) -> Receipt:
        if capability == "browser.open":
            return self._open(args)
        if capability == "browser.observe":
            observation = self.observe(
                0, include_screenshot=bool(args.get("include_screenshot"))
            )
            return Receipt(True, observation.to_dict())
        if capability == "browser.act":
            return self._act(args)
        if capability == "browser.verify":
            verification = self.verify(args)
            return Receipt(
                verification.ok, verification.to_dict(), verification.detail
            )
        if capability == "browser.session":
            return self._session_op(args)
        return Receipt(False, error=f"浏览器通道不支持 {capability}")

    def _open(self, args: dict[str, Any]) -> Receipt:
        url = str(args.get("url") or "about:blank").strip()
        session = self.session()
        if args.get("new_tab"):
            self._page = session.new_page(url)
        else:
            # 不带 new_tab 就复用当前页：启动时会接管内核自带的那一页，
            # 不再多开一个永远空着的标签
            self._page = self.page()
            self._page.navigate(url)
        session = self.session()
        if not session.headless:
            session.ensure_visible()
        if args.get("wait_loaded", True):
            self.page().wait_loaded(timeout=float(args.get("timeout_s") or 20))
        self._last_used = time.time()
        self._sync_mirror()
        page = self.page()
        return Receipt(
            True,
            {
                "url": page.url,
                "title": page.title,
                "engine": session.description(),
                "headless": session.headless,
            },
        )

    def _act(self, args: dict[str, Any]) -> Receipt:
        action = str(args.get("action") or "").strip().lower()
        page = self.page()
        self._last_used = time.time()
        detail: dict[str, Any] = {"action": action}

        if action in {"goto", "navigate", "open"}:
            url = str(args.get("url") or "").strip()
            if not url:
                return Receipt(False, error="goto 需要 url")
            page.navigate(url, timeout=float(args.get("timeout_s") or 20))
            detail["url"] = page.url
        elif action in {"click", "tap"}:
            target: dict[str, Any] = {}
            index = args.get("element_index", args.get("element_id"))
            if index is not None and index != "":
                item = page.click_element(_as_index(index), limit=120)
                target = {
                    "element": f"b{item.get('i')}",
                    "text": item.get("text"),
                }
            elif args.get("x") is not None and args.get("y") is not None:
                page.click(float(args["x"]), float(args["y"]))
                target = {"x": args["x"], "y": args["y"]}
            elif args.get("selector"):
                ok = page.evaluate(
                    """((sel) => {const el=document.querySelector(sel);
                    if(!el) return false; el.click(); return true;})"""
                    f"({args['selector']!r})"
                )
                if not ok:
                    return Receipt(False, error=f"选择器未命中: {args['selector']}")
                target = {"selector": args["selector"]}
            else:
                return Receipt(
                    False, error="click 需要 element_index、坐标或 selector"
                )
            detail.update(target)
            if args.get("wait_loaded"):
                page.wait_loaded(timeout=float(args.get("timeout_s") or 15))
        elif action in {"type", "fill", "input"}:
            text = str(args.get("text") or "")
            if not text:
                return Receipt(False, error="type 需要 text")
            index = args.get("element_index", args.get("element_id"))
            if index is not None and index != "":
                item = page.click_element(_as_index(index), limit=120)
                detail["element"] = f"b{item.get('i')}"
            if args.get("selector"):
                if not page.type_into(str(args["selector"]), text):
                    return Receipt(False, error=f"选择器未命中: {args['selector']}")
            else:
                page.type_text(text)
            detail["chars"] = len(text)
            if args.get("press_enter"):
                page.press("Enter")
        elif action in {"press", "key"}:
            keys = str(args.get("keys") or args.get("text") or "").strip()
            if not keys:
                return Receipt(False, error="press 需要 keys")
            page.press(keys)
            detail["keys"] = keys
        elif action in {"scroll", "wheel"}:
            amount = int(args.get("amount") or 500)
            page.scroll(
                float(amount),
                x=args.get("x"),
                y=args.get("y"),
            )
            detail["amount"] = amount
        elif action in {"back", "forward", "reload"}:
            timeout = float(args.get("timeout_s") or args.get("timeout") or 15)
            if action == "reload":
                page.reload(
                    ignore_cache=bool(args.get("ignore_cache")
                                      or args.get("hard")),
                    timeout=timeout,
                )
            else:
                moved = (
                    page.back(timeout=timeout)
                    if action == "back"
                    else page.forward(timeout=timeout)
                )
                if not moved:
                    edge = "最前" if action == "back" else "最后"
                    return Receipt(
                        False,
                        detail,
                        error=f"已经是历史里的{edge}一页，无法{action}",
                    )
            time.sleep(float(args.get("delay_s") or 0.2))
            detail["url"] = page.url
            detail["title"] = page.title
        elif action in {"wait", "wait_text"}:
            needle = str(args.get("text") or "")
            timeout = float(args.get("timeout_s") or 15)
            if needle:
                found = page.wait_text(needle, timeout=timeout)
                detail["found"] = found
                if not found:
                    return Receipt(
                        False, detail, error=f"等待超时：页面上没出现“{needle}”"
                    )
            else:
                time.sleep(min(timeout, 10))
        else:
            return Receipt(False, error=f"未支持的浏览器动作: {action or '(空)'}")

        if args.get("include_screenshot"):
            data = page.screenshot(quality=65)
            detail["screenshot_path"] = self._save_shot(0, data)
        return Receipt(True, detail)

    def _session_op(self, args: dict[str, Any]) -> Receipt:
        action = str(args.get("action") or "status").strip().lower()
        if action == "status":
            return Receipt(True, self.status())
        if action in {"handoff", "show"}:
            return self.handoff()
        if action in {"close", "quit"}:
            self.close()
            return Receipt(True, {"closed": True})
        if action == "retire":
            closed = self.retire_if_idle(force=True)
            return Receipt(True, {"closed": closed})
        return Receipt(False, error=f"未支持的会话动作: {action}")

    def handoff(self) -> Receipt:
        """把浏览器显示到用户桌面——扫码登录时需要人眼看窗口。

        无头模式没法「显示」，只能带着同一个 profile 重开成可见窗口：
        优雅关闭保证 Cookie 落盘，重开后已登录的站点仍是登录态。
        """
        if not self.started or self._session is None:
            return Receipt(False, error="浏览器尚未启动，无需接管")
        session = self._session
        restarted = False
        if session.headless:
            self._page = None
            restarted = bool(session.ensure_visible())
            self._page = session.first_page()
            note = "已切换到可见窗口"
        else:
            self.page().bring_to_front()
            note = "浏览器窗口已前置"
        self._last_used = time.time()
        self._sync_mirror()
        page = self.page()
        return Receipt(
            True,
            {
                "visible": True,
                "restarted": restarted,
                "description": session.description(),
                "url": page.url,
                "note": note,
            },
        )

    # ------------------------------------------------------------ 校验
    def bring_to_front(self) -> bool:
        """把真窗口提到最前，任务结束后用户能立刻接手。

        无头模式没有窗口可提，返回 False（这不算失败）。
        """
        if not self.started or self._session is None:
            return False
        if self._session.headless:
            return False
        try:
            self.page().bring_to_front()
        except Exception:  # noqa: BLE001 - 前置失败不该影响任务结果的判定
            return False
        return True

    def focus_single_tab(self) -> int:
        """只留一个标签页：关掉任务过程中开出来的多余标签。

        只关本通道自己开的（登记在 `_pages` 里的），用户自己在窗口里按
        Ctrl+T 开的标签不在名册上，不会被误伤。
        """
        if not self.started or self._session is None:
            return 0
        keep = self.page()
        pages = getattr(self._session, "pages", None)
        if callable(pages):
            pages = pages()
        closed = 0
        for page in list(pages or []):
            if page is keep or getattr(page, "dead", False):
                continue
            try:
                self._session.close_page(page)
                closed += 1
            except Exception:  # noqa: BLE001 - 关不掉就留着，不该中断任务
                continue
        return closed

    def verify(self, expect: str | dict[str, Any] | None) -> Verification:
        if not expect:
            return Verification(True, "浏览器动作已执行")
        if isinstance(expect, str):
            expect = {"text": expect}
        page = self.page()
        checks: list[str] = []
        ok = True

        url_needle = str(expect.get("url_contains") or expect.get("url") or "")
        if url_needle:
            current = page.url
            hit = url_needle.lower() in current.lower()
            checks.append(f"URL {'包含' if hit else '不包含'}“{url_needle}”")
            ok = ok and hit

        title_needle = str(expect.get("title_contains") or expect.get("title") or "")
        if title_needle:
            current = page.title
            hit = title_needle.lower() in current.lower()
            checks.append(f"标题 {'包含' if hit else '不包含'}“{title_needle}”")
            ok = ok and hit

        text_needle = str(expect.get("text") or "")
        if text_needle:
            body = page.text(limit=4000)
            hit = text_needle in body
            checks.append(f"页面{'有' if hit else '没有'}“{text_needle}”")
            ok = ok and hit

        element_text = str(expect.get("element_text") or "")
        if element_text:
            items = page.elements(limit=120)
            hit = any(element_text in str(item.get("text", "")) for item in items)
            checks.append(f"元素{'有' if hit else '没有'}“{element_text}”")
            ok = ok and hit

        if not checks:
            return Verification(True, "浏览器动作已执行（未指定校验条件）")
        return Verification(
            ok,
            "；".join(checks),
            {"url": page.url, "title": page.title},
        )

    # ------------------------------------------------------------ 生命周期
    def retire_if_idle(self, force: bool = False) -> bool:
        """空闲回收：超时则优雅关闭内核，profile 与登录态留在磁盘。

        负的 `keep_alive_seconds` 表示永不自动回收（一直开着等下次用）。
        有人在看镜像时不回收——用户正盯着页面，关掉等于打断他。
        可见窗口也不自动回收：那个窗口就是用户手上的浏览器，他可能正在
        里面填表、翻页，按空闲时长关掉等于把他正在用的东西收走。
        """
        if not self.started or self._session is None:
            return False
        if not force:
            if self.mirror_active:
                return False
            if self.keep_alive_seconds < 0:
                return False
            if not self._session.headless:
                return False
            idle = time.time() - (self._last_used or time.time())
            if idle < self.keep_alive_seconds:
                return False
        self.close()
        return True

    def close(self) -> None:
        with self._frame_lock:
            self._frame_subscribers.clear()
        page, self._page = self._page, None
        session, self._session = self._session, None
        if page is not None:
            try:
                page.stop_mirror()
            except Exception:  # noqa: BLE001
                pass
        if session is not None:
            try:
                session.close(reason="通道关闭")
            except Exception:  # noqa: BLE001
                pass
