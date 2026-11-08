"""Process-wide pause hotkey that works while another window has focus."""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from typing import Callable

from ctypes import wintypes

logger = logging.getLogger(__name__)

_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_MOD_NOREPEAT = 0x4000
_VK_F8 = 0x77


class GlobalHotkey:
    """RegisterHotKey on a dedicated message loop; no-op off Windows."""

    def __init__(
        self,
        callback: Callable[[], None],
        vk: int = _VK_F8,
        label: str = "F8",
        enabled: bool | None = None,
    ) -> None:
        self.callback = callback
        self.vk = vk
        self.label = label
        self.enabled = (os.name == "nt") if enabled is None else bool(enabled)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._active = False
        self._thread_id = 0

    @property
    def active(self) -> bool:
        return self._active

    def start(self, timeout: float = 2.0) -> bool:
        if not self.enabled or self._thread is not None:
            return self._active
        self._thread = threading.Thread(
            target=self._run, name="xm-global-hotkey", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout)
        return self._active

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        self._thread.join(timeout=2.0)
        self._thread = None
        self._active = False

    def _run(self) -> None:
        self._thread_id = _kernel32.GetCurrentThreadId()
        if not _user32.RegisterHotKey(None, 1, _MOD_NOREPEAT, self.vk):
            logger.warning(
                "global %s hotkey unavailable; falling back to window focus",
                self.label,
            )
            self._ready.set()
            return
        self._active = True
        self._ready.set()
        message = wintypes.MSG()
        try:
            while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == _WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:
                        logger.exception("global hotkey callback failed")
        finally:
            _user32.UnregisterHotKey(None, 1)


if os.name == "nt":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.RegisterHotKey.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.RegisterHotKey.restype = wintypes.BOOL
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.UnregisterHotKey.restype = wintypes.BOOL
    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.GetMessageW.restype = wintypes.BOOL
    _user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    _user32.PostThreadMessageW.restype = wintypes.BOOL
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
else:
    _user32 = None
    _kernel32 = None


__all__ = ["GlobalHotkey"]
