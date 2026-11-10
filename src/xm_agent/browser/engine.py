"""引擎解析与浏览器进程生命周期。

关键约定（都来自真机踩坑，改之前先看 TECH_DESIGN 4.21.14）：
1. 启动前必须删掉 `<profile>/DevToolsActivePort`，否则会读到上次实例的端口。
2. 只判断文件存在不够，还要校验端口真的可连。
3. 关闭必须走 `Browser.close`，硬杀会丢内存里的 Cookie 与 localStorage。
4. 同一 profile 只允许一个实例，否则 Chromium 会复用已有进程并让新进程退出。
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


class BrowserError(RuntimeError):
    """浏览器相关的可预期失败（未安装、启动超时、profile 占用）。"""


class EngineNotFound(BrowserError):
    """指定或兜底的浏览器内核都不存在。"""


# Windows 常见安装路径。注册表 App Paths 作为补充（见 _registry_paths）。
ENGINE_PATHS: dict[str, list[str]] = {
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

ENGINE_ORDER = ("edge", "chrome", "chromium")
ENGINE_LABELS = {"edge": "Microsoft Edge", "chrome": "Google Chrome", "chromium": "内置 Chromium"}

_APP_PATHS = {
    "edge": r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
    "chrome": r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
}


@dataclass
class EngineInfo:
    """一个可用内核的描述。"""

    name: str
    path: str
    version: str
    source: str  # system | bundled
    note: str = ""  # 非空表示发生了降级，需要提示用户

    @property
    def label(self) -> str:
        return ENGINE_LABELS.get(self.name, self.name)


def _registry_paths(name: str) -> list[str]:
    """从注册表 App Paths 读安装位置（用 winreg，不调用 reg.exe）。"""
    key = _APP_PATHS.get(name)
    if not key or sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    found: list[str] = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (0, getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)):
            try:
                with winreg.OpenKey(root, key, 0, winreg.KEY_READ | view) as handle:
                    value, _ = winreg.QueryValueEx(handle, "")
                    if value and Path(value).exists():
                        found.append(str(value))
            except OSError:
                continue
    return found


def _pinned_bundled_version(bundled_root: Path) -> str:
    """读资源管理器的启用版本记录。

    内置内核没有稳定入口链接（引擎解析直接找 `chromium-*` 目录），
    所以在用版本由 `.xm-agent/resources/active.json` 决定；读不到就当没有，
    绝不能因为一份记录坏了而启动不了浏览器。
    """
    try:
        pin_path = bundled_root.parents[1] / "resources" / "active.json"
        data = json.loads(pin_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, IndexError):
        return ""
    record = data.get("chromium") if isinstance(data, dict) else None
    return str(record.get("version") or "") if isinstance(record, dict) else ""


def find_engine_path(name: str, bundled_root: Path | None = None) -> str | None:
    """按名称定位可执行文件；内置 Chromium 在 resources 安装目录下找版本。"""
    if name == "chromium":
        if bundled_root is None:
            return None
        pinned = _pinned_bundled_version(bundled_root)
        if pinned:
            # 有启用版本就听它的（升级后是新的，回滚后是旧的）
            for pattern in (
                f"chromium-{pinned}/chrome.exe",
                f"chromium-{pinned}/**/chrome.exe",
            ):
                for candidate in sorted(bundled_root.glob(pattern), reverse=True):
                    if candidate.exists():
                        return str(candidate)
        # 没有记录时取最新版本；zip 解压后可能多一层目录，两种布局都试
        for pattern in ("chromium-*/chrome.exe", "chromium-*/**/chrome.exe"):
            for candidate in sorted(bundled_root.glob(pattern), reverse=True):
                if candidate.exists():
                    return str(candidate)
        return None
    for path in ENGINE_PATHS.get(name, []):
        if path and Path(path).exists():
            return path
    from_registry = _registry_paths(name)
    return from_registry[0] if from_registry else None


def engine_version(path: str, timeout: float = 8.0) -> str:
    """读内核版本；失败返回空串，不抛异常（版本只用于展示与探测）。"""
    try:
        result = subprocess.run(
            [path, "--product-version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = (result.stdout or "").strip().splitlines()
        if text:
            return text[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def bundled_root_for(profile_root: Path) -> Path:
    """内置内核目录：与 profiles 同级，便于整体迁移与清理。"""
    return profile_root.parent / "engines"


def default_user_agent(engine: EngineInfo) -> str:
    """按内核生成一条普通浏览器的 UA。

    无头模式原生的 UA 是 `HeadlessChrome/<v>`，这是最容易被站点直接拦的一条
    特征（自如的 EdgeOne 就是照这个拦的）。这里用真实产品版本重组成标准 UA：
    Chrome 内核只带 `Chrome/<v>`，Edge 再补 `Edg/<v>`——和真人开同一个内核时
    发出去的字符串一致。
    """
    version = (engine.version or "").strip() or "120.0.0.0"
    # 和真机一致：Chrome 令牌只带大版本（x.0.0.0），Edg 令牌才带完整版本
    major = version.split(".")[0] if version[0].isdigit() else "120"
    base = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )
    if engine.name == "edge":
        return f"{base} Edg/{version}"
    return base


def resolve_engine(
    preference: str = "auto",
    bundled_root: Path | None = None,
    probe_version: bool = True,
) -> EngineInfo:
    """解析使用哪个内核。

    `preference` 为 `auto` 时按 Edge → Chrome → 内置 Chromium 顺序回落；
    指定具体引擎而该引擎缺失时**不静默替换**，直接抛 EngineNotFound，
    由调用方决定是否提示用户改配置。
    """
    preference = (preference or "auto").strip().lower()
    if preference not in ("auto", *ENGINE_ORDER):
        raise BrowserError(f"未知引擎: {preference}")

    order = ENGINE_ORDER if preference == "auto" else (preference,)
    missing: list[str] = []
    for name in order:
        path = find_engine_path(name, bundled_root)
        if not path:
            missing.append(name)
            continue
        note = ""
        if preference == "auto" and missing:
            skipped = "、".join(ENGINE_LABELS.get(m, m) for m in missing)
            note = f"未检测到 {skipped}，已回落 {ENGINE_LABELS[name]}"
        return EngineInfo(
            name=name,
            path=path,
            version=engine_version(path) if probe_version else "",
            source="bundled" if name == "chromium" else "system",
            note=note,
        )

    if preference == "auto":
        raise EngineNotFound(
            "未检测到可用的浏览器内核（Edge / Chrome / 内置 Chromium 均缺失）。"
            "请安装 Edge 或 Chrome，或在控制台「资源」里安装内置 Chromium，"
            "也可以执行 python -m xm_agent resources ensure --only chromium（约 210MB）。"
        )
    raise EngineNotFound(
        f"未检测到 {ENGINE_LABELS.get(preference, preference)}，且已指定引擎不自动替换。"
        "请安装该浏览器，或把 [runtime.browser].engine 改为 auto。"
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class ProfileLock:
    """同一 profile 的独占锁，防止两个实例抢同一个 user-data-dir。

    分两层：进程内用注册表拦住「同进程开两个会话」，跨进程用锁文件 + pid 存活判断。
    """

    _held: ClassVar[dict[str, ProfileLock]] = {}
    _guard: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, profile_dir: Path):
        self.path = profile_dir / ".browser-lock"
        self._key = str(profile_dir.resolve()).lower()
        self._held_by_me = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with ProfileLock._guard:
            holder = ProfileLock._held.get(self._key)
            if holder is not None and holder is not self:
                raise BrowserError(
                    f"该 profile 已被本进程的另一个会话占用：{self.path.parent}"
                )
            if holder is self:
                return

        for _ in range(2):
            try:
                with open(self.path, "x", encoding="utf-8") as handle:
                    handle.write(str(os.getpid()))
                self._mark_held()
                return
            except FileExistsError:
                holder_pid = 0
                try:
                    holder_pid = int(
                        self.path.read_text(encoding="utf-8").strip() or "0"
                    )
                except (OSError, ValueError):
                    holder_pid = 0
                if holder_pid and _pid_alive(holder_pid):
                    raise BrowserError(
                        f"该 profile 已被进程 {holder_pid} 占用：{self.path.parent}"
                    )
                # 残留锁：持有者已退出，直接清掉重试
                try:
                    self.path.unlink()
                except OSError as exc:
                    raise BrowserError(f"清理残留锁失败: {exc}") from exc
        raise BrowserError(f"无法获取 profile 锁: {self.path}")

    def _mark_held(self) -> None:
        with ProfileLock._guard:
            ProfileLock._held[self._key] = self
        self._held_by_me = True

    def release(self) -> None:
        if not self._held_by_me:
            return
        with ProfileLock._guard:
            if ProfileLock._held.get(self._key) is self:
                ProfileLock._held.pop(self._key, None)
        self._held_by_me = False
        try:
            self.path.unlink()
        except OSError:
            pass


def _port_open(port: int, timeout: float = 0.4) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class BrowserProcess:
    """一个浏览器内核进程。启动后通过 `ws_url` 连 CDP。"""

    def __init__(
        self,
        engine: EngineInfo,
        profile_dir: Path,
        headless: bool = True,
        window_size: tuple[int, int] = (1280, 800),
        extra_args: list[str] | None = None,
        user_agent: str = "",
        hide_automation: bool = True,
    ):
        self.engine = engine
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.window_size = window_size
        self.extra_args = extra_args or []
        # 空串表示「按内核自动生成」：无头模式下必须换掉 HeadlessChrome
        self.user_agent = user_agent
        self.hide_automation = hide_automation
        self.proc: subprocess.Popen | None = None
        self.ws_url = ""
        self.port = 0
        self._lock = ProfileLock(self.profile_dir)
        self._closed = False

    def resolved_user_agent(self) -> str:
        if self.user_agent:
            return self.user_agent
        if not self.headless:
            return ""  # 可见窗口的 UA 本来就是正常的，不用改
        return default_user_agent(self.engine)

    def command_args(self) -> list[str]:
        """拼启动命令行。

        单独成方法是为了能被单测直接检查：UA 与自动化标记这两条一旦丢了，
        真机表现是「站点返回拦截页」而不是崩溃，很难靠别的用例发现。
        """
        args = [
            self.engine.path,
            f"--user-data-dir={self.profile_dir}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            # 翻译气泡会盖在页面右上角（Edge 的窗口类也是 Chrome_WidgetWin_1，
            # 还会被 window.py 误当成一个顶层窗口），既挡视线又干扰截图。
            # 真机对比过：只关 Translate/TranslateUI 时气泡照样弹（Edge 走自己的
            # 开关名），把 msEdgeTranslate、EdgeTranslate 一起关掉才彻底不出现。
            "--disable-features=Translate,TranslateUI,msEdgeTranslate,EdgeTranslate",
            f"--window-size={self.window_size[0]},{self.window_size[1]}",
            *self.extra_args,
        ]
        user_agent = self.resolved_user_agent()
        if user_agent:
            args.append(f"--user-agent={user_agent}")
        if self.hide_automation:
            # 去掉 `navigator.webdriver` 背后的自动化标记，配合 stealth.py
            # 里注入的脚本，页面读到的就是一台普通浏览器
            args.append("--disable-blink-features=AutomationControlled")
            # 上面这条会被内核当成「不受支持的命令行标记」，在页面顶部挂一条
            # 黄色提示条，把内容往下挤一截。--test-type 就是让内核别为此报警。
            args.append("--test-type")
        if self.headless:
            args.append("--headless=new")
        # 不传 URL：内核自己开一个起始页就够了。传 `about:blank` 会让 Edge 多开
        # 一个空白标签（真机：窗口里并排两个标签，其中一个永远空着），
        # 而「只有一个浏览器」在观感上不能有两个标签。
        return args

    @property
    def port_file(self) -> Path:
        return self.profile_dir / "DevToolsActivePort"

    def start(self, timeout: float = 20.0) -> str:
        """启动内核并返回 CDP WebSocket 端点。"""
        if self.proc is not None and self.proc.poll() is None:
            return self.ws_url
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._lock.acquire()
        # 坑 1：残留的端口文件会指向上一次实例，必须先删
        try:
            self.port_file.unlink(missing_ok=True)
        except OSError as exc:
            self._lock.release()
            raise BrowserError(f"删除残留 DevToolsActivePort 失败: {exc}") from exc

        args = self.command_args()

        try:
            self.proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self._lock.release()
            raise BrowserError(f"启动 {self.engine.label} 失败: {exc}") from exc

        # 坑 2：文件出现不代表能用，还要端口可连
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.port_file.exists():
                try:
                    lines = self.port_file.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                except OSError:
                    lines = []
                if len(lines) >= 2 and lines[0].strip().isdigit():
                    port = int(lines[0].strip())
                    if _port_open(port):
                        self.port = port
                        self.ws_url = f"ws://127.0.0.1:{port}{lines[1].strip()}"
                        return self.ws_url
            if self.proc.poll() is not None:
                self._lock.release()
                raise BrowserError(
                    f"{self.engine.label} 启动后立即退出（退出码 {self.proc.returncode}），"
                    f"可能 profile 被占用：{self.profile_dir}"
                )
            time.sleep(0.05)

        self.kill()
        raise BrowserError(f"{self.engine.label} 在 {timeout}s 内未就绪（DevToolsActivePort 未出现）")

    def close(self, graceful: bool = True, timeout: float = 15.0) -> bool:
        """优雅关闭。返回是否干净退出。

        `graceful=True` 必须配合 `Browser.close` CDP 调用（由 session 调用）；
        这里只负责等待进程退出，超时才强杀。
        """
        if self.proc is None:
            self._lock.release()
            return True
        clean = False
        if graceful:
            try:
                self.proc.wait(timeout=timeout)
                clean = True
            except subprocess.TimeoutExpired:
                clean = False
        if not clean:
            self.kill()
        self._closed = True
        self._lock.release()
        return clean

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._closed = True
        self._lock.release()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None
