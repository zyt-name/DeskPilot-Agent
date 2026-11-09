from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .storage import Storage

DEFAULT_CONTEXT_WINDOW = 32768
CONTEXT_SHARE = 0.35
MAX_RECENT_MESSAGES = 8
SUMMARY_MAX_CHARS = 1600
MAX_MESSAGE_CHARS = 1200
SUMMARY_SYSTEM_PROMPT = (
    "你在压缩一个桌面自动化任务的对话历史。保留用户目标、已完成操作、关键结果、"
    "未完成事项与限制条件。用简洁的中文分条输出，不超过 300 字，不要编造。"
)


@dataclass
class ContextSnapshot:
    summary: str
    recent: list[dict[str, Any]]
    used_tokens: int
    budget_tokens: int
    context_window: int
    total_messages: int
    covered_message_id: int
    compacted: bool = False
    older: list[dict[str, Any]] = field(default_factory=list)

    def prompt(self) -> str:
        blocks: list[str] = []
        if self.summary.strip():
            blocks.append("任务记忆（历史对话压缩摘要）：\n" + self.summary.strip())
        if self.older:
            lines = [_format_message(item) for item in self.older]
            blocks.append("较早对话（未压缩，按时间顺序）：\n" + "\n".join(lines))
        if self.recent:
            lines = [_format_message(item) for item in self.recent]
            blocks.append("最近对话（按时间顺序）：\n" + "\n".join(lines))
        return "\n\n".join(blocks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "older": list(self.older),
            "recent": list(self.recent),
            "used_tokens": self.used_tokens,
            "budget_tokens": self.budget_tokens,
            "context_window": self.context_window,
            "total_messages": self.total_messages,
            "covered_message_id": self.covered_message_id,
            "compacted": self.compacted,
        }


def budget_for(context_window: int) -> int:
    window = max(4096, int(context_window or DEFAULT_CONTEXT_WINDOW))
    return max(1024, int(window * CONTEXT_SHARE))


def estimate_tokens(text: str) -> int:
    value = str(text or "")
    if not value:
        return 0
    ascii_chars = sum(1 for char in value if char.isascii())
    other_chars = len(value) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + other_chars)


def _content(message: dict[str, Any]) -> str:
    text = str(message.get("content") or "").strip()
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + "…"
    return text


def _format_message(message: dict[str, Any]) -> str:
    role = "用户" if message.get("role") == "user" else "AI"
    payload = message.get("payload")
    status = ""
    if isinstance(payload, dict):
        status = str(payload.get("status") or "")
    status = status or str(message.get("status") or "")
    prefix = f"[{status}] " if status else ""
    return f"{role}：{prefix}{_content(message)}"


def _recent_entries(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for message in messages:
        entry: dict[str, Any] = {
            "id": int(message["id"]),
            "role": message.get("role", ""),
            "content": _content(message),
            "kind": message.get("kind", "text"),
        }
        payload = message.get("payload")
        if isinstance(payload, dict) and payload.get("status"):
            entry["status"] = str(payload["status"])
        entries.append(entry)
    return entries


def _exclude_current_goal(
    messages: list[dict[str, Any]], goal: str
) -> list[dict[str, Any]]:
    target = str(goal or "").strip()
    if not target or not messages:
        return messages
    last = messages[-1]
    if (
        last.get("role") == "user"
        and last.get("kind") == "goal"
        and str(last.get("content") or "").strip() == target
    ):
        return messages[:-1]
    return messages


def _build_snapshot(
    *,
    summary: str,
    older: list[dict[str, Any]],
    recent: list[dict[str, Any]],
    goal: str,
    covered_message_id: int,
    total_messages: int,
    context_window: int,
    compacted: bool,
) -> ContextSnapshot:
    used = estimate_tokens(goal) + estimate_tokens(summary)
    used += sum(estimate_tokens(_format_message(item)) for item in older)
    used += sum(estimate_tokens(_format_message(item)) for item in recent)
    return ContextSnapshot(
        summary=summary,
        recent=_recent_entries(recent),
        used_tokens=used,
        budget_tokens=budget_for(context_window),
        context_window=max(4096, int(context_window or DEFAULT_CONTEXT_WINDOW)),
        total_messages=total_messages,
        covered_message_id=covered_message_id,
        compacted=compacted,
        older=_recent_entries(older),
    )


def _split_window(
    storage: Storage, conversation_id: str, goal: str
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    messages = _exclude_current_goal(
        storage.list_messages(conversation_id), goal
    )
    memory = storage.get_task_memory(conversation_id)
    covered = int(memory.get("covered_message_id") or 0)
    summary = str(memory.get("summary") or "")
    return {
        "summary": summary,
        "covered": covered,
        "total": len(messages),
    }, messages, covered


def _summary_limit(budget: int) -> int:
    return max(240, min(SUMMARY_MAX_CHARS, max(1, budget) // 3))


def _trim_text(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    head = max(1, limit // 3)
    tail = max(1, limit - head - 3)
    return value[:head] + "\n…\n" + value[-tail:]


def _recent_and_older(
    messages: list[dict[str, Any]],
    *,
    covered: int,
    goal: str,
    budget: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reserve = _summary_limit(budget)
    available = max(200, budget - estimate_tokens(goal) - reserve - 80)
    recent: list[dict[str, Any]] = []
    spent = 0
    for message in reversed(messages):
        cost = estimate_tokens(_format_message(message))
        if recent and (spent + cost > available or len(recent) >= MAX_RECENT_MESSAGES):
            break
        recent.append(message)
        spent += cost
        if len(recent) >= MAX_RECENT_MESSAGES:
            break
    recent.reverse()
    recent_start = len(messages) - len(recent)
    older = [
        message
        for message in messages[:recent_start]
        if int(message["id"]) > covered
    ]
    return older, recent


def snapshot_context(
    storage: Storage,
    conversation_id: str,
    *,
    goal: str = "",
    context_window: int = DEFAULT_CONTEXT_WINDOW,
) -> ContextSnapshot:
    memory, messages, covered = _split_window(storage, conversation_id, goal)
    budget = budget_for(context_window)
    older, recent = _recent_and_older(
        messages, covered=covered, goal=goal, budget=budget
    )
    return _build_snapshot(
        summary=_trim_text(memory["summary"], _summary_limit(budget)),
        older=older,
        recent=recent,
        goal=goal,
        covered_message_id=covered,
        total_messages=memory["total"],
        context_window=context_window,
        compacted=False,
    )


def compact_context(
    storage: Storage,
    conversation_id: str,
    *,
    goal: str = "",
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    provider: Any | None = None,
) -> ContextSnapshot:
    memory, messages, covered = _split_window(storage, conversation_id, goal)
    budget = budget_for(context_window)
    summary_limit = _summary_limit(budget)
    summary = _trim_text(memory["summary"], summary_limit)
    older, recent = _recent_and_older(
        messages, covered=covered, goal=goal, budget=budget
    )
    used_before = estimate_tokens(goal) + estimate_tokens(summary)
    used_before += sum(estimate_tokens(_format_message(item)) for item in older)
    used_before += sum(estimate_tokens(_format_message(item)) for item in recent)
    compacted = False
    if older and used_before > budget:
        summary = _summarize(provider, summary, older, summary_limit)
        covered = int(older[-1]["id"])
        storage.save_task_memory(conversation_id, summary, covered)
        older = []
        compacted = True
    return _build_snapshot(
        summary=summary,
        older=older,
        recent=recent,
        goal=goal,
        covered_message_id=covered,
        total_messages=memory["total"],
        context_window=context_window,
        compacted=compacted,
    )


def _summarize(
    provider: Any | None,
    previous: str,
    messages: list[dict[str, Any]],
    max_chars: int = SUMMARY_MAX_CHARS,
) -> str:
    lines = [_format_message(item) for item in messages]
    body = ""
    if previous.strip():
        body += f"已有摘要：\n{previous.strip()}\n\n"
    body += "新增对话：\n" + "\n".join(lines)
    if len(body) > 16000:
        body = body[-16000:]
    if provider is not None:
        try:
            text = str(
                provider.complete(
                    SUMMARY_SYSTEM_PROMPT, body, max_tokens=500
                )
                or ""
            ).strip()
            if text.startswith("```"):
                text = text.strip("`").strip()
            if (
                text
                and not text.startswith(("{", "["))
                and '"verdict"' not in text
            ):
                return text[:max_chars]
        except Exception:
            pass
    return _fallback_summary(previous, lines, max_chars)


def _fallback_summary(
    previous: str, lines: list[str], max_chars: int = SUMMARY_MAX_CHARS
) -> str:
    merged = "\n".join([previous.strip(), *lines]).strip()
    if len(merged) <= max_chars:
        return merged
    head = max(1, max_chars // 3)
    tail = max(1, max_chars - head - 3)
    return f"{merged[:head]}\n…\n{merged[-tail:]}"
