"""端到端路由：一个任务里同时用桌面和浏览器。

这不是通道单测的重复——它验证 Runtime 真的会在两条通道间切换观测对象，
并且浏览器动作执行完后不会再去截桌面（那会让模型看到错的界面）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from xm_agent.adapters.mock import MockAdapter
from xm_agent.audit import AuditLog
from xm_agent.browser.channel import BrowserChannel
from xm_agent.browser.session import BrowserSettings
from xm_agent.channels import BROWSER_KIND, ChannelHub
from xm_agent.contracts import CAPABILITIES
from xm_agent.lock import LeaseLock
from xm_agent.policy import Policy
from xm_agent.providers.fake import ScriptedProvider
from xm_agent.runtime import AgentRuntime
from xm_agent.types import Action


class FakePage:
    def __init__(self, url: str = "about:blank") -> None:
        self._url = url
        self.title_text = "搜索页"
        self.actions: list[tuple[str, Any]] = []
        self.snapshot_calls = 0

    @property
    def url(self) -> str:
        return self._url

    @property
    def title(self) -> str:
        return self.title_text

    def snapshot(self, include_screenshot: bool = False, limit: int = 80) -> dict:
        self.snapshot_calls += 1
        return {
            "meta": {"url": self._url, "title": self.title_text},
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

    def click_element(self, index: int, elements: list | None = None, limit: int = 80) -> dict:
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

    def scroll(self, delta_y: float = 400, x: float | None = None, y: float | None = None) -> None:
        self.actions.append(("scroll", delta_y))

    def wait_text(self, needle: str, timeout: float = 15.0) -> bool:
        return True

    def wait_loaded(self, timeout: float = 20.0) -> bool:
        return True

    def evaluate(self, expression: str, timeout: float = 20.0) -> Any:
        return None

    def stop_mirror(self) -> None:
        pass


class FakeSession:
    def __init__(self, page: FakePage) -> None:
        self._closed = False
        self.headless = True
        self.started = True
        self._page = page
        self.close_calls = 0

    def pages(self) -> list[FakePage]:
        return [self._page]

    def new_page(self, url: str = "about:blank") -> FakePage:
        self._page = FakePage(url)
        return self._page

    def open(self, url: str, timeout: float = 20.0) -> FakePage:
        self._page = FakePage(url)
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
        self._closed = True


class BrowserRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.desktop = MockAdapter()
        self.page = FakePage()
        self.session = FakeSession(self.page)
        self.browser = BrowserChannel(
            BrowserSettings(),
            session=self.session,
            page=self.page,
            screenshot_dir=str(self.root / "shots"),
        )
        self.hub = ChannelHub(desktop=self.desktop)
        self.hub.register(self.browser)
        self.policy = Policy(
            allowed_capabilities=list(CAPABILITIES),
            allowed_command_prefixes=["echo"],
            require_confirmation=[],
            allowed_paths=[str(self.root)],
        )

    def _runtime(self, actions: list[Action], on_event=None) -> AgentRuntime:
        return AgentRuntime(
            adapter=self.desktop,
            provider=ScriptedProvider(actions),
            policy=self.policy,
            audit=AuditLog(self.root / "audit"),
            lock=LeaseLock(self.root / "agent.lock", ttl_seconds=10),
            max_steps=8,
            confirm=lambda action, reason, mandatory=False: True,
            on_event=on_event,
            hub=self.hub,
        )

    def test_web_task_runs_through_browser_channel(self):
        result = self._runtime(
            [
                Action("browser.open", {"url": "https://search.test/?q=上海"}),
                Action(
                    "browser.act",
                    {"action": "click", "element_index": 0},
                    expect="搜索框",
                ),
                Action(
                    "browser.act",
                    {"action": "type", "text": "上海天气", "press_enter": True},
                    expect="上海天气",
                ),
                Action("task.finish", {"summary": "查完了"}),
            ]
        ).run("查一下上海天气")

        self.assertEqual(result.status, "done")
        self.assertIn(("navigate", "https://search.test/?q=上海"), self.page.actions)
        self.assertIn(("click_element", 0), self.page.actions)
        self.assertIn(("type", "上海天气"), self.page.actions)

    def test_observation_switches_to_browser_after_browser_action(self):
        seen: list[str] = []

        def on_event(kind: str, payload: dict[str, Any]) -> None:
            if kind == "observation":
                seen.append(str(payload.get("summary", "")))

        self._runtime(
            [
                Action("browser.open", {"url": "https://a.test/"}),
                Action("task.finish", {"summary": "ok"}),
            ],
            on_event=on_event,
        ).run("打开首页")

        self.assertTrue(seen)
        self.assertIn("浏览器页面", seen[-1])
        self.assertIn("window=Notepad", seen[0])  # 首帧仍是桌面

    def test_browser_verify_uses_page_not_desktop(self):
        runtime = self._runtime(
            [
                Action("browser.open", {"url": "https://a.test/"}),
                Action(
                    "browser.act",
                    {"action": "goto", "url": "https://a.test/r"},
                    expect="上海天气",
                ),
                Action("task.finish", {"summary": "ok"}),
            ]
        )
        result = runtime.run("打开页面")
        self.assertEqual(result.status, "done")
        self.assertEqual(self.hub.last_kind, BROWSER_KIND)

    def test_session_close_capability_is_routable(self):
        result = self._runtime(
            [
                Action("browser.open", {"url": "https://a.test/"}),
                Action("browser.session", {"action": "close"}),
                Action("task.finish", {"summary": "关掉了"}),
            ]
        ).run("打开再关闭")
        self.assertEqual(result.status, "done")
        self.assertEqual(self.session.close_calls, 1)

    def test_capabilities_advertise_browser(self):
        self.assertIn("browser.open", self.hub.capabilities())


if __name__ == "__main__":
    unittest.main()
