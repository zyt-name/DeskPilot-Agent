from __future__ import annotations

import re
from typing import Any

_REASON_HINTS = (
    ("max steps reached", "已达到最大执行步数，目标状态仍未确认"),
    ("consecutive failures", "连续多次操作未通过验证"),
    ("observation failed", "无法读取当前桌面状态"),
    ("provider error", "模型调用失败"),
    ("runtime error", "执行过程中出现异常"),
    ("completion not verified", "完成核对未通过，屏幕未显示目标状态"),
    ("audit failed closed", "顶层审计不可用，出于安全已停止操作"),
    ("audit denied", "顶层审计阻止了操作"),
    ("confirmation denied", "操作未获得确认"),
    ("cancelled by user", "用户结束了控制"),
    ("preempted by queued task", "被新任务插队而中止"),
)

_CJK = re.compile(r"[\u4e00-\u9fff]")

FINISH_LABELS = {
    "done": "任务完成",
    "failed": "任务失败",
    "aborted": "任务已中止",
    "needs_human": "任务需要处理",
}


def one_line(text: Any, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def clip(text: Any, limit: int = 2000) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def final_summary(status: str, reason: str, steps: list[Any]) -> str:
    reason_text = str(reason or "").strip()
    reason_line = one_line(reason_text, 320)
    count = len(steps or [])
    if status == "done":
        return clip(reason_text) or "任务已完成。"
    if status == "needs_human":
        if "desktop lock is held" in reason_line:
            return "任务未启动：桌面正被另一个进程控制，请稍后重试。"
        if reason_line:
            return f"任务需要人工处理：{reason_line}"
        return "任务需要人工处理。"
    if _CJK.search(reason_text):
        return clip(reason_text)
    label = "任务失败" if status == "failed" else "任务已中止"
    hint = next(
        (text for key, text in _REASON_HINTS if key in reason_line), ""
    )
    if hint and reason_line:
        summary = f"{label}：{hint}。原因：{reason_line}"
    elif reason_line:
        summary = f"{label}：{reason_line}"
    else:
        summary = f"{label}：未提供具体原因。"
    if count:
        summary += f"（已执行 {count} 步）"
    return summary
