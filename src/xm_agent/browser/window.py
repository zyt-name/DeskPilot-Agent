"""把浏览器真窗口提到最前（Windows）。

为什么需要这一层：CDP 只有 `Page.bringToFront`，它激活的是**标签页**，不一定
把操作系统窗口抬到前台——真机试过，IDE 占着前台时调用它，前台窗口一动不动。
而产品要求是「任务结束后可以在浏览器里直接接着操作」，窗口得真的在最前面，
所以补一次 Win32 的 SetForegroundWindow。

只做前置，不注入、不模拟输入，也不改窗口内容。
"""

from __future__ import annotations

import ctypes
import sys

_SW_RESTORE = 9
_GW_OWNER = 4
_WINDOW_CLASS = "Chrome_WidgetWin_1"


def _win_dlls():
    if sys.platform != "win32":
        return None, None
    try:
        return ctypes.windll.user32, ctypes.windll.kernel32
    except (AttributeError, OSError):  # pragma: no cover - 非 Windows
        return None, None


def _main_window(user32, pid: int) -> int:
    """这个进程的「主」窗口：可见、无 owner、面积最大的那个。

    浏览器进程会带好几个顶层窗口（翻译气泡、下载气泡都是），它们都有 owner；
    主窗口 owner 为 0。按面积取最大，避免把气泡当成浏览器。
    """
    best = 0
    best_area = 0
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd, _lparam) -> bool:
        nonlocal best, best_area
        if not user32.IsWindowVisible(hwnd):
            return True
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, name, 256)
        if name.value != _WINDOW_CLASS:
            return True
        owner_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if int(owner_pid.value) != pid:
            return True
        if user32.GetWindow(hwnd, _GW_OWNER):
            return True
        rect = (ctypes.c_long * 4)()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
        if area > best_area:
            best, best_area = int(hwnd), area
        return True

    user32.EnumWindows(enum_proc(visit), 0)
    return best


def _force_foreground(user32, kernel32, hwnd: int) -> bool:
    user32.ShowWindow(hwnd, _SW_RESTORE)
    if user32.SetForegroundWindow(hwnd):
        return True
    # 前台窗口属于别的线程时，Windows 会拒绝 SetForegroundWindow；
    # 临时把输入队列挂到一起就能拿到许可，之后立刻解开。
    foreground = user32.GetForegroundWindow()
    target_thread = user32.GetWindowThreadProcessId(foreground, None)
    my_thread = kernel32.GetCurrentThreadId()
    attached = bool(user32.AttachThreadInput(my_thread, target_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        if user32.SetForegroundWindow(hwnd):
            return True
    finally:
        if attached:
            user32.AttachThreadInput(my_thread, target_thread, False)
    return int(user32.GetForegroundWindow()) == hwnd


def raise_window(pid: int) -> bool:
    """把该进程的浏览器主窗口抬到最前。失败返回 False（调用方当成没做成）。"""
    user32, kernel32 = _win_dlls()
    if user32 is None or kernel32 is None or int(pid) <= 0:
        return False
    try:
        hwnd = _main_window(user32, int(pid))
        if not hwnd:
            return False
        return _force_foreground(user32, kernel32, hwnd)
    except (AttributeError, OSError, ValueError):  # pragma: no cover - 老系统
        return False
