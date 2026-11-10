from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Callable

from dataclasses import replace

from ..contracts import CAPABILITIES, capabilities_prompt
from ..types import Action, Observation, StepRecord

SYSTEM_PROMPT = """You are the decision layer of a Windows computer-control agent.
Return one JSON object only:
{"capability": "...", "args": {...}, "rationale": "...", "expect": "...",
 "steps": [{"capability": "...", "args": {...}, "expect": "..."}],
 "screen_card": "..."}

Rules:
- You may batch 1-4 deterministic actions to save screenshots and tokens.
  The first action runs immediately; the actions in "steps" run in order
  without a new screenshot. Only batch actions whose result is predictable
  from the current screen (typing a known value, pressing a known shortcut,
  clicking a known control). Stop the batch before anything that navigates,
  opens, submits, sends, publishes, pays, deletes or installs; that action
  must be the last action of the batch.
- screen_card is a 1-2 sentence description of the visible screen plus the
  regions you will use. When the next screenshot is identical, the platform
  reuses this card and does not resend the image.
- Return the JSON object only, with no prose, markdown or reasoning before
  or after it. Keep rationale under 60 characters.
- The desktop is observed automatically at the start of every plan, so do not
  spend actions on desktop.observe unless you truly need a fresh image.
- Never invent element ids; use ids from the observation.
- expect is matched against exact UI text, so use 1-4 words copied verbatim
  from a window title, label, or visible text (for example "运行" or
  "GitHub"). Never write a sentence or describe the goal; use null when no
  exact string is known.
- Copy names, URLs and search terms verbatim from the goal. Never substitute
  a different name or project.
- If an action could be hard to undo (send, submit, publish, delete, pay,
  uninstall, overwrite), set "rollback_risk": true in its args and make it
  the last action of a batch. An independent top-level vision auditor will
  review it against the screenshot.
- Login and authentication:
  - If the screen requires a login (password field, sign-in form, a page that
    says you must log in) and the goal or user_instructions do not contain the
    account and password, do not try to bypass it, guess credentials, register,
    reset the password, or search for another route. Call task.await_user with
    {"kind": "login", "prompt": "请先在当前窗口登录…", "summary": "任务已暂停…"}
    and stop.
  - If the user provided credentials, use them normally. Never invent them.
  - When the user says they have logged in and to continue, resume the original
    goal from the current screen; do not start over.
  - task.await_user is also valid for other unavoidable human steps, with
    "kind": "input".
- task.finish is only for a goal you can actually see completed. When the
  latest observation does not show the goal state, keep working instead of
  finishing. Never claim an action succeeded when its result is not visible.
- task.finish summary must be written in Simplified Chinese and is the final
  task report shown to the user:
  - On success, start with "已完成" and describe the state now visible on
    screen (what was opened, typed, saved or changed).
  - When the task cannot be completed, call task.finish with
    "success": false and a summary starting with "未能完成：" that explains
    the reason and what remains.
  - Use 2-4 short sentences or "-" bullets. Never mention tools, steps, JSON
    or internal reasoning.
- If observation.elements is not empty (UIA backend):
  - Click or type by element_id where possible.
  - To open a web page, use Win+R, type the URL with "clear": true and
    "press_enter": true, then wait. Do not try to type into a browser address
    bar: UIA backends do not expose it, so type would have no valid target.
  - If a page shows an error or the wrong URL, press Win+R and enter the full
    URL again instead of editing the address bar.
- If observation.elements is empty (vision backend):
  - A screenshot is attached only when observation.screenshot_attached is
    true. When it is false but observation.screen_card is present, the screen
    is unchanged: use the cached card and the previous coordinates.
  - When neither an image nor a screen card is available, you cannot see the
    screen: do not act blindly.
  - Every coordinate must be in the attached image's pixel space, whose size
    is stated in observation.summary. The backend rescales automatically.
  - To navigate a browser, press "ctrl+l", type the literal URL with
    "clear": true, then press "return". Win+R also works when you pass
    "press_enter": true so the Run dialog actually submits.
  - URL-encode non-ASCII search terms when typing a URL; that is expected.
  - After each batch wait for the next observation; never assume it worked.
  - desktop.verify and wait.for cannot check text on this backend: they only
    capture a screenshot and report deferred=true. That is not proof. Judge
    the next screenshot yourself before calling task.finish.
- Use terminal.exec only for terminal or remote-server tasks.
- If observation.source is "terminal" and the goal needs a desktop or browser,
  do not run a local shell command; call task.finish with "success": false and
  a Chinese summary starting "未能完成：当前终端后端无法执行桌面操作".

Available capabilities:
"""

BROWSER_PROMPT = """
Managed browser (browser.* capabilities):
- The browser is a separate process with its own persistent profile. A login
  done once survives restarts, so reuse it instead of logging in again.
- Prefer the browser over the desktop when the task is a website, needs a
  logged-in session, or must not take over the user's screen.
- Flow: browser.open (or browser.act action=goto) -> read observation ->
  browser.act with element_index from the observation. Never invent indexes.
- observation.source is "browser" when the page is what you are looking at;
  observation.source is "desktop" otherwise. Act on the channel you observed.
- After a click that navigates (submit, search, login, pagination), stop the
  batch and wait for the next observation instead of batching more actions.
- Login walls: when the page shows a QR code or a password form and the task
  gave no credentials, call browser.session with {"action": "handoff"} to
  bring the window to the user, then task.await_user with {"kind": "login"}.
  Do not try other routes, and never invent credentials.
- browser.verify checks exact strings (url_contains, title_contains, text,
  element_text). Use short verbatim fragments, not sentences.
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


def system_prompt(capabilities: Any = None) -> str:
    allow = None if capabilities is None else {str(item) for item in capabilities}
    body = SYSTEM_PROMPT
    if allow is None or any(name.startswith("browser.") for name in allow):
        body = body.replace(
            "\nAvailable capabilities:\n",
            BROWSER_PROMPT + "\nAvailable capabilities:\n",
        )
    return body + capabilities_prompt(allow)


def observation_text(
    goal: str,
    observation: Observation,
    history: list[StepRecord],
    remaining_steps: int,
    instructions: list[str] | None = None,
    screenshot_attached: bool = False,
    task_context: str = "",
    known_capabilities: Any = None,
) -> str:
    data = observation.to_dict()
    data["screenshot_attached"] = bool(screenshot_attached)
    if not observation.screen_card:
        data.pop("screen_card", None)
    available = (
        list(CAPABILITIES)
        if known_capabilities is None
        else [str(item) for item in known_capabilities]
    )
    payload = {
        "goal": goal,
        "observation": data,
        "recent_history": history_compact(history),
        "remaining_steps": remaining_steps,
        "user_instructions": list(instructions or []),
        "known_capabilities": available,
    }
    if task_context.strip():
        payload["task_context"] = task_context.strip()
    return json.dumps(payload, ensure_ascii=False)


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


def _json_objects(text: str):
    """Yield balanced top-level JSON objects embedded in free-form text."""
    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            current = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : index + 1]
                    break


def _coerce_action(value: Any) -> Action | None:
    if not isinstance(value, dict):
        return None
    capability = str(value.get("capability") or value.get("tool") or "")
    if capability not in CAPABILITIES:
        return None
    args = value.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    expect = value.get("expect")
    return Action(
        capability=capability,
        args=args,
        rationale=str(value.get("rationale", ""))[:300],
        expect=str(expect) if expect is not None else None,
    )


def parse_action_plan(parsed: dict[str, Any]) -> Action:
    """Build the first action plus the optional batched continuation plan."""
    first = _coerce_action(parsed)
    if first is None:
        capability = str(parsed.get("capability") or parsed.get("tool") or "")
        raise ValueError(f"model chose unknown capability: {capability!r}")
    raw_steps = parsed.get("steps") or parsed.get("plan") or []
    steps: list[Action] = []
    if isinstance(raw_steps, list):
        for item in raw_steps[:3]:
            action = _coerce_action(item)
            if action is not None:
                steps.append(action)
    card = str(parsed.get("screen_card") or parsed.get("screen") or "")
    return replace(
        first,
        steps=tuple(steps),
        screen_card=card.strip()[:400],
    )


def parse_action_json(content: str) -> dict[str, Any]:
    text = _strip_code_fence(content)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    # Models often wrap the decision in prose or reasoning text.
    for candidate in _json_objects(text):
        try:
            embedded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(embedded, dict):
            return embedded
    raise ValueError(f"model did not return valid JSON: {text[:500]}")


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
    "parse_action_plan",
    "parse_verdict_json",
    "supports_temperature",
    "system_prompt",
    "usage_with_latency",
]
