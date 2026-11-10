"""浏览器通道（浏览器功能模块）。

与桌面通道并列的独立执行通道：Agent 在自己的浏览器进程里完成任务，
不截取、不点击、不占用用户桌面。

驱动为 **纯 Python 标准库直连 CDP**，不依赖 Playwright / Selenium。
设计见 TECH_DESIGN 4.21，实测数据见 4.21.14。
"""

from __future__ import annotations

from .cdp import CdpConnection, CdpError
from .engine import (
    ENGINE_LABELS,
    ENGINE_ORDER,
    BrowserError,
    BrowserProcess,
    EngineInfo,
    EngineNotFound,
    ProfileLock,
    bundled_root_for,
    engine_version,
    find_engine_path,
    resolve_engine,
)
from .mirror import ScreencastStream
from .session import BrowserSession, BrowserSettings, Page, SessionStats

__all__ = [
    "ENGINE_LABELS",
    "ENGINE_ORDER",
    "BrowserError",
    "BrowserProcess",
    "BrowserSession",
    "BrowserSettings",
    "CdpConnection",
    "CdpError",
    "EngineInfo",
    "EngineNotFound",
    "Page",
    "ProfileLock",
    "ScreencastStream",
    "SessionStats",
    "bundled_root_for",
    "engine_version",
    "find_engine_path",
    "resolve_engine",
]
