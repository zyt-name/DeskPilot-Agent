#!/usr/bin/env python3
"""Dependency-free fake MCP stdio server used by the test suite."""

from __future__ import annotations

import json
import sys
from typing import Any

STATE: dict[str, Any] = {"text": "", "saved": False, "clicks": []}


def _tool(name: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": {}},
    }


TOOLS = [
    _tool("Snapshot", "Return a fake UIA tree"),
    _tool("Screenshot", "Return a fake screenshot path"),
    _tool("Click", "Click an element"),
    _tool("Type", "Type text"),
    _tool("Shortcut", "Press a keyboard shortcut"),
    _tool("Scroll", "Scroll the active window"),
    _tool("Move", "Move the pointer"),
    _tool("WaitFor", "Wait for text, an element or a window"),
    _tool("xshell_send", "Send a command to the terminal backend"),
    _tool("xshell_read_screen", "Read the terminal screen"),
    _tool("sftp_upload", "Upload a file"),
    _tool("sftp_download", "Download a file"),
]


def _snapshot() -> dict[str, Any]:
    return {
        "summary": (
            f"window=Notepad; text={STATE['text']!r}; "
            f"saved={STATE['saved']}"
        ),
        "active_window": "Notepad",
        "elements": [
            {
                "id": "e1",
                "role": "Edit",
                "name": "text",
                "enabled": True,
                "bounds": [10, 10, 300, 40],
            },
            {
                "id": "e2",
                "role": "Button",
                "name": "Save",
                "enabled": True,
                "bounds": [320, 10, 60, 30],
            },
            {
                "id": "e3",
                "role": "Button",
                "name": "Submit",
                "enabled": not STATE["saved"],
                "bounds": [390, 10, 70, 30],
            },
        ],
    }


def _wait_for(args: dict[str, Any]) -> dict[str, Any]:
    needle = str(
        args.get("text")
        or args.get("element_id")
        or args.get("window")
        or ""
    )
    if needle == "saved":
        found = bool(STATE["saved"])
    elif needle.lower() == "ready":
        found = True
    elif needle in {"e1", "e2", "e3"}:
        found = True
    elif needle.casefold() == "notepad":
        found = True
    else:
        found = needle in str(STATE["text"])
    return {"ok": found, "detail": "found" if found else "not found"}


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "Snapshot":
        return _snapshot()
    if name == "Screenshot":
        return {"ok": True, "path": "fake://screenshot.png"}
    if name == "Click":
        element_id = str(args.get("element_id", ""))
        STATE["clicks"].append(element_id)
        if element_id in {"e2", "e3"}:
            STATE["saved"] = True
        return {"ok": True, "clicked": element_id, "saved": STATE["saved"]}
    if name == "Type":
        text = str(args.get("text", ""))
        STATE["text"] += text
        return {"ok": True, "text": STATE["text"]}
    if name == "Shortcut":
        keys = str(args.get("keys", "")).casefold()
        if keys in {"ctrl+s", "ctrl+shift+s"}:
            STATE["saved"] = True
        return {"ok": True, "keys": keys, "saved": STATE["saved"]}
    if name in {"Scroll", "Move"}:
        return {"ok": True}
    if name == "WaitFor":
        return _wait_for(args)
    if name == "xshell_send":
        command = str(args.get("text") or args.get("command") or "")
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": f"fake: {command}\n",
            "stderr": "",
        }
    if name == "xshell_read_screen":
        return {"ok": True, "text": f"fake terminal: {STATE['text']}"}
    if name in {"sftp_upload", "sftp_download"}:
        return {"ok": True, "bytes": 0}
    return {"ok": False, "error": f"unknown tool: {name}"}


def _handle(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-mcp", "version": "0.1.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = str(params.get("name", ""))
        args = params.get("arguments") or {}
        payload = _call(name, args if isinstance(args, dict) else {})
        result: dict[str, Any] = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ]
        }
        if payload.get("error"):
            result["isError"] = True
        return result
    raise KeyError(method)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict) or "id" not in request:
            continue
        response: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request["id"],
        }
        try:
            response["result"] = _handle(
                str(request.get("method", "")),
                request.get("params") or {},
            )
        except KeyError as exc:
            response["error"] = {
                "code": -32601,
                "message": f"method not found: {exc}",
            }
        except Exception as exc:  # pragma: no cover - defensive server loop
            response["error"] = {"code": -32603, "message": str(exc)}
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
