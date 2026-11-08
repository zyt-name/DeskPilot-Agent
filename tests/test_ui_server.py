from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

from xm_agent.ai.crypto import decrypt_secret
from xm_agent.config import AdapterConfig, AgentConfig
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


def test_bootstrap_returns_token_and_config(server):
    app, url = server
    status, data = _request(url + "api/bootstrap")
    assert status == 200
    assert data["token"] == app.token
    assert data["profile"]["display_name"]
    assert data["config"]["adapters"] == ["mock"]
    assert data["config"]["model"]["api_key_env"]


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
            "vision": False,
        },
        token=app.token,
    )
    assert status == 200
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
