from __future__ import annotations

import re
from pathlib import Path

from .types import Action, PolicyDecision

_DANGEROUS_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\brm\s+-[a-z]*r[a-z]*f\b",
        r"\brm\s+-[a-z]*f[a-z]*r\b",
        r"\bdel\b[^\n]*\s/[fsq]\b",
        r"\bformat\b",
        r"\bmkfs\b",
        r"\bshutdown\b",
        r"\bremove-item\b[^\n]*-recurse\b",
        r"\breg\s+delete\b",
        r"\bgit\s+push\b[^\n]*--force\b",
        r"\bdrop\s+(database|table)\b",
        r">\s*/dev/sd[a-z]",
    )
]


class Policy:
    """Default-deny policy evaluated before every adapter call."""

    def __init__(
        self,
        allowed_capabilities: list[str] | set[str],
        allowed_command_prefixes: list[str] | None = None,
        require_confirmation: list[str] | set[str] | None = None,
        allowed_paths: list[str] | None = None,
    ) -> None:
        self.allowed_capabilities = set(allowed_capabilities)
        self.allowed_command_prefixes = list(allowed_command_prefixes or [])
        self.require_confirmation = set(require_confirmation or [])
        self.allowed_paths = [Path(item).resolve() for item in (allowed_paths or [])]

    def check(self, action: Action) -> PolicyDecision:
        capability = action.capability
        if capability not in self.allowed_capabilities:
            return PolicyDecision("deny", f"capability not allowed: {capability}")

        if capability == "terminal.exec":
            command = str(action.args.get("command", "")).strip()
            if not command:
                return PolicyDecision("deny", "empty command")
            if not self._command_allowed(command):
                return PolicyDecision(
                    "deny", "command is not in the configured allowlist"
                )
            if any(pattern.search(command) for pattern in _DANGEROUS_PATTERNS):
                return PolicyDecision(
                    "confirm", "destructive command pattern requires confirmation"
                )

        if capability == "file.transfer":
            for key in ("source", "destination"):
                value = action.args.get(key)
                if not value:
                    return PolicyDecision("deny", f"missing file.transfer arg: {key}")
                if not self._path_allowed(str(value)):
                    return PolicyDecision(
                        "deny", f"path outside allowed roots: {value}"
                    )

        if capability in self.require_confirmation:
            return PolicyDecision(
                "confirm", f"capability requires confirmation: {capability}"
            )

        return PolicyDecision("allow", "allowed")

    def _command_allowed(self, command: str) -> bool:
        normalized = command.casefold().lstrip()
        return any(
            normalized.startswith(prefix.casefold())
            for prefix in self.allowed_command_prefixes
        )

    def _path_allowed(self, value: str) -> bool:
        if not self.allowed_paths:
            return False
        try:
            path = Path(value).resolve()
        except OSError:
            return False
        return any(
            path == root or root in path.parents for root in self.allowed_paths
        )
