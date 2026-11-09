from __future__ import annotations

import sqlite3

from xm_agent.storage import DEFAULT_PROFILE, Storage


def test_profile_defaults_are_created(tmp_path):
    storage = Storage(tmp_path / "app.db")
    profile = storage.get_profile()
    assert profile["display_name"] == DEFAULT_PROFILE["display_name"]
    assert profile["default_adapter"] == "mock"
    assert profile["auto_confirm"] is False


def test_profile_roundtrip_persists(tmp_path):
    path = tmp_path / "app.db"
    storage = Storage(path)
    storage.save_profile(
        display_name="  测试用户  ",
        default_adapter="windows",
        default_goal="打开记事本",
        auto_confirm=True,
    )

    reopened = Storage(path).get_profile()
    assert reopened["display_name"] == "测试用户"
    assert reopened["default_adapter"] == "windows"
    assert reopened["default_goal"] == "打开记事本"
    assert reopened["auto_confirm"] is True


def test_profile_partial_update_keeps_other_fields(tmp_path):
    storage = Storage(tmp_path / "app.db")
    storage.save_profile(default_goal="目标一", auto_confirm=True)
    profile = storage.save_profile(display_name="新名字")
    assert profile["default_goal"] == "目标一"
    assert profile["auto_confirm"] is True


def test_run_lifecycle_and_history(tmp_path):
    storage = Storage(tmp_path / "app.db")
    storage.create_run("run-1", "输入 hello 并保存", "mock")
    storage.finish_run("run-1", "done", "mock task complete", 3)

    stored = storage.get_run("run-1")
    assert stored is not None
    assert stored["status"] == "done"
    assert stored["steps"] == 3
    assert stored["reason"] == "mock task complete"
    assert stored["finished_at"]

    storage.create_run("run-2", "第二个任务", "windows")
    runs = storage.list_runs()
    assert [run["id"] for run in runs] == ["run-2", "run-1"]
    assert storage.get_run("missing") is None


def test_run_goal_is_truncated(tmp_path):
    storage = Storage(tmp_path / "app.db")
    storage.create_run("run-long", "x" * 900, "mock")
    assert len(storage.get_run("run-long")["goal"]) == 500


def test_conversations_messages_and_runs(tmp_path):
    storage = Storage(tmp_path / "app.db")
    conversation = storage.create_conversation(
        title="清理任务", adapter="mock", audit_mode="confirm_all"
    )
    assert conversation["audit_mode"] == "confirm_all"
    storage.add_message(conversation["id"], "user", "清理构建目录", kind="goal")
    storage.add_message(
        conversation["id"],
        "assistant",
        "thinking...",
        kind="thinking",
        payload={"step": 1},
    )
    storage.create_run("run-a", "清理构建目录", "mock", conversation["id"])

    detail = storage.get_conversation(conversation["id"])
    assert [item["role"] for item in detail["messages"]] == [
        "user",
        "assistant",
    ]
    assert detail["messages"][1]["payload"] == {"step": 1}
    assert [run["id"] for run in detail["runs"]] == ["run-a"]

    listed = storage.list_conversations()
    assert listed[0]["message_count"] == 2
    assert listed[0]["task_count"] == 1

    storage.delete_conversation(conversation["id"])
    assert storage.get_conversation(conversation["id"]) is None
    assert storage.list_messages(conversation["id"]) == []
    assert storage.get_run("run-a")["conversation_id"] is None


def test_run_events_are_ordered_and_replayable(tmp_path):
    storage = Storage(tmp_path / "app.db")
    conversation = storage.create_conversation("事件", "mock")
    first = storage.append_run_event(
        "run-b", conversation["id"], "start", {"goal": "x"}
    )
    second = storage.append_run_event(
        "run-b", conversation["id"], "step", {"step": 1}
    )
    assert second > first

    events = storage.list_run_events("run-b")
    assert [event["event"] for event in events] == ["start", "step"]
    replay = storage.list_run_events("run-b", after_id=first)
    assert [event["event"] for event in replay] == ["step"]
    grouped = storage.list_conversation_events(conversation["id"])
    assert len(grouped) == 2


def test_task_queue_ordering_and_jump(tmp_path):
    storage = Storage(tmp_path / "app.db")
    conversation = storage.create_conversation("队列", "mock")
    first = storage.enqueue_task(conversation["id"], "任务一", "mock")
    second = storage.enqueue_task(
        conversation["id"], "任务二", "windows", audit_mode="confirm_all"
    )
    assert second["audit_mode"] == "confirm_all"
    assert storage.next_queued_task()["id"] == first["id"]

    assert storage.jump_queue_task(second["id"]) is True
    assert storage.next_queued_task()["id"] == second["id"]

    storage.set_queue_status(second["id"], "running", run_id="run-c")
    queued = storage.list_queue()
    assert [item["id"] for item in queued] == [first["id"]]
    stored = storage.get_queue_task(second["id"])
    assert stored["status"] == "running"
    assert stored["run_id"] == "run-c"

    assert storage.jump_queue_task("missing") is False
    storage.remove_queue_task(first["id"])
    assert storage.get_queue_task(first["id"]) is None


def test_audit_decisions_are_recorded_and_marked(tmp_path):
    storage = Storage(tmp_path / "app.db")
    payload = {
        "operation": {
            "operation_id": "sha256:abc",
            "capability": "terminal.exec",
            "action": "exec",
        },
        "environment": {"cwd": str(tmp_path)},
        "verdict": {
            "verdict": "ask_user",
            "risk": "high",
            "categories": ["data_loss"],
            "source": "ai",
            "model": "audit-model",
            "latency_ms": 12,
        },
        "user_decision": "allow",
        "reason": "",
    }
    decision_id = storage.record_audit_decision(payload, "run-d", "conv-d")
    assert decision_id > 0

    records = storage.list_audit_decisions(run_id="run-d")
    assert len(records) == 1
    record = records[0]
    assert record["operation_id"] == "sha256:abc"
    assert record["verdict"] == "ask_user"
    assert record["categories"] == ["data_loss"]
    assert record["user_decision"] == "allow"
    assert record["executed"] is None

    storage.mark_audit_executed("run-d", "sha256:abc", True, "")
    record = storage.list_audit_decisions(run_id="run-d")[0]
    assert record["executed"] is True

    assert storage.list_audit_decisions(conversation_id="other") == []


def test_conversation_title_source_and_auto_title(tmp_path):
    storage = Storage(tmp_path / "app.db")
    conversation = storage.create_conversation("", "mock")
    assert conversation["title"] == ""
    assert conversation["title_source"] == "auto"

    storage.set_auto_title(conversation["id"], "自动生成的任务名")
    stored = storage.get_conversation(
        conversation["id"], include_messages=False
    )
    assert stored["title"] == "自动生成的任务名"
    assert stored["title_source"] == "auto"

    storage.update_conversation(conversation["id"], {"title": "手动命名"})
    stored = storage.get_conversation(
        conversation["id"], include_messages=False
    )
    assert stored["title"] == "手动命名"
    assert stored["title_source"] == "user"

    storage.set_auto_title(conversation["id"], "不应覆盖")
    stored = storage.get_conversation(
        conversation["id"], include_messages=False
    )
    assert stored["title"] == "手动命名"


def test_task_memory_roundtrip_and_delete(tmp_path):
    storage = Storage(tmp_path / "app.db")
    conversation = storage.create_conversation("", "mock")
    assert storage.get_task_memory(conversation["id"])["summary"] == ""

    storage.save_task_memory(conversation["id"], "已完成第一步", 12)
    memory = storage.get_task_memory(conversation["id"])
    assert memory["summary"] == "已完成第一步"
    assert memory["covered_message_id"] == 12

    storage.save_task_memory(conversation["id"], "已更新", 20)
    memory = storage.get_task_memory(conversation["id"])
    assert memory["summary"] == "已更新"
    assert memory["covered_message_id"] == 20

    storage.delete_conversation(conversation["id"])
    assert storage.get_task_memory(conversation["id"])["summary"] == ""


def test_conversations_are_isolated(tmp_path):
    storage = Storage(tmp_path / "app.db")
    first = storage.create_conversation("", "mock")
    second = storage.create_conversation("", "mock")
    storage.add_message(first["id"], "user", "第一条任务的目标", "goal")
    storage.add_message(first["id"], "assistant", "第一条结果", "result")

    assert [
        message["content"]
        for message in storage.list_messages(first["id"])
    ] == ["第一条任务的目标", "第一条结果"]
    assert storage.list_messages(second["id"]) == []
    assert storage.get_conversation(
        second["id"], include_messages=False
    )["title"] == ""


def test_legacy_conversation_table_gets_title_source(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            adapter TEXT NOT NULL DEFAULT 'mock',
            audit_mode TEXT NOT NULL DEFAULT 'ai',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO conversations
            (id, title, adapter, audit_mode, created_at, updated_at)
        VALUES ('c1', '旧任务', 'mock', 'ai', '2026-01-01', '2026-01-01');
        """
    )
    conn.commit()
    conn.close()

    storage = Storage(path)
    stored = storage.get_conversation("c1", include_messages=False)
    assert stored["title"] == "旧任务"
    assert stored["title_source"] == "auto"


def test_legacy_vision_flag_is_recomputed_on_migration(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE ai_models (
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            vision INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'api',
            created_at TEXT NOT NULL,
            PRIMARY KEY (provider_id, model_id)
        );
        INSERT INTO ai_models
            (provider_id, model_id, display_name, vision, source, created_at)
        VALUES ('p1', 'glm-4.6', 'GLM', 1, 'custom', '2026-01-01');
        """
    )
    conn.commit()
    conn.close()

    storage = Storage(path)
    models = storage.list_ai_models("p1")
    assert models[0]["model_id"] == "glm-4.6"
    assert models[0]["vision"] is False

    with storage._connect() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(ai_models)")
        }
    assert "vision_source" in columns
