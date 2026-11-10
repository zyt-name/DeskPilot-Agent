"""资源更新检测：只负责「发现 + 判断能不能换」。

不能自动换的情况必须说清理由——跨大版本的 MCP 工具集变了，
静默升级会让 Runtime 的提示词和审计规则对不上。
"""

from __future__ import annotations

import json

from xm_agent.resources import Resource, UpdateChecker
from xm_agent.resources.updates import version_key


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


def _checker(payload: dict | None = None, status: int = 200):
    calls: list[str] = []

    def fetch(url: str) -> tuple[int, bytes]:
        calls.append(url)
        if payload is None:
            return status, b""
        return status, json.dumps(payload).encode()

    return UpdateChecker(fetcher=fetch), calls


def test_version_key_handles_tags_and_suffixes():
    assert version_key("v1.2.3") == (1, 2, 3)
    assert version_key("1.10.0-beta.2") > version_key("1.9.9")
    assert version_key("") == (0,)


def test_newer_patch_is_compatible():
    checker, _ = _checker(
        {
            "tag_name": "v1.4.3",
            "html_url": "https://github.com/example/test-mcp/releases/tag/v1.4.3",
        }
    )
    found = checker.check([_resource()])

    assert len(found) == 1
    assert found[0].latest == "v1.4.3"
    assert found[0].compatible is True
    assert "同大版本" in found[0].reason
    assert found[0].url.endswith("v1.4.3")


def test_major_bump_is_not_compatible():
    checker, _ = _checker({"tag_name": "2.0.0"})
    found = checker.check([_resource()])

    assert found and found[0].compatible is False
    assert "跨大版本" in found[0].reason


def test_same_or_older_version_is_ignored():
    checker, _ = _checker({"tag_name": "1.4.2"})
    assert checker.check([_resource()]) == []

    checker, _ = _checker({"tag_name": "0.9.0"})
    assert checker.check([_resource()]) == []


def test_browser_kernel_is_never_auto_upgraded():
    checker, _ = _checker({"tag_name": "158.0.1.1"})
    found = checker.check(
        [
            _resource(
                id="chromium",
                name="内置 Chromium",
                version="157.0.8103.50",
                repo="GoogleChromeLabs/chrome-for-testing",
                kind="browser",
                entry="chrome.exe",
            )
        ]
    )

    assert found and found[0].compatible is False
    assert "CDP" in found[0].reason


def test_latest_is_cached_between_calls():
    checker, calls = _checker({"tag_name": "1.5.0"})
    checker.check([_resource()])
    checker.check([_resource()])
    assert len(calls) == 1


def test_falls_back_to_tags_when_releases_missing():
    urls: list[str] = []

    def fetch(url: str) -> tuple[int, bytes]:
        urls.append(url)
        if "releases/latest" in url:
            return 404, b""
        return 200, json.dumps([{"name": "v1.9.0"}, {"name": "v1.10.1"}]).encode()

    found = UpdateChecker(fetcher=fetch).check([_resource()])

    assert found and found[0].latest == "v1.10.1"
    assert any("tags" in url for url in urls)


def test_network_failure_is_silent():
    checker, _ = _checker(payload=None, status=500)
    assert checker.check([_resource()]) == []


def _server(tmp_path, mode: str = "notify"):
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


def test_server_skips_check_when_mode_off(tmp_path):
    app = _server(tmp_path, mode="off")
    data = app.resource_updates()
    assert data["mode"] == "off"
    assert data["candidates"] == []


def test_server_reports_candidates_and_caches(tmp_path):
    calls: list[str] = []

    def fetch(url: str) -> tuple[int, bytes]:
        calls.append(url)
        return 200, json.dumps({"tag_name": "9.9.9"}).encode()

    app = _server(tmp_path)
    app.updates = UpdateChecker(fetcher=fetch)

    first = app.resource_updates()
    assert first["mode"] == "notify"
    assert first["checked_at"]
    assert {item["id"] for item in first["candidates"]} >= {
        "windows_mcp",
        "computer_use_mcp",
    }

    fetched = len(calls)
    second = app.resource_updates()
    assert len(calls) == fetched  # 十分钟内走缓存，不打 GitHub
    assert second["candidates"] == first["candidates"]

    app.resource_updates(refresh=True)
    assert len(calls) > fetched

