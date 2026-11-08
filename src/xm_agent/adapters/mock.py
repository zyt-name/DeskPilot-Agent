from __future__ import annotations

from typing import Any

from ..types import Element, Observation, Receipt, Verification


class MockAdapter:
    """Deterministic desktop adapter used by the demo and unit tests."""

    name = "mock"

    def __init__(self) -> None:
        self.window = "Notepad"
        self.text = ""
        self.saved = False
        self.clicks: list[str] = []
        self.commands: list[str] = []
        self.transfers: list[dict[str, str]] = []

    def observe(self, step: int, include_screenshot: bool = False) -> Observation:
        elements = [
            Element("e1", "Edit", "text", True, (10, 10, 300, 40)),
            Element("e2", "Button", "Save", True, (320, 10, 60, 30)),
            Element("e3", "Button", "Submit", not self.saved, (390, 10, 70, 30)),
        ]
        return Observation(
            step=step,
            source="mock",
            summary=(
                f"window={self.window}; text={self.text!r}; saved={self.saved}"
            ),
            elements=elements,
            active_window=self.window,
            screenshot_path="mock://screenshot.png" if include_screenshot else None,
            raw={"mock": True},
        )

    def execute(self, capability: str, args: dict[str, Any]) -> Receipt:
        if capability == "desktop.act":
            return self._desktop_act(args)
        if capability == "desktop.verify":
            expected = {key: args[key] for key in ("text", "element_id", "window") if key in args}
            verification = self.verify(expected or None)
            return Receipt(verification.ok, verification.evidence, verification.detail)
        if capability == "terminal.exec":
            command = str(args.get("command", ""))
            self.commands.append(command)
            return Receipt(
                True,
                {
                    "exit_code": 0,
                    "stdout": f"mock: {command}\n",
                    "stderr": "",
                },
            )
        if capability == "file.transfer":
            self.transfers.append(
                {str(key): str(value) for key, value in args.items()}
            )
            return Receipt(True, {"ok": True, "bytes": 0})
        if capability == "wait.for":
            return Receipt(True, {"ok": True, "detail": "mock wait completed"})
        return Receipt(False, error=f"unsupported capability: {capability}")

    def verify(self, expect: str | dict[str, Any] | None) -> Verification:
        state = self._state()
        if expect is None:
            return Verification(True, "no expectation", {"state": state})
        if isinstance(expect, dict):
            mismatches = {
                key: value
                for key, value in expect.items()
                if state.get(key) != value
            }
            return Verification(
                not mismatches,
                "state matched" if not mismatches else f"mismatch: {mismatches}",
                {"state": state},
            )
        text = str(expect)
        if text == "saved":
            ok = self.saved
        elif text.startswith("text="):
            ok = self.text == text[5:]
        elif text in state:
            ok = bool(state[text])
        else:
            ok = text in self.text or text in self.window
        return Verification(
            ok,
            f"expect {text!r}: {'matched' if ok else 'not matched'}",
            {"state": state},
        )

    def close(self) -> None:
        return None

    def _desktop_act(self, args: dict[str, Any]) -> Receipt:
        action = str(args.get("action", ""))
        if action == "type":
            text = str(args.get("text", ""))
            self.text += text
            return Receipt(True, {"ok": True, "text": self.text})
        if action == "click":
            element_id = str(args.get("element_id", ""))
            self.clicks.append(element_id)
            if element_id == "e2":
                self.saved = True
            if element_id == "e3":
                self.saved = True
            return Receipt(
                True, {"ok": True, "element_id": element_id, "saved": self.saved}
            )
        if action == "key":
            keys = str(args.get("keys", "")).casefold()
            if keys in {"ctrl+s", "ctrl+shift+s"}:
                self.saved = True
            return Receipt(True, {"ok": True, "keys": keys, "saved": self.saved})
        if action in {"scroll", "move"}:
            return Receipt(True, {"ok": True, "action": action})
        return Receipt(False, error=f"unsupported desktop action: {action}")

    def _state(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "text": self.text,
            "saved": self.saved,
        }
