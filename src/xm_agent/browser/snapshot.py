"""页面观测：截图、可交互元素清单、无障碍树摘要、可见文本。

给模型看的东西要**紧凑且带坐标**，所以默认主路径是一次 JS 遍历拿到
「元素 + 矩形 + 中心点」，无障碍树作为语义补充。
"""

from __future__ import annotations

import base64
from typing import Any

from .cdp import CdpConnection

_INTERACTIVE_JS = """
(() => {
  const sel = 'a[href],button,input,textarea,select,' +
    '[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],' +
    '[role=menuitem],[role=option],[onclick],[contenteditable=""],[contenteditable=true]';
  const out = [];
  const vw = window.innerWidth, vh = window.innerHeight;
  const nodes = document.querySelectorAll(sel);
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = window.getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    const inView = r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
    let label = (el.innerText || el.getAttribute('aria-label') ||
      el.getAttribute('placeholder') || el.title || '').replace(/\\s+/g, ' ').trim();
    if (!label && el.value) label = String(el.value);
    const item = {
      i: out.length,
      tag: el.tagName.toLowerCase(),
      type: el.type || '',
      role: el.getAttribute('role') || '',
      text: label.slice(0, 80),
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
      w: Math.round(r.width),
      h: Math.round(r.height),
      inView: !!inView,
      disabled: !!el.disabled,
    };
    if (el.tagName === 'A' && el.href) item.href = String(el.href).slice(0, 120);
    if (el.tagName === 'INPUT') {
      item.value = el.type === 'password' ? '•••' : String(el.value || '').slice(0, 60);
    }
    out.push(item);
    if (out.length >= LIMIT) break;
  }
  return out;
})()
"""


def evaluate(
    conn: CdpConnection,
    session_id: str,
    expression: str,
    timeout: float = 20.0,
    await_promise: bool = False,
) -> Any:
    """在页面里求值并把结果按值返回。"""
    result = conn.call(
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        },
        session_id,
        timeout=timeout,
    )
    if "exceptionDetails" in result:
        detail = result["exceptionDetails"]
        message = detail.get("text", "求值异常")
        exception = detail.get("exception") or {}
        raise RuntimeError(f"页面求值失败: {exception.get('description', message)}")
    return (result.get("result") or {}).get("value")


def viewport(conn: CdpConnection, session_id: str) -> dict[str, int]:
    metrics = conn.call("Page.getLayoutMetrics", {}, session_id)
    box = metrics.get("cssVisualViewport") or metrics.get("visualViewport") or {}
    return {
        "width": int(box.get("clientWidth") or 0),
        "height": int(box.get("clientHeight") or 0),
    }


def capture_screenshot(
    conn: CdpConnection,
    session_id: str,
    quality: int = 70,
    max_width: int = 1280,
    format: str = "jpeg",
    full_page: bool = False,
) -> bytes:
    """页面截图（不含桌面）。超过 max_width 时按比例缩小以省 Token。"""
    params: dict[str, Any] = {"format": format, "captureBeyondViewport": full_page}
    if format == "jpeg":
        params["quality"] = quality

    size = viewport(conn, session_id)
    width = size["width"] or 1280
    height = size["height"] or 800
    if full_page:
        metrics = conn.call("Page.getLayoutMetrics", {}, session_id)
        content = metrics.get("cssContentSize") or {}
        height = int(content.get("height") or height)
    if width > max_width > 0:
        params["clip"] = {
            "x": 0,
            "y": 0,
            "width": width,
            "height": height,
            "scale": round(max_width / width, 4),
        }
    data = conn.call("Page.captureScreenshot", params, session_id, timeout=60.0)
    return base64.b64decode(data.get("data", ""))


def interactive_elements(
    conn: CdpConnection, session_id: str, limit: int = 80
) -> list[dict[str, Any]]:
    """当前视口相关的可交互元素（带中心点坐标）。"""
    result = evaluate(conn, session_id, _INTERACTIVE_JS.replace("LIMIT", str(int(limit))))
    return result or []


def page_meta(conn: CdpConnection, session_id: str) -> dict[str, Any]:
    """标题、URL、滚动位置、是否有弹窗等一眼可见的状态。"""
    return evaluate(
        conn,
        session_id,
        """(() => ({
          url: location.href,
          title: document.title,
          readyState: document.readyState,
          scrollY: Math.round(window.scrollY),
          scrollHeight: Math.round(document.documentElement.scrollHeight),
          viewportW: window.innerWidth,
          viewportH: window.innerHeight,
        }))()""",
    ) or {}


def visible_text(
    conn: CdpConnection, session_id: str, limit: int = 1500
) -> str:
    """可读文本摘要，供模型理解页面在说什么。"""
    text = evaluate(
        conn,
        session_id,
        "(() => (document.body && document.body.innerText) || '')()",
    ) or ""
    text = " ".join(str(text).split())
    return text[:limit]


def ax_summary(
    conn: CdpConnection, session_id: str, limit: int = 120
) -> list[dict[str, Any]]:
    """无障碍树精简版：只保留有名字或有角色的节点，用于语义校验。"""
    tree = conn.call("Accessibility.getFullAXTree", {}, session_id, timeout=30.0)
    nodes: list[dict[str, Any]] = []
    for node in tree.get("nodes", []):
        if node.get("ignored"):
            continue
        role = (node.get("role") or {}).get("value") or ""
        name = (node.get("name") or {}).get("value") or ""
        if not role and not name:
            continue
        if role in ("generic", "none", "InlineTextBox") and not name:
            continue
        item = {"role": role, "name": str(name)[:80]}
        value = (node.get("value") or {}).get("value")
        if value not in (None, ""):
            item["value"] = str(value)[:60]
        nodes.append(item)
        if len(nodes) >= limit:
            break
    return nodes


def page_snapshot(
    conn: CdpConnection,
    session_id: str,
    include_screenshot: bool = False,
    max_width: int = 1280,
    element_limit: int = 80,
) -> dict[str, Any]:
    """一次拿齐模型决策需要的信息。截图按需附加。"""
    snapshot: dict[str, Any] = {
        "meta": page_meta(conn, session_id),
        "elements": interactive_elements(conn, session_id, element_limit),
        "text": visible_text(conn, session_id),
    }
    if include_screenshot:
        data = capture_screenshot(conn, session_id, max_width=max_width)
        snapshot["screenshot_bytes"] = len(data)
        snapshot["_screenshot"] = data  # 由调用方决定落盘还是直接送模型
    return snapshot
