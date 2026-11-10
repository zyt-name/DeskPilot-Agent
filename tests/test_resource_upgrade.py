"""资源升级链路：旁路安装 → 探针 → 切换 → 失败回滚。

这些用例盯的都是「不能把用户正在用的版本弄坏」：
探针没通过不许切、切换失败要接回旧链接、失败版本要冷却、
跨大版本默认不许自动换。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from xm_agent.resources import (
    Resource,
    ResourceManager,
    UpdateCandidate,
    UpdateChecker,
    Upgrader,
    clean_version,
    relocate,
)


def _resource(**overrides) -> Resource:
    data = {
        "id": "test_mcp",
        "name": "Test MCP",
        "version": "1.4.2",
        "repo": "example/test-mcp",
        "ref": "0" * 40,
        "kind": "node",
        "directory": "test-mcp",
        "entry": "dist/main.js",
        "purpose": "test",
        "license": "MIT",
    }
    data.update(overrides)
    return Resource(**data)


def _manager(tmp_path: Path, resource: Resource) -> ResourceManager:
    return ResourceManager(tmp_path, resources=[resource])


def _fake_install(manager: ResourceManager, *, broken: set[str] | None = None):
    """把「下载 + 构建」换成直接造出目录与 marker。"""
    broken = broken or set()

    def install(resource, state, phase, emit, *, switch_link=True):
        dest = manager.root / resource.dest
        dest.mkdir(parents=True, exist_ok=True)
        for name in (resource.entry, *resource.runtime_checks):
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("ok", encoding="utf-8")
        phase("build", "假构建", 80)
        if resource.version in broken:
            raise RuntimeError("构建炸了")
        manager._write_marker(resource, dest, stage="ready", source="test")
        if switch_link and resource.kind != "browser":
            manager._ensure_link(
                resource, manager.root / resource.link, dest, phase
            )

    manager._install = install  # type: ignore[method-assign]


def _candidate(
    resource: Resource, latest: str, compatible: bool = True, reason: str = ""
):
    return UpdateCandidate(
        resource_id=resource.id,
        name=resource.name,
        current=resource.version,
        latest=latest,
        compatible=compatible,
        reason=reason or ("" if compatible else "跨大版本"),
    )


def _upgrader(manager, resource, probe):
    return Upgrader(
        manager,
        UpdateChecker(fetcher=lambda url: (500, b"")),
        prober=lambda res, dest: probe(res, dest),
    )


def _link_target(manager: ResourceManager, resource: Resource) -> str:
    link = manager.root / resource.link
    return link.resolve().name


# --------------------------------------------------------------- 目录与版本
def test_clean_version_strips_v_prefix():
    assert clean_version("v1.4.3") == "1.4.3"
    assert clean_version(" 1.4.3 ") == "1.4.3"
    assert clean_version("157.0.8103.50") == "157.0.8103.50"
    assert clean_version("") == ""


def test_with_version_keeps_tag_for_download_but_cleans_dir():
    resource = _resource()
    target = resource.with_version("v1.4.3", "v1.4.3")

    assert target.version == "1.4.3"
    assert target.ref == "v1.4.3"
    assert target.dest.endswith("test-mcp-1.4.3")


def test_stage_installs_beside_and_leaves_link_alone(tmp_path):
    resource = _resource()
    manager = _manager(tmp_path, resource)
    _fake_install(manager)

    manager.ensure(only=[resource.id])
    assert _link_target(manager, resource) == "test-mcp-1.4.2"

    state = manager.stage(resource.id, "v1.4.3", ref="v1.4.3")

    assert state.ok
    assert Path(state.location).name == "test-mcp-1.4.3"
    assert _link_target(manager, resource) == "test-mcp-1.4.2"
    assert manager.pins() == {}


# -------------------------------------------------------------------- 升级
def test_upgrade_switches_only_after_probe_passes(tmp_path):
    resource = _resource()
    manager = _manager(tmp_path, resource)
    _fake_install(manager)
    manager.ensure(only=[resource.id])

    seen: list[str] = []

    def probe(res, dest):
        seen.append(dest.name)
        return True, "MCP 握手成功，共 12 个工具"

    result = _upgrader(manager, resource, probe).upgrade(
        resource.id, candidate=_candidate(resource, "v1.4.3")
    )

    assert result.status == "upgraded"
    assert result.to_version == "1.4.3"
    assert seen == ["test-mcp-1.4.3"]  # 探针打在旁路目录上
    assert _link_target(manager, resource) == "test-mcp-1.4.3"
    assert manager.pins()[resource.id]["version"] == "1.4.3"
    # 状态面板看到的版本也要跟着变，否则会一直提示「版本不符」
    state = manager.status()[0]
    assert state.version == "1.4.3"
    assert state.ok


def test_probe_failure_deletes_candidate_and_keeps_old(tmp_path):
    resource = _resource()
    manager = _manager(tmp_path, resource)
    _fake_install(manager)
    manager.ensure(only=[resource.id])

    result = _upgrader(
        manager, resource, lambda res, dest: (False, "握手超时")
    ).upgrade(resource.id, candidate=_candidate(resource, "v1.4.3"))

    assert result.status == "failed"
    assert "探针未通过" in result.detail
    assert "已删除旁路版本" in result.detail
    assert _link_target(manager, resource) == "test-mcp-1.4.2"
    assert not (manager.root / "vendor" / "test-mcp-1.4.3").exists()
    assert manager.pins() == {}
    assert manager.status()[0].version == "1.4.2"


def test_build_failure_keeps_old_version_usable(tmp_path):
    resource = _resource()
    manager = _manager(tmp_path, resource)
    _fake_install(manager, broken={"1.4.3"})
    manager.ensure(only=[resource.id])

    result = _upgrader(
        manager, resource, lambda res, dest: (True, "ok")
    ).upgrade(resource.id, candidate=_candidate(resource, "v1.4.3"))

    assert result.status == "failed"
    assert "构建炸了" in result.detail
    assert _link_target(manager, resource) == "test-mcp-1.4.2"
    state = manager.status()[0]
    assert state.ok and state.version == "1.4.2"


def test_cross_major_needs_force(tmp_path):
    resource = _resource()
    manager = _manager(tmp_path, resource)
    _fake_install(manager)
    manager.ensure(only=[resource.id])
    upgrader = _upgrader(manager, resource, lambda res, dest: (True, "ok"))

    skipped = upgrader.upgrade(
        resource.id, candidate=_candidate(resource, "2.0.0", False)
    )
    assert skipped.status == "skipped"
    assert "跨大版本" in skipped.detail
    assert _link_target(manager, resource) == "test-mcp-1.4.2"

    forced = upgrader.upgrade(
        resource.id, candidate=_candidate(resource, "2.0.0", False), force=True
    )
    assert forced.status == "upgraded"
    assert _link_target(manager, resource) == "test-mcp-2.0.0"


def test_same_version_is_a_noop(tmp_path):
    resource = _resource()
    manager = _manager(tmp_path, resource)
    _fake_install(manager)
    manager.ensure(only=[resource.id])
    calls: list[str] = []

    result = _upgrader(
        manager, resource, lambda res, dest: (calls.append("x"), (True, ""))[1]
    ).upgrade(resource.id, candidate=_candidate(resource, "1.4.2"))

    assert result.status == "current"
    assert calls == []


def test_failed_version_cools_down(tmp_path):
    resource = _resource()
    manager = _manager(tmp_path, resource)
    _fake_install(manager)
    manager.ensure(only=[resource.id])
    upgrader = _upgrader(manager, resource, lambda res, dest: (False, "boom"))

    upgrader.upgrade(resource.id, candidate=_candidate(resource, "v1.4.3"))

    assert upgrader.blocked(resource.id, "1.4.3") is True
    assert upgrader.blocked(resource.id, "1.4.4") is False
    assert upgrader.pending([_candidate(resource, "v1.4.3")]) == []
    # 冷却期过了就允许再试
    record = upgrader.attempts()[resource.id]
    record["ts"] = time.time() - upgrader.cooldown - 1
    upgrader.attempts_path.write_text(
        json.dumps({resource.id: record}), encoding="utf-8"
    )
    assert upgrader.blocked(resource.id, "1.4.3") is False


def test_switch_failure_restores_previous_link(tmp_path):
    resource = _resource()
    manager = _manager(tmp_path, resource)
    _fake_install(manager)
    manager.ensure(only=[resource.id])
    manager.stage(resource.id, "1.4.3", ref="v1.4.3")

    original = manager._create_link

    def flaky(link, dest):
        if "1.4.3" in str(dest):
            raise OSError("mklink 失败")
        return original(link, dest)

    manager._create_link = flaky  # type: ignore[method-assign]

    with pytest.raises(OSError):
        manager.switch(resource.id, "1.4.3", ref="v1.4.3")

    assert _link_target(manager, resource) == "test-mcp-1.4.2"
    assert manager.pins() == {}


def test_rollback_points_link_back_at_pinned_version(tmp_path):
    resource = _resource()
    manager = _manager(tmp_path, resource)
    _fake_install(manager)
    manager.ensure(only=[resource.id])
    upgrader = _upgrader(manager, resource, lambda res, dest: (True, "ok"))
    upgrader.upgrade(resource.id, candidate=_candidate(resource, "v1.4.3"))

    result = upgrader.rollback(resource.id)

    assert result.status == "rolled_back"
    assert _link_target(manager, resource) == "test-mcp-1.4.2"
    assert manager.pins() == {}
    assert manager.status()[0].version == "1.4.2"


def test_rollback_without_pin_is_harmless(tmp_path):
    resource = _resource()
    manager = _manager(tmp_path, resource)
    _fake_install(manager)
    manager.ensure(only=[resource.id])

    result = _upgrader(manager, resource, lambda res, dest: (True, "ok")).rollback(
        resource.id
    )

    assert result.status == "rolled_back"
    assert "没有升级记录" in result.detail
    assert _link_target(manager, resource) == "test-mcp-1.4.2"


def test_prune_keeps_active_and_pinned_versions(tmp_path):
    resource = _resource()
    manager = _manager(tmp_path, resource)
    _fake_install(manager)
    manager.ensure(only=[resource.id])
    for version in ("1.4.3", "1.4.4"):
        manager.stage(resource.id, version, ref=f"v{version}")
    manager.switch(resource.id, "1.4.3", ref="v1.4.3")

    removed = manager.prune(resource.id, keep=0)

    assert removed == ["1.4.4"]
    assert (manager.root / "vendor" / "test-mcp-1.4.3").is_dir()
    assert (manager.root / "vendor" / "test-mcp-1.4.2").is_dir()


def test_browser_kernel_needs_force_to_switch(tmp_path):
    resource = _resource(
        id="chromium",
        name="内置 Chromium",
        version="157.0.8103.50",
        kind="browser",
        directory="chromium",
        entry="chrome.exe",
        install_dir=".xm-agent/browser/engines",
    )
    manager = _manager(tmp_path, resource)
    _fake_install(manager)
    manager.ensure(only=[resource.id])
    upgrader = _upgrader(manager, resource, lambda res, dest: (True, "157"))
    compatible, reason = UpdateChecker.compatible(resource, "158.0.1.1")

    result = upgrader.upgrade(
        resource.id,
        candidate=_candidate(resource, "158.0.1.1", compatible, reason),
    )

    assert result.status == "skipped"
    assert "CDP" in result.detail


def test_browser_pin_drives_engine_resolution(tmp_path):
    """内核没有稳定入口，升级与回滚都靠「启用版本」记录生效。"""
    from xm_agent.browser.engine import find_engine_path

    resource = _resource(
        id="chromium",
        name="内置 Chromium",
        version="157.0.8103.50",
        kind="browser",
        directory="chromium",
        entry="chrome.exe",
        install_dir=".xm-agent/browser/engines",
    )
    manager = _manager(tmp_path, resource)
    _fake_install(manager)
    manager.ensure(only=[resource.id])
    upgrader = _upgrader(manager, resource, lambda res, dest: (True, "157"))

    upgrader.upgrade(
        resource.id,
        candidate=_candidate(resource, "158.0.1.1", False),
        force=True,
    )

    engines = tmp_path / ".xm-agent" / "browser" / "engines"
    assert not (engines / "chromium").exists()  # 不建链接
    assert manager.pins()["chromium"]["version"] == "158.0.1.1"
    assert Path(find_engine_path("chromium", engines)).parent.name == (
        "chromium-158.0.1.1"
    )

    upgrader.rollback(resource.id)

    assert manager.pins()["chromium"]["version"] == "157.0.8103.50"
    assert Path(find_engine_path("chromium", engines)).parent.name == (
        "chromium-157.0.8103.50"
    )


def test_engine_resolution_ignores_broken_pin_file(tmp_path):
    from xm_agent.browser.engine import find_engine_path

    engines = tmp_path / ".xm-agent" / "browser" / "engines"
    install = engines / "chromium-157.0.8103.50"
    install.mkdir(parents=True)
    (install / "chrome.exe").write_text("x", encoding="utf-8")
    pins = tmp_path / ".xm-agent" / "resources" / "active.json"
    pins.parent.mkdir(parents=True)
    pins.write_text("{不是 JSON", encoding="utf-8")

    assert find_engine_path("chromium", engines) == str(install / "chrome.exe")


def test_link_pin_divergence_is_reported(tmp_path):
    """入口和启用记录不一致时要报「版本不符」，不能装作已就绪。"""
    resource = _resource()
    manager = _manager(tmp_path, resource)
    _fake_install(manager)
    manager.ensure(only=[resource.id])
    manager.stage(resource.id, "1.4.3", ref="v1.4.3")
    manager.switch(resource.id, "1.4.3", ref="v1.4.3")
    # 手工把入口改回旧版，模拟用户/别的工具动过 vendor/
    manager._clear_link(manager.root / resource.link, resource)
    manager._create_link(
        manager.root / resource.link,
        manager.root / "vendor" / "test-mcp-1.4.2",
    )

    state = manager.status()[0]

    assert not state.ok
    assert state.status == "outdated"
    assert "test-mcp-1.4.2" in state.detail


# ------------------------------------------------------------------ 路径改写def test_relocate_rewrites_configured_entry_paths():
    link = "vendor/Windows-MCP"
    dest = "vendor/Windows-MCP-0.8.8"

    assert (
        relocate(f"{link}/.venv/Scripts/python.exe", link, dest)
        == f"{dest}/.venv/Scripts/python.exe"
    )
    assert relocate(r"vendor\Windows-MCP\dist\main.js", link, dest) == (
        r"vendor\Windows-MCP-0.8.8\dist\main.js"
    )
    assert relocate(link, link, dest) == dest
    assert relocate("C:/other/python.exe", link, dest) == "C:/other/python.exe"
    assert relocate(f"{dest}/x.py", link, dest) == f"{dest}/x.py"


def test_prober_rewrites_to_the_staged_dir_not_the_pinned_one(tmp_path, monkeypatch):
    """回归：探针必须打旁路目录。

    曾经把 `resource.dest`（清单版本）当替换目标，结果「验证的是旧版、切换的
    是新版」——真机上表现为一个坏掉的 python.exe 也能探针通过。
    """
    from xm_agent.config import AdapterConfig, AgentConfig
    from xm_agent.resources import make_prober
    from xm_agent import bootstrap

    resource = _resource()
    manager = _manager(tmp_path, resource)
    config = AgentConfig()
    config.adapters = {
        "test": AdapterConfig(
            kind="test_mcp",
            command=[
                "vendor/test-mcp/.venv/Scripts/python.exe",
                "-m",
                "test_mcp",
                "serve",
                "--config",
                "../../config/test.toml",
            ],
            cwd="vendor/test-mcp",
        )
    }
    seen: dict = {}

    def fake_probe(command, *, cwd=None, env=None, timeout=20.0):
        seen["command"] = list(command)
        seen["cwd"] = cwd
        return {"ok": True, "detail": "ok", "tools": ["a", "b"]}

    monkeypatch.setattr(bootstrap, "probe_mcp_command", fake_probe)

    staged = manager.root / "vendor" / "test-mcp-1.4.3"
    ok, detail = make_prober(config, manager)(resource, staged)

    assert ok is True and "2 个工具" not in detail  # 摘要里带的是工具名
    assert seen["command"][0] == "vendor/test-mcp-1.4.3/.venv/Scripts/python.exe"
    assert seen["cwd"] == "vendor/test-mcp-1.4.3"
    # 相对 cwd 的配置参数原样保留，靠 cwd 指向新目录来解析
    assert seen["command"][-2:] == ["--config", "../../config/test.toml"]


# ------------------------------------------------------------------ 日志摘要def test_reason_of_picks_the_pip_error_line():
    from xm_agent.resources import reason_of

    output = (
        "Collecting tabulate>=0.9.0\n"
        "  Using cached tabulate-0.10.0-py3-none-any.whl (40 kB)\n"
        "ERROR: Package 'windows-mcp' requires a different Python: "
        "3.12.10 not in '>=3.14'\n"
    )
    assert reason_of(output).startswith("ERROR: Package 'windows-mcp'")
    assert reason_of("") == ""
    assert reason_of("just one line") == "just one line"


def test_build_tail_keeps_pip_stderr_last():
    """以前把 stderr 拼在前面，取尾部只剩 stdout，把真正的错误截掉了。"""
    import subprocess

    result = subprocess.CompletedProcess(
        [],
        1,
        stdout="Collecting a\nCollecting b\n",
        stderr="ERROR: Package 'x' requires a different Python: 3.12.10\n",
    )
    tail = ResourceManager._tail(result)

    assert tail.splitlines()[-1] == (
        "ERROR: Package 'x' requires a different Python: 3.12.10"
    )
    assert "Collecting a" in tail


# -------------------------------------------------------------------- 服务端
def _app(tmp_path, mode: str = "notify"):
    from xm_agent.config import AdapterConfig, AgentConfig
    from xm_agent.ui.server import UiServer

    config = AgentConfig()
    config.adapters = {"mock": AdapterConfig()}
    config.resources.update_check = mode
    config.audit.root = str(tmp_path / "audit")
    config.runtime.lock_path = str(tmp_path / "agent.lock")
    return UiServer(
        config,
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
    )


def _wait_job(app, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = app.resources_snapshot().get("job") or {}
        if job and not job.get("running"):
            return job
        time.sleep(0.05)
    raise AssertionError("升级任务没有在预期时间内结束")


def test_server_upgrade_job_reports_result(tmp_path):
    app = _app(tmp_path)
    resource = app.resources.resource("windows_mcp")
    _fake_install(app.resources)
    app.resources.ensure(only=[resource.id])
    app.upgrader = Upgrader(
        app.resources,
        app.updates,
        prober=lambda res, dest: (True, "MCP 握手成功，共 8 个工具"),
    )

    app.start_upgrade(
        only=[resource.id],
        candidates=[_candidate(resource, "0.8.6")],
    )
    job = _wait_job(app)

    assert job["kind"] == "upgrade"
    assert job["ok"] is True
    assert "0.8.6" in job["message"]
    assert app.resources.pins()[resource.id]["version"] == "0.8.6"


def test_server_auto_mode_starts_upgrade(tmp_path):
    def fetch(url: str) -> tuple[int, bytes]:
        return 200, json.dumps({"tag_name": "0.8.6"}).encode()

    app = _app(tmp_path, mode="auto")
    app.updates = UpdateChecker(fetcher=fetch)
    _fake_install(app.resources)
    app.resources.ensure(only=["windows_mcp"])
    app.upgrader = Upgrader(
        app.resources,
        app.updates,
        prober=lambda res, dest: (True, "ok"),
    )

    data = app.resource_updates()

    assert data["mode"] == "auto"
    assert data["candidates"]
    assert data["started"]
    job = _wait_job(app)
    assert job["kind"] == "upgrade"
    assert app.resources.pins()["windows_mcp"]["version"] == "0.8.6"


def test_server_auto_mode_waits_for_idle(tmp_path):
    app = _app(tmp_path, mode="auto")
    app.updates = UpdateChecker(
        fetcher=lambda url: (200, json.dumps({"tag_name": "0.8.6"}).encode())
    )
    assert app._active.acquire(blocking=False)
    try:
        data = app.resource_updates()
    finally:
        app._active.release()

    assert data["started"] == []
    assert app.resources_snapshot().get("job") is None


def test_server_notify_mode_never_upgrades(tmp_path):
    app = _app(tmp_path)
    app.updates = UpdateChecker(
        fetcher=lambda url: (200, json.dumps({"tag_name": "0.8.6"}).encode())
    )

    data = app.resource_updates()

    assert data["mode"] == "notify"
    assert "started" not in data
    assert app.resources_snapshot().get("job") is None
