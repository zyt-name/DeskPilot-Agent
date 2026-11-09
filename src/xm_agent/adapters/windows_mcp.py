from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

from .. import vision
from ..mcp_stdio import MCPError, StdioMCPClient
from ..types import Element, Observation, Receipt, Verification
from .mcp_tool import McpToolAdapter

WINDOWS_TOOLS: dict[str, str] = {
    "observe": "Snapshot",
    "screenshot": "Screenshot",
    "click": "Click",
    "type": "Type",
    "key": "Shortcut",
    "scroll": "Scroll",
    "move": "Move",
    "wait_for": "WaitFor",
    "app": "App",
}

_ELEMENT_RE = re.compile(
    r'\((?P<x>-?\d+),(?P<y>-?\d+)\)\s+(?P<role>.+?)\s+"(?P<name>(?:[^"\\]|\\.)*)"\s+\[action:\s*(?P<action>[^\]]*)\]'
)
_WINDOW_RE = re.compile(r'window\s+"(?P<name>(?:[^"\\]|\\.)*)"')

# Browser DOM trees expose every rendered word as a clickable node. Keeping
# them would fill the element budget with single-character "elements" and
# hide the buttons the model actually needs.
_NOISY_ROLES = {"word", "text"}

_SECTION_HEADERS = (
    "Active Desktop:",
    "All Desktops:",
    "Focused Window:",
    "Opened Windows:",
    "UI Tree:",
)


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def parse_snapshot(text: str) -> tuple[list[Element], list[str], str | None]:
    """Parse Windows-MCP Snapshot text into contract elements.

    Snapshot returns human-readable text plus an optional image; the UI tree
    lines use ``(x,y) control_type "name" [action: ...]``. We keep the click
    point as the element bounds because that is all the tree exposes.
    """
    elements: list[Element] = []
    for match in _ELEMENT_RE.finditer(text):
        role = match.group("role").strip().lower()
        if role in _NOISY_ROLES:
            continue
        if len(elements) >= 200:
            break
        name = _unescape(match.group("name"))
        elements.append(
            Element(
                id=f"e{len(elements)}",
                role=role,
                name=name,
                bounds=(
                    int(match.group("x")),
                    int(match.group("y")),
                    1,
                    1,
                ),
            )
        )

    windows = [
        _unescape(match.group("name")) for match in _WINDOW_RE.finditer(text)
    ]
    active_window = _first_table_name(_section(text, "Focused Window:"))
    if active_window is None and windows:
        active_window = windows[0]
    return elements, windows, active_window


def _section(text: str, header: str) -> list[str]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != header:
            continue
        block: list[str] = []
        for candidate in lines[index + 1 :]:
            stripped = candidate.strip()
            if stripped in _SECTION_HEADERS:
                break
            block.append(candidate)
            if len(block) > 80:
                break
        return block
    return []


def _first_table_name(block: list[str]) -> str | None:
    seen_separator = False
    for line in block:
        stripped = line.strip()
        if not stripped:
            continue
        if set(stripped) <= {"-", " "}:
            seen_separator = True
            continue
        if not seen_separator:
            continue
        name = re.split(r"\s{2,}", stripped, maxsplit=1)[0].strip()
        return name or None
    return None


class WindowsMcpAdapter(McpToolAdapter):
    """Adapter for CursorTouch/Windows-MCP.

    Its Snapshot tool returns text (and optional image) instead of structured
    content, so observation parsing and argument shapes differ from the generic
    adapter. Everything else still goes through the configurable tool map.
    """

    name = "windows_mcp"

    def __init__(
        self,
        client: StdioMCPClient,
        tools: dict[str, str] | None = None,
        tool_args: dict[str, dict[str, str]] | None = None,
        screenshot_dir: str | Path = ".xm-agent/screenshots",
    ) -> None:
        super().__init__(client, {**WINDOWS_TOOLS, **(tools or {})}, tool_args)
        self.screenshot_dir = Path(screenshot_dir)
        self._coords: dict[str, tuple[int, int]] = {}
        self._names: dict[str, str] = {}

    def observe(self, step: int, include_screenshot: bool = False) -> Observation:
        result = self.client.call_tool(
            self.tools["observe"],
            {
                "use_vision": bool(include_screenshot),
                "use_ui_tree": True,
                "use_annotation": False,
            },
        )
        text = _text_of(result)
        elements, windows, active_window = parse_snapshot(text)
        self._coords = {
            element.id: (element.bounds[0], element.bounds[1])
            for element in elements
            if element.bounds
        }
        self._names = {element.id: element.name for element in elements}
        summary = (
            f"{len(elements)} elements, {len(windows)} windows, "
            f"active window: {active_window or 'unknown'}"
        )
        raw: dict[str, Any] = {"mcp": "windows_mcp", "windows": windows[:20]}
        screenshot_path: str | None = None
        if include_screenshot:
            saved = self._save(_image_of(result), prefix="snapshot")
            screenshot_path = str(saved.get("path") or "") or None
            if screenshot_path is None:
                screenshot_path = self.capture_screenshot(prefix="snapshot")
            if screenshot_path:
                raw["screenshot_hash"] = vision.digest_path(screenshot_path)
        return Observation(
            step=step,
            source="uia",
            summary=summary,
            elements=elements,
            active_window=active_window,
            screenshot_path=screenshot_path,
            raw=raw,
        )

    def capture_screenshot(self, prefix: str = "audit") -> str | None:
        """Capture the desktop for the top-level vision auditor."""
        try:
            result = self.client.call_tool(self.tools["screenshot"], {})
            saved = self._save(_image_of(result), prefix=prefix)
            if saved.get("path"):
                return str(saved["path"])
        except Exception:
            pass
        saved = vision.capture_primary_screen(
            self.screenshot_dir, prefix=prefix
        )
        if not saved:
            return None
        return str(saved.get("path") or "") or None

    def _save(self, image: bytes | None, *, prefix: str) -> dict[str, Any]:
        if not image:
            return {}
        return vision.save_image(image, self.screenshot_dir, prefix=prefix)

    def execute(self, capability: str, args: dict[str, Any]) -> Receipt:
        if capability == "desktop.verify":
            return self._wait_receipt(args)
        if capability == "wait.for":
            return self._wait_receipt(args)
        try:
            if capability == "desktop.act":
                return self._desktop_act(args)
        except MCPError as exc:
            return Receipt(False, error=str(exc))
        return Receipt(False, error=f"unsupported capability: {capability}")

    def verify(self, expect: str | dict[str, Any] | None) -> Verification:
        if expect is None:
            return Verification(True, "no expectation")
        args = (
            expect
            if isinstance(expect, dict)
            else {"text": _exact_expect(str(expect))}
        )
        try:
            data = self._call("wait_for", self._wait_args(args))
        except MCPError as exc:
            return Verification(False, f"verify failed: {exc}")
        error = data.get("error")
        if error:
            return Verification(False, str(error), data)
        text = str(data.get("text", ""))
        ok = "satisfied" in text.casefold() or not text
        return Verification(ok, text or "verified", data)

    def _desktop_act(self, args: dict[str, Any]) -> Receipt:
        action = str(args.get("action", ""))
        if action == "click":
            payload: dict[str, Any] = {"button": args.get("button", "left")}
            payload.update(self._loc(args))
            return self._receipt(self._call("click", payload))
        if action == "type":
            payload = {"text": str(args.get("text", ""))}
            payload.update(self._loc(args))
            if args.get("clear") is not None:
                payload["clear"] = bool(args["clear"])
            if args.get("press_enter") is not None:
                payload["press_enter"] = bool(args["press_enter"])
            return self._receipt(self._call("type", payload))
        if action == "key":
            keys = str(args.get("keys") or args.get("text") or "")
            return self._receipt(self._call("key", {"shortcut": keys}))
        if action == "scroll":
            amount = int(args.get("amount", 1))
            payload = {
                "direction": "down" if amount >= 0 else "up",
                "type": "vertical",
                "wheel_times": max(1, abs(amount)),
            }
            if (
                args.get("x") is not None
                or args.get("y") is not None
                or args.get("element_id")
            ):
                payload.update(self._loc(args))
            return self._receipt(self._call("scroll", payload))
        if action == "move":
            return self._receipt(self._call("move", self._loc(args)))
        return Receipt(False, error=f"unsupported desktop action: {action}")

    def _call(
        self, tool_key: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        data = super()._call(tool_key, arguments)
        deadline = time.monotonic() + 30.0
        while True:
            wait = _user_control_wait(data)
            if wait is None:
                return data
            # The human at the desk keeps priority; wait for a full idle window
            # instead of turning the transient guard into a task error.
            if time.monotonic() + wait + 0.25 > deadline:
                return data
            time.sleep(wait + 0.25)
            data = super()._call(tool_key, arguments)

    def _loc(self, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("x") is not None and args.get("y") is not None:
            return {"loc": [int(args["x"]), int(args["y"])]}
        element_id = str(args.get("element_id", ""))
        if not element_id:
            raise MCPError(
                "action requires element_id or x/y; browser address bars are "
                "not in the UIA tree, so use Win+R to enter URLs"
            )
        coords = self._coords.get(element_id)
        if coords is None:
            raise MCPError(
                f"unknown element_id {element_id!r}; call desktop.observe first"
            )
        return {"loc": [coords[0], coords[1]]}

    def _wait_receipt(self, args: dict[str, Any]) -> Receipt:
        try:
            data = self._call("wait_for", self._wait_args(args))
        except MCPError as exc:
            return Receipt(False, error=str(exc))
        error = data.get("error")
        text = str(data.get("text", ""))
        ok = not error and "satisfied" in text.casefold()
        return Receipt(ok, data, str(error) if error else None)

    def _wait_args(self, args: dict[str, Any]) -> dict[str, Any]:
        timeout = args.get("timeout_s", 10)
        if args.get("window"):
            condition = "active_window"
            payload = {"condition": condition, "window_name": str(args["window"])}
        elif args.get("text"):
            condition = "text_exists"
            payload = {"condition": condition, "text": str(args["text"])}
        elif args.get("element_id"):
            element_id = str(args["element_id"])
            name = self._names.get(element_id)
            if not name:
                raise MCPError(
                    f"unknown element_id {element_id!r}; call desktop.observe first"
                )
            payload = {"condition": "element_exists", "text": name}
        else:
            raise MCPError("wait requires text, window or element_id")
        payload["timeout"] = float(timeout)
        return payload


_QUOTED_RE = re.compile(r"[「\"“'‘]([^」\"”'’]{1,80})[」\"”'’]")
_URL_RE = re.compile(r"https?://[^\s，。；、）)」\"”'’]+")


def _exact_expect(value: str) -> str:
    """Turn a model-written expectation into text WaitFor can match.

    WaitFor does exact UIA text matching, but models often phrase expect as a
    sentence (``出现「运行」对话框``). Prefer the quoted fragment or URL inside
    it; otherwise keep the original text.
    """
    text = value.strip()
    quoted = _QUOTED_RE.search(text)
    if quoted:
        return quoted.group(1).strip()
    url = _URL_RE.search(text)
    if url:
        return url.group(0).strip()
    return text


def _user_control_wait(data: dict[str, Any]) -> float | None:
    error = data.get("error")
    payload: Any = error
    if isinstance(error, str):
        try:
            payload = json.loads(error)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict) or payload.get("code") != "USER_CONTROL":
        return None
    status = payload.get("status")
    if not isinstance(status, dict):
        return 0.0
    try:
        return max(0.0, float(status.get("wait_seconds") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _image_of(result: dict[str, Any]) -> bytes | None:
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "image":
            continue
        data = item.get("data")
        if not data:
            continue
        try:
            return base64.b64decode(data)
        except Exception:
            return None
    return None


def _text_of(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return str(result)
    content = result.get("content")
    if isinstance(content, list):
        text = "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    else:
        text = str(result)

    # FastMCP serializes the Snapshot list return value as one JSON array
    # string, so unwrap it before parsing the tree.
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, list):
            return "\n".join(str(part) for part in parsed)
    return text


__all__ = ["WindowsMcpAdapter", "parse_snapshot", "WINDOWS_TOOLS"]
