from __future__ import annotations

from typing import Any, Callable

from ..providers.anthropic import AnthropicProvider
from ..providers.openai_compat import OpenAICompatProvider
from ..storage import Storage
from .catalog import preset_endpoint
from .crypto import decrypt_secret


def usage_recorder(
    storage: Storage,
    provider_id: str,
    provider_name: str,
    model: str,
    source: str,
    run_id: str | None,
) -> Callable[[dict[str, int]], None]:
    def record(usage: dict[str, int]) -> None:
        storage.record_ai_usage(
            provider_id=provider_id,
            provider_name=provider_name,
            model=model,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            total_tokens=int(usage.get("total_tokens", 0)),
            source=source,
            run_id=run_id,
            latency_ms=int(usage.get("latency_ms", 0)),
        )

    return record


def _attach(provider: Any, row: dict[str, Any], model: str, vision: bool):
    provider.provider_id = row.get("id", "")
    provider.provider_name = row.get("name", "")
    provider.provider_model = model
    provider.provider_vision = bool(vision)
    return provider


def build_ai_provider(
    row: dict[str, Any],
    model: str,
    *,
    api_key: str = "",
    vision: bool = False,
    timeout_seconds: int = 60,
    temperature: float = 0.0,
    on_usage: Callable[[dict[str, int]], None] | None = None,
):
    protocol = str(row.get("protocol") or "openai")
    keyless = not bool(row.get("requires_key", True))
    if protocol == "anthropic":
        provider = AnthropicProvider(
            base_url=str(row.get("base_url") or ""),
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            vision=vision,
            on_usage=on_usage,
            keyless=keyless,
        )
    else:
        provider = OpenAICompatProvider(
            base_url=str(row.get("base_url") or ""),
            model=model,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            vision=vision,
            api_key=api_key,
            on_usage=on_usage,
            keyless=keyless,
        )
    return _attach(provider, row, model, vision)


def build_model_provider(
    storage: Storage,
    provider_id: str,
    model_id: str,
    *,
    timeout_seconds: int = 60,
    temperature: float = 0.0,
    source: str = "ui",
    run_id: str | None = None,
    vision: bool | None = None,
):
    """Build a provider for an explicit (provider, model) pair."""
    row = storage.get_ai_provider(provider_id, include_secret=True)
    if row is None:
        raise ValueError("unknown AI provider")
    endpoint = preset_endpoint(
        str(row.get("preset_id") or ""), str(row.get("plan_id") or "")
    )
    if endpoint is not None:
        row = {**row, **endpoint}
    models = {
        str(item["model_id"]): item
        for item in storage.list_ai_models(provider_id)
    }
    model = models.get(model_id)
    if model is None:
        raise ValueError("unknown model for this provider")
    api_key = decrypt_secret(str(row.get("api_key_enc") or ""))
    if row.get("requires_key") and not api_key:
        raise RuntimeError(
            f"AI provider '{row.get('name')}' has no API key; "
            "configure it in AI 模型"
        )
    recorder = usage_recorder(
        storage,
        provider_id,
        str(row.get("name") or ""),
        model_id,
        source,
        run_id,
    )
    return build_ai_provider(
        row,
        model_id,
        api_key=api_key,
        vision=bool(model.get("vision")) if vision is None else bool(vision),
        timeout_seconds=timeout_seconds,
        temperature=temperature,
        on_usage=recorder,
    )


def build_active_provider(
    storage: Storage,
    *,
    timeout_seconds: int = 60,
    temperature: float = 0.0,
    source: str = "ui",
    run_id: str | None = None,
):
    active = storage.get_active_ai(include_secret=True)
    if active is None:
        return None
    row = active["provider"]
    endpoint = preset_endpoint(
        str(row.get("preset_id") or ""), str(row.get("plan_id") or "")
    )
    if endpoint is not None:
        row = {**row, **endpoint}
    model_row = active["model"]
    api_key = decrypt_secret(str(row.get("api_key_enc") or ""))
    if row.get("requires_key") and not api_key:
        raise RuntimeError(
            f"AI provider '{row.get('name')}' has no API key; "
            "configure it in AI 模型"
        )
    recorder = usage_recorder(
        storage,
        str(row.get("id") or ""),
        str(row.get("name") or ""),
        str(model_row.get("model_id") or ""),
        source,
        run_id,
    )
    return build_ai_provider(
        row,
        str(model_row.get("model_id") or ""),
        api_key=api_key,
        vision=bool(model_row.get("vision")),
        timeout_seconds=timeout_seconds,
        temperature=temperature,
        on_usage=recorder,
    )


__all__ = [
    "build_active_provider",
    "build_ai_provider",
    "build_model_provider",
    "usage_recorder",
]
