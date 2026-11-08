from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .catalog import is_chat_model, is_vision_model, plan_by_id


class ModelListError(RuntimeError):
    pass


def _raise_on_error(data: Any) -> None:
    if not isinstance(data, dict):
        return
    if data.get("success") is False:
        message = data.get("msg") or data.get("message") or "model list returned an error"
        raise ModelListError(str(message))
    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("msg") or str(error)
        raise ModelListError(f"model list error: {message}")
    if isinstance(error, str) and error:
        raise ModelListError(f"model list error: {error}")
    if "data" not in data and "models" not in data:
        code = data.get("code")
        if code not in (None, 0, "0", 200, "200"):
            detail = data.get("msg") or data.get("message") or ""
            raise ModelListError(
                f"model list returned code {code}: {detail}".rstrip(": ")
            )


def _get_json(url: str, headers: dict[str, str], timeout: int = 20) -> Any:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            body = raw.decode("gb18030", errors="replace")
        raise ModelListError(f"model list HTTP {exc.code}: {body[:800]}") from exc
    except urllib.error.URLError as exc:
        raise ModelListError(f"model list request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ModelListError("model list response is not valid JSON") from exc


def _model_item(
    model_id: str, display_name: str = "", vision: bool | None = None
) -> dict[str, Any]:
    model_id = model_id.strip()
    return {
        "model_id": model_id,
        "display_name": (display_name or model_id).strip()[:200],
        "vision": is_vision_model(model_id) if vision is None else bool(vision),
        "source": "api",
    }


def _clean(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for item in items:
        model_id = item.get("model_id", "")
        if not model_id or model_id in seen:
            continue
        if not is_chat_model(model_id):
            continue
        seen.add(model_id)
        result.append(item)
        if len(result) >= 2000:
            break
    result.sort(key=lambda item: item["model_id"].casefold())
    return result


def _openai_style(
    base_url: str, api_key: str, timeout: int
) -> list[dict[str, Any]]:
    url = base_url if base_url.rstrip("/").endswith("/models") else base_url + "/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = _get_json(url, headers, timeout)
    _raise_on_error(data)
    entries = data if isinstance(data, list) else data.get("data", [])
    if not isinstance(entries, list):
        raise ModelListError("unexpected model list shape")
    items = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or entry.get("name") or "")
        display = str(entry.get("display_name") or entry.get("name") or model_id)
        items.append(_model_item(model_id, display))
    return _clean(items)


def _anthropic_style(
    base_url: str, api_key: str, timeout: int
) -> list[dict[str, Any]]:
    if not api_key:
        raise ModelListError("Anthropic model list requires an API key")
    root = base_url.rstrip("/")
    url = root if root.endswith("/models") else root + "/models"
    url += ("&" if "?" in url else "?") + "limit=100"
    headers = {
        "Accept": "application/json",
        "x-api-key": api_key,
        "Authorization": f"Bearer {api_key}",
        "anthropic-version": "2023-06-01",
    }
    data = _get_json(url, headers, timeout)
    _raise_on_error(data)
    entries = data.get("data", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        raise ModelListError("unexpected model list shape")
    items = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or "")
        display = str(entry.get("display_name") or model_id)
        items.append(_model_item(model_id, display))
    return _clean(items)


def _gemini_style(
    base_url: str, api_key: str, timeout: int
) -> list[dict[str, Any]]:
    if not api_key:
        raise ModelListError("Gemini model list requires an API key")
    root = base_url.rstrip("/")
    if root.endswith("/openai"):
        root = root[: -len("/openai")]
    if not root.endswith("/v1beta"):
        root = root + "/v1beta"
    url = root + "/models?key=" + urllib.parse.quote(api_key, safe="")
    data = _get_json(url, {"Accept": "application/json"}, timeout)
    _raise_on_error(data)
    entries = data.get("models", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        raise ModelListError("unexpected model list shape")
    items = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        model_id = name.removeprefix("models/")
        methods = entry.get("supportedGenerationMethods") or []
        if methods and "generateContent" not in methods:
            continue
        if any(marker in model_id.casefold() for marker in ("embedding", "aqa")):
            continue
        items.append(
            _model_item(model_id, str(entry.get("displayName") or model_id))
        )
    return _clean(items)


def _ollama_style(
    base_url: str, api_key: str, timeout: int
) -> list[dict[str, Any]]:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    try:
        data = _get_json(root + "/api/tags", {}, timeout)
        _raise_on_error(data)
        entries = data.get("models", []) if isinstance(data, dict) else []
    except ModelListError:
        return _openai_style(base_url, api_key, timeout)
    items = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("name") or entry.get("model") or "")
        details = entry.get("details") or {}
        families = details.get("families") or []
        vision = "clip" in families or is_vision_model(model_id)
        items.append(_model_item(model_id, model_id, vision))
    return _clean(items)


def _fetch_by_style(
    style: str, base_url: str, api_key: str, timeout: int
) -> list[dict[str, Any]]:
    if style == "gemini":
        return _gemini_style(base_url, api_key, timeout)
    if style == "ollama":
        return _ollama_style(base_url, api_key, timeout)
    if style == "anthropic":
        return _anthropic_style(base_url, api_key, timeout)
    return _openai_style(base_url, api_key, timeout)


def fetch_models(
    provider: dict[str, Any], api_key: str, timeout: int = 20
) -> list[dict[str, Any]]:
    base_url = str(provider.get("base_url") or "").strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ModelListError("base URL must start with http:// or https://")
    style = str(provider.get("models_style") or "openai")
    plan = plan_by_id(
        str(provider.get("preset_id") or ""), str(provider.get("plan_id") or "")
    )

    errors: list[str] = []
    try:
        models = _fetch_by_style(style, base_url, api_key, timeout)
    except ModelListError as exc:
        models = []
        errors.append(str(exc))

    if not models and plan is not None and plan.fallback_base_url:
        try:
            models = _fetch_by_style(
                plan.fallback_models_style or "openai",
                plan.fallback_base_url.rstrip("/"),
                api_key,
                timeout,
            )
        except ModelListError as exc:
            errors.append(str(exc))

    if not models and errors:
        raise ModelListError("; ".join(dict.fromkeys(errors)))
    return models


__all__ = ["ModelListError", "fetch_models"]
