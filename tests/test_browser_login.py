"""扫码登录接管：AI 碰到登录页时，把窗口交给人，并停下来等。

关键不是「能不能打开登录页」，而是：
1. 无头模式下要真的切成可见窗口，否则用户看不到二维码；
2. 交互要停在 needs_human，不能一边等一边把步数烧完；
3. 任务本身就是要登录时别多此一举地暂停（用户已经明确要做这件事）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from xm_agent.audit import AuditLog
from xm_agent.browser.channel import BrowserChannel
from xm_agent.browser.session import BrowserSettings
from xm_agent.channels import BROWSER_KIND, ChannelHub
from xm_agent.contracts import CAPABILITIES
from xm_agent.lock import LeaseLock
from xm_agent.policy import Policy
from xm_agent.providers.fake import ScriptedProvider
from xm_agent.runtime import AgentRuntime
from xm_agent.types import Action, Element, Observation, Receipt, Verification

_LOGIN_ELEMENTS = [
    Element("b0", "input", "手机号/账号"),
    Element("b1", "input", "密码"),
    Element("b2", "button", "登录"),
]


class FakeBrowserChannel:
    """登录页 + 可观测的 handoff。"""

    name = "browser"
    kind = BROWSER_KIND
    capabilities = frozenset(
        {
            "browser.open",
            "browser.observe",
            "browser.act",
            "browser.verify",
            "browser.session",
        }
    )

    def __init__(self, login: bool = True) -> None:
        self.login = login
        self.handed = 0
        self.calls: list[tuple[str, dict]] = []
        self.observations = 0

    def observe(self, step: int, include_screenshot: bool = False) -> Observation:
        self.observations += 1
        if not self.login:
            return Observation(
                step=step,
                source=BROWSER_KIND,
                summary="浏览器页面「订单」https://shop.test/orders",
                elements=[Element("b0", "a", "订单 A")],
                active_window="订单",
                screen_card="页面：订单\n正文：我的订单",
                raw={"login_wall": False},
            )
        return Observation(
            step=step,
            source=BROWSER_KIND,
            summary="浏览器页面「登录」https://shop.test/login，3 个可交互元素（检测到登录/扫码页）",
            elements=list(_LOGIN_ELEMENTS),
            active_window="登录 - 示例商城",
            screen_card="页面：登录\n正文：扫码登录 密码登录 登录后查看订单",
            raw={"login_wall": True},
        )

    def execute(self, capability: str, args: dict[str, Any]) -> Receipt:
        self.calls.append((capability, args))
        return Receipt(True, {"via": "fake"})

    def verify(self, expect: Any) -> Verification:
        return Verification(True, "fake verified")

    def handoff(self) -> Receipt:
        self.handed += 1
        return Receipt(True, {"visible": True, "restarted": True})

    def close(self) -> None:
        pass


class _Page:
    def __init__(self) -> None:
        self.fronted = 0

    @property
    def url(self) -> str:
        return "https://shop.test/login"

    def bring_to_front(self) -> None:
        self.fronted += 1

    def start_mirror(self, on_frame=None, fps=None, quality=None) -> None:
        pass

    def stop_mirror(self) -> None:
        pass


class _Session:
    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self.started = True
        self._closed = False
        self.visible_calls = 0
        self._page = _Page()

    def first_page(self) -> _Page:
        return self._page

    def ensure_visible(self) -> bool:
        self.visible_calls += 1
        self.headless = False
        return True

    def description(self) -> str:
        return "Chrome fake · 无头"

    def close(self, reason: str = "关闭") -> None:
        self._closed = True


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = _Session()
        self.channel = BrowserChannel(
            BrowserSettings(),
            session=self.session,
            page=self.session.first_page(),
            screenshot_dir=str(self.root / "shots"),
        )

    def test_handoff_restarts_headless_into_visible(self):
        receipt = self.channel.handoff()
        self.assertTrue(receipt.ok)
        self.assertEqual(self.session.visible_calls, 1)
        self.assertTrue(receipt.detail["restarted"])
        # 重启后必须换到新页面对象，旧的在关闭时已失效
        self.assertIs(self.channel.page(), self.session.first_page())

    def test_handoff_fronts_existing_visible_window(self):
        self.session.headless = False
        page = self.session.first_page()
        receipt = self.channel.handoff()
        self.assertTrue(receipt.ok)
        self.assertEqual(page.fronted, 1)
        self.assertFalse(receipt.detail["restarted"])

    def test_handoff_without_session_is_rejected(self):
        channel = BrowserChannel(
            BrowserSettings(), screenshot_dir=str(self.root / "shots")
        )
        receipt = channel.handoff()
        self.assertFalse(receipt.ok)
        self.assertIn("尚未启动", receipt.error or "")

    def test_session_capability_routes_to_handoff(self):
        receipt = self.channel.execute("browser.session", {"action": "handoff"})
        self.assertTrue(receipt.ok)
        self.assertEqual(self.session.visible_calls, 1)


class LoginHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = Policy(
            allowed_capabilities=list(CAPABILITIES),
            allowed_command_prefixes=[],
            require_confirmation=[],
            allowed_paths=[str(self.root)],
        )

    def _runtime(
        self, channel: FakeBrowserChannel, actions: list[Action]
    ) -> AgentRuntime:
        hub = ChannelHub(browser_only=channel)
        return AgentRuntime(
            adapter=None,
            hub=hub,
            provider=ScriptedProvider(actions),
            policy=self.policy,
            audit=AuditLog(self.root / "audit"),
            lock=LeaseLock(self.root / "agent.lock", ttl_seconds=10),
            max_steps=6,
            confirm=lambda action, reason, mandatory=False: True,
        )

    def test_login_wall_hands_off_and_pauses(self):
        channel = FakeBrowserChannel(login=True)
        result = self._runtime(
            channel,
            [
                Action("browser.act", {"action": "click", "element_index": 0}),
                Action("task.finish", {"summary": "不该走到这里"}),
            ],
        ).run("看一下我的订单")

        self.assertEqual(result.status, "needs_human")
        self.assertEqual(channel.handed, 1)
        self.assertIsNotNone(result.await_user)
        assert result.await_user is not None
        self.assertEqual(result.await_user["kind"], "login")
        self.assertIn("扫码", result.await_user["prompt"])
        # 暂停发生在动作执行之前：点击请求不该被送出去
        self.assertEqual(channel.calls, [])

    def test_login_goal_does_not_pause(self):
        channel = FakeBrowserChannel(login=True)
        result = self._runtime(
            channel,
            [
                Action("browser.act", {"action": "click", "element_index": 0}),
                Action("task.finish", {"summary": "登录页已打开"}),
            ],
        ).run("帮我登录这个网站")

        self.assertEqual(channel.handed, 0)
        self.assertEqual(result.status, "done")

    def test_no_login_wall_no_pause(self):
        channel = FakeBrowserChannel(login=False)
        result = self._runtime(
            channel,
            [
                Action("browser.act", {"action": "click", "element_index": 0}),
                Action("task.finish", {"summary": "看完了"}),
            ],
        ).run("看一下我的订单")

        self.assertEqual(channel.handed, 0)
        self.assertEqual(result.status, "done")


if __name__ == "__main__":
    unittest.main()
