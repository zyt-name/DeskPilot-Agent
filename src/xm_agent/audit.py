from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_SECRET_KEYS = {
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "secret",
    "authorization",
    "private_key",
}


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<max-depth>"
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if str(key).lower() in _SECRET_KEYS
                else _redact(item, depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in list(value)[:100]]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "...<truncated>"
    return value


class AuditLog:
    def __init__(self, root: str | Path, keep_days: int = 7) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.keep_days = max(1, int(keep_days))

    def event(self, trace_id: str, event: str, payload: Any) -> Path:
        record = {
            "ts": time.time(),
            "trace_id": trace_id,
            "event": event,
            "payload": _redact(payload),
        }
        path = self.path_for(trace_id)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path

    def path_for(self, trace_id: str) -> Path:
        return self.root / f"{trace_id}.jsonl"

    def prune(self) -> int:
        cutoff = time.time() - self.keep_days * 86400
        removed = 0
        for path in self.root.glob("*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        return removed
