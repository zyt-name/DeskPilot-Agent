"""面板侧的浏览器接口：状态、动作、帧订阅。

不启动真实内核——这里验证的是 HTTP 契约与订阅生命周期；
真机帧流由 scripts/probe_browser_channel.py 和手工验证覆盖。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from xm_agent.config import AdapterConfig, AgentConfig
from xm_agent.types import Receipt
from xm_agent.ui.server import UiServer


def _config(tmp_path, *, enabled: bool = True) -> AgentConfig:
    config = AgentConfig()
    config.browser.enabled = enabled
    config.browser.profile_root = str(tmp_path / "profiles")
    config.adapters = {"mock": AdapterConfig()}
    config.default_adapter = "mock"
    config.audit.root = str(tmp_path / "audit")
    config.runtime.lock_path = str(tmp_path / "agent.lock")
    return config


def _request(
    url: str,
    method: str = "GET",
    body: dict | None = None,
    token: str | None = None,
) -> tuple[int, object]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-XM-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def server(tmp_path):
    app = UiServer(
        _config(tmp_path),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
    )
    url = app.start()
    try:
        yield app, url
    finally:
        app.stop()


class _FakeBrowser:
    name = "browser"
    kind = "browser"

    def __init__(self) -> None:
        self.subscribers: dict[int, object] = {}
        self.seq = 0
        self.unsubscribed: list[int] = []
        self.calls: list[tuple[str, dict]] = []

    def status(self) -> dict:
        return {"started": True, "url": "https://a.test/", "title": "示例"}

    def subscribe(self, callback) -> int:
        self.seq += 1
        self.subscribers[self.seq] = callback
        return self.seq

    def unsubscribe(self, sid: int) -> None:
        self.subscribers.pop(sid, None)
        self.unsubscribed.append(sid)

    def execute(self, capability: str, args: dict) -> Receipt:
        self.calls.append((capability, args))
        return Receipt(True, {"echo": capability})

    def close(self) -> None:
        pass


def test_browser_status_reports_not_started(server):
    app, url = server
    status, data = _request(url + "api/browser")
    assert status == 200
    assert data["enabled"] is True
    assert data["started"] is False


def test_browser_status_when_channel_disabled(tmp_path):
    app = UiServer(
        _config(tmp_path, enabled=False),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
    )
    url = app.start()
    try:
        status, data = _request(url + "api/browser")
        assert status == 200
        assert data == {"enabled": False, "started": False}
        # 未启用时动作接口要给出可读错误，而不是 500
        status, data = _request(
            url + "api/browser",
            method="POST",
            body={"action": "open", "url": "https://a.test"},
            token=app.token,
        )
        assert status == 200
        assert data["ok"] is False
        assert "未启用" in data["error"]
    finally:
        app.stop()


def test_browser_action_requires_token(server):
    _, url = server
    status, data = _request(
        url + "api/browser", method="POST", body={"action": "handoff"}
    )
    assert status == 401


def test_browser_action_rejects_unknown(server):
    app, url = server
    status, data = _request(
        url + "api/browser",
        method="POST",
        body={"action": "dance"},
        token=app.token,
    )
    assert status == 200
    assert data["ok"] is False
    assert "未知动作" in data["error"]


def test_browser_frames_subscribe_and_push(tmp_path):
    app = UiServer(
        _config(tmp_path),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
    )
    fake = _FakeBrowser()
    app.browser = fake

    subscriber, frames = app.browser_frames()
    assert subscriber == 1
    assert frames is not None
    assert 1 in fake.subscribers

    fake.subscribers[1]("data:image/jpeg;base64,AAA", {"timestamp": 12})
    image, meta = frames.get_nowait()
    assert image.startswith("data:image/jpeg")
    assert meta["timestamp"] == 12

    app.browser.unsubscribe(subscriber)
    assert fake.unsubscribed == [1]


def test_browser_frames_drops_old_frames(tmp_path):
    app = UiServer(
        _config(tmp_path),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
    )
    fake = _FakeBrowser()
    app.browser = fake
    _, frames = app.browser_frames()
    callback = next(iter(fake.subscribers.values()))

    for index in range(10):
        callback(f"data:image/jpeg;base64,{index}", {})
    # 队列有上限，最后的帧一定在，旧的被丢弃
    latest = None
    while not frames.empty():
        latest = frames.get_nowait()
    assert latest is not None
    assert latest[0].endswith("9")


def test_browser_action_delegates_when_fake_channel(tmp_path):
    app = UiServer(
        _config(tmp_path),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
    )
    fake = _FakeBrowser()
    app.browser = fake

    result = app.browser_action("handoff")
    assert result["ok"] is True
    assert fake.calls[-1] == ("browser.session", {"action": "handoff"})

    app.browser_action("open", "https://a.test/x")
    assert fake.calls[-1] == ("browser.open", {"url": "https://a.test/x"})


class _RetirableBrowser(_FakeBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.retired = 0
        self.status_calls = 0

    def retire_if_idle(self) -> bool:
        self.retired += 1
        return True

    def status(self) -> dict:
        self.status_calls += 1
        return {"started": False, "description": "已释放"}


def test_reaper_retires_idle_browser(tmp_path):
    app = UiServer(
        _config(tmp_path),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
    )
    fake = _RetirableBrowser()
    app.browser = fake

    assert app._tick_browser_reaper() is True
    assert fake.retired == 1
    assert fake.status_calls == 1  # 回收后把新状态播出去
    assert fake.calls == []  # 不触发任何用户动作


def test_reaper_leaves_browser_alone_while_task_runs(tmp_path):
    app = UiServer(
        _config(tmp_path),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
    )
    fake = _RetirableBrowser()
    app.browser = fake

    assert app._active.acquire(blocking=False)
    try:
        assert app._tick_browser_reaper() is False
    finally:
        app._active.release()
    assert fake.retired == 0


def test_reaper_noop_when_browser_disabled(tmp_path):
    app = UiServer(
        _config(tmp_path, enabled=False),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
    )
    assert app.browser is None
    assert app._tick_browser_reaper() is False



# ---------------------------------------------------------------- 静态资源
# 真机踩过：index.html 引了 /static/browser.js，但 _ASSETS 里没登记，
# 于是脚本 404、浏览器按钮永远不出现。这类错误只能在「引用了就取得到」
# 这一层拦住，所以直接按 index.html 里的引用逐个请求。
def _raw(url: str) -> tuple[int, bytes]:
    """取原始响应体：静态资源不是 JSON，不能走 _request。"""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_every_script_and_style_in_index_is_served(server):
    import re

    _app, url = server
    code, body = _raw(url)
    assert code == 200
    html = body.decode("utf-8")
    refs = set(re.findall(r'(?:src|href)="(/static/[^"]+)"', html))
    assert refs, "index.html 没有引用任何静态资源，测试失去意义"
    for ref in sorted(refs):
        code, _ = _raw(url.rstrip("/") + ref)
        assert code == 200, f"{ref} 没有被面板提供（_ASSETS 漏登记）"


def test_browser_view_script_is_served(server):
    _app, url = server
    code, body = _raw(url + "static/browser.js")
    assert code == 200
    assert b"browserView" in body
