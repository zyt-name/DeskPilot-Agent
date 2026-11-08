from __future__ import annotations

from typing import Any

from ..mcp_stdio import MCPError
from ..types import Observation, Receipt, Verification
from .mcp_tool import McpToolAdapter

XSHELL_TOOLS: dict[str, str] = {
    "exec": "xshell_exec",
    "send": "xshell_send",
    "read": "xshell_read_screen",
    "wait_for": "xshell_wait_for",
    "sessions": "xshell_list_sessions",
}


class XshellMcpAdapter(McpToolAdapter):
    """Adapter for 2head1i/xshell-mcp.

    The server drives existing Xshell tabs through a file-queue bridge, so a
    bridge script must be running inside the target tab. It reports screen text
    rather than an exit code, and it has no SFTP tools.
    """

    name = "xshell_mcp"

    def __init__(self, client, tools=None, tool_args=None) -> None:
        super().__init__(client, {**XSHELL_TOOLS, **(tools or {})}, tool_args)
        self.default_session = "default"

    def observe(self, step: int, include_screenshot: bool = False) -> Observation:
        try:
            data = self._call(
                "read",
                {
                    "rows": 60,
                    "columns": 200,
                    "session": self.default_session,
                },
            )
        except MCPError as exc:
            return Observation(
                step=step,
                source="terminal",
                summary=f"terminal read failed: {exc}",
                raw={"mcp": "xshell_mcp"},
            )
        text = str(data.get("text") or data.get("value") or "")
        return Observation(
            step=step,
            source="terminal",
            summary=text[-2000:] or "terminal screen is empty",
            raw={"mcp": "xshell_mcp"},
        )

    def execute(self, capability: str, args: dict[str, Any]) -> Receipt:
        if capability == "terminal.exec":
            try:
                data = self._call("exec", self._exec_args(args))
            except MCPError as exc:
                return Receipt(False, error=str(exc))
            if data.get("error"):
                return Receipt(False, data, str(data["error"]))
            text = str(data.get("text", ""))
            return Receipt(
                True,
                {"exit_code": 0, "stdout": text, "stderr": ""},
            )
        if capability == "wait.for":
            try:
                data = self._call("wait_for", self._wait_args(args))
            except MCPError as exc:
                return Receipt(False, error=str(exc))
            return self._receipt(data)
        if capability == "file.transfer":
            return Receipt(
                False,
                error="xshell-mcp has no file transfer tools; use an SFTP backend",
            )
        return Receipt(
            False, error=f"unsupported capability for xshell backend: {capability}"
        )

    def verify(self, expect: str | dict[str, Any] | None) -> Verification:
        if expect is None:
            return Verification(True, "no expectation")
        args = expect if isinstance(expect, dict) else {"text": str(expect)}
        if not args.get("text"):
            return Verification(False, "terminal verification requires text")
        try:
            data = self._call("wait_for", self._wait_args(args))
        except MCPError as exc:
            return Verification(False, f"verify failed: {exc}")
        if data.get("error"):
            return Verification(False, str(data["error"]), data)
        text = str(data.get("text", ""))
        return Verification(True, text or "matched", data)

    def _exec_args(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "command": str(args.get("command", "")),
            "session": args.get("host") or self.default_session,
            "timeout_s": args.get("timeout_s", 30),
        }

    def _wait_args(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "text": str(args.get("text", "")),
            "session": args.get("host") or self.default_session,
            "timeout_s": args.get("timeout_s", 30),
        }

    def _map_args(
        self, tool_key: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        mapped = super()._map_args(tool_key, arguments)
        if "session" in mapped:
            mapped["sessionId"] = mapped.pop("session")
        if "timeout_s" in mapped:
            seconds = float(mapped.pop("timeout_s"))
            mapped["timeoutMs"] = max(1, int(seconds * 1000))
        return mapped


__all__ = ["XshellMcpAdapter", "XSHELL_TOOLS"]
