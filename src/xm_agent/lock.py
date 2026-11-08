from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


class LeaseLock:
    """Single-machine lease lock with expiry-based stale takeover.

    The lock is intentionally not exposed as an MCP tool. The runtime acquires
    it for the whole task so a crashed model cannot leave a permanent lock.
    """

    def __init__(self, path: str | Path, ttl_seconds: int = 60) -> None:
        self.path = Path(path)
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def acquire(self, owner: str = "agent", wait_seconds: float = 0.0) -> str | None:
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            token = uuid.uuid4().hex
            payload = {
                "owner": owner,
                "token": token,
                "expires_at": time.time() + self.ttl_seconds,
            }
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._takeover_if_stale():
                    continue
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.05)
                continue

            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                return token
            except Exception:
                self.path.unlink(missing_ok=True)
                raise

    def heartbeat(self, token: str) -> bool:
        data = self._read()
        if not data or data.get("token") != token:
            return False
        data["expires_at"] = time.time() + self.ttl_seconds
        temp_path = self.path.with_name(f"{self.path.name}.{token}.tmp")
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temp_path, self.path)
        return True

    def release(self, token: str) -> bool:
        data = self._read()
        if not data or data.get("token") != token:
            return False
        self.path.unlink(missing_ok=True)
        return True

    def holder(self) -> dict[str, Any] | None:
        return self._read()

    def _read(self) -> dict[str, Any] | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _takeover_if_stale(self) -> bool:
        data = self._read()
        if not data:
            return False
        try:
            expires_at = float(data.get("expires_at", 0))
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at > time.time():
            return False
        self.path.unlink(missing_ok=True)
        return True
