"""浏览器通道地基验证：纯标准库直连 CDP，不依赖 Playwright / Selenium。

验证五件事，全部打印实测数字：
  1. 内核启动到可连 CDP 的耗时
  2. Page.captureScreenshot 耗时与体积
  3. Input.dispatchMouseEvent 坐标点击是否精确命中
  4. Input.insertText 中文输入（必须先聚焦）
  5. Page.startScreencast 帧率
  6. 登录态持久化：优雅关闭 vs 硬杀（对比两种关闭方式）

用法（Windows，需本机装有 Edge 或 Chrome）：
    PYTHONPATH=src .venv\\Scripts\\python.exe scripts\\probe_browser_cdp.py
    ... --engine chrome          # 指定引擎
    ... --headful                # 可见模式（扫码场景）
    ... --skip-persistence       # 跳过登录态测试（省时间）

产物：截图写入系统临时目录的 probe-cdp-shot.jpg，路径会打印出来。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Windows 常见安装路径；后续由 browser.engine 模块正式接管
CANDIDATES = {
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ],
}


def find_engine(name: str) -> str | None:
    for path in CANDIDATES.get(name, []):
        if Path(path).exists():
            return path
    return None


class CdpClient:
    """极简 CDP 客户端：RFC 6455 客户端分帧 + JSON-RPC 调用。"""

    def __init__(self, url: str, timeout: float = 20.0):
        if not url.startswith("ws://"):
            raise ValueError("expect ws:// url")
        hostport, _, path = url[5:].partition("/")
        path = "/" + path
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {hostport}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("握手被关闭")
            buf += chunk
        head, _, self._buf = buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError("握手失败: " + head.decode(errors="replace"))
        self._id = 0

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        mask = os.urandom(4)
        n = len(payload)
        frame = bytearray([0x80 | opcode])
        if n < 126:
            frame.append(0x80 | n)
        elif n < (1 << 16):
            frame.append(0x80 | 126)
            frame += struct.pack(">H", n)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack(">Q", n)
        frame += mask
        frame += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(frame)

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self) -> tuple[int, bytes]:
        b0, b1 = self._read_exact(2)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        return b0 & 0x0F, (self._read_exact(length) if length else b"")

    def recv_json(self, timeout: float = 20.0) -> dict:
        deadline = time.time() + timeout
        while True:
            self.sock.settimeout(max(0.1, deadline - time.time()))
            opcode, data = self._read_frame()
            if opcode == 0x9:
                self._send_frame(data, opcode=0xA)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise RuntimeError("对端已关闭")
            return json.loads(data)

    def call(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = 20.0) -> dict:
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self._send_frame(json.dumps(msg).encode())
        deadline = time.time() + timeout
        while True:
            reply = self.recv_json(timeout=max(0.1, deadline - time.time()))
            if reply.get("id") == self._id:
                if "error" in reply:
                    raise RuntimeError(f"{method} 失败: {reply['error']}")
                return reply.get("result", {})

    def close(self) -> None:
        try:
            self._send_frame(b"", opcode=0x8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class Engine:
    """内核进程生命周期。必须删掉 DevToolsActivePort 残留，否则会连到上次实例的端口。"""

    def __init__(self, exe: str, profile: Path, headful: bool = False):
        self.exe = exe
        self.profile = profile
        self.headful = headful
        self.proc: subprocess.Popen | None = None
        self.port_file = profile / "DevToolsActivePort"
        self.ws_url = ""

    def start(self, timeout: float = 15.0) -> float:
        self.profile.mkdir(parents=True, exist_ok=True)
        if self.port_file.exists():
            self.port_file.unlink()
        args = [
            self.exe,
            f"--user-data-dir={self.profile}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            "--window-size=1280,800",
        ]
        if not self.headful:
            args.append("--headless=new")
        args.append("about:blank")
        t0 = time.time()
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        while time.time() - t0 < timeout:
            if self.port_file.exists():
                lines = self.port_file.read_text(encoding="utf-8", errors="replace").splitlines()
                if len(lines) >= 2 and lines[0].strip().isdigit():
                    url = f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
                    if wait_port(int(lines[0].strip())):
                        self.ws_url = url
                        return time.time() - t0
            time.sleep(0.05)
        self.kill()
        raise RuntimeError("DevToolsActivePort 未出现或端口不可连")

    def new_page(self, client: CdpClient, url: str) -> tuple[str, str]:
        target = client.call("Target.createTarget", {"url": url})
        sid = client.call(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
        )["sessionId"]
        client.call("Page.enable", {}, sid)
        client.call("Runtime.enable", {}, sid)
        return target["targetId"], sid

    def close_gracefully(self, client: CdpClient, timeout: float = 15.0) -> bool:
        """必须走 Browser.close，硬杀会丢 Cookie。"""
        try:
            client.call("Browser.close", {}, timeout=timeout)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=timeout)
            return True
        except Exception:
            self.kill()
            return False

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()


def wait_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def serve_page(root: Path, html: str) -> tuple[ThreadingHTTPServer, str]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(html, encoding="utf-8")
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None  # type: ignore[method-assign]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/index.html"


def run_probe(exe: str, headful: bool, skip_persistence: bool) -> int:
    work = Path(tempfile.mkdtemp(prefix="probe-cdp-"))
    srv, page_url = serve_page(work / "site", "<html><body><h1>probe</h1></body></html>")
    profile = work / "profile"
    engine = Engine(exe, profile, headful=headful)
    print(f"引擎: {exe}")
    print(f"模式: {'可见窗口' if headful else '无头 --headless=new'}")

    try:
        cost = engine.start()
        client = CdpClient(engine.ws_url)
        version = client.call("Browser.getVersion")
        print(f"[启动] {cost * 1000:.0f}ms  {version.get('product')}")

        _, sid = engine.new_page(client, page_url)
        time.sleep(0.4)

        # 1) 截图
        t = time.time()
        shot = client.call("Page.captureScreenshot", {"format": "jpeg", "quality": 70}, sid)
        raw = base64.b64decode(shot["data"])
        out = Path(tempfile.gettempdir()) / "probe-cdp-shot.jpg"
        out.write_bytes(raw)
        print(f"[截图] {(time.time() - t) * 1000:.0f}ms  {len(raw) / 1024:.1f} KB  "
              f"magic={raw[:2].hex()}  -> {out}")

        # 2) 坐标点击
        client.call("Runtime.evaluate", {"expression": """
document.body.style.margin='0';
document.body.innerHTML='<button id=b style="position:absolute;left:100px;top:100px;' +
  'width:200px;height:80px">click</button>';
window.__hit = 0;
document.getElementById('b').onclick = e => { window.__hit = 1; window.__xy = [e.clientX, e.clientY]; };
"""}, sid)
        t = time.time()
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            client.call(
                "Input.dispatchMouseEvent",
                {"type": kind, "x": 200, "y": 140, "button": "left", "clickCount": 1},
                sid,
            )
        cost_click = (time.time() - t) * 1000
        hit = client.call(
            "Runtime.evaluate",
            {"expression": "[window.__hit, window.__xy]", "returnByValue": True},
            sid,
        )["result"]["value"]
        print(f"[点击] {cost_click:.0f}ms  命中={hit[0]}  收到坐标={hit[1]}（派发 200,140）")

        # 3) 中文输入（必须先聚焦）
        client.call("Runtime.evaluate", {
            "expression": "document.body.innerHTML='<input id=i>';"
                          "document.getElementById('i').focus();"
        }, sid)
        client.call("Input.insertText", {"text": "扫码登录测试 😀"}, sid)
        value = client.call(
            "Runtime.evaluate",
            {"expression": "document.getElementById('i').value", "returnByValue": True},
            sid,
        )["result"]["value"]
        print(f"[中文] insertText -> {value!r}")

        # 4) 无障碍树
        ax = client.call("Accessibility.getFullAXTree", {}, sid)
        print(f"[AX] {len(ax.get('nodes', []))} 个节点")

        # 5) 镜像帧
        client.call("Runtime.evaluate", {"expression": """
document.body.style.margin='0';
document.body.innerHTML='<div id=box style="width:1280px;height:700px;background:#2266cc"></div>';
let n=0; setInterval(()=>{n++; document.getElementById('box').style.background=
  `hsl(${n*7%360},70%,50%)`;},120);
"""}, sid)
        client.call("Page.startScreencast",
                    {"format": "jpeg", "quality": 60, "maxWidth": 1280}, sid)
        stamps, sizes = [], []
        t0 = time.time()
        while time.time() - t0 < 4 and len(stamps) < 6:
            try:
                msg = client.recv_json(timeout=2.0)
            except Exception:
                break
            if msg.get("method") == "Page.screencastFrame":
                stamps.append(round((time.time() - t0) * 1000))
                sizes.append(round(len(msg["params"]["data"]) / 1024, 1))
                client.call("Page.screencastFrameAck",
                            {"sessionId": msg["params"]["sessionId"]}, sid)
        client.call("Page.stopScreencast", {}, sid)
        if len(stamps) > 1:
            gap = (stamps[-1] - stamps[0]) / (len(stamps) - 1)
            print(f"[镜像] {len(stamps)} 帧  间隔≈{gap:.0f}ms ({1000 / gap:.1f} fps)  "
                  f"单帧KB={sizes}")
        else:
            print(f"[镜像] 仅 {len(stamps)} 帧（画面无变化时正常）")

        # 6) 登录态持久化
        if not skip_persistence:
            client.call("Runtime.evaluate", {"expression": """
document.cookie='probe_token=keep-me; path=/; max-age=86400';
localStorage.setItem('probe_user','tester');
"""}, sid)
            print("[登录态] 已写入 cookie + localStorage，优雅关闭后重开验证")
            engine.close_gracefully(client)
            time.sleep(1.0)
            engine2 = Engine(exe, profile, headful=headful)
            engine2.start()
            client2 = CdpClient(engine2.ws_url)
            cookies = client2.call("Storage.getCookies", {}).get("cookies", [])
            names = [(c["name"], c["value"][:14]) for c in cookies]
            _, sid2 = engine2.new_page(client2, page_url)
            time.sleep(0.5)
            ls = client2.call(
                "Runtime.evaluate",
                {"expression": "localStorage.getItem('probe_user')", "returnByValue": True},
                sid2,
            )["result"]["value"]
            ok = any(n == "probe_token" for n, _ in names)
            print(f"[登录态] 重开后 cookie={names}  localStorage={ls!r}  "
                  f"{'保留 ✓' if ok else '丢失 ✗'}")
            engine2.close_gracefully(client2)
        else:
            engine.close_gracefully(client)
    finally:
        try:
            engine.kill()
        except Exception:
            pass
        srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print("OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="浏览器通道 CDP 地基验证")
    parser.add_argument("--engine", choices=sorted(CANDIDATES), default="edge")
    parser.add_argument("--headful", action="store_true", help="可见窗口模式")
    parser.add_argument("--skip-persistence", action="store_true",
                        help="跳过登录态持久化测试")
    args = parser.parse_args()

    exe = find_engine(args.engine)
    if not exe:
        print(f"未检测到 {args.engine}，可改用 --engine chrome，或安装 Edge/Chrome 后重试")
        return 2
    return run_probe(exe, args.headful, args.skip_persistence)


if __name__ == "__main__":
    sys.exit(main())
