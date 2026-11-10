"""通道 Hub 的路由与观测归属。

重点不是覆盖每行代码，而是锁住三条不变量：
1. 能力名决定去哪个通道，浏览器能力不会落到桌面后端；
2. 上一步谁干的活，下一步就观测谁；
3. 通道缺失时给出能读懂的错误，而不是静默失败。
"""

from __future__ import annotations

from typing import Any

from xm_agent.channels import (
    BROWSER_KIND,
    DESKTOP_KIND,
    ChannelHub,
    is_observe,
    kind_of,
)
from xm_agent.types import Element, Observation, Receipt, Verification


class FakeDesktop:
    name = "desktop"
    kind = DESKTOP_KIND

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.observations = 0

    def observe(self, step: int, include_screenshot: bool = False) -> Observation:
        self.observations += 1
        return Observation(
            step=step,
            source="desktop",
            summary="桌面",
            elements=[Element("e1", "button", "保存")],
            active_window="记事本",
        )

    def execute(self, capability: str, args: dict[str, Any]) -> Receipt:
        self.calls.append((capability, args))
        return Receipt(True, {"via": "desktop"})

    def verify(self, expect: Any) -> Verification:
        return Verification(True, "desktop verified")

    def close(self) -> None:
        pass


class FakeBrowser:
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

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.observations = 0
        self.closed = False

    def observe(self, step: int, include_screenshot: bool = False) -> Observation:
        self.observations += 1
        return Observation(
            step=step,
            source="browser",
            summary="网页",
            elements=[Element("b0", "a", "登录")],
            active_window="示例站点",
        )

    def execute(self, capability: str, args: dict[str, Any]) -> Receipt:
        self.calls.append((capability, args))
        return Receipt(True, {"via": "browser"})

    def verify(self, expect: Any) -> Verification:
        return Verification(True, "browser verified")

    def close(self) -> None:
        self.closed = True


def test_kind_of_routes_by_prefix():
    assert kind_of("browser.act") == BROWSER_KIND
    assert kind_of("desktop.act") == DESKTOP_KIND
    assert kind_of("terminal.exec") == DESKTOP_KIND
    assert kind_of("wait.for") == DESKTOP_KIND
    assert kind_of("something.new") == DESKTOP_KIND


def test_is_observe_covers_both_channels():
    assert is_observe("desktop.observe")
    assert is_observe("browser.observe")
    assert not is_observe("browser.act")


def test_execute_routes_to_declaring_channel():
    desktop, browser = FakeDesktop(), FakeBrowser()
    hub = ChannelHub(desktop=desktop)
    hub.register(browser)

    receipt = hub.execute("browser.act", {"action": "click", "element_index": 0})
    assert receipt.ok and receipt.detail["via"] == "browser"
    assert desktop.calls == []
    assert hub.last_kind == BROWSER_KIND

    hub.execute("desktop.act", {"action": "click", "element_id": "e1"})
    assert len(desktop.calls) == 1
    assert hub.last_kind == DESKTOP_KIND


def test_observe_follows_last_channel():
    desktop, browser = FakeDesktop(), FakeBrowser()
    hub = ChannelHub(desktop=desktop)
    hub.register(browser)

    first = hub.observe(1)
    assert first.source == "desktop"

    hub.execute("browser.open", {"url": "https://example.com"})
    second = hub.observe(2)
    assert second.source == "browser"
    assert desktop.observations == 1
    assert browser.observations == 1


def test_observe_can_target_a_channel_explicitly():
    desktop, browser = FakeDesktop(), FakeBrowser()
    hub = ChannelHub(desktop=desktop)
    hub.register(browser)
    hub.execute("browser.open", {"url": "https://example.com"})

    observation = hub.observe(3, kind=DESKTOP_KIND)
    assert observation.source == "desktop"
    assert hub.last_kind == DESKTOP_KIND


def test_verify_follows_the_channel_that_acted():
    desktop, browser = FakeDesktop(), FakeBrowser()
    hub = ChannelHub(desktop=desktop)
    hub.register(browser)

    hub.execute("browser.act", {"action": "goto", "url": "https://a.test"})
    assert hub.verify("登录").detail == "browser verified"

    hub.execute("desktop.act", {"action": "click", "element_id": "e1"})
    assert hub.verify("保存").detail == "desktop verified"


def test_missing_browser_channel_returns_actionable_error():
    hub = ChannelHub(desktop=FakeDesktop())
    receipt = hub.execute("browser.open", {"url": "https://example.com"})
    assert receipt.ok is False
    assert "通道" in (receipt.error or "")


def test_capabilities_reflect_registered_channels():
    desktop_only = ChannelHub(desktop=FakeDesktop())
    names = desktop_only.capabilities()
    assert "desktop.observe" in names
    assert not any(name.startswith("browser.") for name in names)

    full = ChannelHub(desktop=FakeDesktop())
    full.register(FakeBrowser())
    names = full.capabilities()
    assert "browser.open" in names
    assert "browser.observe" in names
    # 顺序跟契约一致，模型看到的清单稳定
    assert names.index("desktop.observe") < names.index("browser.open")


def test_desktop_channel_without_declared_capabilities_still_works():
    hub = ChannelHub(desktop=FakeDesktop())
    receipt = hub.execute("terminal.exec", {"command": "echo hi"})
    assert receipt.ok


def test_close_all_is_resilient():
    browser = FakeBrowser()
    hub = ChannelHub(desktop=FakeDesktop())
    hub.register(browser)
    hub.close_all()
    assert browser.closed


def test_describe_marks_active_channel():
    hub = ChannelHub(desktop=FakeDesktop())
    hub.register(FakeBrowser())
    hub.execute("browser.observe", {})
    described = {item["kind"]: item for item in hub.describe()}
    assert described[BROWSER_KIND]["active"] is True
    assert described[DESKTOP_KIND]["active"] is False
