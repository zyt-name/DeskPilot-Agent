from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from ..contracts import CAPABILITIES
from ..types import Action, Observation, StepRecord
from .common import (
    UsageCallback,
    image_data,
    normalize_usage,
    observation_text,
    parse_action_json,
    system_prompt,
    usage_with_latency,
)


class AnthropicError(RuntimeError):
    pass


class AnthropicProvider:
    """Anthropic Messages API, including Anthropic-compatible gateways."""

    name = "anthropic"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: int = 60,
        temperature: float = 0.0,
        vision: bool = False,
        on_usage: UsageCallback | None = None,
        keyless: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.vision = vision
        self.on_usage = on_usage
        self.keyless = keyless

    def decide(
        self,
        goal: str,
        observation: Observation,
        history: list[StepRecord],
        remaining_steps: int,
    ) -> Action:
        content: list[dict[str, Any]] = []
        image = image_data(observation.screenshot_path) if self.vision else None
        if image is not None:
            mime, encoded = image
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": encoded,
                    },
                }
            )
        content.append(
            {
                "type": "text",
                "text": observation_text(
                    goal, observation, history, remaining_steps
                ),
            }
        )
        text, _, _ = self._chat(content, max_tokens=2048)
        try:
            parsed = parse_action_json(text)
        except ValueError as exc:
            raise AnthropicError(str(exc)) from exc

        capability = str(parsed.get("capability") or parsed.get("tool") or "")
        if capability not in CAPABILITIES:
            raise AnthropicError(f"model chose unknown capability: {capability!r}")
        args = parsed.get("args") or {}
        if not isinstance(args, dict):
            raise AnthropicError("model args must be a JSON object")
        expect = parsed.get("expect")
        return Action(
            capability=capability,
            args=args,
            rationale=str(parsed.get("rationale", "")),
            expect=str(expect) if expect is not None else None,
        )

    def ping(self) -> dict[str, Any]:
        text, usage, latency = self._chat(
            [
                {
                    "type": "text",
                    "text": "This is a connectivity test. Reply with exactly OK.",
                }
            ],
            max_tokens=256,
            system="You are a connectivity test. Reply with exactly OK.",
            allow_thinking=True,
        )
        return {
            "ok": True,
            "text": text.strip()[:300],
            "usage": usage,
            "latency_ms": latency,
        }

    def _chat(
        self,
        content: list[dict[str, Any]],
        max_tokens: int,
        system: str | None = None,
        allow_thinking: bool = False,
    ) -> tuple[str, dict[str, int], int]:
        if not self.api_key.strip() and not self.keyless:
            raise AnthropicError("missing API key for Anthropic provider")
        if not self.base_url:
            raise AnthropicError("base URL is required")
        url = self.base_url
        if not url.endswith("/messages"):
            url += "/messages"
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": self.temperature,
            "system": system if system is not None else system_prompt(),
            "messages": [{"role": "user", "content": content}],
        }
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if self.api_key.strip():
            headers["x-api-key"] = self.api_key.strip()
            headers["Authorization"] = f"Bearer {self.api_key.strip()}"
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
            raise AnthropicError(f"model HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise AnthropicError(f"model request failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise AnthropicError(f"invalid model response JSON: {exc}") from exc
        latency = int((time.monotonic() - started) * 1000)
        text = self._message_text(data, allow_thinking)
        usage = normalize_usage(data.get("usage"))
        if self.on_usage is not None:
            self.on_usage(usage_with_latency(usage, latency))
        return text, usage, latency

    @staticmethod
    def _message_text(
        data: dict[str, Any], allow_thinking: bool = False
    ) -> str:
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise AnthropicError(f"unexpected model response shape: {data}")
        parts = [
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "".join(parts)
        if not text and allow_thinking:
            text = "".join(
                str(block.get("thinking", ""))
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "thinking"
            )
        if not text:
            raise AnthropicError(f"model response has no text: {data}")
        return text


__all__ = ["AnthropicProvider", "AnthropicError"]
