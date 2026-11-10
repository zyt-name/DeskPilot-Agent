"""浏览器通道：引擎解析、profile 锁、配置解析（不启动浏览器）。"""

from __future__ import annotations

import pytest

from xm_agent.browser import engine as engine_mod
from xm_agent.browser.engine import (
    BrowserError,
    EngineNotFound,
    ProfileLock,
    bundled_root_for,
    resolve_engine,
)
from xm_agent.browser.input import _js_str
from xm_agent.browser.session import BrowserSession, BrowserSettings
from xm_agent.config import BrowserConfig, load_config


def _fake_paths(monkeypatch, mapping: dict[str, str | None]):
    monkeypatch.setattr(
        engine_mod,
        "find_engine_path",
        lambda name, bundled_root=None: mapping.get(name),
    )
    monkeypatch.setattr(engine_mod, "engine_version", lambda path, timeout=8.0: "157.0.0")


def test_resolve_auto_picks_edge_first(monkeypatch):
    _fake_paths(monkeypatch, {"edge": r"C:\edge.exe", "chrome": r"C:\chrome.exe"})
    info = resolve_engine("auto")
    assert info.name == "edge"
    assert info.source == "system"
    assert info.note == ""
    assert info.version == "157.0.0"


def test_resolve_auto_falls_back_and_reports_note(monkeypatch):
    _fake_paths(monkeypatch, {"edge": None, "chrome": r"C:\chrome.exe"})
    info = resolve_engine("auto")
    assert info.name == "chrome"
    assert "Edge" in info.note


def test_resolve_auto_uses_bundled_chromium_last(monkeypatch):
    _fake_paths(monkeypatch, {"chromium": r"D:\engines\chromium-143\chrome.exe"})
    info = resolve_engine("auto")
    assert info.name == "chromium"
    assert info.source == "bundled"
    assert "Microsoft Edge" in info.note and "Google Chrome" in info.note


def test_resolve_specific_missing_does_not_silently_swap(monkeypatch):
    _fake_paths(monkeypatch, {"chrome": r"C:\chrome.exe"})
    with pytest.raises(EngineNotFound) as excinfo:
        resolve_engine("edge")
    assert "不自动替换" in str(excinfo.value)


def test_resolve_all_missing_auto_raises(monkeypatch):
    _fake_paths(monkeypatch, {})
    with pytest.raises(EngineNotFound):
        resolve_engine("auto")


def test_resolve_rejects_unknown_engine(monkeypatch):
    _fake_paths(monkeypatch, {})
    with pytest.raises(BrowserError):
        resolve_engine("firefox")


def test_bundled_root_is_sibling_of_profiles(tmp_path):
    root = bundled_root_for(tmp_path / "browser" / "profiles")
    assert root == tmp_path / "browser" / "engines"


def test_profile_lock_is_exclusive(tmp_path):
    first = ProfileLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(BrowserError):
            ProfileLock(tmp_path).acquire()
    finally:
        first.release()
    second = ProfileLock(tmp_path)
    second.acquire()
    second.release()
    assert not (tmp_path / ".browser-lock").exists()


def test_profile_lock_clears_stale_holder(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".browser-lock").write_text("2147483646", encoding="utf-8")
    lock = ProfileLock(tmp_path)
    lock.acquire()
    lock.release()
    assert not (tmp_path / ".browser-lock").exists()


def test_profile_lock_reentrant_on_same_instance(tmp_path):
    lock = ProfileLock(tmp_path)
    lock.acquire()
    lock.acquire()  # 同一实例重复获取应幂等，不能自锁
    assert (tmp_path / ".browser-lock").exists()
    lock.release()
    assert not (tmp_path / ".browser-lock").exists()


def test_browser_session_reports_not_started():
    session = BrowserSession()
    stats = session.stats()
    assert stats.closed is False
    assert stats.pages == 0
    assert "未启动" in session.description()


def test_browser_headless_mode_resolution():
    assert BrowserSession(BrowserSettings(headless="always")).headless is True
    assert BrowserSession(BrowserSettings(headless="never")).headless is False
    assert BrowserSession(BrowserSettings(headless="auto")).headless is True
    assert BrowserSession(BrowserSettings(headless="auto"), headless=False).headless is False


def test_js_str_escapes_quotes_and_newlines():
    assert _js_str("a'b") == r"'a\'b'"
    assert _js_str("a\\b") == r"'a\\b'"
    assert _js_str("a\nb") == r"'a\nb'"


def test_browser_config_defaults():
    config = load_config(None)
    assert config.browser.engine == "auto"
    assert config.browser.headless == "auto"
    assert config.browser.profile_root == ".xm-agent/browser/profiles"
    assert config.browser.mirror_fps == 15


def test_browser_config_parsed_from_toml(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text(
        """
[runtime.browser]
engine = "chrome"
profile_root = "D:/data/profiles"
headless = "never"
mirror_fps = 30
screenshot_max_width = 1024
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.browser.engine == "chrome"
    assert config.browser.profile_root == "D:/data/profiles"
    assert config.browser.headless == "never"
    assert config.browser.mirror_fps == 30
    assert config.browser.screenshot_max_width == 1024


def test_browser_config_rejects_unknown_engine(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text('[runtime.browser]\nengine = "firefox"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_browser_config_rejects_unknown_headless(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text('[runtime.browser]\nheadless = "sometimes"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_browser_config_dataclass_defaults():
    config = BrowserConfig()
    assert config.engine == "auto"
    assert config.keep_alive_seconds == 300
