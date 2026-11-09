"""Click-through desktop overlay shown while a task owns the desktop.

The overlay has two parts that share one message-loop thread:

* a thin glow along the screen edges that breathes slowly and cycles through a
  few colours, so it reads as "the agent is driving" without flashing;
* a small status pill at the top centre of the primary screen that spells out
  the F8 pause hint and the current state (running / paused).

The indicator is task-scoped: the UI server shows it once when a run starts
using the screen and hides it when the run ends, fails or is cancelled. It is
deliberately not tied to individual MCP calls so the highlight does not blink
between actions. Both surfaces are click-through and excluded from screen
capture so the agent never sees them in its own screenshots.
"""

from __future__ import annotations

import ctypes
import logging
import math
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
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1

_DT_SINGLELINE = 0x00000020
_DT_CALCRECT = 0x00000400
_DT_NOPREFIX = 0x00000800
_TRANSPARENT_BK = 1
_FW_SEMIBOLD = 600
_DEFAULT_CHARSET = 1
_ANTIALIASED_QUALITY = 4

_GLOW = 14
_TIMER_ID = 1
_INTERVAL_MS = 120
_BREATH_SECONDS = 3.6
_BREATH_FRAMES = max(1, round(_BREATH_SECONDS * 1000 / _INTERVAL_MS))
_LEVEL_MIN = 0.5
_LEVEL_MAX = 1.0
_COLOR_STEP_SECONDS = 4.0
_COLOR_STEP_FRAMES = max(1, round(_COLOR_STEP_SECONDS * 1000 / _INTERVAL_MS))
_PAUSED_LEVEL = 0.85
_RUN_PALETTE = (
    (80, 170, 255),
    (150, 120, 255),
    (70, 210, 190),
    (90, 205, 255),
)
_PAUSED_BODY = (255, 190, 80)

_BADGE_TOP = 10
_BADGE_FONT_PX = 17
_BADGE_PADDING_X = 15
_BADGE_PADDING_Y = 8
_BADGE_DOT = 7
_BADGE_GAP = 9
_BADGE_RADIUS = 16
_BADGE_BG = (16, 20, 28)
_BADGE_BG_ALPHA = 214
_BADGE_BORDER_ALPHA = 235
_BADGE_TEXT_COLOR = (238, 244, 252)
_RUNNING_TEXT = "正在操控 · 按 F8 暂停"
_PAUSED_TEXT = "已暂停 · 按 F8 继续"


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
    user32.DrawTextW.argtypes = [
        wintypes.HDC,
        wintypes.LPCWSTR,
        ctypes.c_int,
        ctypes.POINTER(wintypes.RECT),
        wintypes.UINT,
    ]
    user32.DrawTextW.restype = ctypes.c_int

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
    gdi32.CreateFontW.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPCWSTR,
    ]
    gdi32.CreateFontW.restype = wintypes.HGDIOBJ
    gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetBkMode.restype = ctypes.c_int
    gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    gdi32.SetTextColor.restype = wintypes.DWORD
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


def _status_text(paused: bool) -> str:
    return _PAUSED_TEXT if paused else _RUNNING_TEXT


def _breath_level(frame: int) -> float:
    phase = 2.0 * math.pi * (frame % _BREATH_FRAMES) / _BREATH_FRAMES
    wave = (math.sin(phase - math.pi / 2.0) + 1.0) / 2.0
    return _LEVEL_MIN + (_LEVEL_MAX - _LEVEL_MIN) * wave


def _mix(first, second, ratio: float):
    return tuple(round(a + (b - a) * ratio) for a, b in zip(first, second))


def _palette_color(frame: int):
    position = (frame % (_COLOR_STEP_FRAMES * len(_RUN_PALETTE))) / _COLOR_STEP_FRAMES
    index = int(position)
    mix = position - index
    if mix < 0.75:
        return _RUN_PALETTE[index]
    ratio = (mix - 0.75) / 0.25
    ratio = ratio * ratio * (3.0 - 2.0 * ratio)
    return _mix(
        _RUN_PALETTE[index], _RUN_PALETTE[(index + 1) % len(_RUN_PALETTE)], ratio
    )


def _tint(color, factor: float):
    return tuple(min(255, max(0, round(channel * factor))) for channel in color)


def _glow_pixel(distance: int, body, level: float) -> bytes:
    if distance >= _GLOW:
        return b"\x00\x00\x00\x00"
    alpha = int(215 * ((1.0 - distance / _GLOW) ** 1.7) * level)
    if distance < 2:
        alpha = max(alpha, int(190 * level))
    if alpha <= 0:
        return b"\x00\x00\x00\x00"
    red, green, blue = _tint(body, 1.45) if distance < 2 else body
    # UpdateLayeredWindow expects premultiplied BGRA.
    return bytes(
        (
            min(255, blue * alpha // 255),
            min(255, green * alpha // 255),
            min(255, red * alpha // 255),
            min(255, alpha),
        )
    )


def _edge_frame(side: str, width: int, height: int, body, level: float) -> bytes:
    if width <= 0 or height <= 0:
        return b""
    if side in {"left", "right"}:
        row = bytearray()
        for x in range(width):
            distance = x if side == "left" else width - 1 - x
            row += _glow_pixel(distance, body, level)
        return bytes(row) * height
    rows = []
    for y in range(height):
        distance = y if side == "top" else height - 1 - y
        rows.append(_glow_pixel(distance, body, level) * width)
    return b"".join(rows)


def _inside_round_rect(x: int, y: int, width: int, height: int, radius: int) -> bool:
    if width <= 0 or height <= 0:
        return False
    if x < 0 or y < 0 or x >= width or y >= height:
        return False
    radius = min(radius, width // 2, height // 2)
    if radius <= 0:
        return True
    if x < radius:
        center_x = radius
    elif x >= width - radius:
        center_x = width - 1 - radius
    else:
        center_x = x
    if y < radius:
        center_y = radius
    elif y >= height - radius:
        center_y = height - 1 - radius
    else:
        center_y = y
    if center_x == x and center_y == y:
        return True
    delta_x = x - center_x
    delta_y = y - center_y
    return delta_x * delta_x + delta_y * delta_y <= radius * radius


def _blend(
    buffer: bytearray, offset: int, red: int, green: int, blue: int, alpha: int
) -> None:
    if alpha <= 0:
        return
    inverse = 255 - alpha
    buffer[offset] = blue * alpha // 255 + buffer[offset] * inverse // 255
    buffer[offset + 1] = green * alpha // 255 + buffer[offset + 1] * inverse // 255
    buffer[offset + 2] = red * alpha // 255 + buffer[offset + 2] * inverse // 255
    buffer[offset + 3] = min(255, alpha + buffer[offset + 3] * inverse // 255)


def _create_ui_font(font_px: int):
    return _gdi32.CreateFontW(
        -int(font_px),
        0,
        0,
        0,
        _FW_SEMIBOLD,
        0,
        0,
        0,
        _DEFAULT_CHARSET,
        0,
        0,
        _ANTIALIASED_QUALITY,
        0,
        "Microsoft YaHei UI",
    )


def _text_mask(text: str, font_px: int) -> tuple[bytes, int, int]:
    """Rasterize text into a coverage mask with one byte per pixel."""
    if _gdi32 is None or _user32 is None or not text:
        return b"", 0, 0
    screen_dc = _user32.GetDC(None)
    if not screen_dc:
        return b"", 0, 0
    dc = None
    bitmap = None
    font = None
    try:
        dc = _gdi32.CreateCompatibleDC(screen_dc)
        font = _create_ui_font(font_px) if dc else None
        if not dc or not font:
            return b"", 0, 0
        _gdi32.SelectObject(dc, font)
        rect = wintypes.RECT(0, 0, 0, 0)
        _user32.DrawTextW(
            dc,
            text,
            -1,
            ctypes.byref(rect),
            _DT_CALCRECT | _DT_SINGLELINE | _DT_NOPREFIX,
        )
        width = max(1, rect.right - rect.left)
        height = max(1, rect.bottom - rect.top)
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB
        bits = ctypes.c_void_p()
        bitmap = _gdi32.CreateDIBSection(
            dc,
            ctypes.byref(info),
            _DIB_RGB_COLORS,
            ctypes.byref(bits),
            None,
            0,
        )
        if not bitmap:
            return b"", 0, 0
        _gdi32.SelectObject(dc, bitmap)
        _gdi32.SetBkMode(dc, _TRANSPARENT_BK)
        _gdi32.SetTextColor(dc, 0x00FFFFFF)
        target = wintypes.RECT(0, 0, width, height)
        _user32.DrawTextW(
            dc, text, -1, ctypes.byref(target), _DT_SINGLELINE | _DT_NOPREFIX
        )
        raw = ctypes.string_at(bits, width * height * 4)
        mask = bytes(
            max(raw[index], raw[index + 1], raw[index + 2])
            for index in range(0, len(raw), 4)
        )
        return mask, width, height
    finally:
        if dc:
            _gdi32.DeleteDC(dc)
        if bitmap:
            _gdi32.DeleteObject(bitmap)
        if font:
            _gdi32.DeleteObject(font)
        _user32.ReleaseDC(None, screen_dc)


def _badge_frame(
    paused: bool, scale: float = 1.0, min_width: int = 0
) -> tuple[bytes, int, int]:
    """Render the status pill as premultiplied BGRA for one state."""
    text = _status_text(paused)
    accent = _PAUSED_BODY if paused else _RUN_PALETTE[0]
    font_px = max(11, round(_BADGE_FONT_PX * scale))
    pad_x = max(8, round(_BADGE_PADDING_X * scale))
    pad_y = max(5, round(_BADGE_PADDING_Y * scale))
    dot = max(5, round(_BADGE_DOT * scale))
    gap = max(5, round(_BADGE_GAP * scale))
    radius = max(6, round(_BADGE_RADIUS * scale))
    mask, text_w, text_h = _text_mask(text, font_px)
    if not mask:
        text_w = max(font_px, int(len(text) * font_px * 0.7))
        text_h = font_px
    content_w = dot + gap + text_w
    height = max(text_h, dot) + pad_y * 2
    width = max(int(min_width), content_w + pad_x * 2)
    if width <= 0 or height <= 0:
        return b"", 0, 0
    left = max(0, (width - content_w) // 2)
    dot_cx = left + dot // 2
    dot_cy = height // 2
    text_x = left + dot + gap
    text_y = (height - text_h) // 2
    dot_radius2 = (dot * dot) // 4 + 1
    inner_radius = max(0, radius - 1)
    buffer = bytearray(width * height * 4)
    for y in range(height):
        row = y * width * 4
        for x in range(width):
            if not _inside_round_rect(x, y, width, height, radius):
                continue
            offset = row + x * 4
            _blend(buffer, offset, *_BADGE_BG, _BADGE_BG_ALPHA)
            if not _inside_round_rect(
                x - 1, y - 1, width - 2, height - 2, inner_radius
            ):
                _blend(buffer, offset, *accent, _BADGE_BORDER_ALPHA)
            delta_x = x - dot_cx
            delta_y = y - dot_cy
            if delta_x * delta_x + delta_y * delta_y <= dot_radius2:
                _blend(buffer, offset, *accent, 255)
            if mask:
                text_offset_x = x - text_x
                text_offset_y = y - text_y
                if 0 <= text_offset_x < text_w and 0 <= text_offset_y < text_h:
                    coverage = mask[text_offset_y * text_w + text_offset_x]
                    if coverage:
                        _blend(buffer, offset, *_BADGE_TEXT_COLOR, coverage)
    return bytes(buffer), width, height


class _Surface:
    def __init__(
        self, hwnd, hdc, bitmap, bits, x: int, y: int, width: int, height: int
    ):
        self.hwnd = hwnd
        self.hdc = hdc
        self.bitmap = bitmap
        self.bits = bits
        self.x = x
        self.y = y
        self.width = width
        self.height = height

    def draw(self, frame: bytes, screen_dc) -> None:
        if len(frame) != self.width * self.height * 4:
            raise ValueError("frame size does not match surface size")
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
        _gdi32.DeleteDC(self.hdc)
        _gdi32.DeleteObject(self.bitmap)


class ScreenIndicator:
    """Best-effort desktop overlay; all failures degrade to no indicator."""

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
        self._edges: list[_Surface] = []
        self._sides: list[str] = []
        self._geometry: tuple[int, int, int, int] | None = None
        self._screen_dc = None
        self._frame = 0
        self._paused = False
        self._badge: _Surface | None = None
        self._badge_scale = 1.0
        self._badge_frames: dict[bool, tuple[bytes, int, int]] = {}

    @property
    def available(self) -> bool:
        return self._available

    @property
    def paused(self) -> bool:
        return self._paused

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
        self._paused = False
        self._submit("show")

    def hide(self) -> None:
        self._submit("hide")

    def set_paused(self, paused: bool) -> None:
        paused = bool(paused)
        if paused == self._paused:
            return
        self._paused = paused
        self._submit("state")

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
            elif command == "state":
                self._apply_state()
            elif command == "close":
                self._hide()
                _user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
                return

    def _show(self) -> None:
        try:
            self._ensure_edges()
            self._ensure_badge()
            self._raise_surfaces()
            self._visible = True
            self._frame = 0
            self._apply_state()
        except Exception:
            logger.warning("screen indicator could not be shown", exc_info=True)
            self._hide()

    def _hide(self) -> None:
        self._paused = False
        if not self._visible:
            return
        _user32.KillTimer(None, _TIMER_ID)
        for surface in self._edges:
            _user32.ShowWindow(surface.hwnd, _SW_HIDE)
        if self._badge is not None:
            _user32.ShowWindow(self._badge.hwnd, _SW_HIDE)
        self._visible = False

    def _apply_state(self) -> None:
        if not self._visible:
            return
        if self._paused:
            _user32.KillTimer(None, _TIMER_ID)
            self._draw_frame()
        else:
            self._draw_frame()
            _user32.SetTimer(None, _TIMER_ID, _INTERVAL_MS, None)

    def _tick(self) -> None:
        if not self._visible or not self._edges or self._paused:
            return
        self._draw_frame()
        self._frame += 1

    def _draw_frame(self) -> None:
        if not self._edges:
            return
        try:
            if self._paused:
                body, level = _PAUSED_BODY, _PAUSED_LEVEL
            else:
                body = _palette_color(self._frame)
                level = _breath_level(self._frame)
            for index, edge in enumerate(self._edges):
                frame = _edge_frame(
                    self._sides[index], edge.width, edge.height, body, level
                )
                edge.draw(frame, self._screen_dc)
            self._draw_badge()
        except Exception:
            logger.warning("screen indicator frame failed", exc_info=True)
            self._hide()

    def _draw_badge(self) -> None:
        if self._badge is None:
            return
        frame = self._badge_frames.get(self._paused)
        if frame is None:
            frame = _badge_frame(self._paused, self._badge_scale)
            self._badge_frames[self._paused] = frame
        if frame[1] != self._badge.width or frame[2] != self._badge.height:
            return
        self._badge.draw(frame[0], self._screen_dc)

    def _raise_surfaces(self) -> None:
        for surface in self._edges:
            _user32.ShowWindow(surface.hwnd, _SW_SHOWNOACTIVATE)
            _user32.SetWindowPos(
                surface.hwnd,
                wintypes.HWND(-1),
                surface.x,
                surface.y,
                surface.width,
                surface.height,
                _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
            )
        if self._badge is not None:
            _user32.ShowWindow(self._badge.hwnd, _SW_SHOWNOACTIVATE)
            _user32.SetWindowPos(
                self._badge.hwnd,
                wintypes.HWND(-1),
                self._badge.x,
                self._badge.y,
                self._badge.width,
                self._badge.height,
                _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
            )

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
            self._edges.append(self._create_surface(x, y, w, h))
            self._sides.append(side)
        self._geometry = geometry

    def _ensure_badge(self) -> None:
        if self._badge is not None:
            return
        if not self._screen_dc:
            raise RuntimeError("screen dc missing")
        screen_width = _user32.GetSystemMetrics(_SM_CXSCREEN)
        screen_height = _user32.GetSystemMetrics(_SM_CYSCREEN)
        self._badge_scale = min(1.5, max(0.9, screen_height / 1080))
        running = _badge_frame(False, self._badge_scale)
        paused = _badge_frame(True, self._badge_scale)
        width = max(running[1], paused[1])
        height = max(running[2], paused[2])
        self._badge_frames = {
            False: _badge_frame(False, self._badge_scale, width),
            True: _badge_frame(True, self._badge_scale, width),
        }
        x = max(0, (screen_width - width) // 2)
        y = max(0, round(_BADGE_TOP * self._badge_scale))
        self._badge = self._create_surface(x, y, width, height)

    def _create_surface(self, x: int, y: int, width: int, height: int) -> _Surface:
        hdc = _gdi32.CreateCompatibleDC(self._screen_dc)
        if not hdc:
            raise RuntimeError("CreateCompatibleDC failed")
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
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
            width,
            height,
            None,
            None,
            self._instance,
            None,
        )
        if not hwnd:
            _gdi32.DeleteDC(hdc)
            _gdi32.DeleteObject(bitmap)
            raise RuntimeError("CreateWindowExW failed")
        _user32.SetWindowDisplayAffinity(hwnd, _WDA_EXCLUDEFROMCAPTURE)
        return _Surface(hwnd, hdc, bitmap, bits, x, y, width, height)

    def _destroy_edges(self) -> None:
        for edge in self._edges:
            try:
                edge.release()
            except Exception:
                pass
        self._edges.clear()
        self._sides.clear()
        self._geometry = None

    def _destroy_badge(self) -> None:
        if self._badge is None:
            return
        try:
            self._badge.release()
        except Exception:
            pass
        self._badge = None
        self._badge_frames = {}

    def _destroy(self) -> None:
        try:
            self._hide()
        finally:
            self._destroy_badge()
            self._destroy_edges()
            if self._screen_dc:
                _user32.ReleaseDC(None, self._screen_dc)
                self._screen_dc = None
            _user32.UnregisterClassW(self._class_name, self._instance)
            self._available = False


__all__ = ["ScreenIndicator"]
