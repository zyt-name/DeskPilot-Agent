from __future__ import annotations

import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import pytest

from xm_agent.ai.catalog import (
    PRESETS,
    Plan,
    is_vision_model,
    plan_by_id,
    preset_by_id,
    preset_endpoint,
)
from xm_agent.ai.crypto import decrypt_secret, encrypt_secret, secret_hint
from xm_agent.ai.factory import build_active_provider
from xm_agent.ai.models import ModelListError, fetch_models
from xm_agent.ai.service import AiService
from xm_agent.bootstrap import build_provider
from xm_agent.config import AgentConfig
from xm_agent.providers.anthropic import AnthropicProvider
from xm_agent.providers.common import supports_temperature
from xm_agent.providers.fake import HeuristicMockProvider
from xm_agent.providers.openai_compat import OpenAICompatProvider
from xm_agent.storage import Storage
from xm_agent.types import Observation

Response = dict[str, Any] | Callable[[BaseHTTPRequestHandler, bytes], dict[str, Any]]


class _MockApi:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], Response] = {}
        self.requests: list[dict[str, Any]] = []

    def add(self, method: str, path: str, response: Response) -> None:
        self.responses[(method, path)] = response

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        self.requests.append(
            {
                "method": handler.command,
                "path": handler.path,
                "headers": dict(handler.headers),
                "body": body,
            }
        )
        response = self.responses.get((handler.command, handler.path))
        if response is None:
            data = json.dumps({"error": "not found"}).encode("utf-8")
            status = 404
        else:
            if callable(response):
                response = response(handler, body)
            data = json.dumps(response, ensure_ascii=False).encode("utf-8")
            status = 200
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    port: int = 0


@contextmanager
def _mock_server():
    api = _MockApi()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            api.handle(self)

        def do_POST(self) -> None:  # noqa: N802
            api.handle(self)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    api.port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield api
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _custom_openai(service: AiService, api: _MockApi) -> dict[str, Any]:
    return service.save_provider(
        {
            "preset_id": "custom",
            "plan_id": "openai",
            "name": "本地 Mock",
            "protocol": "openai",
            "base_url": api.base,
            "models_style": "openai",
            "requires_key": True,
            "api_key": "sk-local-test",
        }
    )


def test_catalog_covers_vendors_plans_and_vision():
    ids = {preset.id for preset in PRESETS}
    for expected in {
        "openai",
        "anthropic",
        "deepseek",
        "glm",
        "kimi",
        "gemini",
        "grok",
        "opencode",
        "ollama",
        "custom",
    }:
        assert expected in ids

    coding = plan_by_id("glm", "cn-coding")
    assert coding is not None
    assert coding.protocol == "anthropic"
    assert coding.base_url.endswith("/api/anthropic/v1")
    assert preset_endpoint("glm", "cn-coding")["base_url"] == coding.base_url
    assert preset_endpoint("custom", "openai") is None
    assert plan_by_id("opencode", "go") is not None
    ollama = preset_by_id("ollama")
    assert ollama is not None and ollama.plans[0].requires_key is False

    assert is_vision_model("gpt-4o-mini")
    assert is_vision_model("claude-3-5-sonnet-latest")
    assert is_vision_model("glm-4v")
    assert not is_vision_model("deepseek-chat")
    assert not is_vision_model("gpt-3.5-turbo")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_crypto_roundtrip_and_hint():
    token = encrypt_secret("sk-abcdef123456")
    assert token != "sk-abcdef123456"
    assert decrypt_secret(token) == "sk-abcdef123456"
    assert secret_hint("sk-abcdef123456").endswith("3456")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_service_encrypts_key_and_keeps_it_on_blank_update(tmp_path):
    storage = Storage(tmp_path / "app.db")
    service = AiService(storage)
    saved = service.save_provider(
        {
            "preset_id": "deepseek",
            "plan_id": "standard",
            "name": "DeepSeek 主账号",
            "api_key": "sk-secret-value",
        }
    )
    raw = storage.get_ai_provider(saved["id"], include_secret=True)
    assert raw["api_key_enc"] and raw["api_key_enc"] != "sk-secret-value"
    assert decrypt_secret(raw["api_key_enc"]) == "sk-secret-value"
    assert "api_key_enc" not in saved

    updated = service.save_provider(
        {"id": saved["id"], "name": "改名", "api_key": ""}
    )
    assert updated["has_key"] is True
    assert decrypt_secret(
        storage.get_ai_provider(saved["id"], include_secret=True)["api_key_enc"]
    ) == "sk-secret-value"

    cleared = service.save_provider(
        {"id": saved["id"], "clear_key": True}
    )
    assert cleared["has_key"] is False


def test_fetch_models_marks_vision_and_test_records_usage(tmp_path):
    with _mock_server() as api:
        api.add(
            "GET",
            "/v1/models",
            {
                "data": [
                    {"id": "gpt-4o-mini"},
                    {"id": "text-chat"},
                    {"id": "text-embedding-3-small"},
                ]
            },
        )
        api.add(
            "POST",
            "/v1/chat/completions",
            {
                "choices": [{"message": {"content": "OK"}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )
        service = AiService(Storage(tmp_path / "app.db"))
        provider = _custom_openai(service, api)

        models = service.fetch_models(provider["id"])
        assert [model["model_id"] for model in models] == [
            "gpt-4o-mini",
            "text-chat",
        ]
        vision = {model["model_id"]: model["vision"] for model in models}
        assert vision["gpt-4o-mini"] is True
        assert vision["text-chat"] is False

        active = service.active_summary()
        assert active is not None and active["model"] == "gpt-4o-mini"

        result = service.test_provider(provider["id"], "gpt-4o-mini")
        assert result["ok"] is True
        assert result["usage"]["total_tokens"] == 5
        assert result["vision"] is True

        usage = service.usage()
        assert usage["totals"]["calls"] == 1
        assert usage["totals"]["total_tokens"] == 5
        assert usage["recent"][0]["source"] == "test"

        request = [
            item
            for item in api.requests
            if item["method"] == "POST" and item["path"] == "/v1/chat/completions"
        ][0]
        headers = {key.lower(): value for key, value in request["headers"].items()}
        assert headers.get("authorization") == "Bearer sk-local-test"


def test_model_list_error_payload_is_reported(tmp_path):
    with _mock_server() as api:
        api.add(
            "GET",
            "/v1/models",
            {"code": 500, "msg": "404 NOT_FOUND", "success": False},
        )
        service = AiService(Storage(tmp_path / "app.db"))
        provider = _custom_openai(service, api)
        with pytest.raises(ModelListError, match="404 NOT_FOUND"):
            service.fetch_models(provider["id"])
        assert service.storage.list_ai_models(provider["id"]) == []


def test_glm_coding_plan_falls_back_to_standard_endpoint(tmp_path, monkeypatch):
    with _mock_server() as api:
        api.add(
            "GET",
            "/anthropic/models?limit=100",
            {"code": 500, "msg": "404 NOT_FOUND", "success": False},
        )
        api.add(
            "GET",
            "/v1/models",
            {"data": [{"id": "glm-4.6"}, {"id": "glm-4v-plus"}]},
        )
        plan = Plan(
            id="cn-coding",
            name="国内 Coding Plan",
            protocol="anthropic",
            base_url=api.base.replace("/v1", "/anthropic"),
            models_style="anthropic",
            fallback_base_url=api.base,
            fallback_models_style="openai",
        )
        monkeypatch.setattr(
            "xm_agent.ai.models.plan_by_id",
            lambda preset_id, plan_id: plan,
        )
        provider = {
            "preset_id": "glm",
            "plan_id": "cn-coding",
            "base_url": api.base.replace("/v1", "/anthropic"),
            "models_style": "anthropic",
        }
        models = fetch_models(provider, "sk-test")
        assert [model["model_id"] for model in models] == [
            "glm-4.6",
            "glm-4v-plus",
        ]
        vision = {model["model_id"]: model["vision"] for model in models}
        assert vision["glm-4v-plus"] is True


def test_preset_plan_overrides_client_endpoint_fields(tmp_path):
    service = AiService(Storage(tmp_path / "app.db"))
    saved = service.save_provider(
        {
            "preset_id": "glm",
            "plan_id": "cn-coding",
            "name": "GLM Coding",
            "protocol": "openai",
            "base_url": "https://wrong.example/v1",
            "models_style": "ollama",
            "requires_key": False,
        }
    )
    assert saved["protocol"] == "anthropic"
    assert saved["base_url"] == "https://open.bigmodel.cn/api/anthropic/v1"
    assert saved["models_style"] == "anthropic"
    assert saved["requires_key"] is True


def test_legacy_preset_provider_is_repaired_on_overview(tmp_path):
    storage = Storage(tmp_path / "app.db")
    storage.save_ai_provider(
        {
            "id": "legacy-glm",
            "preset_id": "glm",
            "plan_id": "cn-coding",
            "name": "GLM Coding",
            "protocol": "openai",
            "base_url": "https://open.bigmodel.cn/api/anthropic",
            "models_style": "anthropic",
            "requires_key": True,
        }
    )
    service = AiService(storage)
    provider = service.overview()["providers"][0]
    assert provider["protocol"] == "anthropic"
    assert provider["base_url"] == "https://open.bigmodel.cn/api/anthropic/v1"
    repaired = storage.get_ai_provider("legacy-glm")
    assert repaired["protocol"] == "anthropic"
    assert repaired["base_url"].endswith("/api/anthropic/v1")


def test_active_provider_uses_plan_endpoint_for_stale_row(tmp_path):
    storage = Storage(tmp_path / "app.db")
    storage.save_ai_provider(
        {
            "id": "legacy-active",
            "preset_id": "glm",
            "plan_id": "cn-coding",
            "name": "GLM Coding",
            "protocol": "openai",
            "base_url": "https://open.bigmodel.cn/api/anthropic",
            "models_style": "anthropic",
            "requires_key": False,
            "api_key_enc": encrypt_secret("sk-test"),
            "api_key_hint": secret_hint("sk-test"),
        }
    )
    storage.add_ai_model("legacy-active", "glm-4.6", "glm-4.6", False)
    storage.set_active_ai("legacy-active", "glm-4.6")
    provider = build_active_provider(storage)
    assert isinstance(provider, AnthropicProvider)
    assert provider.base_url.endswith("/api/anthropic/v1")


def test_anthropic_provider_posts_to_v1_messages():
    with _mock_server() as api:
        api.add(
            "POST",
            "/anthropic/v1/messages",
            {
                "content": [{"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )
        provider = AnthropicProvider(
            base_url=api.base.replace("/v1", "/anthropic/v1"),
            model="glm-4.6",
            api_key="sk-test",
        )
        assert provider.ping()["ok"] is True
        assert api.requests[-1]["path"] == "/anthropic/v1/messages"


def test_anthropic_ping_accepts_thinking_only_response():
    with _mock_server() as api:
        api.add(
            "POST",
            "/v1/messages",
            {
                "content": [{"type": "thinking", "thinking": "OK"}],
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )
        provider = AnthropicProvider(
            base_url=api.base, model="glm-4.5", api_key="sk-test"
        )
        result = provider.ping()
        assert result["ok"] is True
        assert result["text"] == "OK"


def test_failed_business_response_does_not_record_usage(tmp_path):
    with _mock_server() as api:
        api.add(
            "POST",
            "/v1/chat/completions",
            {"code": 500, "msg": "404 NOT_FOUND", "success": False},
        )
        storage = Storage(tmp_path / "app.db")
        service = AiService(storage)
        provider = _custom_openai(service, api)
        service.add_model(provider["id"], "gpt-4o-mini")
        result = service.test_provider(provider["id"], "gpt-4o-mini")
        assert result["ok"] is False
        assert storage.ai_usage_summary()["totals"]["calls"] == 0


def test_usage_summary_ignores_zero_token_rows(tmp_path):
    storage = Storage(tmp_path / "app.db")
    storage.record_ai_usage("p1", "Provider", "model-a", 0, 0, 0)
    storage.record_ai_usage("p1", "Provider", "model-a", 10, 5, 15)
    summary = storage.ai_usage_summary()
    assert summary["totals"]["calls"] == 1
    assert summary["totals"]["total_tokens"] == 15
    assert len(summary["by_model"]) == 1
    assert len(summary["by_provider"]) == 1
    assert len(summary["recent"]) == 1


def test_empty_refresh_keeps_existing_models(tmp_path):
    with _mock_server() as api:
        api.add("GET", "/v1/models", {"data": [{"id": "gpt-4o-mini"}]})
        service = AiService(Storage(tmp_path / "app.db"))
        provider = _custom_openai(service, api)
        service.fetch_models(provider["id"])

        api.add("GET", "/v1/models", {"data": []})
        assert service.fetch_models(provider["id"]) == []
        assert [
            model["model_id"]
            for model in service.storage.list_ai_models(provider["id"])
        ] == ["gpt-4o-mini"]


def test_changing_endpoint_clears_fetched_models(tmp_path):
    with _mock_server() as api:
        api.add("GET", "/v1/models", {"data": [{"id": "gpt-4o-mini"}]})
        service = AiService(Storage(tmp_path / "app.db"))
        provider = _custom_openai(service, api)
        service.fetch_models(provider["id"])
        assert [
            model["model_id"]
            for model in service.storage.list_ai_models(provider["id"])
        ] == ["gpt-4o-mini"]
        assert service.active_summary()["model"] == "gpt-4o-mini"

        service.save_provider(
            {"id": provider["id"], "base_url": "http://127.0.0.1:1/v1"}
        )
        assert service.storage.list_ai_models(provider["id"]) == []
        assert service.active_summary() is None


def test_openai_provider_decide_parses_action(tmp_path):
    with _mock_server() as api:
        api.add(
            "POST",
            "/v1/chat/completions",
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "capability": "desktop.observe",
                                    "args": {},
                                    "rationale": "look first",
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )
        provider = OpenAICompatProvider(
            api.base, "gpt-4o-mini", api_key="sk-test"
        )
        action = provider.decide("查看屏幕", Observation(step=1), [], 3)
        assert action.capability == "desktop.observe"
        assert action.rationale == "look first"
        assert action.expect is None


def test_anthropic_provider_uses_messages_api(tmp_path):
    with _mock_server() as api:
        api.add(
            "POST",
            "/v1/messages",
            {
                "content": [{"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )
        service = AiService(Storage(tmp_path / "app.db"))
        provider = service.save_provider(
            {
                "preset_id": "custom",
                "plan_id": "anthropic",
                "name": "Claude 本地网关",
                "protocol": "anthropic",
                "base_url": api.base,
                "models_style": "anthropic",
                "api_key": "sk-ant-test",
            }
        )
        service.add_model(
            provider["id"], "claude-3-5-sonnet-latest", vision=True
        )
        result = service.test_provider(
            provider["id"], "claude-3-5-sonnet-latest"
        )
        assert result["ok"] is True
        assert result["usage"]["total_tokens"] == 3

        request = api.requests[-1]
        headers = {key.lower(): value for key, value in request["headers"].items()}
        assert request["path"] == "/v1/messages"
        assert headers.get("x-api-key") == "sk-ant-test"
        assert headers.get("anthropic-version") == "2023-06-01"
        payload = json.loads(request["body"].decode("utf-8"))
        assert payload["model"] == "claude-3-5-sonnet-latest"
        assert payload["max_tokens"] > 0


def test_disabled_requires_key_allows_local_ollama(tmp_path):
    storage = Storage(tmp_path / "app.db")
    service = AiService(storage)
    provider = service.save_provider(
        {
            "preset_id": "ollama",
            "plan_id": "local",
            "name": "本机 Ollama",
        }
    )
    assert provider["requires_key"] is False
    service.add_model(provider["id"], "llama3.2", vision=False)
    built = build_active_provider(storage, timeout_seconds=5)
    assert built is not None
    assert built.keyless is True
    assert built.model == "llama3.2"


def test_build_provider_prefers_active_model_then_mock(tmp_path):
    config = AgentConfig()
    config.model.provider = "fake"
    config.model.timeout_seconds = 5

    empty = Storage(tmp_path / "empty.db")
    fallback = build_provider(config, "mock", storage=empty)
    assert isinstance(fallback, HeuristicMockProvider)

    with pytest.raises(RuntimeError):
        build_provider(config, "windows_mcp", storage=empty)

    storage = Storage(tmp_path / "app.db")
    service = AiService(storage)
    provider = service.save_provider(
        {
            "preset_id": "ollama",
            "plan_id": "local",
            "name": "本机 Ollama",
        }
    )
    service.add_model(provider["id"], "llama3.2")
    built = build_provider(config, "mock", storage=storage, source="test")
    assert built.provider_model == "llama3.2"


def test_supports_temperature_for_reasoning_models():
    assert supports_temperature("gpt-4o-mini") is True
    assert supports_temperature("o3-mini") is False
    assert supports_temperature("gpt-5") is False


def test_anthropic_error_on_missing_key():
    provider = AnthropicProvider("http://127.0.0.1:9/v1", "claude", api_key="")
    with pytest.raises(Exception):
        provider.ping()
