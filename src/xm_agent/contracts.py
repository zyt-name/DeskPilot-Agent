from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    destructive: bool = False


CAPABILITIES: dict[str, CapabilitySpec] = {
    "desktop.observe": CapabilitySpec(
        name="desktop.observe",
        version="1.0",
        description="Read the current desktop: UIA element tree, active window, optional screenshot.",
        input_schema={
            "type": "object",
            "properties": {
                "include_screenshot": {"type": "boolean"},
            },
            "required": [],
        },
        output_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "active_window": {"type": ["string", "null"]},
                "screenshot_path": {"type": ["string", "null"]},
                "elements": {"type": "array"},
            },
        },
    ),
    "desktop.act": CapabilitySpec(
        name="desktop.act",
        version="1.0",
        description="Perform one UI action: click, type, key, scroll or move.",
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "type", "key", "scroll", "move"],
                },
                "element_id": {"type": "string"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "text": {"type": "string"},
                "keys": {"type": "string"},
                "amount": {"type": "integer"},
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle", "double"],
                },
                "clear": {
                    "type": "boolean",
                    "description": "Clear the field before typing (UIA backends).",
                },
                "press_enter": {
                    "type": "boolean",
                    "description": "Press Enter after typing (UIA backends).",
                },
                "label": {
                    "type": "string",
                    "description": "Visible name of the control, used when the backend has no element ids.",
                },
                "rollback_risk": {
                    "type": "boolean",
                    "description": "Set true when a mistake would be hard to undo; the top-level vision auditor reviews it.",
                },
            },
            "required": ["action"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "detail": {"type": "object"},
            },
        },
    ),
    "desktop.verify": CapabilitySpec(
        name="desktop.verify",
        version="1.0",
        description="Check an observable desktop condition: element, text or window.",
        input_schema={
            "type": "object",
            "properties": {
                "element_id": {"type": "string"},
                "text": {"type": "string"},
                "window": {"type": "string"},
            },
            "required": [],
        },
        output_schema={
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "detail": {"type": "string"},
            },
        },
    ),
    "browser.open": CapabilitySpec(
        name="browser.open",
        version="1.0",
        description=(
            "Open the managed browser and load a URL. It runs in its own "
            "process with a persistent profile, so a login done once (including "
            "QR code) stays valid across tasks. Use it instead of the desktop "
            "for web work that needs a logged-in session or must not take over "
            "the user's screen."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "new_tab": {"type": "boolean"},
                "wait_loaded": {"type": "boolean"},
                "timeout_s": {"type": "integer"},
            },
            "required": ["url"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "title": {"type": "string"},
                "engine": {"type": "string"},
                "headless": {"type": "boolean"},
            },
        },
    ),
    "browser.observe": CapabilitySpec(
        name="browser.observe",
        version="1.0",
        description=(
            "Read the managed browser page: URL, title, numbered interactive "
            "elements with coordinates, visible text and an optional "
            "screenshot. The page is observed automatically after each action, "
            "so only call it when you need a fresh read."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_screenshot": {"type": "boolean"},
            },
            "required": [],
        },
        output_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "url": {"type": "string"},
                "title": {"type": "string"},
                "elements": {"type": "array"},
                "screenshot_path": {"type": ["string", "null"]},
                "login_wall": {"type": "boolean"},
            },
        },
    ),
    "browser.act": CapabilitySpec(
        name="browser.act",
        version="1.0",
        description=(
            "Perform one action inside the managed browser page: goto, click, "
            "type, press, scroll, back, forward, reload or wait. Prefer "
            "element_index from the observation over raw coordinates."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "goto",
                        "click",
                        "type",
                        "press",
                        "scroll",
                        "back",
                        "forward",
                        "reload",
                        "wait",
                    ],
                },
                "element_index": {
                    "type": "integer",
                    "description": "Element number from browser.observe, e.g. 3 for [b3].",
                },
                "url": {"type": "string"},
                "selector": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "text": {"type": "string"},
                "keys": {"type": "string"},
                "amount": {
                    "type": "integer",
                    "description": "Scroll delta in pixels; positive scrolls down.",
                },
                "press_enter": {"type": "boolean"},
                "timeout_s": {"type": "integer"},
                "include_screenshot": {"type": "boolean"},
                "rollback_risk": {
                    "type": "boolean",
                    "description": "Set true for submit/pay/publish/delete clicks.",
                },
            },
            "required": ["action"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "detail": {"type": "object"},
            },
        },
    ),
    "browser.verify": CapabilitySpec(
        name="browser.verify",
        version="1.0",
        description=(
            "Check a browser page condition by exact text: URL fragment, page "
            "title, visible text or an element label."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url_contains": {"type": "string"},
                "title_contains": {"type": "string"},
                "text": {"type": "string"},
                "element_text": {"type": "string"},
            },
            "required": [],
        },
        output_schema={
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "detail": {"type": "string"},
            },
        },
    ),
    "browser.session": CapabilitySpec(
        name="browser.session",
        version="1.0",
        description=(
            "Inspect or manage the browser session. action=handoff brings the "
            "window to the front so the user can scan a QR code or log in; "
            "use it together with task.await_user. status reports the engine, "
            "url and idle time; close shuts the browser down."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "handoff", "close"],
                },
            },
            "required": ["action"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "started": {"type": "boolean"},
                "description": {"type": "string"},
            },
        },
    ),
    "terminal.exec": CapabilitySpec(
        name="terminal.exec",
        version="1.0",
        description="Run one remote command through the terminal backend and return exit code and output.",
        input_schema={
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "command": {"type": "string"},
                "timeout_s": {"type": "integer"},
            },
            "required": ["command"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "exit_code": {"type": "integer"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
            },
        },
    ),
    "file.transfer": CapabilitySpec(
        name="file.transfer",
        version="1.0",
        description="Upload or download one file through the terminal backend.",
        input_schema={
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["upload", "download"]},
                "source": {"type": "string"},
                "destination": {"type": "string"},
            },
            "required": ["direction", "source", "destination"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "bytes": {"type": "integer"},
            },
        },
    ),
    "wait.for": CapabilitySpec(
        name="wait.for",
        version="1.0",
        description="Wait until text, an element or a window appears.",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "element_id": {"type": "string"},
                "window": {"type": "string"},
                "timeout_s": {"type": "integer"},
            },
            "required": [],
        },
        output_schema={
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "detail": {"type": "string"},
            },
        },
    ),
    "task.finish": CapabilitySpec(
        name="task.finish",
        version="1.0",
        description=(
            "Finish the task with a short Chinese summary. Set success=false "
            "when the goal cannot be completed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "success": {"type": "boolean"},
            },
            "required": ["summary"],
        },
        output_schema={"type": "object", "properties": {}},
    ),
    "task.await_user": CapabilitySpec(
        name="task.await_user",
        version="1.0",
        description=(
            "Stop and hand control to the user when the task needs a human "
            "step that the agent cannot perform, such as logging in without "
            "provided credentials. Do not call it when you can continue."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["login", "input"],
                },
                "prompt": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["prompt"],
        },
        output_schema={"type": "object", "properties": {}},
    ),
}


def get_capability(name: str) -> CapabilitySpec:
    try:
        return CAPABILITIES[name]
    except KeyError:
        raise KeyError(f"unknown capability: {name}") from None


def browser_capabilities() -> tuple[str, ...]:
    return tuple(name for name in CAPABILITIES if name.startswith("browser."))


def capabilities_prompt(enabled: Any = None) -> str:
    """能力清单。传 enabled 时只列当前通道真能处理的能力。

    模型看到的清单必须和实际可执行的一致：列了却调不通，它会反复重试；
    漏列了，它会绕远路。
    """
    allow = None if enabled is None else {str(item) for item in enabled}
    lines: list[str] = []
    for spec in CAPABILITIES.values():
        if allow is not None and spec.name not in allow:
            continue
        lines.append(f"- {spec.name} v{spec.version}: {spec.description}")
        lines.append(f"  input schema: {json.dumps(spec.input_schema, ensure_ascii=False)}")
    return "\n".join(lines)
