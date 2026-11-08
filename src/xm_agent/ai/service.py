from __future__ import annotations

from typing import Any

from ..storage import Storage
from .catalog import catalog_dict, preset_by_id, preset_endpoint
from .crypto import decrypt_secret, encrypt_secret, secret_hint
from .factory import build_ai_provider, usage_recorder
from .models import ModelListError, fetch_models as fetch_remote_models

_PROTOCOLS = {"openai", "anthropic"}
_STYLES = {"openai", "anthropic", "gemini", "ollama"}


class AiService:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def overview(self) -> dict[str, Any]:
        usage = self.storage.ai_usage_summary()
        providers = [
            self._sync_provider(provider)
            for provider in self.storage.list_ai_providers()
        ]
        return {
            "catalog": catalog_dict(),
            "providers": providers,
            "active": self.active_summary(),
            "totals": usage["totals"],
            "today": usage["today"],
        }

    def _sync_provider(self, provider: dict[str, Any]) -> dict[str, Any]:
        endpoint = preset_endpoint(
            str(provider.get("preset_id") or ""),
            str(provider.get("plan_id") or ""),
        )
        if endpoint is None or not any(
            provider.get(key) != value for key, value in endpoint.items()
        ):
            return provider
        self.storage.save_ai_provider(
            {
                "id": provider["id"],
                "preset_id": str(provider.get("preset_id") or ""),
                "name": str(provider.get("name") or ""),
                "plan_id": str(provider.get("plan_id") or ""),
                **endpoint,
            }
        )
        repaired = self.storage.get_ai_provider(
            provider["id"], include_secret="api_key_enc" in provider
        )
        return repaired or provider

    def active_summary(self) -> dict[str, Any] | None:
        active = self.storage.get_active_ai()
        if active is None:
            return None
        provider = active["provider"]
        model = active["model"]
        return {
            "provider_id": provider["id"],
            "provider_name": provider["name"],
            "preset_id": provider.get("preset_id", ""),
            "plan_id": provider.get("plan_id", ""),
            "protocol": provider.get("protocol", ""),
            "has_key": provider.get("has_key", False),
            "model": model["model_id"],
            "display_name": model.get("display_name") or model["model_id"],
            "vision": bool(model.get("vision")),
        }

    def save_provider(self, fields: dict[str, Any]) -> dict[str, Any]:
        provider_id = str(fields.get("id") or "")
        existing = (
            self.storage.get_ai_provider(provider_id, include_secret=True)
            if provider_id
            else None
        )
        preset_id = str(
            fields.get("preset_id")
            or (existing or {}).get("preset_id")
            or "custom"
        )
        preset = preset_by_id(preset_id)
        plan_id = str(
            fields.get("plan_id") or (existing or {}).get("plan_id") or ""
        )
        endpoint = preset_endpoint(preset_id, plan_id)
        if endpoint is not None:
            protocol = endpoint["protocol"]
            base_url = endpoint["base_url"]
            models_style = endpoint["models_style"]
            requires_key = endpoint["requires_key"]
        else:
            protocol = str(
                fields.get("protocol")
                or (existing or {}).get("protocol")
                or "openai"
            )
            base_url = str(
                fields.get("base_url")
                or (existing or {}).get("base_url")
                or ""
            ).strip().rstrip("/")
            models_style = str(
                fields.get("models_style")
                or (existing or {}).get("models_style")
                or "openai"
            )
            requires_key = fields.get("requires_key")
            if requires_key is None:
                requires_key = (existing or {}).get("requires_key", True)

        if protocol not in _PROTOCOLS:
            raise ValueError(f"unsupported protocol: {protocol}")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base URL must start with http:// or https://")
        if models_style not in _STYLES:
            raise ValueError(f"unsupported model list style: {models_style}")

        name = str(
            fields.get("name")
            or (existing or {}).get("name")
            or (preset.name if preset else preset_id)
        ).strip()

        api_key_enc: str | None = None
        api_key_hint: str | None = None
        if fields.get("clear_key"):
            api_key_enc = ""
            api_key_hint = ""
        else:
            raw_key = str(fields.get("api_key") or "").strip()
            if raw_key:
                try:
                    api_key_enc = encrypt_secret(raw_key)
                except Exception as exc:
                    raise ValueError(f"failed to encrypt API key: {exc}") from exc
                api_key_hint = secret_hint(raw_key)

        if existing is not None and (
            str(existing.get("protocol") or "") != protocol
            or str(existing.get("base_url") or "") != base_url
            or str(existing.get("models_style") or "") != models_style
        ):
            self.storage.clear_fetched_models(existing["id"])
            active = self.storage.get_active_ai()
            if active is not None and active["provider"]["id"] == existing["id"]:
                remaining = {
                    item["model_id"]
                    for item in self.storage.list_ai_models(existing["id"])
                }
                if active["model"]["model_id"] not in remaining:
                    self.storage.clear_active_ai()

        saved = self.storage.save_ai_provider(
            {
                "id": existing["id"] if existing else provider_id or None,
                "preset_id": preset_id,
                "name": name or preset_id,
                "protocol": protocol,
                "base_url": base_url,
                "plan_id": plan_id,
                "models_style": models_style,
                "requires_key": bool(requires_key),
                "api_key_enc": api_key_enc,
                "api_key_hint": api_key_hint,
            }
        )
        if saved is None:
            raise ValueError("failed to save provider")
        return saved

    def delete_provider(self, provider_id: str) -> None:
        self.storage.delete_ai_provider(provider_id)

    def provider_key(self, provider: dict[str, Any]) -> str:
        return decrypt_secret(str(provider.get("api_key_enc") or ""))

    def fetch_models(
        self, provider_id: str, api_key_override: str = ""
    ) -> list[dict[str, Any]]:
        provider = self.storage.get_ai_provider(provider_id, include_secret=True)
        if provider is None:
            raise ValueError("unknown AI provider")
        provider = self._sync_provider(provider)
        api_key = api_key_override.strip() or self.provider_key(provider)
        if provider.get("requires_key") and not api_key:
            raise ModelListError("该提供商需要 API Key，请先填写并保存")
        models = fetch_remote_models(provider, api_key)
        if models:
            self.storage.replace_fetched_models(provider_id, models)
        saved = self.storage.list_ai_models(provider_id)
        active = self.storage.get_active_ai()
        if active is None:
            if saved:
                self.storage.set_active_ai(provider_id, saved[0]["model_id"])
        elif active["provider"]["id"] == provider_id:
            ids = {item["model_id"] for item in saved}
            if active["model"]["model_id"] not in ids:
                if saved:
                    self.storage.set_active_ai(provider_id, saved[0]["model_id"])
                else:
                    self.storage.clear_active_ai()
        return models

    def add_model(
        self,
        provider_id: str,
        model_id: str,
        display_name: str = "",
    ) -> dict[str, Any]:
        provider = self.storage.get_ai_provider(provider_id)
        if provider is None:
            raise ValueError("unknown AI provider")
        model = self.storage.add_ai_model(provider_id, model_id, display_name)
        if model is None:
            raise ValueError("failed to add model")
        if self.storage.get_active_ai() is None:
            self.storage.set_active_ai(provider_id, model["model_id"])
        return model

    def delete_model(self, provider_id: str, model_id: str) -> None:
        self.storage.delete_ai_model(provider_id, model_id)

    def set_active(self, provider_id: str, model_id: str) -> dict[str, Any]:
        self.storage.set_active_ai(provider_id, model_id)
        summary = self.active_summary()
        if summary is None:
            raise ValueError("failed to activate model")
        return summary

    def test_provider(
        self, provider_id: str, model_id: str, api_key_override: str = ""
    ) -> dict[str, Any]:
        provider = self.storage.get_ai_provider(provider_id, include_secret=True)
        if provider is None:
            raise ValueError("unknown AI provider")
        provider = self._sync_provider(provider)
        models = {item["model_id"]: item for item in self.storage.list_ai_models(provider_id)}
        model = models.get(model_id)
        if model is None:
            raise ValueError("unknown model; fetch the model list or add a custom model")
        api_key = api_key_override.strip() or self.provider_key(provider)
        if provider.get("requires_key") and not api_key:
            raise ValueError("该提供商需要 API Key，请先填写并保存")

        recorder = usage_recorder(
            self.storage,
            provider_id,
            str(provider.get("name") or ""),
            model_id,
            "test",
            None,
        )

        try:
            instance = build_ai_provider(
                provider,
                model_id,
                api_key=api_key,
                vision=bool(model.get("vision")),
                timeout_seconds=30,
                temperature=0.0,
                on_usage=recorder,
            )
            result = instance.ping()
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "provider": provider.get("name"),
                "model": model_id,
            }
        return {
            **result,
            "provider": provider.get("name"),
            "model": model_id,
            "vision": bool(model.get("vision")),
        }

    def usage(self) -> dict[str, Any]:
        return self.storage.ai_usage_summary()

    def bootstrap_summary(self) -> dict[str, Any]:
        summary = self.active_summary()
        usage = self.storage.ai_usage_summary()
        return {
            "active": summary,
            "providers": len(self.storage.list_ai_providers()),
            "totals": usage["totals"],
            "today": usage["today"],
        }


__all__ = ["AiService"]
