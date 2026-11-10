"""浏览器通道端到端自测：走的是 AI 真正会调的那五个能力。

和 probe_browser_cdp.py 的区别：那个验证 CDP 地基，这个验证上层封装。
它按模型的方式使用通道——open → observe（读元素序号）→ act（点序号、输中文）
→ verify（查文本）→ session（关闭）——并在本地起一个静态页，不依赖外网。

用法：
    PYTHONPATH=src .venv\\Scripts\\python.exe scripts\\probe_browser_channel.py
    ... --engine chrome --headful

失败会以非零码退出，便于挂到 CI 或发布前检查。
"""

from __future__ import annotations

import argparse
import functools
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xm_agent.browser.channel import BrowserChannel  # noqa: E402
from xm_agent.browser.session import BrowserSettings  # noqa: E402

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>通道自测</title></head>
<body style="font-family:sans-serif;padding:24px">
  <h1 id="h">通道自测页</h1>
  <input id="q" placeholder="搜索词">
  <button id="go" onclick="run()">开始搜索</button>
  <button id="late" style="display:none">删除全部</button>
  <p id="out">等待操作</p>
  <script>
    function run() {
      const v = document.getElementById('q').value || '(空)';
      document.getElementById('out').textContent = '已搜索：' + v;
      document.getElementById('late').style.display = 'inline';
    }
  </script>
</body></html>
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args, **kwargs) -> None:
        pass


def serve(root: Path) -> tuple[ThreadingHTTPServer, str]:
    (root / "index.html").write_text(PAGE, encoding="utf-8")
    handler = functools.partial(_QuietHandler, directory=str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/index.html"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default="auto", choices=["auto", "edge", "chrome", "chromium"])
    parser.add_argument("--headful", action="store_true", help="可见窗口（扫码场景）")
    args = parser.parse_args()

    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    httpd, url = serve(root)

    settings = BrowserSettings(
        engine=args.engine,
        headless="never" if args.headful else "always",
        profile_root=str(root / "profiles"),
    )
    channel = BrowserChannel(settings, screenshot_dir=str(root / "shots"))
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        print(f"[{'OK ' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            failures.append(name)

    try:
        t0 = time.perf_counter()
        opened = channel.execute("browser.open", {"url": url})
        started = (time.perf_counter() - t0) * 1000
        check("打开页面", opened.ok, f"{started:.0f}ms {opened.detail.get('engine', '')}")
        if not opened.ok:
            return 1

        t1 = time.perf_counter()
        obs = channel.observe(1, include_screenshot=True)
        observed = (time.perf_counter() - t1) * 1000
        names = [e.name for e in obs.elements]
        check("观测", len(obs.elements) >= 2, f"{observed:.0f}ms，{len(obs.elements)} 个元素：{names[:5]}")
        check("截图落盘", bool(obs.screenshot_path) and Path(obs.screenshot_path).is_file(),
              obs.screenshot_path or "(无)")
        check("标题", "通道自测" in (obs.active_window or ""), obs.active_window or "")

        # 用元素名找序号，模拟模型从清单里挑
        index = next((i for i, n in enumerate(names) if "搜索词" in n), None)
        if index is None:
            check("定位输入框", False, f"元素清单里没有输入框：{names}")
        else:
            typed = channel.execute("browser.act", {"action": "click", "element_index": index})
            check("点输入框", typed.ok, f"element_index={index}")

        typed = channel.execute("browser.act", {"action": "type", "text": "上海天气"})
        check("输入中文", typed.ok, str(typed.detail))

        idx_btn = next((i for i, n in enumerate(names) if "开始搜索" in n), None)
        t2 = time.perf_counter()
        clicked = channel.execute(
            "browser.act", {"action": "click", "element_index": idx_btn}
        )
        clicked_ms = (time.perf_counter() - t2) * 1000
        check("点击按钮", clicked.ok, f"{clicked_ms:.0f}ms index={idx_btn}")

        verified = channel.verify({"text": "已搜索：上海天气", "title_contains": "通道自测"})
        check("校验结果", verified.ok, verified.detail)

        after = channel.observe(2)
        check("轮询到新元素", any("删除全部" in e.name for e in after.elements),
              f"{len(after.elements)} 个元素")
        check("登录墙不误判", after.raw.get("login_wall") is False, str(after.raw.get("login_wall")))

        status = channel.execute("browser.session", {"action": "status"})
        check("会话状态", status.ok and status.detail.get("started") is True,
              str(status.detail.get("description")))

        closed = channel.execute("browser.session", {"action": "close"})
        check("优雅关闭", closed.ok, f"closed={closed.detail.get('closed')}")

        profile = Path(settings.profile_root)
        check("profile 保留", profile.exists(), str(profile))

    finally:
        channel.close()
        httpd.shutdown()
        tmp.cleanup()

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
