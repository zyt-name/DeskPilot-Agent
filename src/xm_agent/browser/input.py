"""页面输入：鼠标、滚轮、中文文本、按键。

约定：所有坐标都是**视口坐标**（与截图同源），不涉及屏幕坐标与 DPI。
中文走 `Input.insertText` 整段写入，绕开输入法；`insertText` 依赖焦点，
无焦点时会静默丢弃，所以写入前必须显式聚焦。
"""

from __future__ import annotations

import time
from typing import Any

from .cdp import CdpConnection, CdpError
from .snapshot import evaluate

# key -> (windowsVirtualKeyCode, text)
_KEYS: dict[str, tuple[int, str]] = {
    "Enter": (13, "\r"),
    "Tab": (9, ""),
    "Escape": (27, ""),
    "Backspace": (8, ""),
    "Delete": (46, ""),
    "ArrowUp": (38, ""),
    "ArrowDown": (40, ""),
    "ArrowLeft": (37, ""),
    "ArrowRight": (39, ""),
    "Home": (36, ""),
    "End": (35, ""),
    "PageUp": (33, ""),
    "PageDown": (34, ""),
    " ": (32, " "),
}

_MODIFIERS = {"alt": 1, "control": 2, "ctrl": 2, "meta": 4, "shift": 8}


def _dispatch_mouse(
    conn: CdpConnection,
    session_id: str,
    event_type: str,
    x: float,
    y: float,
    button: str = "left",
    buttons: int = 0,
    click_count: int = 0,
    delta_y: float = 0,
    delta_x: float = 0,
) -> None:
    params: dict[str, Any] = {
        "type": event_type,
        "x": float(x),
        "y": float(y),
    }
    if event_type != "mouseWheel":
        params.update({"button": button, "buttons": buttons, "clickCount": click_count})
    else:
        params.update({"deltaX": delta_x, "deltaY": delta_y})
    conn.call("Input.dispatchMouseEvent", params, session_id)


def move(conn: CdpConnection, session_id: str, x: float, y: float) -> None:
    _dispatch_mouse(conn, session_id, "mouseMoved", x, y, button="none")


def click(
    conn: CdpConnection,
    session_id: str,
    x: float,
    y: float,
    button: str = "left",
    clicks: int = 1,
) -> None:
    """在视口坐标点一下。先移动再按下，模拟真实指针轨迹。"""
    _dispatch_mouse(conn, session_id, "mouseMoved", x, y, button="none")
    for index in range(1, max(1, clicks) + 1):
        _dispatch_mouse(
            conn, session_id, "mousePressed", x, y, button=button, buttons=1,
            click_count=index,
        )
        _dispatch_mouse(
            conn, session_id, "mouseReleased", x, y, button=button, buttons=0,
            click_count=index,
        )


def scroll(
    conn: CdpConnection, session_id: str, x: float, y: float, delta_y: float = 400
) -> None:
    _dispatch_mouse(conn, session_id, "mouseWheel", x, y, delta_y=delta_y)


def focus(conn: CdpConnection, session_id: str, selector: str) -> bool:
    """把焦点放到元素上；返回是否找到。"""
    found = evaluate(
        conn,
        session_id,
        """(() => {
          const el = document.querySelector(SEL);
          if (!el) return false;
          el.focus();
          if (typeof el.select === 'function') { try { el.select(); } catch (e) {} }
          return document.activeElement === el;
        })()""".replace("SEL", _js_str(selector)),
    )
    return bool(found)


def type_text(conn: CdpConnection, session_id: str, text: str) -> None:
    """整段写入文本。调用方需保证焦点在目标输入框。"""
    if not text:
        return
    conn.call("Input.insertText", {"text": text}, session_id)
    time.sleep(0.05)


def type_into(conn: CdpConnection, session_id: str, selector: str, text: str) -> bool:
    """聚焦后写入，常用于一次填一个字段。"""
    if not focus(conn, session_id, selector):
        return False
    type_text(conn, session_id, text)
    return True


def press_key(
    conn: CdpConnection, session_id: str, key: str, modifiers: str = ""
) -> None:
    """按一个键，支持 `Control+a` 这类组合（用 modifiers 参数传 control）。"""
    vk, text = _KEYS.get(key, (0, ""))
    mods = 0
    for part in modifiers.replace("+", " ").split():
        mods |= _MODIFIERS.get(part.lower(), 0)
    base = {
        "key": key,
        "code": key,
        "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk,
        "modifiers": mods,
    }
    conn.call("Input.dispatchKeyEvent", {**base, "type": "rawKeyDown", "text": ""},
              session_id)
    if text:
        conn.call("Input.dispatchKeyEvent", {**base, "type": "char", "text": text},
                  session_id)
    conn.call("Input.dispatchKeyEvent", {**base, "type": "keyUp"}, session_id)


def open_url(conn: CdpConnection, session_id: str, url: str, wait_timeout: float = 20.0) -> None:
    """导航并等待加载。"""
    conn.call("Page.navigate", {"url": url}, session_id, timeout=wait_timeout + 10)
    wait_loaded(conn, session_id, wait_timeout)
    if url.startswith(("http://", "https://")):
        # readyState=complete 不等于「有内容」：自如这类 SPA 首屏在 complete
        # 之后才渲染，紧接着观测会看到空标题、0 个元素，模型就以为被拦了。
        wait_content(conn, session_id, min(6.0, wait_timeout))


def wait_content(conn: CdpConnection, session_id: str, timeout: float = 4.0) -> bool:
    """等到页面真的有标题或正文；超时也算成功（有些页确实没有正文）。"""
    probe = (
        "(() => {const b = document.body;"
        " return !!b && (((document.title || '').trim().length > 0)"
        " || ((b.innerText || '').trim().length > 0));})()"
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if evaluate(conn, session_id, probe):
                time.sleep(0.25)  # 让首屏的异步渲染再跑一小会儿
                return True
        except CdpError:
            pass
        time.sleep(0.1)
    return False


def wait_loaded(conn: CdpConnection, session_id: str, timeout: float = 20.0) -> bool:
    """等 readyState 变成 complete；超时返回 False 而不是抛异常。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = evaluate(conn, session_id, "document.readyState")
        except CdpError:
            # 导航进行中求值会报「Inspected target navigated or closed」，
            # 这不是失败，只是这一刻没有可求值的文档——继续等就行。
            time.sleep(0.1)
            continue
        if state == "complete":
            time.sleep(0.15)  # 给前端框架一点渲染时间
            return True
        time.sleep(0.1)
    return False


def wait_text(
    conn: CdpConnection, session_id: str, needle: str, timeout: float = 15.0
) -> bool:
    """等待页面文本出现某段内容，用于登录成功、结果出现这类判定。"""
    deadline = time.time() + timeout
    literal = _js_str(needle)
    while time.time() < deadline:
        hit = evaluate(
            conn,
            session_id,
            f"(() => ((document.body && document.body.innerText) || '').includes({literal}))()",
        )
        if hit:
            return True
        time.sleep(0.2)
    return False


def _js_str(value: str) -> str:
    """安全地嵌进 JS 的单引号字符串。"""
    escaped = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("</", "<\\/")
    )
    return f"'{escaped}'"
