from __future__ import annotations

import os
import time

from xm_agent.config import AgentConfig
from xm_agent.maintenance import cleanup


def _age(path, days: float) -> None:
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


def _config(tmp_path) -> AgentConfig:
    config = AgentConfig()
    config.audit.root = str(tmp_path / "audit")
    config.runtime.screenshot_dir = str(tmp_path / "shots")
    (tmp_path / "audit").mkdir()
    (tmp_path / "shots").mkdir()
    return config


def test_cleanup_removes_expired_audit_and_screenshots(tmp_path):
    config = _config(tmp_path)
    old_audit = tmp_path / "audit" / "old.jsonl"
    new_audit = tmp_path / "audit" / "new.jsonl"
    old_shot = tmp_path / "shots" / "old.png"
    new_shot = tmp_path / "shots" / "new.jpg"
    other = tmp_path / "shots" / "notes.txt"
    for path, days in (
        (old_audit, 30),
        (new_audit, 0),
        (old_shot, 30),
        (new_shot, 0),
        (other, 30),
    ):
        path.write_bytes(b"x" * 10)
        _age(path, days)

    report = cleanup(config, keep_days=7)

    assert not old_audit.exists()
    assert new_audit.exists()
    assert not old_shot.exists()
    assert new_shot.exists()
    assert other.exists()
    assert report["keep_days"] == 7
    assert report["removed"] == 2
    assert report["freed_bytes"] == 10


def test_cleanup_dry_run_keeps_expired_files(tmp_path):
    config = _config(tmp_path)
    old_audit = tmp_path / "audit" / "old.jsonl"
    old_shot = tmp_path / "shots" / "old.png"
    for path in (old_audit, old_shot):
        path.write_bytes(b"x")
        _age(path, 30)

    report = cleanup(config, dry_run=True)

    assert old_audit.exists()
    assert old_shot.exists()
    assert report["dry_run"] is True
    assert report["removed"] == 2


def test_cleanup_ignores_missing_directories(tmp_path):
    config = AgentConfig()
    config.audit.root = str(tmp_path / "nope-audit")
    config.runtime.screenshot_dir = str(tmp_path / "nope-shots")

    report = cleanup(config)

    assert report["removed"] == 0
    assert report["freed_bytes"] == 0
    assert not (tmp_path / "nope-audit").exists()
    assert not (tmp_path / "nope-shots").exists()


def test_cleanup_keep_days_has_floor(tmp_path):
    config = _config(tmp_path)
    shot = tmp_path / "shots" / "two-days.png"
    shot.write_bytes(b"x")
    _age(shot, 2)

    report = cleanup(config, keep_days=0)

    assert report["keep_days"] == 1
    assert report["removed"] == 1
    assert not shot.exists()
