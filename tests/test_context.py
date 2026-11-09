from __future__ import annotations

import pytest

from xm_agent.context import (
    compact_context,
    estimate_tokens,
    snapshot_context,
)
from xm_agent.storage import Storage


class _Summarizer:
    def __init__(self, text: str = "摘要：已完成第一次操作，待办整理文件") -> None:
        self.text = text
        self.calls: list[str] = []

    def complete(self, system, user, *, max_tokens=800, on_delta=None):
        self.calls.append(user)
        return self.text


class _BrokenSummarizer:
    def complete(self, system, user, *, max_tokens=800, on_delta=None):
        raise RuntimeError("model offline")


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path / "app.db")


def _seed(storage: Storage, conversation_id: str, count: int) -> None:
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        storage.add_message(
            conversation_id,
            role,
            f"第{index}条消息：" + "这是一段用于测试上下文裁剪的历史内容。" * 6,
            "goal" if role == "user" else "result",
        )


def test_estimate_tokens_prefers_chinese_and_ascii():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("中文内容") == 4
    assert estimate_tokens("a中") >= 1


def test_snapshot_excludes_current_goal(storage):
    conversation = storage.create_conversation("", "mock")
    storage.add_message(conversation["id"], "user", "第一轮目标", "goal")
    storage.add_message(conversation["id"], "assistant", "第一轮结果", "result")
    storage.add_message(conversation["id"], "user", "本轮目标", "goal")

    snapshot = snapshot_context(
        storage, conversation["id"], goal="本轮目标", context_window=32768
    )
    contents = [item["content"] for item in snapshot.recent]
    assert "本轮目标" not in contents
    assert contents == ["第一轮目标", "第一轮结果"]
    assert snapshot.total_messages == 2


def test_compaction_rolls_older_messages_into_summary(storage):
    conversation = storage.create_conversation("", "mock")
    _seed(storage, conversation["id"], 30)
    summarizer = _Summarizer()

    snapshot = compact_context(
        storage,
        conversation["id"],
        goal="继续整理",
        context_window=4096,
        provider=summarizer,
    )

    assert summarizer.calls
    assert snapshot.compacted is True
    assert snapshot.summary.startswith("摘要：")
    assert 0 < len(snapshot.recent) <= 8
    assert snapshot.used_tokens <= snapshot.budget_tokens
    memory = storage.get_task_memory(conversation["id"])
    assert memory["summary"] == snapshot.summary
    assert memory["covered_message_id"] == snapshot.recent[0]["id"] - 1
    assert snapshot.total_messages == 30
    assert snapshot.used_tokens < snapshot.budget_tokens
    prompt = snapshot.prompt()
    assert "任务记忆" in prompt
    assert "最近对话" in prompt


def test_compaction_falls_back_when_model_is_unavailable(storage):
    conversation = storage.create_conversation("", "mock")
    _seed(storage, conversation["id"], 30)

    snapshot = compact_context(
        storage,
        conversation["id"],
        goal="",
        context_window=4096,
        provider=_BrokenSummarizer(),
    )

    assert snapshot.compacted is True
    assert snapshot.summary
    assert "第0条消息" in snapshot.summary
    assert storage.get_task_memory(conversation["id"])["covered_message_id"] > 0


def test_uncompressed_older_messages_stay_in_prompt(storage):
    conversation = storage.create_conversation("", "mock")
    _seed(storage, conversation["id"], 12)

    snapshot = snapshot_context(
        storage, conversation["id"], goal="", context_window=32768
    )

    assert len(snapshot.older) == 4
    assert len(snapshot.recent) == 8
    prompt = snapshot.prompt()
    assert "较早对话" in prompt
    assert "第0条消息" in prompt
    assert "第11条消息" in prompt


def test_short_history_is_left_uncompressed(storage):
    conversation = storage.create_conversation("", "mock")
    storage.add_message(conversation["id"], "user", "短目标", "goal")
    storage.add_message(conversation["id"], "assistant", "短结果", "result")

    snapshot = compact_context(
        storage,
        conversation["id"],
        goal="下一轮",
        context_window=32768,
        provider=_Summarizer(),
    )
    assert snapshot.compacted is False
    assert snapshot.summary == ""
    assert "短目标" in snapshot.prompt()


def test_memory_is_isolated_per_conversation(storage):
    first = storage.create_conversation("", "mock")
    second = storage.create_conversation("", "mock")
    _seed(storage, first["id"], 30)
    compact_context(
        storage, first["id"], context_window=4096, provider=_Summarizer()
    )

    other = snapshot_context(storage, second["id"], context_window=4096)
    assert other.summary == ""
    assert other.recent == []
    assert storage.get_task_memory(second["id"])["summary"] == ""
