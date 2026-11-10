"""窗口前置：只验证「拿不到窗口时安静失败」，不依赖桌面上真有浏览器。"""

from __future__ import annotations

from xm_agent.browser import window as window_mod


def test_raise_window_ignores_missing_pid():
    assert window_mod.raise_window(0) is False
    assert window_mod.raise_window(-1) is False


def test_raise_window_returns_false_without_that_process():
    # 一个几乎不可能存在的 pid：找不到窗口就返回 False，不能抛
    assert window_mod.raise_window(999_999) is False
