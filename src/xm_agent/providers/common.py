from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Callable

from ..contracts import CAPABILITIES, capabilities_prompt
from ..types import Observation, StepRecord

SYSTEM_PROMPT = """You are the decision layer of a Windows computer-control agent.
Choose exactly one capability and return one JSON object only:
{"capability": "...", "args": {...}, "rationale": "...", "expect": "..."}

Rules:
- One atomic action per step.
- Prefer desktop.observe when the screen state is unclear.
- Use terminal.exec only for terminal or remote-server tasks.
- Use task.finish only when the goal is verified.
- Never invent element ids; use ids from the observation.
- expect is a short verification condition, e.g. text, "saved", or a window title.

Available capabilities:
"""

UsageCallback = Callable[[dict[str, int]], None]


def system_prompt() -> str:
    return SYSTEM_PROMPT + capabilities_prompt()


def observation_text(
    goal: str,
    observation: Observation,
    history: list[StepRecord],
    remaining_steps: int,
) -> str:
    return json.dumps(
        {
            "goal": goal,
            "observation": observation.to_dict(),
            "recent_history": history_compact(history),
            "remaining_steps": remaining_steps,
            "known_capabilities": list(CAPABILITIES),
        },
        ensure_ascii=False,
    )


def history_compact(history: list[StepRecord]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for record in history[-6:]:
        compact.append(
            {
                "step": record.step,
                "action": record.action.capability,
                "args": record.action.args,
                "ok": bool(record.receipt and record.receipt.ok),
                "verified": bool(
                    record.verification and record.verification.ok
                ),
                "error": record.error,
            }
        )
    return compact


def image_data(path: str | None) -> tuple[str, str] | None:
    if not path:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    mime = "image/jpeg" if data[:2] == b"\xff\xd8" else "image/png"
    return mime, base64.b64encode(data).decode("ascii")


def parse_action_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"model did not return valid JSON: {text[:500]}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError("model response must be a JSON object")
    return parsed


def normalize_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    data = usage or {}
    prompt = int(
        data.get("prompt_tokens")
        or data.get("input_tokens")
        or data.get("promptTokenCount")
        or 0
    )
    completion = int(
        data.get("completion_tokens")
        or data.get("output_tokens")
        or data.get("candidatesTokenCount")
        or 0
    )
    total = int(data.get("total_tokens") or data.get("totalTokenCount") or 0)
    if total <= 0:
        total = prompt + completion
    return {
        "prompt_tokens": max(0, prompt),
        "completion_tokens": max(0, completion),
        "total_tokens": max(0, total),
    }


def usage_with_latency(usage: dict[str, int], latency_ms: int) -> dict[str, int]:
    return {**usage, "latency_ms": max(0, int(latency_ms))}


def supports_temperature(model_id: str) -> bool:
    name = model_id.strip().casefold()
    return not re.match(r"^(o[1-9]|gpt-5)", name)


__all__ = [
    "SYSTEM_PROMPT",
    "UsageCallback",
    "history_compact",
    "image_data",
    "normalize_usage",
    "observation_text",
    "parse_action_json",
    "supports_temperature",
    "system_prompt",
    "usage_with_latency",
]
