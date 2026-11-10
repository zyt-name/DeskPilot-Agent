"""纯浏览器模式：没有桌面通道时系统仍然能跑完整任务。

这是「选浏览器当后端」的核心承诺——不截屏、不点击、不占用用户桌面。
测试要守住三件事：
1. 能力清单里不出现 desktop.*，模型不会去调它；
2. 首帧观测能唤醒唯一的通道，而不是因为「没启动」直接失败；
3. 审计要图时绝不退回去截桌面。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import xm_agent.runtime as runtime_module
from xm_agent.audit import AuditLog
from xm_agent.bootstrap import build_hub
from xm_agent.browser.channel import BrowserChannel
from xm_agent.browser.session import BrowserSettings
from xm_agent.channels import BROWSER_KIND, DESKTOP_KIND, ChannelHub
from xm_agent.config import AgentConfig
from xm_agent.contracts import CAPABILITIES
from xm_agent.lock import LeaseLock
from xm_agent.policy import Policy
from xm_agent.providers.common import system_prompt
from xm_agent.providers.fake import ScriptedProvider
from xm_agent.runtime import AgentRuntime
from xm_agent.types import Action


class _Page:
    """够 BrowserChannel 用的最小页面。"""

    def __init__(self, url: str = "about:blank") -> None:
        self._url = url
        self.actions: list[tuple[str, Any]] = []

    @property
    def url(self) -> str:
        return self._url

    @property
    def title(self) -> str:
        return "搜索页"

    def snapshot(self, include_screenshot: bool = False, limit: int = 80) -> dict:
        return {
            "meta": {"url": self._url, "title": "搜索页"},
            "elements": [
                {
                    "i": 0,
                    "tag": "input",
                    "type": "text",
                    "text": "搜索框",
                    "x": 100,
                    "y": 50,
                    "w": 200,
                    "h": 30,
                }
            ],
            "text": "结果：上海天气 晴",
            "engine": "chrome",
        }

    def screenshot(self, quality: int = 70, max_width: int | None = None) -> bytes:
        return b"\x89PNG\r\n\x1a\nfake"

    def elements(self, limit: int = 80) -> list[dict]:
        return [{"i": 0, "text": "搜索框"}]

    def text(self, limit: int = 1500) -> str:
        return "结果：上海天气 晴"

    def click_element(
        self, index: int, elements: list | None = None, limit: int = 80
    ) -> dict:
        self.actions.append(("click_element", index))
        return {"i": index, "text": "搜索框"}

    def click(self, x: float, y: float, button: str = "left", clicks: int = 1) -> None:
        self.actions.append(("click", (x, y)))

    def navigate(self, url: str, timeout: float = 20.0) -> None:
        self.actions.append(("navigate", url))
        self._url = url

    def type_text(self, text: str) -> None:
        self.actions.append(("type", text))

    def type_into(self, selector: str, text: str) -> bool:
        self.actions.append(("type_into", (selector, text)))
        return True

    def press(self, key: str, modifiers: str = "") -> None:
        self.actions.append(("press", key))

    def scroll(
        self, delta_y: float = 400, x: float | None = None, y: float | None = None
    ) -> None:
        self.actions.append(("scroll", delta_y))

    def wait_text(self, needle: str, timeout: float = 15.0) -> bool:
        return True

    def wait_loaded(self, timeout: float = 20.0) -> bool:
        return True

    def evaluate(self, expression: str, timeout: float = 20.0) -> Any:
        return None

    def stop_mirror(self) -> None:
        pass


class _Session:
    def __init__(self, page: _Page) -> None:
        self.headless = True
        self.started = True
        self._page = page
        self.close_calls = 0

    def pages(self) -> list[_Page]:
        return [self._page]

    def first_page(self) -> _Page:
        return self._page

    def new_page(self, url: str = "about:blank") -> _Page:
        self._page = _Page(url)
        return self._page

    def open(self, url: str, timeout: float = 20.0) -> _Page:
        self._page = _Page(url)
        return self._page

    def ensure_visible(self) -> bool:
        return True

    def description(self) -> str:
        return "Chrome fake · 无头"

    def stats(self):
        class Stats:
            pages = 1
            engine = "chrome"
            version = "fake"
            headless = True

        return Stats()

    def close(self, reason: str = "用户关闭") -> None:
        self.close_calls += 1


class _WakefulBrowser(BrowserChannel):
    """把「未启动 → 被唤醒」显式建模出来，用来测 Hub 的唤醒路径。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, session=None, page=None, **kwargs)
        self.activated = 0

    def page(self) -> _Page:
        if self._session is None:
            self._session = _Session(_Page())
        self._page = self._session.first_page()
        return self._page

    def activate(self) -> None:
        self.activated += 1
        self.page()


class BrowserOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.page = _Page()
        self.session = _Session(self.page)
        self.browser = BrowserChannel(
            BrowserSettings(),
            session=self.session,
            page=self.page,
            screenshot_dir=str(self.root / "shots"),
        )
        self.policy = Policy(
            allowed_capabilities=list(CAPABILITIES),
            allowed_command_prefixes=[],
            require_confirmation=[],
            allowed_paths=[str(self.root)],
        )

    def _runtime(
        self, actions: list[Action], hub: ChannelHub | None = None
    ) -> AgentRuntime:
        return AgentRuntime(
            adapter=None,
            provider=ScriptedProvider(actions),
            policy=self.policy,
            audit=AuditLog(self.root / "audit"),
            lock=LeaseLock(self.root / "agent.lock", ttl_seconds=10),
            max_steps=8,
            confirm=lambda action, reason, mandatory=False: True,
            hub=hub if hub is not None else ChannelHub(browser_only=self.browser),
        )

    # ------------------------------------------------------------ 能力面
    def test_capabilities_exclude_desktop(self):
        hub = ChannelHub(browser_only=self.browser)
        caps = hub.capabilities()
        self.assertIn("browser.open", caps)
        self.assertNotIn("desktop.act", caps)
        self.assertNotIn("terminal.exec", caps)

    def test_prompt_does_not_offer_desktop_actions(self):
        hub = ChannelHub(browser_only=self.browser)
        prompt = system_prompt(hub.capabilities())
        self.assertIn("browser.act", prompt)
        self.assertNotIn("desktop.act", prompt)

    def test_build_hub_without_desktop(self):
        config = AgentConfig()
        config.browser.enabled = True
        hub = build_hub(config, None)
        self.assertIsNone(hub.get(DESKTOP_KIND))
        self.assertIsNotNone(hub.get(BROWSER_KIND))
        self.assertIn("browser.open", hub.capabilities())
        self.assertNotIn("desktop.observe", hub.capabilities())

    # ------------------------------------------------------------ 唤醒
    def test_first_observation_wakes_the_only_channel(self):
        browser = _WakefulBrowser(
            BrowserSettings(), screenshot_dir=str(self.root / "shots")
        )
        self.assertFalse(browser.started)
        hub = ChannelHub(browser_only=browser)
        active = hub.active()
        self.assertIs(active, browser)
        self.assertEqual(browser.activated, 1)
        self.assertTrue(browser.started)

    # ------------------------------------------------------------ 执行
    def test_browser_only_task_runs_and_never_touches_desktop(self):
        called: list[str] = []

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            called.append("desktop-screenshot")
            raise AssertionError("纯浏览器模式不该截桌面")

        original = runtime_module.vision.capture_primary_screen
        runtime_module.vision.capture_primary_screen = forbidden
        self.addCleanup(
            setattr, runtime_module.vision, "capture_primary_screen", original
        )

        result = self._runtime(
            [
                Action("browser.open", {"url": "https://a.test/"}),
                Action("browser.act", {"action": "click", "element_index": 0}),
                Action("task.finish", {"summary": "查完了"}),
            ]
        ).run("在浏览器里查天气")

        self.assertEqual(result.status, "done")
        self.assertEqual(called, [])
        self.assertIn(("navigate", "https://a.test/"), self.page.actions)
        self.assertIn(("click_element", 0), self.page.actions)
        for record in result.steps:
            if record.observation is not None:
                self.assertEqual(record.observation.source, BROWSER_KIND)

    def test_audit_screenshot_comes_from_browser(self):
        runtime = self._runtime([Action("task.finish", {"summary": "ok"})])
        runtime.run("随便看看")
        path = runtime._capture_for_audit()
        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(Path(path).exists())

    def test_missing_channel_gives_readable_error(self):
        hub = ChannelHub()
        result = self._runtime(
            [
                Action("browser.open", {"url": "https://a.test/"}),
                Action("task.finish", {"summary": "ok"}),
            ],
            hub=hub,
        ).run("打开页面")
        # 没有通道时不该崩，而是把错误交给模型/用户
        self.assertEqual(result.status, "failed")
        self.assertIn("没有可用的观测通道", result.reason)


if __name__ == "__main__":
    unittest.main()
