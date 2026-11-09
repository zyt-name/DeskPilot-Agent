"""Retention cleanup for audit logs and screenshots."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .config import AgentConfig

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
MIN_KEEP_DAYS = 1


def _prune_images(root: str | Path, keep_days: int, dry_run: bool) -> dict[str, Any]:
    path = Path(root)
    stats: dict[str, Any] = {
        "path": str(path),
        "removed": 0,
        "kept": 0,
        "freed_bytes": 0,
    }
    if not path.is_dir():
        return stats
    cutoff = time.time() - max(MIN_KEEP_DAYS, int(keep_days)) * 86400
    for item in path.iterdir():
        try:
            if not item.is_file() or item.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            info = item.stat()
        except OSError:
            continue
        if info.st_mtime >= cutoff:
            stats["kept"] += 1
            continue
        if not dry_run:
            try:
                item.unlink()
            except OSError:
                continue
        stats["removed"] += 1
        stats["freed_bytes"] += int(info.st_size)
    return stats


def cleanup(
    config: AgentConfig,
    *,
    keep_days: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete audit jsonl files and screenshots older than the retention window.

    Only the two application-owned directories are touched, and only files with
    the expected suffix are considered. Sub-directories are never removed.
    """

    requested = config.audit.keep_days if keep_days is None else keep_days
    days = max(MIN_KEEP_DAYS, int(requested))
    audit_root = Path(config.audit.root)
    if audit_root.is_dir():
        audit_removed = AuditLog(audit_root, days).prune(dry_run=dry_run)
    else:
        audit_removed = 0
    screenshots = _prune_images(
        config.runtime.screenshot_dir, days, dry_run
    )
    return {
        "keep_days": days,
        "dry_run": bool(dry_run),
        "audit": {"path": config.audit.root, "removed": audit_removed},
        "screenshots": screenshots,
        "removed": audit_removed + screenshots["removed"],
        "freed_bytes": screenshots["freed_bytes"],
    }


__all__ = ["cleanup", "IMAGE_SUFFIXES", "MIN_KEEP_DAYS"]
