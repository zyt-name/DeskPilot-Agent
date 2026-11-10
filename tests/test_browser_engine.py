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
    # auto = 可见窗口：任务结束后用户要能直接在这个窗口里接手继续操作
    assert BrowserSession(BrowserSettings(headless="auto")).headless is False
    assert BrowserSession(BrowserSettings(headless="auto"), headless=True).headless is True


def test_auto_mode_falls_back_to_headless(monkeypatch, tmp_path):
    """auto 档：可见窗口起不来（无桌面会话）时退回无头。

    否则在没有桌面的机器上，浏览器通道直接等于不可用——这比用户看不到窗口
    更糟。never 是显式要求窗口，不做这个回退。
    """
    import types

    from xm_agent.browser import session as session_mod

    started: list[bool] = []

    class FakeProcess:
        def __init__(self, engine, profile_dir, headless=True, **_kwargs):
            self.headless = headless

        def start(self, timeout=20.0):
            started.append(self.headless)
            raise BrowserError(f"起不来（headless={self.headless}）")

    monkeypatch.setattr(session_mod, "BrowserProcess", FakeProcess)
    monkeypatch.setattr(
        session_mod,
        "resolve_engine",
        lambda *args, **kwargs: types.SimpleNamespace(
            name="edge", path="msedge.exe", version="157.0",
            source="system", label="Microsoft Edge",
        ),
    )

    browser = BrowserSession(BrowserSettings(profile_root=str(tmp_path / "p")))
    with pytest.raises(BrowserError):
        browser.start()
    assert started == [False, True]

    forced = BrowserSession(
        BrowserSettings(profile_root=str(tmp_path / "q"), headless="never")
    )
    with pytest.raises(BrowserError):
        forced.start()
    assert started == [False, True, False]


def test_first_page_adopts_the_startup_tab():
    """内核按 about:blank 启动时已经有一页：直接接管，别再新开一个空标签。

    不接管的话用户看到两个标签，其中一个永远空白；「只有一个浏览器」的
    观感就破了。
    """
    created: list[tuple[str, object]] = []

    class FakeConn:
        closed = False

        def call(self, method, params=None, session_id=None, timeout=None):
            created.append((method, session_id))
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"type": "page", "targetId": "T1", "url": "about:blank"}
                    ]
                }
            if method == "Target.attachToTarget":
                return {"sessionId": "S1"}
            return {}

    browser = BrowserSession(BrowserSettings())
    browser.conn = FakeConn()
    browser._closed = False

    page = browser.first_page()
    assert page.target_id == "T1"
    assert page.cdp_session == "S1"
    assert browser.first_page() is page
    assert "Target.createTarget" not in [name for name, _ in created]
    assert ("Page.enable", "S1") in created


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


def test_profile_dir_is_always_absolute(monkeypatch):
    """相对 profile 路径会让 Chromium 把 DevToolsActivePort 写到别处。

    默认配置就是相对路径（`.xm-agent/browser/profiles`），所以这条必须锁住：
    传给内核的 `--user-data-dir` 一律解析成绝对路径。
    """
    import types

    from xm_agent.browser import session as session_mod

    captured: dict[str, object] = {}

    class _Stop(Exception):
        pass

    class FakeProcess:
        def __init__(self, engine, profile_dir, headless=True, **_kwargs):
            captured["dir"] = profile_dir

        def start(self, timeout=20.0):
            raise _Stop("stop here")

    monkeypatch.setattr(session_mod, "BrowserProcess", FakeProcess)
    monkeypatch.setattr(
        session_mod,
        "resolve_engine",
        lambda *args, **kwargs: types.SimpleNamespace(
            name="chromium",
            path="chromium.exe",
            version="157.0",
            source="bundled",
            label="内置 Chromium",
        ),
    )
    browser = BrowserSession(
        BrowserSettings(profile_root="relative/profiles", engine="chromium"),
        headless=True,
    )
    with pytest.raises(_Stop):
        browser.start()

    profile_dir = captured["dir"]
    assert profile_dir.is_absolute()
    assert profile_dir.name == "chromium"


# ---------------------------------------------------------------- 反自动化指纹
# 真机踩过：无头模式的 UA 是 `HeadlessChrome/157`、`navigator.webdriver` 为 true，
# 自如的腾讯 EdgeOne 直接返回「请求已被拦截」。这两条必须留在启动参数里。
def test_default_user_agent_looks_like_a_real_browser():
    from xm_agent.browser.engine import EngineInfo, default_user_agent

    edge = default_user_agent(EngineInfo("edge", r"C:\msedge.exe", "157.0.4341.42", "system"))
    assert "Headless" not in edge
    assert "Edg/157.0.4341.42" in edge
    # 和真机一致：Chrome 令牌只带大版本
    assert "Chrome/157.0.0.0" in edge

    chrome = default_user_agent(
        EngineInfo("chrome", r"C:\chrome.exe", "140.0.7339.207", "system")
    )
    assert "Headless" not in chrome
    assert "Edg/" not in chrome
    assert "Chrome/140.0.0.0" in chrome


def test_headless_command_line_hides_automation(tmp_path):
    from xm_agent.browser.engine import BrowserProcess, EngineInfo

    proc = BrowserProcess(
        EngineInfo("edge", r"C:\msedge.exe", "157.0.4341.42", "system"),
        tmp_path / "edge",
        headless=True,
    )
    args = proc.command_args()
    assert any(item.startswith("--user-agent=") and "Headless" not in item for item in args)
    assert "--disable-blink-features=AutomationControlled" in args
    assert "--headless=new" in args


def test_automation_flag_comes_with_infobar_suppressor(tmp_path):
    """`--disable-blink-features=AutomationControlled` 会让内核在页面顶部挂一条
    「不受支持的命令行标记」提示条，把内容往下挤；`--test-type` 正是让它别报警。
    两者必须成对出现，否则界面看起来像被篡改过。
    """
    from xm_agent.browser.engine import BrowserProcess, EngineInfo

    hiding = BrowserProcess(
        EngineInfo("edge", r"C:\msedge.exe", "157.0.4341.42", "system"),
        tmp_path / "a",
        headless=False,
    )
    args = hiding.command_args()
    assert "--disable-blink-features=AutomationControlled" in args
    assert "--test-type" in args

    plain = BrowserProcess(
        EngineInfo("edge", r"C:\msedge.exe", "157.0.4341.42", "system"),
        tmp_path / "b",
        headless=False,
        hide_automation=False,
    )
    assert "--test-type" not in plain.command_args()


def test_visible_window_keeps_native_user_agent(tmp_path):
    """可见窗口的 UA 本来就正常，别再覆盖一层（覆盖反而可能与版本不符）。"""
    from xm_agent.browser.engine import BrowserProcess, EngineInfo

    proc = BrowserProcess(
        EngineInfo("edge", r"C:\msedge.exe", "157.0.4341.42", "system"),
        tmp_path / "edge",
        headless=False,
    )
    assert proc.resolved_user_agent() == ""
    assert not [a for a in proc.command_args() if a.startswith("--user-agent=")]
    assert "--headless=new" not in proc.command_args()
    # 翻译气泡是独立顶层窗口，会盖住页面右上角（截图里也是一块遮挡）。
    # Edge 走自己的开关名，只关 Translate/TranslateUI 时气泡照样弹——
    # 真机对比过，四个 feature 一起关才彻底不出现。
    features = [
        a for a in proc.command_args()
        if a.startswith("--disable-features=")
    ][0]
    for name in ("Translate", "TranslateUI", "msEdgeTranslate", "EdgeTranslate"):
        assert name in features.split("=", 1)[1].split(",")


def test_explicit_user_agent_wins(tmp_path):
    from xm_agent.browser.engine import BrowserProcess, EngineInfo

    proc = BrowserProcess(
        EngineInfo("edge", r"C:\msedge.exe", "157.0.4341.42", "system"),
        tmp_path / "edge",
        headless=True,
        user_agent="Mozilla/5.0 (X11; Linux x86_64) Custom/1.0",
    )
    assert "--user-agent=Mozilla/5.0 (X11; Linux x86_64) Custom/1.0" in proc.command_args()


def test_browser_config_reads_stealth_switches(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text(
        """
[runtime.browser]
hide_automation = false
user_agent = "Custom/2.0"
""",
        encoding="utf-8",
    )
    config = load_config(str(path))
    assert config.browser.hide_automation is False
    assert config.browser.user_agent == "Custom/2.0"
    assert BrowserConfig().hide_automation is True
