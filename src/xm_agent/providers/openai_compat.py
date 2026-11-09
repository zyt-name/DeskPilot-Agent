from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from ..types import Action, Observation, StepRecord
from .common import (
    VERIFY_SYSTEM_PROMPT,
    DeltaCallback,
    UsageCallback,
    history_compact,
    image_data,
    normalize_usage,
    observation_text,
    parse_action_json,
    parse_action_plan,
    parse_verdict_json,
    supports_temperature,
    system_prompt,
    usage_with_latency,
)


class ProviderError(RuntimeError):
    pass


def _content_parts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            elif isinstance(block, str):
                parts.append(block)
        return parts
    return []


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
        *,
        instructions: list[str] | None = None,
        on_delta: DeltaCallback | None = None,
        context: str = "",
    ) -> Action:
        messages = [
            {"role": "system", "content": system_prompt()},
            {
                "role": "user",
                "content": self._user_content(
                    goal,
                    observation,
                    history,
                    remaining_steps,
                    instructions=instructions,
                    task_context=context,
                ),
            },
        ]
        content, _, _ = self._chat(
            messages, max_tokens=8192, on_delta=on_delta
        )
        parsed = self._parse_json(content)
        try:
            return parse_action_plan(parsed)
        except ValueError as exc:
            raise ProviderError(str(exc)) from exc

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 800,
        on_delta: DeltaCallback | None = None,
        image_path: str | None = None,
    ) -> str:
        user_content: Any = user
        image = image_data(image_path) if image_path else None
        if image is not None:
            mime, encoded = image
            user_content = [
                {"type": "text", "text": user},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                },
            ]
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        text, _, _ = self._chat(
            messages, max_tokens=max_tokens, on_delta=on_delta
        )
        return text

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
        messages = [
            {"role": "system", "content": VERIFY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": payload},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{encoded}",
                        },
                    },
                ],
            },
        ]
        content, _, _ = self._chat(messages, max_tokens=400)
        return parse_verdict_json(content)

    def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        on_delta: DeltaCallback | None = None,
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
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if supports_temperature(self.model):
            payload["temperature"] = self.temperature
        stream = on_delta is not None
        if stream:
            payload["stream"] = True

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
                content_type = response.headers.get("Content-Type", "")
                if stream and "text/event-stream" in content_type:
                    text, usage_raw = self._stream(response, on_delta)
                else:
                    data = json.loads(response.read().decode("utf-8"))
                    text = self._message_text(data)
                    usage_raw = data.get("usage") or {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1200]
            raise ProviderError(f"model HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"model request failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError(f"invalid model response JSON: {exc}") from exc
        latency = int((time.monotonic() - started) * 1000)
        usage = normalize_usage(usage_raw)
        if usage["total_tokens"] <= 0:
            usage = self._estimate_usage(messages, text)
        if self.on_usage is not None:
            self.on_usage(usage_with_latency(usage, latency))
        return text, usage, latency

    def _stream(
        self,
        response: Any,
        on_delta: DeltaCallback,
    ) -> tuple[str, dict[str, Any]]:
        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            chunk_text = line[5:].strip()
            if chunk_text == "[DONE]":
                break
            try:
                chunk = json.loads(chunk_text)
            except json.JSONDecodeError:
                continue
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            choices = chunk.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta") or choice.get("message") or {}
                if not isinstance(delta, dict):
                    continue
                reasoning = delta.get("reasoning_content") or delta.get(
                    "reasoning"
                )
                if reasoning:
                    on_delta("thinking", str(reasoning))
                for part in _content_parts(delta.get("content")):
                    if not part:
                        continue
                    text_parts.append(part)
                    on_delta("text", part)
        return "".join(text_parts), usage

    def _estimate_usage(
        self, messages: list[dict[str, Any]], text: str
    ) -> dict[str, int]:
        prompt_chars = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                prompt_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        prompt_chars += len(str(block.get("text", "")))
        prompt = max(1, prompt_chars // 4)
        completion = max(1, len(text) // 4) if text else 0
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

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
        *,
        instructions: list[str] | None = None,
        task_context: str = "",
    ) -> str | list[dict[str, Any]]:
        attach = self.vision and not observation.screenshot_cached
        image = image_data(observation.screenshot_path) if attach else None
        text = observation_text(
            goal,
            observation,
            history,
            remaining_steps,
            instructions=instructions,
            screenshot_attached=image is not None,
            task_context=task_context,
        )
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
