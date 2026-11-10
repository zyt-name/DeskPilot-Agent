"""浏览器通道的行为测试。

用假会话替代真实内核：验证的是「能力名 -> 页面动作 -> Observation 形状」
这条链路，以及登录墙识别、元素序号解析和空闲回收。真机启动由
test_browser_session.py 负责，两者互补。
"""

from __future__ import annotations

from typing import Any

import pytest

from xm_agent.browser.channel import BrowserChannel, _as_index
from xm_agent.browser.session import BrowserSettings
from xm_agent.types import Observation

ELEMENTS = [
    {
        "i": 0,
        "tag": "input",
        "type": "text",
        "role": "",
        "text": "搜索",
        "x": 100,
        "y": 40,
        "w": 200,
        "h": 30,
        "inView": True,
        "disabled": False,
    },
    {
        "i": 1,
        "tag": "button",
        "type": "submit",
        "role": "",
        "text": "提交",
        "x": 320,
        "y": 40,
        "w": 80,
        "h": 30,
        "inView": True,
        "disabled": False,
    },
]


class FakePage:
    def __init__(self, url: str = "https://example.com/") -> None:
        self._url = url
        self.title_text = "示例站点"
        self.actions: list[tuple[str, Any]] = []
        self.mirror_stops = 0
        self.fronted = 0
        self.text_body = "欢迎来到示例站点"
        self.snapshot_kwargs: dict[str, Any] = {}

    @property
    def url(self) -> str:
        return self._url

    def bring_to_front(self) -> None:
        self.fronted += 1

    @property
    def title(self) -> str:
        return self.title_text

    def snapshot(self, include_screenshot: bool = False, limit: int = 80) -> dict:
        self.snapshot_kwargs = {
            "include_screenshot": include_screenshot,
            "limit": limit,
        }
        data: dict[str, Any] = {
            "meta": {
                "url": self._url,
                "title": self.title_text,
                "readyState": "complete",
                "scrollY": 0,
                "scrollHeight": 2000,
            },
            "elements": list(ELEMENTS),
            "text": self.text_body,
            "engine": "chrome",
        }
        if include_screenshot:
            data["_screenshot"] = b"\x89PNG\r\n\x1a\nfake"
        return data

    def screenshot(self, quality: int = 70, max_width: int | None = None) -> bytes:
        return b"\x89PNG\r\n\x1a\nfake"

    def elements(self, limit: int = 80) -> list[dict]:
        return list(ELEMENTS)

    def text(self, limit: int = 1500) -> str:
        return self.text_body

    def click_element(self, index: int, elements: list | None = None, limit: int = 80) -> dict:
        self.actions.append(("click_element", index))
        return ELEMENTS[index]

    def click(self, x: float, y: float, button: str = "left", clicks: int = 1) -> None:
        self.actions.append(("click", (x, y)))

    def navigate(self, url: str, timeout: float = 20.0) -> None:
        self.actions.append(("navigate", url))
        self._url = url

    def type_text(self, text: str) -> None:
        self.actions.append(("type", text))

    def type_into(self, selector: str, text: str) -> bool:
        self.actions.append(("type_into", (selector, text)))
        return selector == "#q"

    def press(self, key: str, modifiers: str = "") -> None:
        self.actions.append(("press", key))

    def scroll(self, delta_y: float = 400, x: float | None = None, y: float | None = None) -> None:
        self.actions.append(("scroll", delta_y))

    def wait_text(self, needle: str, timeout: float = 15.0) -> bool:
        return needle in self.text_body

    def wait_loaded(self, timeout: float = 20.0) -> bool:
        return True

    def evaluate(self, expression: str, timeout: float = 20.0) -> Any:
        self.actions.append(("evaluate", expression[:20]))
        if "document.querySelector" in expression:
            return True
        return None

    def stop_mirror(self) -> None:
        self.mirror_stops += 1


class FakeSession:
    def __init__(self, page: FakePage | None = None) -> None:
        self._closed = False
        self.headless = False
        self.started = True
        self.engine = None
        self._page = page or FakePage()
        self.opened: list[str] = []
        self.visible_calls = 0
        self.close_calls = 0

    def pages(self) -> list[FakePage]:
        return [self._page]

    def new_page(self, url: str = "about:blank") -> FakePage:
        self._page = FakePage(url)
        return self._page

    def open(self, url: str, timeout: float = 20.0) -> FakePage:
        self.opened.append(url)
        self._page = FakePage(url)
        return self._page

    def ensure_visible(self) -> bool:
        self.visible_calls += 1
        return True

    def stats(self):
        class Stats:
            pages = 1
            engine = "chrome"
            version = "130"
            headless = False

        return Stats()

    def description(self) -> str:
        return "Chrome 130 · 可见窗口"

    def close(self, reason: str = "用户关闭") -> None:
        self.close_calls += 1
        self._closed = True


def make_channel(page: FakePage | None = None, **kwargs) -> tuple[BrowserChannel, FakeSession, FakePage]:
    page = page or FakePage()
    session = FakeSession(page)
    channel = BrowserChannel(
        BrowserSettings(), session=session, page=page, **kwargs
    )
    return channel, session, page


def test_observe_builds_a_desktop_shaped_observation():
    channel, _, page = make_channel()
    observation = channel.observe(3)

    assert isinstance(observation, Observation)
    assert observation.source == "browser"
    assert observation.active_window == "示例站点"
    assert observation.screenshot_path is None
    assert [element.id for element in observation.elements] == ["b0", "b1"]
    assert observation.elements[0].name == "搜索"
    # bounds 是左上角 + 尺寸，由中心点还原
    assert observation.elements[0].bounds == (0, 25, 200, 30)
    assert "https://example.com/" in observation.summary
    assert observation.raw["url"] == "https://example.com/"
    assert page.snapshot_kwargs["include_screenshot"] is False


def test_observe_saves_screenshot_when_asked(tmp_path):
    channel, _, _ = make_channel(screenshot_dir=str(tmp_path))
    observation = channel.observe(1, include_screenshot=True)

    assert observation.screenshot_path is not None
    assert observation.screenshot_path.startswith(str(tmp_path))
    assert (tmp_path).glob("browser-step-1-*.png")


def test_login_wall_detection_by_password_field():
    page = FakePage("https://passport.example.com/login")
    page.text_body = "请扫码登录后继续"
    page.snapshot = lambda include_screenshot=False, limit=80: {
        "meta": {"url": page.url, "title": "登录"},
        "elements": [
            {"i": 0, "tag": "input", "type": "password", "text": "密码", "x": 1, "y": 1, "w": 10, "h": 10}
        ],
        "text": page.text_body,
    }
    channel, _, _ = make_channel(page)
    observation = channel.observe(1)
    assert observation.raw["login_wall"] is True
    assert "登录" in observation.summary


def test_plain_page_is_not_flagged_as_login():
    channel, _, _ = make_channel()
    assert channel.observe(1).raw["login_wall"] is False


def test_open_starts_and_navigates():
    channel, session, page = make_channel()
    receipt = channel.execute("browser.open", {"url": "https://a.test/x"})

    assert receipt.ok
    assert page.url == "https://a.test/x"
    assert session.visible_calls == 1
    assert receipt.detail["engine"].startswith("Chrome")


def test_act_click_by_element_index():
    channel, _, page = make_channel()
    receipt = channel.execute(
        "browser.act", {"action": "click", "element_index": 1}
    )
    assert receipt.ok
    assert page.actions == [("click_element", 1)]
    assert receipt.detail["element"] == "b1"


def test_act_click_by_coordinates():
    channel, _, page = make_channel()
    receipt = channel.execute("browser.act", {"action": "click", "x": 10, "y": 20})
    assert receipt.ok
    assert page.actions == [("click", (10, 20))]


def test_act_type_with_enter():
    channel, _, page = make_channel()
    receipt = channel.execute(
        "browser.act", {"action": "type", "text": "上海天气", "press_enter": True}
    )
    assert receipt.ok
    assert ("type", "上海天气") in page.actions
    assert ("press", "Enter") in page.actions


def test_act_type_into_selector_miss_reports_error():
    channel, _, _ = make_channel()
    receipt = channel.execute(
        "browser.act", {"action": "type", "selector": "#missing", "text": "x"}
    )
    assert receipt.ok is False
    assert "选择器" in (receipt.error or "")


def test_act_wait_text_reports_timeout():
    channel, _, _ = make_channel()
    receipt = channel.execute(
        "browser.act", {"action": "wait", "text": "不存在的文字", "timeout_s": 1}
    )
    assert receipt.ok is False
    assert "等待超时" in (receipt.error or "")


def test_act_rejects_unknown_action():
    channel, _, _ = make_channel()
    receipt = channel.execute("browser.act", {"action": "teleport"})
    assert receipt.ok is False
    assert "未支持" in (receipt.error or "")


def test_verify_checks_url_and_text():
    channel, _, _ = make_channel()
    ok = channel.verify({"url_contains": "example.com", "text": "示例"})
    assert ok.ok is True

    bad = channel.verify({"text": "不存在的段落"})
    assert bad.ok is False
    assert "没有" in bad.detail


def test_verify_accepts_plain_string():
    channel, _, _ = make_channel()
    assert channel.verify("欢迎").ok is True


def test_session_status_and_handoff():
    channel, session, page = make_channel()
    status = channel.execute("browser.session", {"action": "status"})
    assert status.ok and status.detail["started"] is True

    handoff = channel.execute("browser.session", {"action": "handoff"})
    assert handoff.ok and handoff.detail["visible"] is True
    # 已经是可见窗口：只前置，不重启（重启会丢掉当前页面状态）
    assert handoff.detail["restarted"] is False
    assert session.visible_calls == 0
    assert page.fronted == 1


def test_session_close_shuts_the_browser():
    channel, session, page = make_channel()
    receipt = channel.execute("browser.session", {"action": "close"})
    assert receipt.ok
    assert session.close_calls == 1
    assert page.mirror_stops == 1
    assert channel.started is False


def test_retire_if_idle_keeps_recent_session():
    channel, session, _ = make_channel(keep_alive_seconds=600)
    channel.observe(1)
    assert channel.retire_if_idle() is False
    assert session.close_calls == 0


def test_retire_if_idle_force_closes():
    channel, session, _ = make_channel(keep_alive_seconds=600)
    assert channel.retire_if_idle(force=True) is True
    assert session.close_calls == 1


def test_unknown_capability_returns_receipt():
    channel, _, _ = make_channel()
    receipt = channel.execute("browser.teleport", {})
    assert receipt.ok is False
    assert "不支持" in (receipt.error or "")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("b3", 3), ("3", 3), (3, 3), ("B12", 12)],
)
def test_as_index_accepts_both_forms(value, expected):
    assert _as_index(value) == expected


def test_as_index_rejects_garbage():
    with pytest.raises(ValueError):
        _as_index("第三个")
