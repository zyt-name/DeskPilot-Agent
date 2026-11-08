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

    def show(self) -> None:
        self.events.append("show")

    def hide(self) -> None:
        self.events.append("hide")


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

    status, bootstrap = _request(url + "api/bootstrap")
    assert status == 200
    assert bootstrap["ai"]["active"]["model"] == "llama3.2"
    assert "api_key_enc" not in json.dumps(bootstrap)


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
