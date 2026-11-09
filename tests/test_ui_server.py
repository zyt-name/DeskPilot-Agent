from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

import xm_agent.ui.server as server_module
from xm_agent.adapters.mock import MockAdapter
from xm_agent.ai.crypto import decrypt_secret
from xm_agent.config import AdapterConfig, AgentConfig
from xm_agent.control import RunControl, RunPreempted
from xm_agent.providers.fake import HeuristicMockProvider
from xm_agent.resources import RESOURCES, ResourceState
from xm_agent.types import Action
from xm_agent.ui.hotkey import GlobalHotkey
from xm_agent.ui.overlay import ScreenIndicator
from xm_agent.ui.server import UiServer


def _config(tmp_path) -> AgentConfig:
    config = AgentConfig()
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
            raw = response.read()
            return response.status, json.loads(raw)
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


class _RecordingIndicator:
    available = True

    def __init__(self) -> None:
        self.events: list[str] = []
        self.paused = False

    def show(self) -> None:
        self.paused = False
        self.events.append("show")

    def hide(self) -> None:
        self.paused = False
        self.events.append("hide")

    def set_paused(self, paused: bool) -> None:
        self.paused = bool(paused)
        self.events.append("paused" if paused else "resumed")


def test_bootstrap_returns_token_and_config(server):
    app, url = server
    status, data = _request(url + "api/bootstrap")
    assert status == 200
    assert data["token"] == app.token
    assert data["profile"]["display_name"]
    assert data["config"]["adapters"] == ["mock"]
    assert data["config"]["model"]["api_key_env"]
    assert data["hotkey"] == {"enabled": False, "key": "F8"}
    assert data["screen_indicator"] is False


def test_bootstrap_reports_native_hotkey(server):
    app, url = server
    app.native_hotkey = True
    _, data = _request(url + "api/bootstrap")
    assert data["hotkey"] == {"enabled": True, "key": "F8"}


def test_screen_indicator_wraps_desktop_run(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.adapters["windows"] = AdapterConfig(kind="windows_mcp")
    indicator = _RecordingIndicator()
    app = UiServer(
        config,
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
        indicator=indicator,
    )
    monkeypatch.setattr(server_module, "build_adapter", lambda *a, **k: MockAdapter())
    monkeypatch.setattr(
        server_module, "build_provider", lambda *a, **k: HeuristicMockProvider()
    )
    app.start()
    try:
        app._active.acquire()
        run_id = app._launch_run("输入 hello 并保存", "windows", True, None, "ai")
        deadline = time.time() + 10
        while time.time() < deadline:
            stored = app.storage.get_run(run_id)
            if stored is not None and stored["status"] != "running":
                break
            time.sleep(0.05)
        assert stored["status"] == "done"
        assert indicator.events == ["show", "hide"]
    finally:
        app.stop()


def test_mock_run_does_not_show_screen_indicator(server):
    app, url = server
    indicator = _RecordingIndicator()
    app.indicator = indicator
    status, started = _request(
        url + "api/runs",
        method="POST",
        body={"goal": "输入 hello 并保存", "adapter": "mock", "auto_confirm": True},
        token=app.token,
    )
    assert status == 202
    deadline = time.time() + 10
    while time.time() < deadline:
        _, run = _request(url + f"api/runs/{started['id']}")
        if run["status"] != "running":
            break
        time.sleep(0.05)
    assert run["status"] == "done"
    assert indicator.events == []


def _wait_run(app, run_id, timeout=10.0):
    deadline = time.time() + timeout
    stored = app.storage.get_run(run_id)
    while time.time() < deadline:
        stored = app.storage.get_run(run_id)
        if stored is not None and stored["status"] != "running":
            break
        time.sleep(0.05)
    return stored


def _create_provider_model(app, url, *, model_id="llama3.2"):
    status, data = _request(
        url + "api/ai/providers/save",
        method="POST",
        body={
            "preset_id": "ollama",
            "plan_id": "local",
            "name": "本机 Ollama",
        },
        token=app.token,
    )
    assert status == 200
    provider_id = data["provider"]["id"]
    status, data = _request(
        url + "api/ai/models/add",
        method="POST",
        body={"provider_id": provider_id, "model_id": model_id},
        token=app.token,
    )
    assert status == 200
    return provider_id, model_id


def test_first_run_uses_goal_fallback_title(server):
    app, url = server
    _, data = _request(
        url + "api/conversations",
        method="POST",
        body={"adapter": "mock"},
        token=app.token,
    )
    conversation_id = data["conversation"]["id"]
    goal = "输入 hello 并保存到桌面文件"
    _, started = _request(
        url + "api/runs",
        method="POST",
        body={
            "goal": goal,
            "adapter": "mock",
            "auto_confirm": True,
            "conversation_id": conversation_id,
        },
        token=app.token,
    )
    assert started["queued"] is False
    _wait_run(app, started["id"])
    stored = app.storage.get_conversation(
        conversation_id, include_messages=False
    )
    assert stored["title"] == goal[:24]
    assert stored["title_source"] == "auto"


def test_ai_title_is_generated_after_first_run(tmp_path, monkeypatch):
    class TitledProvider(HeuristicMockProvider):
        def complete(self, system, user, *, max_tokens=800, on_delta=None):
            return '"整理桌面文件"'

    app = UiServer(
        _config(tmp_path),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
    )
    monkeypatch.setattr(
        server_module, "build_provider", lambda *a, **k: TitledProvider()
    )
    url = app.start()
    try:
        _, data = _request(
            url + "api/conversations",
            method="POST",
            body={"adapter": "mock"},
            token=app.token,
        )
        conversation_id = data["conversation"]["id"]
        _, started = _request(
            url + "api/runs",
            method="POST",
            body={
                "goal": "把桌面文件按类型整理到文件夹",
                "adapter": "mock",
                "auto_confirm": True,
                "conversation_id": conversation_id,
            },
            token=app.token,
        )
        _wait_run(app, started["id"])
        deadline = time.time() + 5
        stored = app.storage.get_conversation(
            conversation_id, include_messages=False
        )
        while time.time() < deadline and stored["title"] != "整理桌面文件":
            time.sleep(0.05)
            stored = app.storage.get_conversation(
                conversation_id, include_messages=False
            )
        assert stored["title"] == "整理桌面文件"
        assert stored["title_source"] == "auto"
    finally:
        app.stop()


def test_manual_title_is_not_overwritten_by_ai(tmp_path, monkeypatch):
    class TitledProvider(HeuristicMockProvider):
        def complete(self, system, user, *, max_tokens=800, on_delta=None):
            return "自动标题"

    app = UiServer(
        _config(tmp_path),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
    )
    monkeypatch.setattr(
        server_module, "build_provider", lambda *a, **k: TitledProvider()
    )
    url = app.start()
    try:
        _, data = _request(
            url + "api/conversations",
            method="POST",
            body={"adapter": "mock"},
            token=app.token,
        )
        conversation_id = data["conversation"]["id"]
        status, data = _request(
            url + "api/conversations/update",
            method="POST",
            body={"id": conversation_id, "title": "我的固定标题"},
            token=app.token,
        )
        assert status == 200
        assert data["conversation"]["title_source"] == "user"

        _, started = _request(
            url + "api/runs",
            method="POST",
            body={
                "goal": "随便跑一个任务",
                "adapter": "mock",
                "auto_confirm": True,
                "conversation_id": conversation_id,
            },
            token=app.token,
        )
        _wait_run(app, started["id"])
        stored = app.storage.get_conversation(
            conversation_id, include_messages=False
        )
        assert stored["title"] == "我的固定标题"
        assert stored["title_source"] == "user"
    finally:
        app.stop()


def test_runs_stay_inside_their_conversation(server):
    app, url = server
    ids = []
    for _ in range(2):
        _, data = _request(
            url + "api/conversations",
            method="POST",
            body={"adapter": "mock"},
            token=app.token,
        )
        ids.append(data["conversation"]["id"])

    _, started = _request(
        url + "api/runs",
        method="POST",
        body={
            "goal": "只在第一个任务里执行",
            "adapter": "mock",
            "auto_confirm": True,
            "conversation_id": ids[0],
        },
        token=app.token,
    )
    _wait_run(app, started["id"])
    first = app.storage.get_conversation(ids[0])
    second = app.storage.get_conversation(ids[1])
    assert [message["role"] for message in first["messages"]] == [
        "user",
        "assistant",
    ]
    assert second["messages"] == []
    assert second["title"] == ""


def test_conversation_context_endpoint(server):
    app, url = server
    _, data = _request(
        url + "api/conversations",
        method="POST",
        body={"adapter": "mock"},
        token=app.token,
    )
    conversation_id = data["conversation"]["id"]
    app.storage.add_message(conversation_id, "user", "第一步目标", "goal")
    app.storage.add_message(conversation_id, "assistant", "第一步结果", "result")
    app.storage.save_task_memory(conversation_id, "记忆：第一步已完成", 1)

    status, context = _request(
        url + f"api/conversations/{conversation_id}/context"
    )
    assert status == 200
    assert context["conversation_id"] == conversation_id
    assert context["summary"] == "记忆：第一步已完成"
    assert [item["content"] for item in context["recent"]] == [
        "第一步目标",
        "第一步结果",
    ]
    assert context["budget_tokens"] > 0
    assert context["total_messages"] == 2

    status, _ = _request(url + "api/conversations/missing/context")
    assert status == 404


def test_second_run_receives_conversation_context(tmp_path, monkeypatch):
    class ContextRecordingProvider(HeuristicMockProvider):
        contexts: list[str] = []

        def decide(
            self,
            goal,
            observation,
            history,
            remaining_steps,
            *,
            instructions=None,
            on_delta=None,
            context="",
        ):
            type(self).contexts.append(context)
            return super().decide(
                goal,
                observation,
                history,
                remaining_steps,
                instructions=instructions,
                on_delta=on_delta,
            )

    app = UiServer(
        _config(tmp_path),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
    )
    monkeypatch.setattr(
        server_module,
        "build_provider",
        lambda *a, **k: ContextRecordingProvider(),
    )
    url = app.start()
    try:
        _, data = _request(
            url + "api/conversations",
            method="POST",
            body={"adapter": "mock"},
            token=app.token,
        )
        conversation_id = data["conversation"]["id"]

        def run(goal):
            _, started = _request(
                url + "api/runs",
                method="POST",
                body={
                    "goal": goal,
                    "adapter": "mock",
                    "auto_confirm": True,
                    "conversation_id": conversation_id,
                },
                token=app.token,
            )
            _wait_run(app, started["id"])
            deadline = time.time() + 5
            while time.time() < deadline:
                detail = app.storage.get_conversation(conversation_id)
                if any(
                    message["role"] == "assistant" for message in detail["messages"]
                ) and len(detail["messages"]) >= 2:
                    break
                time.sleep(0.05)

        run("第一步：输入 hello 并保存")
        ContextRecordingProvider.contexts.clear()
        run("第二步：再保存一次")

        assert any(
            "第一步：输入 hello 并保存" in context
            for context in ContextRecordingProvider.contexts
        )
    finally:
        app.stop()


def test_static_index_is_served(server):
    _, url = server
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=5) as response:
        html = response.read().decode("utf-8")
        assert response.status == 200
        assert "XM Agent" in html
        assert "text/html" in response.headers["Content-Type"]


def test_post_requires_token(server):
    _, url = server
    status, data = _request(
        url + "api/profile", method="POST", body={"display_name": "hacker"}
    )
    assert status == 401
    assert "token" in data["error"]


def test_profile_update_validates_adapter(server):
    app, url = server
    status, _ = _request(
        url + "api/profile",
        method="POST",
        body={"display_name": "本地用户", "default_adapter": "missing"},
        token=app.token,
    )
    assert status == 400

    status, data = _request(
        url + "api/profile",
        method="POST",
        body={"display_name": "小明", "default_adapter": "mock"},
        token=app.token,
    )
    assert status == 200
    assert data["display_name"] == "小明"
    assert app.storage.get_profile()["display_name"] == "小明"


def test_run_completes_and_is_persisted(server):
    app, url = server
    status, started = _request(
        url + "api/runs",
        method="POST",
        body={
            "goal": "输入 hello 并保存",
            "adapter": "mock",
            "auto_confirm": True,
        },
        token=app.token,
    )
    assert status == 202

    run = None
    deadline = time.time() + 10
    while time.time() < deadline:
        status, run = _request(url + f"api/runs/{started['id']}")
        assert status == 200
        if run["status"] != "running":
            break
        time.sleep(0.05)

    assert run["status"] == "done"
    assert run["steps"] >= 1
    events = [event["event"] for event in run["events"]]
    assert "start" in events
    assert "result" in events

    stored = app.storage.get_run(started["id"])
    assert stored["status"] == "done"
    assert stored["finished_at"]


def test_finished_run_sends_system_notification(tmp_path):
    class _RecordingNotifier:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def notify(self, title, body=""):
            self.calls.append((title, body))

    notifier = _RecordingNotifier()
    app = UiServer(
        _config(tmp_path),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
        notifier=notifier,
    )
    url = app.start()
    try:
        _, started = _request(
            url + "api/runs",
            method="POST",
            body={
                "goal": "输入 hello 并保存",
                "adapter": "mock",
                "auto_confirm": True,
            },
            token=app.token,
        )
        stored = _wait_run(app, started["id"])
        assert stored["status"] == "done"
    finally:
        app.stop()

    assert len(notifier.calls) == 1
    title, body = notifier.calls[0]
    assert title.startswith("任务完成 · ")
    assert "输入 hello" in title
    assert body


def test_run_requires_goal(server):
    app, url = server
    status, data = _request(
        url + "api/runs",
        method="POST",
        body={"goal": "  ", "adapter": "mock"},
        token=app.token,
    )
    assert status == 400
    assert "goal" in data["error"]


def test_unknown_run_returns_404(server):
    _, url = server
    status, _ = _request(url + "api/runs/nope")
    assert status == 404


def test_rejects_unexpected_host(server):
    app, url = server
    port = app.port
    request = urllib.request.Request(
        url + "api/health", headers={"Host": "evil.example"}
    )
    try:
        urllib.request.urlopen(request, timeout=5)
        raise AssertionError("request should have been rejected")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403
    assert port > 0


def test_ai_provider_save_encrypts_and_never_returns_key(server):
    app, url = server
    status, data = _request(
        url + "api/ai/providers/save",
        method="POST",
        body={
            "preset_id": "custom",
            "plan_id": "openai",
            "name": "测试网关",
            "protocol": "openai",
            "base_url": "http://127.0.0.1:9/v1",
            "models_style": "openai",
            "requires_key": True,
            "api_key": "sk-super-secret",
        },
        token=app.token,
    )
    assert status == 200
    provider = data["provider"]
    assert provider["has_key"] is True
    assert "api_key_enc" not in provider
    encoded = json.dumps(data, ensure_ascii=False)
    assert "sk-super-secret" not in encoded

    raw = app.storage.get_ai_provider(provider["id"], include_secret=True)
    assert raw["api_key_enc"] != "sk-super-secret"
    assert decrypt_secret(raw["api_key_enc"]) == "sk-super-secret"

    status, overview = _request(url + "api/ai")
    assert status == 200
    dumped = json.dumps(overview, ensure_ascii=False)
    assert "sk-super-secret" not in dumped
    assert "api_key_enc" not in dumped
    assert overview["providers"][0]["has_key"] is True

    status, data = _request(
        url + "api/ai/providers/save",
        method="POST",
        body={"id": provider["id"], "clear_key": True},
        token=app.token,
    )
    assert status == 200
    assert data["provider"]["has_key"] is False


def test_ai_model_active_and_usage_endpoints(server):
    app, url = server
    status, data = _request(
        url + "api/ai/providers/save",
        method="POST",
        body={
            "preset_id": "ollama",
            "plan_id": "local",
            "name": "本机 Ollama",
        },
        token=app.token,
    )
    assert status == 200
    provider_id = data["provider"]["id"]

    status, data = _request(
        url + "api/ai/models/add",
        method="POST",
        body={
            "provider_id": provider_id,
            "model_id": "llama3.2",
            "display_name": "Llama 3.2",
            "vision": True,
        },
        token=app.token,
    )
    assert status == 200
    assert data["model"]["vision"] is False
    assert data["ai"]["active"]["model"] == "llama3.2"

    status, data = _request(
        url + "api/ai/active",
        method="POST",
        body={"provider_id": provider_id, "model_id": "llama3.2"},
        token=app.token,
    )
    assert status == 200
    assert data["active"]["provider_name"] == "本机 Ollama"

    status, usage = _request(url + "api/ai/usage")
    assert status == 200
    assert usage["totals"]["calls"] == 0
    assert usage["by_model"] == []
    assert usage["by_source"] == []

    status, bootstrap = _request(url + "api/bootstrap")
    assert status == 200
    assert bootstrap["ai"]["active"]["model"] == "llama3.2"
    assert "api_key_enc" not in json.dumps(bootstrap)


def test_audit_model_selection_roundtrip(server):
    app, url = server
    provider_id, model_id = _create_provider_model(
        app, url, model_id="llava"
    )

    status, data = _request(
        url + "api/audit/model",
        method="POST",
        body={"provider_id": provider_id, "model_id": model_id},
        token=app.token,
    )
    assert status == 200
    assert data["audit"]["follow_executor"] is False
    assert data["audit"]["model_provider_id"] == provider_id
    assert data["audit"]["model_id"] == model_id
    assert app.storage.get_audit_model() == (provider_id, model_id)

    _, bootstrap = _request(url + "api/bootstrap")
    assert bootstrap["audit"]["follow_executor"] is False
    assert bootstrap["ai_choices"][0]["id"] == provider_id
    assert bootstrap["ai_choices"][0]["models"][0]["model_id"] == model_id

    status, _ = _request(
        url + "api/audit/model",
        method="POST",
        body={"provider_id": provider_id, "model_id": ""},
        token=app.token,
    )
    assert status == 400
    assert app.storage.get_audit_model() == (provider_id, model_id)

    status, data = _request(
        url + "api/audit/model",
        method="POST",
        body={"provider_id": "", "model_id": ""},
        token=app.token,
    )
    assert status == 200
    assert data["audit"]["follow_executor"] is True
    assert app.storage.get_audit_model() == ("", "")


def test_audit_model_must_see_images(server):
    app, url = server
    provider_id, model_id = _create_provider_model(app, url)

    status, data = _request(
        url + "api/audit/model",
        method="POST",
        body={"provider_id": provider_id, "model_id": model_id},
        token=app.token,
    )

    assert status == 400
    assert "识图" in data["error"]
    assert app.storage.get_audit_model() == ("", "")


def test_deleting_model_clears_audit_selection(server):
    app, url = server
    provider_id, model_id = _create_provider_model(
        app, url, model_id="llava"
    )
    _request(
        url + "api/audit/model",
        method="POST",
        body={"provider_id": provider_id, "model_id": model_id},
        token=app.token,
    )
    assert app.storage.get_audit_model() == (provider_id, model_id)

    status, _ = _request(
        url + "api/ai/models/delete",
        method="POST",
        body={"provider_id": provider_id, "model_id": model_id},
        token=app.token,
    )
    assert status == 200
    assert app.storage.get_audit_model() == ("", "")


def test_queued_run_keeps_model_selection(server):
    app, url = server
    provider_id, model_id = _create_provider_model(app, url)
    _, data = _request(
        url + "api/conversations",
        method="POST",
        body={"adapter": "mock"},
        token=app.token,
    )
    conversation_id = data["conversation"]["id"]

    app._active.acquire()
    try:
        status, started = _request(
            url + "api/runs",
            method="POST",
            body={
                "goal": "输入 hello",
                "adapter": "mock",
                "conversation_id": conversation_id,
                "model_provider_id": provider_id,
                "model_id": model_id,
            },
            token=app.token,
        )
        assert status == 202
        assert started["queued"] is True
    finally:
        app._active.release()

    task = app.storage.get_queue_task(started["task"]["id"])
    assert task["model_provider_id"] == provider_id
    assert task["model_id"] == model_id
    app.storage.set_queue_status(task["id"], "cancelled")


def test_auto_run_emits_route_event(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.adapters["windows"] = AdapterConfig(kind="windows_mcp")
    app = UiServer(
        config,
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
    )
    monkeypatch.setattr(
        server_module,
        "choose_primary",
        lambda *a, **k: ("windows", "AI 选择 Windows-MCP"),
    )
    monkeypatch.setattr(
        server_module, "build_adapter", lambda *a, **k: MockAdapter()
    )
    monkeypatch.setattr(
        server_module, "build_provider", lambda *a, **k: HeuristicMockProvider()
    )
    url = app.start()
    try:
        _, bootstrap = _request(url + "api/bootstrap")
        assert bootstrap["config"]["auto_available"] is True

        status, data = _request(
            url + "api/conversations",
            method="POST",
            body={"adapter": "auto"},
            token=app.token,
        )
        assert status == 201
        conversation_id = data["conversation"]["id"]

        status, started = _request(
            url + "api/runs",
            method="POST",
            body={
                "goal": "打开记事本",
                "adapter": "auto",
                "auto_confirm": True,
                "conversation_id": conversation_id,
            },
            token=app.token,
        )
        assert status == 202
        assert started["queued"] is False
        _wait_run(app, started["id"])

        run = app.get_run(started["id"])
        assert run["resolved_adapter"] == "windows"
        routes = [
            event for event in run["events"] if event["event"] == "route"
        ]
        assert routes
        assert routes[0]["payload"]["adapter"] == "windows"
        assert routes[0]["payload"]["reason"] == "AI 选择 Windows-MCP"
        assert routes[0]["payload"]["chain"] == ["windows"]
    finally:
        app.stop()


def test_auto_adapter_rejected_without_real_backend(server):
    app, url = server
    status, _ = _request(
        url + "api/conversations",
        method="POST",
        body={"adapter": "auto"},
        token=app.token,
    )
    assert status == 400


def test_conversation_crud_endpoints(server):
    app, url = server
    status, data = _request(
        url + "api/conversations",
        method="POST",
        body={"title": "清理任务", "adapter": "mock", "audit_mode": "ai"},
        token=app.token,
    )
    assert status == 201
    conversation = data["conversation"]
    assert conversation["audit_mode"] == "ai"

    status, listed = _request(url + "api/conversations")
    assert status == 200
    assert listed["conversations"][0]["id"] == conversation["id"]

    status, data = _request(
        url + "api/conversations/update",
        method="POST",
        body={"id": conversation["id"], "audit_mode": "confirm_all"},
        token=app.token,
    )
    assert status == 200
    assert data["conversation"]["audit_mode"] == "confirm_all"

    status, detail = _request(url + f"api/conversations/{conversation['id']}")
    assert status == 200
    assert detail["messages"] == []
    assert detail["queue"] == []

    status, _ = _request(
        url + "api/conversations/update",
        method="POST",
        body={"id": conversation["id"], "audit_mode": "off"},
        token=app.token,
    )
    assert status == 400

    status, _ = _request(
        url + "api/conversations/delete",
        method="POST",
        body={"id": conversation["id"]},
        token=app.token,
    )
    assert status == 200
    status, _ = _request(url + f"api/conversations/{conversation['id']}")
    assert status == 404


def test_auto_confirm_does_not_bypass_mandatory_review(server, monkeypatch):
    app, _ = server
    calls: list[str] = []

    def fake_confirm(run_id, action, prompt):
        calls.append(prompt)
        return False

    monkeypatch.setattr(app, "request_confirmation", fake_confirm)
    action = Action("terminal.exec", {"command": "type C:/other/notes.txt"})
    assert (
        app._confirm_action("run-1", action, "prompt", False, True) is True
    )
    assert app._confirm_action("run-1", action, "prompt", True, True) is False
    assert calls == ["prompt"]


def test_paused_run_state_is_reported(server):
    app, url = server
    _, data = _request(
        url + "api/conversations",
        method="POST",
        body={"title": "暂停任务", "adapter": "mock"},
        token=app.token,
    )
    conversation_id = data["conversation"]["id"]
    run_id = "run-paused"
    app.storage.create_run(run_id, "暂停测试", "mock", conversation_id)
    control = _register_running(app, run_id)
    control.pause("用户暂停")

    _, detail = _request(url + f"api/conversations/{conversation_id}")
    runs = [run for run in detail["runs"] if run["id"] == run_id]
    assert runs and runs[0]["paused"] is True
    assert runs[0]["pause_reason"] == "用户暂停"

    _, run = _request(url + f"api/runs/{run_id}")
    assert run["paused"] is True

    control.resume()
    _, detail = _request(url + f"api/conversations/{conversation_id}")
    runs = [run for run in detail["runs"] if run["id"] == run_id]
    assert runs and runs[0]["paused"] is False


def test_busy_task_is_queued_and_then_executed(server):
    app, url = server
    _, data = _request(
        url + "api/conversations",
        method="POST",
        body={"title": "", "adapter": "mock"},
        token=app.token,
    )
    conversation_id = data["conversation"]["id"]

    app._active.acquire()
    try:
        status, started = _request(
            url + "api/runs",
            method="POST",
            body={
                "goal": "输入 hello 并保存",
                "adapter": "mock",
                "conversation_id": conversation_id,
            },
            token=app.token,
        )
        assert status == 202
        assert started["queued"] is True
        task_id = started["task"]["id"]

        status, queue_data = _request(url + "api/queue")
        assert [item["id"] for item in queue_data["queued"]] == [task_id]

        status, _ = _request(
            url + "api/queue/jump",
            method="POST",
            body={"id": task_id},
            token=app.token,
        )
        assert status == 200
    finally:
        app._active.release()

    app._start_next_queued()
    deadline = time.time() + 10
    while time.time() < deadline:
        task = app.storage.get_queue_task(task_id)
        if task["status"] in {"done", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert task["status"] == "done"

    _, detail = _request(url + f"api/conversations/{conversation_id}")
    roles = [message["role"] for message in detail["messages"]]
    assert roles == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "输入 hello 并保存"
    assert detail["messages"][1]["payload"]["status"] == "done"


def test_sse_replays_conversation_events(server):
    app, url = server
    _, data = _request(
        url + "api/conversations",
        method="POST",
        body={"title": "回放", "adapter": "mock"},
        token=app.token,
    )
    conversation_id = data["conversation"]["id"]
    app.storage.append_run_event(
        "run-sse", conversation_id, "step", {"step": 1}
    )

    request = urllib.request.Request(
        url
        + f"api/events?token={app.token}&conversation_id={conversation_id}"
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert "text/event-stream" in response.headers["Content-Type"]
        messages = []
        while len(messages) < 1:
            line = response.readline()
            if not line:
                break
            text = line.decode("utf-8").rstrip("\n")
            if text.startswith("data: "):
                messages.append(json.loads(text[6:]))

    assert messages[0]["event"] == "step"
    assert messages[0]["payload"] == {"step": 1}


def test_sse_rejects_invalid_token(server):
    _, url = server
    request = urllib.request.Request(
        url + "api/events?token=wrong&conversation_id=x"
    )
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=5)
    assert info.value.code == 401


def test_toggle_pause_active_follows_foreground_run(server):
    app, _ = server
    assert app.toggle_pause_active() is False

    control = _register_running(app, "run-hotkey")
    assert app.toggle_pause_active() is True
    assert control.paused is True
    assert app.toggle_pause_active() is True
    assert control.paused is False

    _register_running(app, "run-hotkey-2")
    assert app.toggle_pause_active() is False

    app._controls.pop("run-hotkey", None)
    app._controls.pop("run-hotkey-2", None)


def test_pause_and_resume_update_screen_indicator(server):
    app, _ = server
    app.indicator = _RecordingIndicator()
    control = _register_running(app, "run-indicator")
    try:
        assert app.pause_run("run-indicator") is True
        assert app.indicator.paused is True
        assert app.resume_run("run-indicator") is True
        assert app.indicator.paused is False
        assert app.indicator.events == ["paused", "resumed"]
    finally:
        app._controls.pop("run-indicator", None)


def test_indicator_and_hotkey_disable_off_demand():
    assert ScreenIndicator(enabled=False).start() is False
    assert GlobalHotkey(lambda: None, enabled=False).start() is False


def test_run_control_endpoints(server):
    app, url = server
    control = RunControl()
    app._controls["run-control"] = control
    try:
        status, _ = _request(
            url + "api/runs/run-control/pause",
            method="POST",
            body={},
            token=app.token,
        )
        assert status == 200
        assert control.paused is True

        status, data = _request(
            url + "api/runs/run-control/instruct",
            method="POST",
            body={"text": "改用其他目录"},
            token=app.token,
        )
        assert status == 200
        assert control.drain_instructions() == ["改用其他目录"]

        status, _ = _request(
            url + "api/runs/run-control/resume",
            method="POST",
            body={},
            token=app.token,
        )
        assert status == 200
        assert control.paused is False

        status, _ = _request(
            url + "api/runs/run-control/cancel",
            method="POST",
            body={},
            token=app.token,
        )
        assert status == 200
        assert control.cancelled is True

        status, _ = _request(
            url + "api/runs/missing/pause",
            method="POST",
            body={},
            token=app.token,
        )
        assert status == 400
    finally:
        app._controls.pop("run-control", None)


def _register_running(app, run_id):
    control = RunControl()
    app._controls[run_id] = control
    with app._runs_lock:
        app._runs[run_id] = {
            "id": run_id,
            "goal": "",
            "adapter": "mock",
            "status": "running",
            "reason": "",
            "steps": 0,
            "model": "",
            "conversation_id": None,
            "audit_mode": "ai",
            "created_at": "",
            "events": [],
        }
    return control


def _wait_for_confirmation(app, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with app._confirm_lock:
            if app._confirm_requests:
                return next(iter(app._confirm_requests))
        time.sleep(0.02)
    return None


def _spawn_confirmation(app, run_id, results):
    thread = threading.Thread(
        target=lambda: results.append(
            app.request_confirmation(
                run_id,
                Action("desktop.act", {"action": "type", "text": "x"}),
                "操作：desktop.act:type x",
            )
        )
    )
    thread.start()
    return thread


def test_jump_preempts_direct_run_and_denies_pending_confirmation(server):
    app, url = server
    app._confirm_timeout = 5
    _, data = _request(
        url + "api/conversations",
        method="POST",
        body={"title": "", "adapter": "mock"},
        token=app.token,
    )
    conversation_id = data["conversation"]["id"]
    app._active.acquire()
    try:
        _, started = _request(
            url + "api/runs",
            method="POST",
            body={
                "goal": "排队任务",
                "adapter": "mock",
                "conversation_id": conversation_id,
            },
            token=app.token,
        )
    finally:
        app._active.release()
    assert started["queued"] is True

    control = _register_running(app, "run-direct")
    results = []
    thread = _spawn_confirmation(app, "run-direct", results)
    assert _wait_for_confirmation(app)

    status, _ = _request(
        url + "api/queue/jump",
        method="POST",
        body={"id": started["task"]["id"], "preempt": True},
        token=app.token,
    )
    thread.join(timeout=5)
    assert status == 200
    assert results == [False]
    with pytest.raises(RunPreempted):
        control.checkpoint()

    app._controls.pop("run-direct", None)
    with app._runs_lock:
        app._runs.pop("run-direct", None)


def test_cancel_run_denies_pending_confirmation(server):
    app, url = server
    app._confirm_timeout = 5
    control = _register_running(app, "run-cancel-confirm")
    results = []
    thread = _spawn_confirmation(app, "run-cancel-confirm", results)
    assert _wait_for_confirmation(app)

    status, _ = _request(
        url + "api/runs/run-cancel-confirm/cancel",
        method="POST",
        body={},
        token=app.token,
    )
    thread.join(timeout=5)
    assert status == 200
    assert control.cancelled is True
    assert results == [False]

    app._controls.pop("run-cancel-confirm", None)
    with app._runs_lock:
        app._runs.pop("run-cancel-confirm", None)


def test_confirmation_broker_roundtrip(server):
    app, url = server
    app._confirm_timeout = 5
    results = []

    def worker():
        results.append(
            app.request_confirmation(
                "run-confirm",
                Action("file.transfer", {"direction": "upload"}),
                "操作：file.transfer",
            )
        )

    thread = threading.Thread(target=worker)
    thread.start()
    request_id = None
    deadline = time.time() + 3
    while time.time() < deadline:
        with app._confirm_lock:
            if app._confirm_requests:
                request_id = next(iter(app._confirm_requests))
                break
        time.sleep(0.02)
    assert request_id

    status, _ = _request(
        url + "api/confirm",
        method="POST",
        body={"request_id": request_id, "approve": True},
        token=app.token,
    )
    thread.join(timeout=5)
    assert status == 200
    assert results == [True]

    status, _ = _request(
        url + "api/confirm",
        method="POST",
        body={"request_id": request_id, "approve": True},
        token=app.token,
    )
    assert status == 404


def test_resource_status_and_sync(server):
    app, url = server
    app.resources.resources = ()

    status, data = _request(url + "api/resources")
    assert status == 200
    assert data["resources"] == []
    assert data["job"] is None

    status, data = _request(
        url + "api/resources/sync",
        method="POST",
        body={},
        token=app.token,
    )
    assert status == 202
    assert data["job"] is not None

    deadline = time.time() + 5
    while time.time() < deadline:
        status, data = _request(url + "api/resources")
        if data["job"] and not data["job"]["running"]:
            break
        time.sleep(0.05)
    assert data["job"]["ok"] is True
    assert data["job"]["percent"] == 100


def _resource_state(
    resource_id: str,
    *,
    required: bool = True,
    status: str = "ready",
) -> ResourceState:
    resource = next(item for item in RESOURCES if item.id == resource_id)
    return ResourceState(
        id=resource.id,
        name=resource.name,
        version=resource.version,
        required=required,
        purpose=resource.purpose,
        status=status,
    )


def _job(job_id: str, rows: list[dict]) -> dict:
    return {
        "id": job_id,
        "running": True,
        "phase": "detecting",
        "percent": 1,
        "message": "正在检测资源",
        "resources": rows,
    }


def test_resources_snapshot_merges_cached_probe(server, monkeypatch):
    app, _ = server
    state = _resource_state("windows_mcp")
    monkeypatch.setattr(app.resources, "status", lambda: [state])
    app._resource_probes["windows_mcp"] = {
        "ok": True,
        "detail": "MCP 握手成功，共 1 个工具",
        "tools": ["Snapshot"],
        "checked_at": "2026-11-09T00:00:00+00:00",
    }

    row = app.resources_snapshot()["resources"][0]
    assert row["runtime_ok"] is True
    assert row["runtime_detail"] == "MCP 握手成功，共 1 个工具"
    assert row["runtime_tools"] == ["Snapshot"]
    assert row["runtime_checked_at"] == "2026-11-09T00:00:00+00:00"


def test_resource_sync_probes_ready_resources(server, monkeypatch):
    app, _ = server
    state = _resource_state("windows_mcp")
    monkeypatch.setattr(app.resources, "ensure", lambda **kwargs: [state])
    calls: list[str] = []

    def fake_probe(config, kind, timeout=20):
        calls.append(kind)
        return {
            "ok": True,
            "detail": "MCP 握手成功，共 1 个工具",
            "tools": ["Snapshot"],
        }

    monkeypatch.setattr(server_module, "probe_adapter_kind", fake_probe)
    job_id = "job-probe-ok"
    app._resource_job = _job(job_id, [state.to_dict()])

    app._run_resource_sync(job_id, [], False)

    assert calls == ["windows_mcp"]
    assert state.runtime_ok is True
    assert state.runtime_tools == ["Snapshot"]
    assert app._resource_probes["windows_mcp"]["ok"] is True
    job = app._resource_job
    assert job["running"] is False
    assert job["phase"] == "done"
    assert job["ok"] is True
    assert job["percent"] == 100
    assert "运行可用" in job["message"]
    assert job["resources"][0]["runtime_ok"] is True


def test_resource_sync_reports_runtime_failure(server, monkeypatch):
    app, _ = server
    state = _resource_state("windows_mcp")
    monkeypatch.setattr(app.resources, "ensure", lambda **kwargs: [state])

    def fake_probe(config, kind, timeout=20):
        return {
            "ok": False,
            "detail": "握手失败：MCP server closed stdout",
            "tools": [],
        }

    monkeypatch.setattr(server_module, "probe_adapter_kind", fake_probe)
    job_id = "job-probe-fail"
    app._resource_job = _job(job_id, [state.to_dict()])

    app._run_resource_sync(job_id, [], False)

    job = app._resource_job
    assert job["phase"] == "done"
    assert job["ok"] is False
    assert "不可用" in job["message"]
    assert "可强制重装失败项" in job["message"]
    assert job["resources"][0]["runtime_ok"] is False
    assert "closed stdout" in job["resources"][0]["runtime_detail"]


def test_await_user_handoff_and_resume(tmp_path, monkeypatch):
    class AwaitProvider(HeuristicMockProvider):
        def __init__(self) -> None:
            self.await_sent = False
            self.contexts: list[str] = []

        def decide(
            self,
            goal,
            observation,
            history,
            remaining_steps,
            *,
            instructions=None,
            on_delta=None,
            context="",
        ):
            self.contexts.append(context)
            if not self.await_sent:
                self.await_sent = True
                return Action(
                    "task.await_user",
                    {
                        "kind": "login",
                        "prompt": "请先登录测试系统",
                        "summary": "任务已暂停，等待你登录。请登录后输入“继续”。",
                    },
                )
            return Action("task.finish", {"summary": "已完成：通知已打开。"})

        def complete(self, system, user, *, max_tokens=800, on_delta=None):
            return "登录后查看通知"

    provider = AwaitProvider()
    app = UiServer(
        _config(tmp_path),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
    )
    monkeypatch.setattr(
        server_module, "build_provider", lambda *a, **k: provider
    )
    url = app.start()
    try:
        _, data = _request(
            url + "api/conversations",
            method="POST",
            body={"adapter": "mock"},
            token=app.token,
        )
        conversation_id = data["conversation"]["id"]
        original_goal = "查看测试系统的通知"
        _, started = _request(
            url + "api/runs",
            method="POST",
            body={
                "goal": original_goal,
                "adapter": "mock",
                "auto_confirm": True,
                "conversation_id": conversation_id,
            },
            token=app.token,
        )
        stored = _wait_run(app, started["id"])
        assert stored["status"] == "needs_human"
        conversation = app.storage.get_conversation(conversation_id)
        last = conversation["messages"][-1]
        assert last["role"] == "assistant"
        assert last["payload"]["await_user"]["kind"] == "login"
        assert "请先登录测试系统" in last["payload"]["await_user"]["prompt"]

        status, resumed = _request(
            url + "api/runs",
            method="POST",
            body={
                "goal": "继续",
                "adapter": "mock",
                "auto_confirm": True,
                "conversation_id": conversation_id,
                "resume": True,
            },
            token=app.token,
        )
        assert status == 202
        resumed_run = _wait_run(app, resumed["id"])
        assert resumed_run["status"] == "done"
        assert resumed_run["goal"] == original_goal
        messages = app.storage.list_messages(conversation_id)
        assert any(
            message["role"] == "user" and message["content"] == "继续"
            for message in messages
        )
        assert "用户已完成暂停事项（登录）" in provider.contexts[-1]
    finally:
        app.stop()


def test_resume_without_pending_handoff_is_rejected(server):
    app, url = server
    _, data = _request(
        url + "api/conversations",
        method="POST",
        body={"adapter": "mock"},
        token=app.token,
    )
    status, body = _request(
        url + "api/runs",
        method="POST",
        body={
            "goal": "继续",
            "adapter": "mock",
            "conversation_id": data["conversation"]["id"],
            "resume": True,
        },
        token=app.token,
    )
    assert status == 400
    assert "没有等待你处理的任务" in body["error"]


def test_resources_snapshot_ignores_probe_for_missing_resource(server, monkeypatch):
    app, _ = server
    state = _resource_state("windows_mcp", status="missing")
    monkeypatch.setattr(app.resources, "status", lambda: [state])
    app._resource_probes["windows_mcp"] = {
        "ok": True,
        "detail": "MCP 握手成功",
        "tools": ["Snapshot"],
        "checked_at": "2026-11-09T00:00:00+00:00",
    }

    row = app.resources_snapshot()["resources"][0]
    assert row["runtime_ok"] is None
    assert row["runtime_detail"] == ""
