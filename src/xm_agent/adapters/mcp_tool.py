from __future__ import annotations

import json
from typing import Any

from ..mcp_stdio import MCPError, StdioMCPClient
from ..types import Element, Observation, Receipt, Verification

DEFAULT_TOOLS: dict[str, str] = {
    "observe": "Snapshot",
    "screenshot": "Screenshot",
    "click": "Click",
    "type": "Type",
    "key": "Shortcut",
    "scroll": "Scroll",
    "move": "Move",
    "wait_for": "WaitFor",
    "exec": "xshell_send",
    "read": "xshell_read_screen",
    "upload": "sftp_upload",
    "download": "sftp_download",
}

_DESKTOP_ACTION_TO_TOOL = {
    "click": "click",
    "type": "type",
    "key": "key",
    "scroll": "scroll",
    "move": "move",
}

_DESKTOP_ACTION_ARGS = {
    "click": ("element_id", "x", "y", "button"),
    "type": ("text", "element_id"),
    "key": ("keys", "text"),
    "scroll": ("amount", "x", "y"),
    "move": ("x", "y"),
}


class McpToolAdapter:
    """Maps stable capabilities onto configurable MCP tool names.

    Tool names and argument renames live in config so an upstream MCP update
    usually changes configuration, not agent code.
    """

    name = "mcp"

    def __init__(
        self,
        client: StdioMCPClient,
        tools: dict[str, str] | None = None,
        tool_args: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.client = client
        self.tools = {**DEFAULT_TOOLS, **(tools or {})}
        self.tool_args = {
            str(tool): {str(source): str(target) for source, target in mapping.items()}
            for tool, mapping in (tool_args or {}).items()
        }

    def observe(self, step: int, include_screenshot: bool = False) -> Observation:
        data = self._call("observe", {})
        raw_elements = (
            data.get("elements")
            or data.get("items")
            or data.get("controls")
            or data.get("ui_tree")
            or []
        )
        elements: list[Element] = []
        if isinstance(raw_elements, list):
            for index, item in enumerate(raw_elements[:200]):
                if isinstance(item, dict):
                    elements.append(self._element(index, item))

        active_window = (
            data.get("active_window")
            or data.get("window")
            or data.get("title")
            or data.get("app")
        )
        summary = (
            data.get("summary")
            or data.get("text")
            or data.get("message")
            or f"{len(elements)} elements"
        )
        screenshot_path: str | None = None
        raw: dict[str, Any] = {"mcp": True}
        if include_screenshot:
            try:
                shot = self._call("screenshot", {})
                screenshot_path = (
                    shot.get("path")
                    or shot.get("file")
                    or shot.get("screenshot_path")
                )
            except MCPError as exc:
                raw["screenshot_error"] = str(exc)

        return Observation(
            step=step,
            source="uia",
            summary=str(summary)[:2000],
            elements=elements,
            active_window=str(active_window) if active_window else None,
            screenshot_path=str(screenshot_path) if screenshot_path else None,
            raw=raw,
        )

    def execute(self, capability: str, args: dict[str, Any]) -> Receipt:
        try:
            if capability == "desktop.act":
                return self._desktop_act(args)
            if capability == "desktop.verify":
                data = self._call("wait_for", self._verify_args(args))
                return self._receipt(data)
            if capability == "terminal.exec":
                payload: dict[str, Any] = {
                    "command": str(args.get("command", ""))
                }
                if args.get("host"):
                    payload["session"] = args["host"]
                if args.get("timeout_s") is not None:
                    payload["timeout_s"] = args["timeout_s"]
                return self._receipt(self._call("exec", payload))
            if capability == "file.transfer":
                direction = str(args.get("direction", "upload"))
                tool_key = "upload" if direction == "upload" else "download"
                payload = {
                    "source": args.get("source", ""),
                    "destination": args.get("destination", ""),
                }
                return self._receipt(self._call(tool_key, payload))
            if capability == "wait.for":
                return self._receipt(self._call("wait_for", dict(args)))
        except MCPError as exc:
            return Receipt(False, error=str(exc))
        return Receipt(False, error=f"unsupported capability: {capability}")

    def verify(self, expect: str | dict[str, Any] | None) -> Verification:
        if expect is None:
            return Verification(True, "no expectation")
        args = expect if isinstance(expect, dict) else {"text": str(expect)}
        try:
            data = self._call("wait_for", args)
        except MCPError as exc:
            return Verification(False, f"verify failed: {exc}")
        error = data.get("error")
        ok = bool(data.get("ok", data.get("found", not error)))
        if error:
            ok = False
        detail = (
            data.get("detail")
            or data.get("text")
            or ("verified" if ok else "not verified")
        )
        return Verification(ok, str(detail), data)

    def close(self) -> None:
        self.client.close()

    def list_tools(self) -> list[dict[str, Any]]:
        return self.client.list_tools()

    def _desktop_act(self, args: dict[str, Any]) -> Receipt:
        action = str(args.get("action", ""))
        tool_key = _DESKTOP_ACTION_TO_TOOL.get(action)
        if tool_key is None:
            return Receipt(False, error=f"unsupported desktop action: {action}")
        allowed = _DESKTOP_ACTION_ARGS[tool_key]
        payload = {key: args[key] for key in allowed if key in args}
        return self._receipt(self._call(tool_key, payload))

    def _call(self, tool_key: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool_name = self.tools.get(tool_key)
        if not tool_name:
            raise MCPError(f"no MCP tool configured for {tool_key!r}")
        mapped = self._map_args(tool_key, arguments)
        return self._parse_result(self.client.call_tool(tool_name, mapped))

    def _map_args(
        self, tool_key: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        mapping = self.tool_args.get(tool_key, {})
        return {mapping.get(key, key): value for key, value in arguments.items()}

    @staticmethod
    def _verify_args(args: dict[str, Any]) -> dict[str, Any]:
        return {
            key: args[key]
            for key in ("text", "element_id", "window", "timeout_s")
            if key in args
        }

    @staticmethod
    def _parse_result(result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {"value": result}
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = result.get("content")
        if isinstance(content, list):
            texts = [
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            text = "\n".join(part for part in texts if part).strip()
            if text:
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = {"text": text}
                if isinstance(parsed, dict):
                    if result.get("isError"):
                        parsed.setdefault("error", text)
                    return parsed
                return {"value": parsed}
        if result.get("isError"):
            return {"error": "MCP tool returned isError"}
        return result

    @staticmethod
    def _element(index: int, item: dict[str, Any]) -> Element:
        element_id = (
            item.get("id")
            or item.get("element_id")
            or item.get("runtime_id")
            or item.get("handle")
            or f"e{index}"
        )
        role = (
            item.get("role")
            or item.get("control_type")
            or item.get("type")
            or ""
        )
        name = (
            item.get("name")
            or item.get("label")
            or item.get("text")
            or item.get("title")
            or ""
        )
        return Element(
            id=str(element_id),
            role=str(role),
            name=str(name),
            enabled=bool(item.get("enabled", True)),
            bounds=McpToolAdapter._bounds(
                item.get("bounds")
                or item.get("rect")
                or item.get("rectangle")
            ),
        )

    @staticmethod
    def _bounds(value: Any) -> tuple[int, int, int, int] | None:
        if isinstance(value, (list, tuple)) and len(value) == 4:
            try:
                return tuple(int(part) for part in value)  # type: ignore[return-value]
            except (TypeError, ValueError):
                return None
        if isinstance(value, dict):
            try:
                return (
                    int(value["x"]),
                    int(value["y"]),
                    int(value["width"]),
                    int(value["height"]),
                )
            except (KeyError, TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _receipt(data: dict[str, Any]) -> Receipt:
        error = data.get("error")
        ok = bool(data.get("ok", not error))
        if "exit_code" in data:
            try:
                ok = int(data["exit_code"]) == 0
            except (TypeError, ValueError):
                ok = False
        return Receipt(ok, data, str(error) if error else None)
