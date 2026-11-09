from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

from .. import vision
from ..mcp_stdio import MCPError, StdioMCPClient
from ..types import Observation, Receipt, Verification

_CLICK_ACTIONS = {
    "left": "left_click",
    "right": "right_click",
    "middle": "middle_click",
    "double": "double_click",
}

_SCROLL_PIXELS_PER_STEP = 100
_SCROLL_DEFAULT_PIXELS = 300


class ComputerUseMcpAdapter:
    """Adapter for domdomegg/computer-use-mcp.

    The backend exposes one ``computer`` tool that takes a screenshot or a
    coordinate-based mouse/keyboard action. It has no element tree, so every
    coordinate must come from the screenshot the model just saw.
    """

    name = "computer_use_mcp"
    vision_only = True

    def __init__(
        self,
        client: StdioMCPClient,
        tools: dict[str, str] | None = None,
        tool_args: dict[str, dict[str, str]] | None = None,
        screenshot_dir: str | Path = ".xm-agent/screenshots",
    ) -> None:
        self.client = client
        self.tool_name = (tools or {}).get("computer", "computer")
        self.screenshot_dir = Path(screenshot_dir)
        self._last_size = (0, 0)
        self._sent_size = (0, 0)
        self._shots = 0

    def observe(self, step: int, include_screenshot: bool = False) -> Observation:
        data, image = self._invoke({"action": "get_screenshot"})
        native_width = int(data.get("image_width") or 0)
        native_height = int(data.get("image_height") or 0)
        self._last_size = (native_width, native_height)
        saved = self._save(image, prefix="shot")
        width = int(saved.get("width") or native_width)
        height = int(saved.get("height") or native_height)
        self._sent_size = (width, height)
        path = str(saved.get("path") or "") or None
        summary = (
            f"screenshot {width}x{height}; give x/y in this image space "
            f"(no UI element ids on this backend)"
        )
        if width != native_width:
            summary += f"; scaled from {native_width}x{native_height}"
        raw: dict[str, Any] = {
            "mcp": "computer_use_mcp",
            "image_width": width,
            "image_height": height,
            "native_image_width": native_width,
            "native_image_height": native_height,
            "image_scale": float(saved.get("scale") or 1.0),
            "screenshot_hash": saved.get("hash", ""),
            "screenshot_bytes": saved.get("bytes", 0),
            "source_bytes": saved.get("source_bytes", 0),
        }
        return Observation(
            step=step,
            source="vision",
            summary=summary,
            elements=[],
            screenshot_path=path,
            raw=raw,
        )

    def capture_screenshot(self) -> str | None:
        """Take one fresh screenshot for the top-level vision auditor."""
        try:
            data, image = self._invoke({"action": "get_screenshot"})
        except MCPError:
            return None
        native_width = int(data.get("image_width") or 0)
        native_height = int(data.get("image_height") or 0)
        if native_width and native_height:
            self._last_size = (native_width, native_height)
        saved = self._save(image, prefix="audit")
        return str(saved.get("path") or "") or None

    def execute(self, capability: str, args: dict[str, Any]) -> Receipt:
        try:
            if capability == "desktop.act":
                return self._desktop_act(args)
            if capability in {"desktop.verify", "wait.for"}:
                return self._capture_receipt(capability, args)
        except MCPError as exc:
            return Receipt(False, error=str(exc))
        return Receipt(False, error=f"unsupported capability: {capability}")

    def verify(self, expect: str | dict[str, Any] | None) -> Verification:
        # This backend has no element tree, so text expectations cannot be
        # checked here. The runtime asks the vision model before task.finish.
        if expect is None:
            return Verification(True, "no expectation", deferred=True)
        try:
            data, image = self._invoke({"action": "get_screenshot"})
        except MCPError as exc:
            return Verification(False, f"verify failed: {exc}")
        saved = self._save(image, prefix="verify")
        return Verification(
            True,
            "screenshot captured; semantic check deferred to the vision model",
            {
                "screenshot_path": saved.get("path"),
                "expect": expect,
                "deferred": True,
                "image_width": saved.get("width") or data.get("image_width"),
                "image_height": saved.get("height") or data.get("image_height"),
                "screenshot_hash": saved.get("hash", ""),
            },
            deferred=True,
        )

    def close(self) -> None:
        self.client.close()

    def list_tools(self) -> list[dict[str, Any]]:
        return self.client.list_tools()

    def _desktop_act(self, args: dict[str, Any]) -> Receipt:
        action = str(args.get("action", ""))
        if action == "click":
            button = str(args.get("button", "left")).casefold()
            mapped = _CLICK_ACTIONS.get(button)
            if mapped is None:
                raise MCPError(f"unsupported click button: {button}")
            return self._receipt(
                self._invoke({"action": mapped, "coordinate": self._coord(args)})[0]
            )
        if action == "type":
            text = str(args.get("text", ""))
            if not text:
                raise MCPError("type requires text")
            if args.get("clear"):
                self._invoke({"action": "key", "text": "ctrl+a"})
            receipt = self._receipt(
                self._invoke({"action": "type", "text": text})[0]
            )
            if receipt.ok and args.get("press_enter"):
                press = self._receipt(
                    self._invoke({"action": "key", "text": "return"})[0]
                )
                if not press.ok:
                    return press
            return receipt
        if action == "key":
            keys = str(args.get("keys") or args.get("text") or "")
            if not keys:
                raise MCPError("key requires keys")
            return self._receipt(self._invoke({"action": "key", "text": keys})[0])
        if action == "move":
            return self._receipt(
                self._invoke(
                    {"action": "mouse_move", "coordinate": self._coord(args)}
                )[0]
            )
        if action == "scroll":
            amount = int(args.get("amount") or 0)
            if amount == 0:
                direction, pixels = "down", _SCROLL_DEFAULT_PIXELS
            else:
                direction = "down" if amount > 0 else "up"
                pixels = abs(amount) * _SCROLL_PIXELS_PER_STEP
            return self._receipt(
                self._invoke(
                    {
                        "action": "scroll",
                        "coordinate": self._coord(args, fallback_cursor=True),
                        "text": f"{direction}:{pixels}",
                    }
                )[0]
            )
        raise MCPError(f"unsupported desktop action: {action}")

    def _capture_receipt(self, capability: str, args: dict[str, Any]) -> Receipt:
        if capability == "wait.for" and args.get("timeout_s"):
            time.sleep(min(float(args["timeout_s"]), 10.0))
        data, image = self._invoke({"action": "get_screenshot"})
        saved = self._save(image, prefix="capture")
        return Receipt(
            True,
            {
                "screenshot_path": saved.get("path"),
                "deferred": True,
                "detail": "visual backend cannot check text or windows without vision",
                "image_width": saved.get("width") or data.get("image_width"),
                "image_height": saved.get("height") or data.get("image_height"),
                "screenshot_hash": saved.get("hash", ""),
            },
        )

    def _coord(
        self, args: dict[str, Any], fallback_cursor: bool = False
    ) -> list[int]:
        if args.get("x") is not None and args.get("y") is not None:
            sent_width, sent_height = self._sent_size
            native_width, native_height = self._last_size
            if (
                sent_width > 0
                and sent_height > 0
                and native_width > 0
                and native_height > 0
            ):
                return list(
                    vision.scale_point(
                        int(args["x"]),
                        int(args["y"]),
                        image_width=sent_width,
                        image_height=sent_height,
                        source_width=native_width,
                        source_height=native_height,
                    )
                )
            return [int(args["x"]), int(args["y"])]
        if args.get("element_id"):
            raise MCPError(
                "computer-use backend has no element ids; provide x/y from the screenshot"
            )
        if fallback_cursor:
            try:
                data = self._invoke({"action": "get_cursor_position"})[0]
                return [int(data["x"]), int(data["y"])]
            except (MCPError, KeyError, TypeError, ValueError):
                if self._last_size != (0, 0):
                    return [self._last_size[0] // 2, self._last_size[1] // 2]
        raise MCPError("action requires x and y coordinates from the screenshot")

    def _invoke(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bytes | None]:
        try:
            result = self.client.call_tool(self.tool_name, payload)
        except MCPError:
            raise
        except Exception as exc:
            raise MCPError(f"computer tool failed: {exc}") from exc

        content = result.get("content") if isinstance(result, dict) else None
        texts: list[str] = []
        image: bytes | None = None
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    texts.append(str(item.get("text", "")))
                elif item.get("type") == "image" and item.get("data"):
                    image = base64.b64decode(item["data"])
        text = "\n".join(part for part in texts if part).strip()
        if isinstance(result, dict) and result.get("isError"):
            raise MCPError(text or "computer tool returned isError")
        data: dict[str, Any] = {}
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise MCPError(f"unexpected computer tool output: {text[:500]}") from exc
            if not isinstance(parsed, dict):
                raise MCPError("computer tool output must be a JSON object")
            data = parsed
        return data, image

    def _save(self, image: bytes | None, *, prefix: str) -> dict[str, Any]:
        if image is None:
            return {}
        self._shots += 1
        return vision.save_image(
            image, self.screenshot_dir, prefix=f"{prefix}-{self._shots}"
        )

    @staticmethod
    def _receipt(data: dict[str, Any]) -> Receipt:
        error = data.get("error")
        ok = bool(data.get("ok", not error))
        return Receipt(ok, data, str(error) if error else None)


__all__ = ["ComputerUseMcpAdapter"]
