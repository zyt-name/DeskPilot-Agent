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
- The desktop is observed automatically before every decision, so do not
  spend steps on desktop.observe.
- Never invent element ids; use ids from the observation.
- expect is matched against exact UI text, so use 1-4 words copied verbatim
  from a window title, label, or visible text (for example "运行" or
  "GitHub"). Never write a sentence or describe the goal; use null when no
  exact string is known.
- Copy names, URLs and search terms verbatim from the goal. Never substitute
  a different name or project.
- task.finish is only for a goal you can actually see completed. When the
  latest observation does not show the goal state, keep working instead of
  finishing. Never claim an action succeeded when its result is not visible.
- If observation.elements is not empty (UIA backend):
  - Click or type by element_id where possible.
  - To open a web page, use Win+R, type the URL with "clear": true and
    "press_enter": true, then wait. Do not try to type into a browser address
    bar: UIA backends do not expose it, so type would have no valid target.
  - If a page shows an error or the wrong URL, press Win+R and enter the full
    URL again instead of editing the address bar.
- If observation.elements is empty (vision backend):
  - A screenshot is attached only when observation.screenshot_attached is
    true. When it is false you cannot see the screen: do not act blindly.
  - Every coordinate must come from the attached screenshot.
  - To navigate a browser, press "ctrl+l", type the literal URL with
    "clear": true, then press "return". Win+R also works when you pass
    "press_enter": true so the Run dialog actually submits.
  - URL-encode non-ASCII search terms when typing a URL; that is expected.
  - After each action wait for the next screenshot; never assume it worked.
  - desktop.verify and wait.for cannot check text on this backend: they only
    capture a screenshot and report deferred=true. That is not proof. Judge
    the next screenshot yourself before calling task.finish.
- Use terminal.exec only for terminal or remote-server tasks.
- If observation.source is "terminal" and the goal needs a desktop or browser,
  do not run a local shell command; call task.finish with a summary that this
  backend cannot perform desktop actions.

Available capabilities:
"""

VERIFY_SYSTEM_PROMPT = """You verify whether a Windows desktop task is complete.
You receive the task goal, the agent's claimed summary, and a screenshot of the
current screen. Decide from the screenshot only and return one JSON object:
{"done": true, "reason": "short evidence-based explanation"}

Rules:
- done is true only when the screenshot clearly shows the goal state (for
  example the requested page, query and results are visible).
- A missing, partial, loading or error screen means done is false.
- Never trust the summary; verify it against the screenshot.
- Keep reason under 40 words and quote the visible text you relied on.
"""

UsageCallback = Callable[[dict[str, int]], None]
DeltaCallback = Callable[[str, str], None]


def system_prompt() -> str:
    return SYSTEM_PROMPT + capabilities_prompt()


def observation_text(
    goal: str,
    observation: Observation,
    history: list[StepRecord],
    remaining_steps: int,
    instructions: list[str] | None = None,
    screenshot_attached: bool = False,
) -> str:
    data = observation.to_dict()
    data["screenshot_attached"] = bool(screenshot_attached)
    return json.dumps(
        {
            "goal": goal,
            "observation": data,
            "recent_history": history_compact(history),
            "remaining_steps": remaining_steps,
            "user_instructions": list(instructions or []),
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
                "deferred": bool(
                    record.verification and record.verification.deferred
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


def _strip_code_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def parse_action_json(content: str) -> dict[str, Any]:
    text = _strip_code_fence(content)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"model did not return valid JSON: {text[:500]}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError("model response must be a JSON object")
    return parsed


def parse_verdict_json(content: str) -> tuple[bool, str]:
    """Parse a completion verdict; anything ambiguous counts as not done."""
    text = _strip_code_fence(content)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return False, text[:300] or "no verdict returned"
    if not isinstance(parsed, dict):
        return False, text[:300]
    done = parsed.get("done")
    if isinstance(done, str):
        done = done.strip().casefold() in {"true", "yes", "1"}
    reason = str(parsed.get("reason") or parsed.get("detail") or "").strip()
    if not isinstance(done, bool):
        return False, reason or "verdict has no boolean done field"
    return done, reason


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
    "VERIFY_SYSTEM_PROMPT",
    "DeltaCallback",
    "UsageCallback",
    "history_compact",
    "image_data",
    "normalize_usage",
    "observation_text",
    "parse_action_json",
    "parse_verdict_json",
    "supports_temperature",
    "system_prompt",
    "usage_with_latency",
]
