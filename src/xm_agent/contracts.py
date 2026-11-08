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
        description="Finish the task with a short summary. Use only when the goal is verified.",
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
            },
            "required": ["summary"],
        },
        output_schema={"type": "object", "properties": {}},
    ),
}


def get_capability(name: str) -> CapabilitySpec:
    try:
        return CAPABILITIES[name]
    except KeyError:
        raise KeyError(f"unknown capability: {name}") from None


def capabilities_prompt() -> str:
    lines: list[str] = []
    for spec in CAPABILITIES.values():
        lines.append(f"- {spec.name} v{spec.version}: {spec.description}")
        lines.append(f"  input schema: {json.dumps(spec.input_schema, ensure_ascii=False)}")
    return "\n".join(lines)
