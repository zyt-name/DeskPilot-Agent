"""Click-through screen edge glow shown while a task owns the desktop.

The indicator is task-scoped: the UI server shows it once when a run starts
using the screen and hides it when the run ends, fails or is cancelled. It is
deliberately not tied to individual MCP calls so the highlight does not blink
between actions.
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import threading

from ctypes import wintypes

logger = logging.getLogger(__name__)

_WM_APP = 0x8000
_WM_TIMER = 0x0113
_WM_NCHITTEST = 0x0084
_HTTRANSPARENT = -1
_WM_ERASEBKGND = 0x0014
_WS_POPUP = 0x80000000
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_TOPMOST = 0x00000008
_WS_EX_NOACTIVATE = 0x08000000
_SW_HIDE = 0
_SW_SHOWNOACTIVATE = 4
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040
_ULW_ALPHA = 0x00000002
_AC_SRC_OVER = 0x00
_AC_SRC_ALPHA = 0x01
_WDA_EXCLUDEFROMCAPTURE = 0x00000011
_DIB_RGB_COLORS = 0
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

_GLOW = 14
_TIMER_ID = 1
_INTERVAL_MS = 70
_LEVELS = (0.55, 0.75, 1.0, 0.75)
_CORE = (150, 220, 255)
_BODY = (80, 170, 255)


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BitmapInfoHeader),
        ("bmiColors", wintypes.DWORD * 3),
    ]


class _BlendFunction(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_WNDPROC = _FUNCTYPE(
    ctypes.c_ssize_t,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class _WindowClass(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _load_win32():
    if os.name != "nt":
        return None, None, None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WindowClass)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.UnregisterClassW.restype = wintypes.BOOL
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetTimer.argtypes = [
        wintypes.HWND,
        ctypes.c_size_t,
        wintypes.UINT,
        wintypes.LPVOID,
    ]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    user32.KillTimer.restype = wintypes.BOOL
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND,
        wintypes.HDC,
        ctypes.POINTER(wintypes.POINT),
        ctypes.POINTER(wintypes.SIZE),
        wintypes.HDC,
        ctypes.POINTER(wintypes.POINT),
        wintypes.COLORREF,
        ctypes.POINTER(_BlendFunction),
        wintypes.DWORD,
    ]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(_BitmapInfo),
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    return user32, gdi32, kernel32


def _wndproc(hwnd, message, wparam, lparam):
    if message == _WM_NCHITTEST:
        return _HTTRANSPARENT
    if message == _WM_ERASEBKGND:
        return 1
    return _user32.DefWindowProcW(hwnd, message, wparam, lparam)


_user32, _gdi32, _kernel32 = _load_win32()
_WNDPROC_REF = _WNDPROC(_wndproc) if _user32 is not None else None


def _edge_bitmap(width: int, height: int, side: str, level: float) -> bytes:
    buffer = bytearray(width * height * 4)
    offset = 0
    for y in range(height):
        for x in range(width):
            if side == "top":
                distance = y
            elif side == "bottom":
                distance = height - 1 - y
            elif side == "left":
                distance = x
            else:
                distance = width - 1 - x
            alpha = 0
            if distance < _GLOW:
                falloff = 1.0 - distance / _GLOW
                alpha = int(215 * (falloff**1.7) * level)
                if distance < 2:
                    alpha = max(alpha, int(190 * level))
            red, green, blue = _CORE if distance < 2 else _BODY
            # UpdateLayeredWindow expects premultiplied BGRA.
            buffer[offset] = blue * alpha // 255
            buffer[offset + 1] = green * alpha // 255
            buffer[offset + 2] = red * alpha // 255
            buffer[offset + 3] = alpha
            offset += 4
    return bytes(buffer)


class _Edge:
    def __init__(self, hwnd, hdc, bitmap, bits, x: int, y: int, width: int, height: int):
        self.hwnd = hwnd
        self.hdc = hdc
        self.bitmap = bitmap
        self.bits = bits
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.frames: list[bytes] = []

    def draw(self, frame: bytes, screen_dc) -> None:
        ctypes.memmove(self.bits, frame, len(frame))
        destination = wintypes.POINT(self.x, self.y)
        size = wintypes.SIZE(self.width, self.height)
        source = wintypes.POINT(0, 0)
        blend = _BlendFunction(_AC_SRC_OVER, 0, 255, _AC_SRC_ALPHA)
        if not _user32.UpdateLayeredWindow(
            self.hwnd,
            screen_dc,
            ctypes.byref(destination),
            ctypes.byref(size),
            self.hdc,
            ctypes.byref(source),
            0,
            ctypes.byref(blend),
            _ULW_ALPHA,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def release(self) -> None:
        _user32.DestroyWindow(self.hwnd)
        _gdi32.DeleteObject(self.bitmap)
        _gdi32.DeleteDC(self.hdc)


class ScreenIndicator:
    """Best-effort desktop edge glow; all failures degrade to no indicator."""

    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = (os.name == "nt") if enabled is None else bool(enabled)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._available = False
        self._visible = False
        self._closed = False
        self._thread_id = 0
        self._queue: queue.Queue[str] = queue.Queue()
        self._class_name = f"XmAgentScreenIndicator_{os.getpid()}"
        self._instance = None
        self._edges: list[_Edge] = []
        self._geometry: tuple[int, int, int, int] | None = None
        self._screen_dc = None
        self._frame = 0

    @property
    def available(self) -> bool:
        return self._available

    def start(self) -> bool:
        if not self.enabled or self._thread is not None:
            return self._available
        self._thread = threading.Thread(
            target=self._run, name="xm-screen-indicator", daemon=True
        )
        self._thread.start()
        self._ready.wait(2.0)
        return self._available

    def show(self) -> None:
        self._submit("show")

    def hide(self) -> None:
        self._submit("hide")

    def close(self) -> None:
        if self._thread is None:
            return
        self._submit("close")
        self._thread.join(timeout=2.0)
        self._thread = None

    def _submit(self, command: str) -> None:
        if self._thread is None or self._closed:
            return
        self._queue.put(command)
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, _WM_APP, 0, 0)

    def _run(self) -> None:
        try:
            self._register_class()
        except Exception:
            logger.warning("screen indicator unavailable", exc_info=True)
            self._closed = True
            self._ready.set()
            return
        self._available = True
        self._thread_id = _kernel32.GetCurrentThreadId()
        self._ready.set()
        try:
            self._drain()
            message = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == _WM_APP:
                    self._drain()
                elif message.message == _WM_TIMER:
                    self._tick()
                else:
                    _user32.TranslateMessage(ctypes.byref(message))
                    _user32.DispatchMessageW(ctypes.byref(message))
        finally:
            self._closed = True
            self._destroy()

    def _register_class(self) -> None:
        self._instance = _kernel32.GetModuleHandleW(None)
        window_class = _WindowClass()
        window_class.lpfnWndProc = _WNDPROC_REF
        window_class.hInstance = self._instance
        window_class.lpszClassName = self._class_name
        if not _user32.RegisterClassW(ctypes.byref(window_class)):
            error = ctypes.get_last_error()
            if error != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                raise ctypes.WinError(error)

    def _drain(self) -> None:
        while True:
            try:
                command = self._queue.get_nowait()
            except queue.Empty:
                return
            if command == "show":
                self._show()
            elif command == "hide":
                self._hide()
            elif command == "close":
                self._hide()
                _user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
                return

    def _show(self) -> None:
        try:
            self._ensure_edges()
            for edge in self._edges:
                _user32.ShowWindow(edge.hwnd, _SW_SHOWNOACTIVATE)
                _user32.SetWindowPos(
                    edge.hwnd,
                    wintypes.HWND(-1),
                    edge.x,
                    edge.y,
                    edge.width,
                    edge.height,
                    _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
                )
            self._visible = True
            self._frame = 0
            self._tick()
            _user32.SetTimer(None, _TIMER_ID, _INTERVAL_MS, None)
        except Exception:
            logger.warning("screen indicator could not be shown", exc_info=True)
            self._hide()

    def _hide(self) -> None:
        if not self._visible:
            return
        _user32.KillTimer(None, _TIMER_ID)
        for edge in self._edges:
            _user32.ShowWindow(edge.hwnd, _SW_HIDE)
        self._visible = False

    def _tick(self) -> None:
        if not self._visible or not self._edges:
            return
        self._frame = (self._frame + 1) % len(_LEVELS)
        try:
            for edge in self._edges:
                edge.draw(edge.frames[self._frame], self._screen_dc)
        except Exception:
            logger.warning("screen indicator frame failed", exc_info=True)
            self._hide()

    def _ensure_edges(self) -> None:
        left = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        top = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        width = _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
        height = _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
        geometry = (left, top, width, height)
        if self._geometry == geometry and self._edges:
            return
        self._destroy_edges()
        self._screen_dc = _user32.GetDC(None)
        if not self._screen_dc:
            raise RuntimeError("GetDC failed")
        glow = min(_GLOW, max(1, width // 4), max(1, height // 4))
        specs = (
            ("top", left, top, width, glow),
            ("bottom", left, top + height - glow, width, glow),
            ("left", left, top, glow, height),
            ("right", left + width - glow, top, glow, height),
        )
        for side, x, y, w, h in specs:
            hdc = _gdi32.CreateCompatibleDC(self._screen_dc)
            if not hdc:
                raise RuntimeError("CreateCompatibleDC failed")
            info = _BitmapInfo()
            info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
            info.bmiHeader.biWidth = w
            info.bmiHeader.biHeight = -h
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0  # BI_RGB
            bits = ctypes.c_void_p()
            bitmap = _gdi32.CreateDIBSection(
                hdc,
                ctypes.byref(info),
                _DIB_RGB_COLORS,
                ctypes.byref(bits),
                None,
                0,
            )
            if not bitmap:
                _gdi32.DeleteDC(hdc)
                raise RuntimeError("CreateDIBSection failed")
            _gdi32.SelectObject(hdc, bitmap)
            hwnd = _user32.CreateWindowExW(
                _WS_EX_LAYERED
                | _WS_EX_TRANSPARENT
                | _WS_EX_TOOLWINDOW
                | _WS_EX_TOPMOST
                | _WS_EX_NOACTIVATE,
                self._class_name,
                "",
                _WS_POPUP,
                x,
                y,
                w,
                h,
                None,
                None,
                self._instance,
                None,
            )
            if not hwnd:
                _gdi32.DeleteObject(bitmap)
                _gdi32.DeleteDC(hdc)
                raise RuntimeError("CreateWindowExW failed")
            _user32.SetWindowDisplayAffinity(hwnd, _WDA_EXCLUDEFROMCAPTURE)
            edge = _Edge(hwnd, hdc, bitmap, bits, x, y, w, h)
            edge.frames = [
                _edge_bitmap(w, h, side, level) for level in _LEVELS
            ]
            self._edges.append(edge)
        self._geometry = geometry

    def _destroy_edges(self) -> None:
        for edge in self._edges:
            try:
                edge.release()
            except Exception:
                pass
        self._edges.clear()
        if self._screen_dc:
            _user32.ReleaseDC(None, self._screen_dc)
            self._screen_dc = None
        self._geometry = None

    def _destroy(self) -> None:
        try:
            self._hide()
        finally:
            self._destroy_edges()
            _user32.UnregisterClassW(self._class_name, self._instance)
            self._available = False


__all__ = ["ScreenIndicator"]
