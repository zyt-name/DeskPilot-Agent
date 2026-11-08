from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from ..contracts import CAPABILITIES
from ..types import Action, Observation, StepRecord
from .common import (
    UsageCallback,
    history_compact,
    image_data,
    normalize_usage,
    observation_text,
    parse_action_json,
    supports_temperature,
    system_prompt,
    usage_with_latency,
)


class ProviderError(RuntimeError):
    pass


class OpenAICompatProvider:
    name = "openai-compat"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        timeout_seconds: int = 60,
        temperature: float = 0.0,
        vision: bool = False,
        api_key: str | None = None,
        on_usage: UsageCallback | None = None,
        keyless: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.vision = vision
        self.api_key = api_key
        self.on_usage = on_usage
        self.keyless = keyless

    @property
    def requires_key(self) -> bool:
        if self.keyless:
            return False
        return not self.base_url.startswith(
            ("http://127.0.0.1", "http://localhost")
        )

    def decide(
        self,
        goal: str,
        observation: Observation,
        history: list[StepRecord],
        remaining_steps: int,
    ) -> Action:
        messages = [
            {"role": "system", "content": system_prompt()},
            {
                "role": "user",
                "content": self._user_content(
                    goal, observation, history, remaining_steps
                ),
            },
        ]
        content, _, _ = self._chat(messages)
        parsed = self._parse_json(content)
        capability = str(parsed.get("capability") or parsed.get("tool") or "")
        if capability not in CAPABILITIES:
            raise ProviderError(f"model chose unknown capability: {capability!r}")
        args = parsed.get("args") or {}
        if not isinstance(args, dict):
            raise ProviderError("model args must be a JSON object")
        expect = parsed.get("expect")
        return Action(
            capability=capability,
            args=args,
            rationale=str(parsed.get("rationale", "")),
            expect=str(expect) if expect is not None else None,
        )

    def ping(self) -> dict[str, Any]:
        messages = [
            {
                "role": "system",
                "content": "You are a connectivity test. Reply with exactly OK.",
            },
            {"role": "user", "content": "ping"},
        ]
        text, usage, latency = self._chat(messages)
        return {
            "ok": True,
            "text": text.strip()[:300],
            "usage": usage,
            "latency_ms": latency,
        }

    def _chat(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str, dict[str, int], int]:
        api_key = self._api_key()
        if self.requires_key and not api_key:
            raise ProviderError(
                "missing API key; configure it in the AI settings or set "
                f"{self.api_key_env}"
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if supports_temperature(self.model):
            payload["temperature"] = self.temperature

        url = self.base_url
        if not url:
            raise ProviderError("base URL is required")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        started = time.monotonic()
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1200]
            raise ProviderError(f"model HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"model request failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError(f"invalid model response JSON: {exc}") from exc
        latency = int((time.monotonic() - started) * 1000)
        text = self._message_text(data)
        usage = normalize_usage(data.get("usage"))
        if self.on_usage is not None:
            self.on_usage(usage_with_latency(usage, latency))
        return text, usage, latency

    def _api_key(self) -> str:
        if self.api_key is not None:
            return self.api_key.strip()
        return os.environ.get(self.api_key_env, "").strip()

    def _user_content(
        self,
        goal: str,
        observation: Observation,
        history: list[StepRecord],
        remaining_steps: int,
    ) -> str | list[dict[str, Any]]:
        text = observation_text(goal, observation, history, remaining_steps)
        if not self.vision:
            return text
        image = image_data(observation.screenshot_path)
        if image is None:
            return text
        mime, encoded = image
        return [
            {"type": "text", "text": text},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            },
        ]

    @staticmethod
    def _image_url(path: str) -> str | None:
        image = image_data(path)
        if image is None:
            return None
        mime, encoded = image
        return f"data:{mime};base64,{encoded}"

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        try:
            return parse_action_json(content)
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc

    @staticmethod
    def _history(history: list[StepRecord]) -> list[dict[str, Any]]:
        return history_compact(history)

    @staticmethod
    def _message_text(data: dict[str, Any]) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected model response shape: {data}") from exc
        if isinstance(content, list):
            parts = [
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict)
            ]
            return "".join(parts)
        return str(content or "")
