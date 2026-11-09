from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from ..types import Action, Observation, StepRecord
from .common import (
    VERIFY_SYSTEM_PROMPT,
    DeltaCallback,
    UsageCallback,
    image_data,
    normalize_usage,
    observation_text,
    parse_action_json,
    parse_action_plan,
    parse_verdict_json,
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
        *,
        instructions: list[str] | None = None,
        on_delta: DeltaCallback | None = None,
        context: str = "",
    ) -> Action:
        content: list[dict[str, Any]] = []
        attach = self.vision and not observation.screenshot_cached
        image = image_data(observation.screenshot_path) if attach else None
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
                    goal,
                    observation,
                    history,
                    remaining_steps,
                    instructions=instructions,
                    screenshot_attached=image is not None,
                    task_context=context,
                ),
            }
        )
        text, _, _ = self._chat(
            content, max_tokens=8192, on_delta=on_delta
        )
        try:
            parsed = parse_action_json(text)
        except ValueError as exc:
            raise AnthropicError(str(exc)) from exc
        try:
            return parse_action_plan(parsed)
        except ValueError as exc:
            raise AnthropicError(str(exc)) from exc

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 800,
        on_delta: DeltaCallback | None = None,
        image_path: str | None = None,
    ) -> str:
        content: list[dict[str, Any]] = []
        image = image_data(image_path) if image_path else None
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
        content.append({"type": "text", "text": user})
        text, _, _ = self._chat(
            content,
            max_tokens=max_tokens,
            system=system,
            on_delta=on_delta,
        )
        return text

    def verify_completion(
        self, goal: str, summary: str, screenshot_path: str | None
    ) -> tuple[bool, str]:
        image = image_data(screenshot_path) if self.vision else None
        if image is None:
            return False, "no vision capability or screenshot to verify"
        mime, encoded = image
        payload = json.dumps(
            {"goal": goal, "claimed_summary": summary}, ensure_ascii=False
        )
        text, _, _ = self._chat(
            [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": encoded,
                    },
                },
                {"type": "text", "text": payload},
            ],
            max_tokens=400,
            system=VERIFY_SYSTEM_PROMPT,
        )
        return parse_verdict_json(text)

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
        on_delta: DeltaCallback | None = None,
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
        stream = on_delta is not None
        if stream:
            payload["stream"] = True
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
                content_type = response.headers.get("Content-Type", "")
                if stream and "text/event-stream" in content_type:
                    text, usage_raw = self._stream(
                        response, on_delta, allow_thinking
                    )
                else:
                    data = json.loads(response.read().decode("utf-8"))
                    text = self._message_text(data, allow_thinking)
                    usage_raw = data.get("usage") or {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1200]
            raise AnthropicError(f"model HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise AnthropicError(f"model request failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise AnthropicError(f"invalid model response JSON: {exc}") from exc
        latency = int((time.monotonic() - started) * 1000)
        usage = normalize_usage(usage_raw)
        if usage["total_tokens"] <= 0:
            usage = self._estimate_usage(content, text)
        if self.on_usage is not None:
            self.on_usage(usage_with_latency(usage, latency))
        return text, usage, latency

    def _stream(
        self,
        response: Any,
        on_delta: DeltaCallback,
        allow_thinking: bool,
    ) -> tuple[str, dict[str, Any]]:
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        usage: dict[str, Any] = {}
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            data_text = line[5:].strip()
            if not data_text or data_text == "[DONE]":
                continue
            try:
                event = json.loads(data_text)
            except json.JSONDecodeError:
                continue
            kind = str(event.get("type") or "")
            if kind == "error":
                error = event.get("error") or {}
                raise AnthropicError(
                    str(
                        error.get("message")
                        if isinstance(error, dict)
                        else error
                    )
                )
            if kind == "message_start":
                message_usage = (event.get("message") or {}).get("usage") or {}
                if isinstance(message_usage, dict):
                    usage.update(message_usage)
                continue
            if kind == "message_delta":
                delta_usage = event.get("usage") or {}
                if isinstance(delta_usage, dict):
                    usage.update(delta_usage)
                continue
            if kind != "content_block_delta":
                continue
            delta = event.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            delta_type = delta.get("type")
            if delta_type == "thinking_delta":
                thinking = str(delta.get("thinking") or "")
                if thinking:
                    thinking_parts.append(thinking)
                    on_delta("thinking", thinking)
            elif delta_type == "text_delta":
                text = str(delta.get("text") or "")
                if text:
                    text_parts.append(text)
                    on_delta("text", text)
        text = "".join(text_parts)
        if not text and allow_thinking:
            text = "".join(thinking_parts)
        return text, usage

    def _estimate_usage(
        self, content: list[dict[str, Any]], text: str
    ) -> dict[str, int]:
        prompt_chars = sum(
            len(str(block.get("text", "")))
            for block in content
            if isinstance(block, dict)
        )
        prompt = max(1, prompt_chars // 4)
        completion = max(1, len(text) // 4) if text else 0
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

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
