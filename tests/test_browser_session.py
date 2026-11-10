"""浏览器通道：真机集成测试（需要本机有 Edge 或 Chrome，否则跳过）。

覆盖 TECH_DESIGN 4.21.14 的全部实测项，确保模块落地后行为不退化。
"""

from __future__ import annotations

import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from xm_agent.browser import BrowserSession, BrowserSettings
from xm_agent.browser.engine import find_engine_path

_ENGINE = find_engine_path("edge") or find_engine_path("chrome")
pytestmark = pytest.mark.skipif(
    _ENGINE is None, reason="本机未安装 Edge / Chrome，跳过真机集成测试"
)

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>探针页</title></head>
<body style="margin:0">
  <button id="btn" style="position:absolute;left:100px;top:100px;width:200px;height:80px">
    点我一下
  </button>
  <input id="field" style="position:absolute;left:100px;top:220px;width:300px;height:40px">
  <div id="log" style="position:absolute;left:100px;top:300px"></div>
  <script>
    window.__hits = [];
    document.getElementById('btn').addEventListener('click', e => {
      window.__hits.push([e.clientX, e.clientY]);
      document.getElementById('log').textContent = '已点击';
    });
  </script>
</body></html>"""


@pytest.fixture(scope="module")
def site() -> ThreadingHTTPServer:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="browser-test-"))
    (root / "index.html").write_text(PAGE, encoding="utf-8")
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


@pytest.fixture(scope="module")
def page_url(site) -> str:
    return f"http://127.0.0.1:{site.server_address[1]}/index.html"


@pytest.fixture(scope="module")
def session(tmp_path_factory) -> BrowserSession:
    root = tmp_path_factory.mktemp("profiles")
    browser = BrowserSession(
        BrowserSettings(profile_root=str(root), headless="always"),
        headless=True,
    )
    browser.start()
    yield browser
    browser.close()


def test_session_starts_and_describes_engine(session):
    stats = session.stats()
    assert stats.closed is False
    assert stats.engine in {"edge", "chrome", "chromium"}
    assert stats.headless is True
    description = session.description()
    assert "无头" in description
    assert session.engine is not None and session.engine.path


def test_navigate_and_snapshot_shape(session, page_url):
    page = session.open(page_url)
    assert page.title == "探针页"
    snapshot = page.snapshot(include_screenshot=True)
    meta = snapshot["meta"]
    assert meta["url"].startswith("http://127.0.0.1:")
    assert meta["readyState"] == "complete"
    assert meta["viewportW"] > 0 and meta["viewportH"] > 0
    assert isinstance(snapshot["elements"], list) and snapshot["elements"]
    tags = {item["tag"] for item in snapshot["elements"]}
    assert "button" in tags and "input" in tags
    assert "点我一下" in snapshot["text"]
    assert snapshot["screenshot_bytes"] > 1000
    assert snapshot["engine"] in {"edge", "chrome", "chromium"}


def test_screenshot_is_jpeg(session, page_url):
    page = session.open(page_url)
    data = page.screenshot(quality=70)
    assert data[:2] == b"\xff\xd8"  # JPEG SOI
    assert len(data) > 1000


def test_click_hits_expected_coordinates(session, page_url):
    page = session.open(page_url)
    elements = page.elements()
    button = next(item for item in elements if item["tag"] == "button")
    assert button["text"] == "点我一下"
    page.click(button["x"], button["y"])
    time.sleep(0.2)
    hits = page.evaluate("window.__hits")
    assert hits and hits[0] == [button["x"], button["y"]]
    assert page.text(200).find("已点击") >= 0


def test_click_element_by_index(session, page_url):
    page = session.open(page_url)
    elements = page.elements()
    index = next(i for i, item in enumerate(elements) if item["tag"] == "button")
    clicked = page.click_element(index, elements)
    assert clicked["tag"] == "button"
    time.sleep(0.2)
    assert page.evaluate("window.__hits.length") >= 1


def test_chinese_input_goes_through_insert_text(session, page_url):
    page = session.open(page_url)
    assert page.type_into("#field", "扫码登录测试 😀") is True
    value = page.evaluate("document.getElementById('field').value")
    assert value == "扫码登录测试 😀"


def test_insert_text_without_focus_is_dropped(session, page_url):
    """文档里写的坑：无焦点时 insertText 静默丢弃。"""
    page = session.open(page_url)
    page.evaluate("document.getElementById('field').value=''")
    page.evaluate("document.body.focus()")
    page.type_text("应该丢弃")
    assert page.evaluate("document.getElementById('field').value") == ""


def test_press_enter_and_scroll(session, page_url):
    page = session.open(page_url)
    page.evaluate("document.getElementById('field').focus()")
    page.type_text("abc")
    page.press("Enter")
    page.scroll(300)
    assert page.evaluate("document.getElementById('field').value") == "abc"


def test_wait_text_detects_and_times_out(session, page_url):
    page = session.open(page_url)
    assert page.wait_text("点我一下", timeout=3) is True
    assert page.wait_text("这句话不存在", timeout=0.6) is False


def test_mirror_streams_frames(session, page_url):
    page = session.open(page_url)
    # 全屏动画层：screencast 只在画面可见变化时推帧
    page.evaluate(
        """
        window.__t = 0;
        const anim = document.createElement('div');
        anim.id = 'anim';
        anim.style.cssText = 'position:fixed;left:0;top:0;width:100%;height:100%;z-index:9999';
        document.body.appendChild(anim);
        setInterval(() => {
          window.__t += 1;
          anim.style.background = 'hsl(' + (window.__t * 9 % 360) + ',70%,50%)';
        }, 100);
        """
    )
    stream = page.start_mirror(fps=15)
    try:
        deadline = time.time() + 5
        while time.time() < deadline and stream.stats()["frames"] < 3:
            time.sleep(0.1)
        stats = stream.stats()
        assert stats["frames"] >= 1, "未收到镜像帧"
        assert stats["bytes"] > 0
        assert stream.latest is not None and stream.latest[:2] == b"\xff\xd8"
        assert stream.as_data_url().startswith("data:image/jpeg;base64,")
    finally:
        page.stop_mirror()
    assert page.mirror is None


def test_mirror_primes_first_frame_on_static_page(session, page_url):
    """静止页面不会推帧，镜像必须自带一张兜底首帧，否则面板一片空白。"""
    page = session.open(page_url)
    stream = page.start_mirror(fps=5)
    try:
        assert stream.latest is not None
        assert stream.latest[:2] == b"\xff\xd8"
        assert stream.as_data_url().startswith("data:image/jpeg;base64,")
    finally:
        page.stop_mirror()


def test_ax_summary_returns_nodes(session, page_url):
    page = session.open(page_url)
    nodes = page.ax()
    assert isinstance(nodes, list)
    assert any(node.get("name") == "点我一下" or node.get("role") == "button"
               for node in nodes)


def test_stats_reflect_open_pages(session, page_url):
    before = session.stats().pages
    session.open(page_url)
    assert session.stats().pages >= before + 1


def test_graceful_close_keeps_login_state(tmp_path):
    """硬杀会丢 Cookie，优雅关闭必须保留——这是「只扫一次码」的前提。"""
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="login-keep-"))
    site_root = Path(tempfile.mkdtemp(prefix="login-site-"))
    (site_root / "index.html").write_text("<html><body>hi</body></html>", encoding="utf-8")
    handler = partial(SimpleHTTPRequestHandler, directory=str(site_root))
    handler.log_message = lambda *a, **k: None  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/index.html"

    settings = BrowserSettings(profile_root=str(root), headless="always")
    first = BrowserSession(settings, headless=True)
    first.start()
    page = first.open(url)
    page.evaluate(
        "document.cookie='sessionid=keepme-42; path=/; max-age=86400';"
        "localStorage.setItem('who','tester');"
    )
    engine_name = first.engine.name if first.engine else ""
    first.close()

    second = BrowserSession(settings, headless=True)
    second.start()
    try:
        resumed = second.open(url)
        cookie = resumed.evaluate("document.cookie")
        who = resumed.evaluate("localStorage.getItem('who')")
        assert "sessionid=keepme-42" in str(cookie), "优雅关闭后 Cookie 丢失"
        assert who == "tester", "优雅关闭后 localStorage 丢失"
        assert second.engine is not None and second.engine.name == engine_name
    finally:
        second.close()
        server.shutdown()
