from __future__ import annotations

import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from xm_agent.resources import Resource, ResourceManager
from xm_agent.resources.manager import ResourceError


def _completed():
    import subprocess

    return subprocess.CompletedProcess([], 0, stdout="ok", stderr="")


def _write(path, text: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _archive(files: dict[str, str], root: str = "pkg-test") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(f"{root}/{name}", content)
    return buffer.getvalue()


def _server(payload: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/direct.zip":
                self.send_response(503)
                self.end_headers()
                return
            if self.path == "/mirror.zip":
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


@pytest.fixture
def mirror_server():
    payload = _archive(
        {
            "package.json": json.dumps({"name": "test-mcp", "version": "9.9.9"}),
            "dist/server.js": "console.log('ok')\n",
        },
        root="test-mcp-9.9.9-source",
    )
    httpd = _server(payload)
    try:
        yield httpd, payload
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_manifest_resources_are_pinned():
    from xm_agent.resources import RESOURCES

    assert {resource.id for resource in RESOURCES} == {
        "windows_mcp",
        "computer_use_mcp",
        "chromium",
        "xshell_mcp",
    }
    for resource in RESOURCES:
        assert resource.version
        assert resource.repo.count("/") == 1
        assert resource.dest.endswith(f"-{resource.version}")
        assert resource.link.endswith(resource.directory)
        assert resource.license
        if resource.kind == "browser":
            # 内核来自 Chrome for Testing，不是 GitHub 仓库归档
            assert resource.archive.startswith("https://")
            assert {label for label, _ in resource.sources} >= {
                "npmmirror",
                "google",
            }
            assert resource.entry == "chrome.exe"
            assert len(resource.sources) >= 2  # 官方 + 国内镜像
            continue
        assert len(resource.ref) == 40
        assert resource.archive.startswith("https://github.com/")


def test_browser_resource_installs_from_explicit_source(tmp_path):
    # Chrome for Testing 的 zip 里是单个顶层目录 chrome-win64/，解压后要能
    # 直接落到 chromium-<version>/chrome.exe，引擎解析才找得到。
    payload = _archive({"chrome.exe": "fake-binary"}, root="chrome-win64")
    httpd = _server(payload)
    port = httpd.server_address[1]
    resource = Resource(
        id="chromium",
        name="内置 Chromium",
        version="1.2.3.4",
        repo="GoogleChromeLabs/chrome-for-testing",
        ref="1.2.3.4",
        kind="browser",
        directory="chromium",
        install_dir="engines",
        entry="chrome.exe",
        purpose="测试用内核",
        required=False,
        license="BSD-3-Clause",
        downloads=(("local", f"http://127.0.0.1:{port}/mirror.zip"),),
    )
    manager = ResourceManager(tmp_path, resources=(resource,))
    try:
        state = manager.ensure(only=["chromium"])[0]
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert state.status == "ready"
    assert state.source == "local"
    assert (tmp_path / resource.dest / "chrome.exe").is_file()
    # 内核不需要稳定入口链接，引擎解析直接按版本目录找
    assert not (tmp_path / resource.link).exists()
    assert not (tmp_path / "vendor").exists()


def test_browser_probe_rejects_non_browser_resource(tmp_path):
    manager = ResourceManager(tmp_path)
    ok, detail = manager.probe_browser("windows_mcp")
    assert ok is False
    assert "浏览器" in detail


def test_browser_probe_reports_missing_install(tmp_path):
    manager = ResourceManager(tmp_path)
    ok, detail = manager.probe_browser("chromium")
    assert ok is False
    assert detail


def test_status_reads_versioned_install(tmp_path):
    manager = ResourceManager(tmp_path)
    resource = next(r for r in manager.resources if r.id == "windows_mcp")
    dest = tmp_path / resource.dest
    _write(dest / "manifest.json", json.dumps({"version": resource.version}))
    _write(dest / "src/windows_mcp/__main__.py", "")
    _write(dest / ".venv/Scripts/python.exe", "")
    _write(
        dest / ".xm-agent-resource.json",
        json.dumps(
            {"version": resource.version, "stage": "ready", "source": "ghfast.top"}
        ),
    )

    state = next(
        item for item in manager.status() if item.id == resource.id
    )
    assert state.status == "ready"
    assert state.source == "ghfast.top"
    assert state.stage == "ready"


def test_status_treats_unfinished_build_as_incomplete(tmp_path):
    manager = ResourceManager(tmp_path)
    resource = next(r for r in manager.resources if r.id == "windows_mcp")
    dest = tmp_path / resource.dest
    _write(dest / "manifest.json", json.dumps({"version": resource.version}))
    _write(dest / resource.entry, "")
    for check in resource.runtime_checks:
        _write(dest / check, "")
    _write(
        dest / ".xm-agent-resource.json",
        json.dumps({"version": resource.version, "stage": "source"}),
    )

    state = next(item for item in manager.status() if item.id == resource.id)
    assert state.status == "incomplete"
    assert "未完成" in state.detail

    calls: list[list[str]] = []

    def runner(command, cwd, env, timeout):
        calls.append(list(command))
        raise AssertionError("unfinished resource must be rebuilt")

    manager.runner = runner
    states = manager.ensure(only=[resource.id])
    assert calls
    assert states[0].status == "error"


def test_status_reports_outdated_legacy_clone(tmp_path):
    manager = ResourceManager(tmp_path)
    resource = next(r for r in manager.resources if r.id == "windows_mcp")
    link = tmp_path / resource.link
    _write(link / "manifest.json", json.dumps({"version": "0.8.7"}))
    _write(link / "src/windows_mcp/__main__.py", "")
    _write(link / ".venv/Scripts/python.exe", "")

    state = next(item for item in manager.status() if item.id == resource.id)
    assert state.status == "outdated"
    assert "0.8.7" in state.detail


def test_safe_extract_rejects_path_traversal(tmp_path):
    manager = ResourceManager(tmp_path)
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.txt", "boom")

    with pytest.raises(ResourceError):
        manager.safe_extract(archive, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_ensure_downloads_from_mirror_and_links_version(tmp_path, mirror_server):
    httpd, _ = mirror_server
    port = httpd.server_address[1]
    resource = Resource(
        id="test_mcp",
        name="Test MCP",
        version="9.9.9",
        repo="example/test-mcp",
        ref="0" * 40,
        kind="node",
        directory="test-mcp",
        entry="dist/server.js",
        runtime_checks=("node_modules",),
        purpose="test",
        required=False,
        archive_url=f"http://127.0.0.1:{port}/direct.zip",
    )
    calls: list[list[str]] = []

    def runner(command, cwd, env, timeout):
        calls.append(list(command))
        _write(cwd / "node_modules" / "dep" / "index.js", "")
        _write(cwd / "dist" / "server.js", "")
        return _completed()

    manager = ResourceManager(
        tmp_path,
        resources=(resource,),
        mirrors=(("mirror", f"http://127.0.0.1:{port}/mirror.zip"),),
        runner=runner,
    )
    states = manager.ensure()

    state = states[0]
    assert state.status == "ready"
    assert state.source == "mirror"
    assert (tmp_path / resource.dest / "dist/server.js").is_file()
    assert (tmp_path / resource.link).is_dir()
    assert (tmp_path / resource.link / "dist/server.js").is_file()
    assert (tmp_path / ".xm-agent/resources/test_mcp-9.9.9.zip").is_file()
    assert any("install" in " ".join(call) for call in calls)
    assert any("run" in " ".join(call) and "build" in " ".join(call) for call in calls)


def test_ensure_skips_ready_resource(tmp_path):
    manager = ResourceManager(tmp_path)
    resource = manager.resources[0]
    dest = tmp_path / resource.dest
    _write(dest / "manifest.json", json.dumps({"version": resource.version}))
    _write(dest / resource.entry, "")
    for check in resource.runtime_checks:
        _write(dest / check, "")
    _write(
        dest / ".xm-agent-resource.json",
        json.dumps({"version": resource.version, "stage": "ready"}),
    )

    def runner(command, cwd, env, timeout):
        raise AssertionError("ready resources must not build")

    manager.runner = runner
    states = manager.ensure()
    assert states[0].status == "ready"
