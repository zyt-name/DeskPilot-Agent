from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model_caps import SOURCE_PROVIDER, resolve_vision_detail

DEFAULT_PROFILE: dict[str, Any] = {
    "display_name": "本地用户",
    "default_adapter": "mock",
    "default_goal": "输入 hello 并保存",
    "auto_confirm": False,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    display_name TEXT NOT NULL,
    default_adapter TEXT NOT NULL,
    default_goal TEXT NOT NULL,
    auto_confirm INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    adapter TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    steps INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    adapter TEXT NOT NULL DEFAULT 'mock',
    audit_mode TEXT NOT NULL DEFAULT 'ai',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_updated
    ON conversations(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'text',
    payload TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    conversation_id TEXT,
    event TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_events_run
    ON run_events(run_id, id);

CREATE INDEX IF NOT EXISTS idx_run_events_conversation
    ON run_events(conversation_id, id);

CREATE TABLE IF NOT EXISTS task_queue (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL,
    adapter TEXT NOT NULL,
    audit_mode TEXT NOT NULL DEFAULT 'ai',
    status TEXT NOT NULL DEFAULT 'queued',
    position INTEGER NOT NULL DEFAULT 0,
    run_id TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_queue_status
    ON task_queue(status, position, created_at);

CREATE TABLE IF NOT EXISTS audit_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id TEXT,
    conversation_id TEXT,
    operation_id TEXT NOT NULL,
    capability TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    descriptor TEXT NOT NULL DEFAULT '',
    environment TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL DEFAULT '',
    verdict_json TEXT NOT NULL DEFAULT '',
    risk TEXT NOT NULL DEFAULT '',
    categories TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    user_decision TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    executed INTEGER,
    error TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_decisions_run
    ON audit_decisions(run_id, id);

CREATE TABLE IF NOT EXISTS ai_providers (
    id TEXT PRIMARY KEY,
    preset_id TEXT NOT NULL DEFAULT 'custom',
    name TEXT NOT NULL,
    protocol TEXT NOT NULL DEFAULT 'openai',
    base_url TEXT NOT NULL DEFAULT '',
    plan_id TEXT NOT NULL DEFAULT '',
    models_style TEXT NOT NULL DEFAULT 'openai',
    api_key_enc TEXT NOT NULL DEFAULT '',
    api_key_hint TEXT NOT NULL DEFAULT '',
    requires_key INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_models (
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    vision INTEGER NOT NULL DEFAULT 0,
    vision_source TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'api',
    created_at TEXT NOT NULL,
    PRIMARY KEY (provider_id, model_id)
);

CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider_id TEXT NOT NULL DEFAULT '',
    provider_name TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    run_id TEXT,
    latency_ms INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_usage_ts ON ai_usage(ts DESC);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _model_record(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    reported = (
        bool(item.get("vision"))
        if item.get("vision_source") == SOURCE_PROVIDER
        else None
    )
    vision, source = resolve_vision_detail(
        str(item.get("model_id") or ""), reported=reported
    )
    item["vision"] = vision
    item["vision_source"] = source
    return item


def _sort_models(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items.sort(key=lambda item: (not item["vision"], item["model_id"].casefold()))
    return items


class Storage:
    """Small SQLite store for local profile and task history."""

    def __init__(self, path: str | Path = ".xm-agent/app.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(runs)")
        }
        if "conversation_id" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN conversation_id TEXT")
        queue_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_queue)")
        }
        if queue_columns and "run_id" not in queue_columns:
            conn.execute("ALTER TABLE task_queue ADD COLUMN run_id TEXT")
        audit_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(audit_decisions)")
        }
        if audit_columns and "verdict_json" not in audit_columns:
            conn.execute(
                "ALTER TABLE audit_decisions ADD COLUMN verdict_json "
                "TEXT NOT NULL DEFAULT ''"
            )
        model_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(ai_models)")
        }
        if model_columns and "vision_source" not in model_columns:
            conn.execute(
                "ALTER TABLE ai_models ADD COLUMN vision_source "
                "TEXT NOT NULL DEFAULT ''"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def get_profile(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = 1").fetchone()
            if row is None:
                now = _now()
                conn.execute(
                    """
                    INSERT INTO users
                        (id, display_name, default_adapter, default_goal,
                         auto_confirm, created_at, updated_at)
                    VALUES (1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        DEFAULT_PROFILE["display_name"],
                        DEFAULT_PROFILE["default_adapter"],
                        DEFAULT_PROFILE["default_goal"],
                        int(DEFAULT_PROFILE["auto_confirm"]),
                        now,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM users WHERE id = 1"
                ).fetchone()
        return self._profile_dict(row)

    def save_profile(self, **fields: Any) -> dict[str, Any]:
        current = self.get_profile()
        display_name = str(
            fields.get("display_name", current["display_name"])
        ).strip()[:64] or DEFAULT_PROFILE["display_name"]
        default_adapter = str(
            fields.get("default_adapter", current["default_adapter"])
        ).strip()[:64] or DEFAULT_PROFILE["default_adapter"]
        default_goal = str(
            fields.get("default_goal", current["default_goal"])
        ).strip()[:500]
        auto_confirm = bool(
            fields.get("auto_confirm", current["auto_confirm"])
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE users
                   SET display_name = ?, default_adapter = ?, default_goal = ?,
                       auto_confirm = ?, updated_at = ?
                 WHERE id = 1
                """,
                (
                    display_name,
                    default_adapter,
                    default_goal,
                    int(auto_confirm),
                    _now(),
                ),
            )
        return self.get_profile()

    def create_run(
        self,
        run_id: str,
        goal: str,
        adapter: str,
        conversation_id: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs
                    (id, goal, adapter, status, conversation_id, created_at)
                VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (run_id, goal[:500], adapter[:64], conversation_id, _now()),
            )

    def finish_run(
        self, run_id: str, status: str, reason: str, steps: int
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                   SET status = ?, reason = ?, steps = ?, finished_at = ?
                 WHERE id = ?
                """,
                (status, reason[:1000], max(0, int(steps)), _now(), run_id),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_runs(
        self, limit: int = 20, conversation_id: str | None = None
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            if conversation_id:
                rows = conn.execute(
                    """
                    SELECT * FROM runs WHERE conversation_id = ?
                     ORDER BY created_at DESC, rowid DESC LIMIT ?
                    """,
                    (conversation_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    # -- conversations -------------------------------------------------

    def create_conversation(
        self,
        title: str = "",
        adapter: str = "mock",
        audit_mode: str = "ai",
    ) -> dict[str, Any]:
        conversation_id = uuid.uuid4().hex[:12]
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations
                    (id, title, adapter, audit_mode, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    title.strip()[:120],
                    adapter[:64],
                    "confirm_all" if audit_mode == "confirm_all" else "ai",
                    now,
                    now,
                ),
            )
        conversation = self.get_conversation(conversation_id)
        assert conversation is not None
        return conversation

    def get_conversation(
        self, conversation_id: str, include_messages: bool = True
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if row is None:
                return None
            conversation = dict(row)
            conversation["runs"] = [
                dict(item)
                for item in conn.execute(
                    """
                    SELECT r.*,
                           (SELECT MIN(e.id) FROM run_events e
                             WHERE e.run_id = r.id) AS first_event_id
                      FROM runs r
                     WHERE r.conversation_id = ?
                     ORDER BY r.created_at ASC, r.rowid ASC
                    """,
                    (conversation_id,),
                ).fetchall()
            ]
            conversation["messages"] = (
                self._messages(conn, conversation_id)
                if include_messages
                else []
            )
            conversation["last_event_id"] = int(
                conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM run_events "
                    "WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()[0]
            )
        return conversation

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM messages m
                         WHERE m.conversation_id = c.id) AS message_count,
                       (SELECT COUNT(*) FROM runs r
                         WHERE r.conversation_id = c.id) AS task_count,
                       (SELECT MAX(m.created_at) FROM messages m
                         WHERE m.conversation_id = c.id) AS last_message_at,
                       (SELECT r.status FROM runs r
                         WHERE r.conversation_id = c.id
                         ORDER BY r.created_at DESC, r.rowid DESC
                         LIMIT 1) AS last_status
                  FROM conversations c
                 ORDER BY COALESCE(last_message_at, c.updated_at) DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_conversation(
        self, conversation_id: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        allowed = {
            key: fields[key]
            for key in ("title", "adapter", "audit_mode")
            if key in fields and fields[key] is not None
        }
        if allowed:
            assignments = ", ".join(f"{key} = ?" for key in allowed)
            values = list(allowed.values()) + [_now(), conversation_id]
            with self._connect() as conn:
                conn.execute(
                    f"UPDATE conversations SET {assignments}, updated_at = ? "
                    "WHERE id = ?",
                    values,
                )
        return self.get_conversation(conversation_id, include_messages=False)

    def touch_conversation(self, conversation_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (_now(), conversation_id),
            )

    def delete_conversation(self, conversation_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            )
            conn.execute(
                "DELETE FROM task_queue WHERE conversation_id = ? "
                "AND status = 'queued'",
                (conversation_id,),
            )
            conn.execute(
                "UPDATE runs SET conversation_id = NULL "
                "WHERE conversation_id = ?",
                (conversation_id,),
            )
            conn.execute(
                "UPDATE run_events SET conversation_id = NULL "
                "WHERE conversation_id = ?",
                (conversation_id,),
            )
            conn.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        kind: str = "text",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages
                    (conversation_id, role, content, kind, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    role[:16],
                    content[:20000],
                    kind[:32],
                    _dump(payload or {}),
                    now,
                ),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            message_id = int(cursor.lastrowid)
        return {
            "id": message_id,
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "kind": kind,
            "payload": payload or {},
            "created_at": now,
        }

    def list_messages(
        self, conversation_id: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 2000))
        with self._connect() as conn:
            return self._messages(conn, conversation_id, limit)

    @staticmethod
    def _messages(
        conn: sqlite3.Connection, conversation_id: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM messages WHERE conversation_id = ?
                 ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (conversation_id, max(1, min(int(limit), 2000))),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = _load(item.get("payload"), {})
            result.append(item)
        return result

    # -- run events ----------------------------------------------------

    def append_run_event(
        self,
        run_id: str,
        conversation_id: str | None,
        event: str,
        payload: dict[str, Any],
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO run_events
                    (run_id, conversation_id, event, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    conversation_id,
                    event[:64],
                    _dump(payload),
                    _now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_run_events(
        self, run_id: str, after_id: int = 0, limit: int = 2000
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM run_events
                 WHERE run_id = ? AND id > ?
                 ORDER BY id ASC LIMIT ?
                """,
                (run_id, max(0, int(after_id)), max(1, min(int(limit), 5000))),
            ).fetchall()
        return [self._event_dict(row) for row in rows]

    def list_conversation_events(
        self, conversation_id: str, after_id: int = 0, limit: int = 2000
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM run_events
                 WHERE conversation_id = ? AND id > ?
                 ORDER BY id ASC LIMIT ?
                """,
                (
                    conversation_id,
                    max(0, int(after_id)),
                    max(1, min(int(limit), 5000)),
                ),
            ).fetchall()
        return [self._event_dict(row) for row in rows]

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "conversation_id": row["conversation_id"],
            "event": row["event"],
            "payload": _load(row["payload"], {}),
            "created_at": row["created_at"],
        }

    # -- task queue ----------------------------------------------------

    def enqueue_task(
        self,
        conversation_id: str,
        goal: str,
        adapter: str,
        audit_mode: str = "ai",
    ) -> dict[str, Any]:
        task_id = uuid.uuid4().hex[:12]
        with self._connect() as conn:
            position = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM task_queue "
                "WHERE status = 'queued'"
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO task_queue
                    (id, conversation_id, goal, adapter, audit_mode,
                     status, position, created_at)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    task_id,
                    conversation_id,
                    goal[:500],
                    adapter[:64],
                    "confirm_all" if audit_mode == "confirm_all" else "ai",
                    int(position),
                    _now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM task_queue WHERE id = ?", (task_id,)
            ).fetchone()
        return dict(row)

    def list_queue(self, status: str = "queued") -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_queue WHERE status = ? "
                "ORDER BY position ASC, created_at ASC",
                (status,),
            ).fetchall()
        return [dict(row) for row in rows]

    def next_queued_task(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_queue WHERE status = 'queued' "
                "ORDER BY position ASC, created_at ASC LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def get_queue_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_queue WHERE id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def set_queue_status(
        self,
        task_id: str,
        status: str,
        run_id: str | None = None,
    ) -> None:
        now = _now()
        started = now if status == "running" else None
        finished = now if status in {"done", "failed", "cancelled"} else None
        with self._connect() as conn:
            if started:
                conn.execute(
                    "UPDATE task_queue SET status = ?, run_id = ?, "
                    "started_at = ? WHERE id = ?",
                    (status, run_id, started, task_id),
                )
            elif finished:
                conn.execute(
                    "UPDATE task_queue SET status = ?, finished_at = ? "
                    "WHERE id = ?",
                    (status, finished, task_id),
                )
            else:
                conn.execute(
                    "UPDATE task_queue SET status = ? WHERE id = ?",
                    (status, task_id),
                )

    def jump_queue_task(self, task_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM task_queue WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None or row["status"] != "queued":
                return False
            minimum = conn.execute(
                "SELECT COALESCE(MIN(position), 1) FROM task_queue "
                "WHERE status = 'queued'"
            ).fetchone()[0]
            conn.execute(
                "UPDATE task_queue SET position = ? WHERE id = ?",
                (int(minimum) - 1, task_id),
            )
        return True

    def remove_queue_task(self, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM task_queue WHERE id = ?", (task_id,))

    # -- audit decisions -----------------------------------------------

    def record_audit_decision(
        self,
        payload: dict[str, Any],
        run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> int:
        descriptor = payload.get("operation") or {}
        verdict = payload.get("verdict") or {}
        categories = verdict.get("categories") or []
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO audit_decisions
                    (ts, run_id, conversation_id, operation_id, capability,
                     action, descriptor, environment, verdict, verdict_json,
                     risk, categories, source, user_decision, model,
                     latency_ms, prompt_tokens, completion_tokens,
                     total_tokens, executed, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    run_id,
                    conversation_id,
                    str(descriptor.get("operation_id") or ""),
                    str(descriptor.get("capability") or ""),
                    str(descriptor.get("action") or ""),
                    _dump(descriptor),
                    _dump(payload.get("environment") or {}),
                    str(verdict.get("verdict") or ""),
                    _dump(verdict),
                    str(verdict.get("risk") or ""),
                    _dump(categories),
                    str(verdict.get("source") or ""),
                    str(payload.get("user_decision") or ""),
                    str(verdict.get("model") or ""),
                    max(0, int(verdict.get("latency_ms") or 0)),
                    max(0, int(verdict.get("prompt_tokens") or 0)),
                    max(0, int(verdict.get("completion_tokens") or 0)),
                    max(0, int(verdict.get("total_tokens") or 0)),
                    None,
                    str(payload.get("reason") or "")[:2000],
                ),
            )
            return int(cursor.lastrowid)

    def mark_audit_executed(
        self,
        run_id: str,
        operation_id: str,
        ok: bool,
        error: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE audit_decisions
                   SET executed = ?, error = ?
                 WHERE id = (
                    SELECT id FROM audit_decisions
                     WHERE run_id = ? AND operation_id = ?
                     ORDER BY id DESC LIMIT 1
                 )
                """,
                (
                    int(bool(ok)),
                    error[:2000],
                    run_id,
                    operation_id,
                ),
            )

    def list_audit_decisions(
        self,
        run_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses: list[str] = []
        values: list[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            values.append(run_id)
        if conversation_id:
            clauses.append("conversation_id = ?")
            values.append(conversation_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM audit_decisions {where} "
                "ORDER BY id DESC LIMIT ?",
                values,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["descriptor"] = _load(item.get("descriptor"), {})
            item["environment"] = _load(item.get("environment"), {})
            item["verdict_json"] = _load(item.get("verdict_json"), {})
            item["categories"] = _load(item.get("categories"), [])
            item["executed"] = (
                None if item["executed"] is None else bool(item["executed"])
            )
            result.append(item)
        return result

    def list_ai_providers(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, preset_id, name, protocol, base_url, plan_id,
                       models_style, api_key_hint, requires_key,
                       (api_key_enc <> '') AS has_key,
                       created_at, updated_at
                  FROM ai_providers
                 ORDER BY name COLLATE NOCASE
                """
            ).fetchall()
            models = conn.execute(
                """
                SELECT provider_id, model_id, display_name, vision,
                       vision_source, source
                  FROM ai_models
                """
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for model in models:
            item = _model_record(model)
            grouped.setdefault(item.pop("provider_id"), []).append(item)
        for items in grouped.values():
            _sort_models(items)
        result = []
        for row in rows:
            item = dict(row)
            item["has_key"] = bool(item["has_key"])
            item["requires_key"] = bool(item["requires_key"])
            item["models"] = grouped.get(item["id"], [])
            result.append(item)
        return result

    def get_ai_provider(
        self, provider_id: str, include_secret: bool = False
    ) -> dict[str, Any] | None:
        columns = (
            "id, preset_id, name, protocol, base_url, plan_id, models_style, "
            "api_key_hint, requires_key, (api_key_enc <> '') AS has_key, "
            "created_at, updated_at"
        )
        if include_secret:
            columns += ", api_key_enc"
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {columns} FROM ai_providers WHERE id = ?", (provider_id,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["has_key"] = bool(data.get("has_key"))
        data["requires_key"] = bool(data.get("requires_key"))
        return data

    def save_ai_provider(self, fields: dict[str, Any]) -> dict[str, Any] | None:
        now = _now()
        provider_id = str(fields.get("id") or uuid.uuid4().hex[:12])
        existing = self.get_ai_provider(provider_id, include_secret=True)
        values = {
            "preset_id": str(fields.get("preset_id") or "custom")[:64],
            "name": str(fields.get("name") or "")[:128] or provider_id,
            "protocol": str(fields.get("protocol") or "openai")[:32],
            "base_url": str(fields.get("base_url") or "")[:500].rstrip("/"),
            "plan_id": str(fields.get("plan_id") or "")[:64],
            "models_style": str(fields.get("models_style") or "openai")[:32],
            "requires_key": int(bool(fields.get("requires_key", True))),
        }
        key_enc = fields.get("api_key_enc")
        key_hint = fields.get("api_key_hint")
        with self._connect() as conn:
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO ai_providers
                        (id, preset_id, name, protocol, base_url, plan_id,
                         models_style, api_key_enc, api_key_hint, requires_key,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        provider_id,
                        values["preset_id"],
                        values["name"],
                        values["protocol"],
                        values["base_url"],
                        values["plan_id"],
                        values["models_style"],
                        str(key_enc or ""),
                        str(key_hint or ""),
                        values["requires_key"],
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE ai_providers
                       SET preset_id = ?, name = ?, protocol = ?, base_url = ?,
                           plan_id = ?, models_style = ?, requires_key = ?,
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        values["preset_id"],
                        values["name"],
                        values["protocol"],
                        values["base_url"],
                        values["plan_id"],
                        values["models_style"],
                        values["requires_key"],
                        now,
                        provider_id,
                    ),
                )
                if key_enc is not None:
                    conn.execute(
                        """
                        UPDATE ai_providers
                           SET api_key_enc = ?, api_key_hint = ?
                         WHERE id = ?
                        """,
                        (str(key_enc), str(key_hint or ""), provider_id),
                    )
        return self.get_ai_provider(provider_id)

    def delete_ai_provider(self, provider_id: str) -> None:
        with self._connect() as conn:
            active = conn.execute(
                "SELECT value FROM app_state WHERE key = 'active_provider_id'"
            ).fetchone()
            conn.execute("DELETE FROM ai_providers WHERE id = ?", (provider_id,))
            conn.execute("DELETE FROM ai_models WHERE provider_id = ?", (provider_id,))
            if active is not None and active["value"] == provider_id:
                conn.execute(
                    "DELETE FROM app_state WHERE key IN "
                    "('active_provider_id', 'active_model_id')"
                )

    def replace_fetched_models(
        self, provider_id: str, models: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM ai_models WHERE provider_id = ? AND source = 'api'",
                (provider_id,),
            )
            for model in models:
                model_id = str(model.get("model_id") or "").strip()
                if not model_id:
                    continue
                if model.get("vision_source") == SOURCE_PROVIDER:
                    vision = bool(model.get("vision"))
                    vision_source = SOURCE_PROVIDER
                else:
                    vision, vision_source = resolve_vision_detail(model_id)
                conn.execute(
                    """
                    INSERT INTO ai_models
                        (provider_id, model_id, display_name, vision,
                         vision_source, source, created_at)
                    VALUES (?, ?, ?, ?, ?, 'api', ?)
                    ON CONFLICT(provider_id, model_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        vision = excluded.vision,
                        vision_source = excluded.vision_source
                    """,
                    (
                        provider_id,
                        model_id[:200],
                        str(model.get("display_name") or model_id)[:200],
                        int(vision),
                        vision_source,
                        now,
                    ),
                )
        return self.list_ai_models(provider_id)

    def list_ai_models(self, provider_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT model_id, display_name, vision, vision_source, source
                  FROM ai_models
                 WHERE provider_id = ?
                """,
                (provider_id,),
            ).fetchall()
        return _sort_models([_model_record(row) for row in rows])

    def add_ai_model(
        self,
        provider_id: str,
        model_id: str,
        display_name: str = "",
    ) -> dict[str, Any] | None:
        model_id = model_id.strip()
        if not model_id:
            raise ValueError("model id is required")
        vision, vision_source = resolve_vision_detail(model_id)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ai_models
                    (provider_id, model_id, display_name, vision,
                     vision_source, source, created_at)
                VALUES (?, ?, ?, ?, ?, 'custom', ?)
                ON CONFLICT(provider_id, model_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    vision = excluded.vision,
                    vision_source = excluded.vision_source,
                    source = 'custom'
                """,
                (
                    provider_id,
                    model_id[:200],
                    (display_name.strip() or model_id)[:200],
                    int(vision),
                    vision_source,
                    _now(),
                ),
            )
        for item in self.list_ai_models(provider_id):
            if item["model_id"] == model_id:
                return item
        return None

    def clear_fetched_models(self, provider_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM ai_models WHERE provider_id = ? AND source = 'api'",
                (provider_id,),
            )

    def delete_ai_model(self, provider_id: str, model_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM ai_models WHERE provider_id = ? AND model_id = ?",
                (provider_id, model_id),
            )
            active = conn.execute(
                "SELECT value FROM app_state WHERE key = 'active_provider_id'"
            ).fetchone()
            active_model = conn.execute(
                "SELECT value FROM app_state WHERE key = 'active_model_id'"
            ).fetchone()
            if (
                active is not None
                and active_model is not None
                and active["value"] == provider_id
                and active_model["value"] == model_id
            ):
                conn.execute(
                    "DELETE FROM app_state WHERE key IN "
                    "('active_provider_id', 'active_model_id')"
                )

    def set_active_ai(self, provider_id: str, model_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            provider = conn.execute(
                "SELECT id FROM ai_providers WHERE id = ?", (provider_id,)
            ).fetchone()
            model = conn.execute(
                "SELECT model_id FROM ai_models WHERE provider_id = ? AND model_id = ?",
                (provider_id, model_id),
            ).fetchone()
            if provider is None:
                raise ValueError("unknown AI provider")
            if model is None:
                raise ValueError("unknown model for this provider")
            conn.execute(
                "INSERT INTO app_state (key, value) VALUES ('active_provider_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (provider_id,),
            )
            conn.execute(
                "INSERT INTO app_state (key, value) VALUES ('active_model_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (model_id,),
            )
        active = self.get_active_ai()
        assert active is not None
        return active

    def clear_active_ai(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM app_state WHERE key IN "
                "('active_provider_id', 'active_model_id')"
            )

    def get_active_ai(self, include_secret: bool = False) -> dict[str, Any] | None:
        columns = (
            "id, preset_id, name, protocol, base_url, plan_id, models_style, "
            "api_key_hint, requires_key, (api_key_enc <> '') AS has_key, "
            "created_at, updated_at"
        )
        if include_secret:
            columns += ", api_key_enc"
        with self._connect() as conn:
            state = {
                row["key"]: row["value"]
                for row in conn.execute(
                    "SELECT key, value FROM app_state WHERE key IN "
                    "('active_provider_id', 'active_model_id')"
                ).fetchall()
            }
            provider_id = state.get("active_provider_id")
            model_id = state.get("active_model_id")
            if not provider_id or not model_id:
                return None
            provider_row = conn.execute(
                f"SELECT {columns} FROM ai_providers WHERE id = ?", (provider_id,)
            ).fetchone()
            model_row = conn.execute(
                """
                SELECT model_id, display_name, vision, vision_source, source
                  FROM ai_models
                 WHERE provider_id = ? AND model_id = ?
                """,
                (provider_id, model_id),
            ).fetchone()
            if provider_row is None or model_row is None:
                conn.execute(
                    "DELETE FROM app_state WHERE key IN "
                    "('active_provider_id', 'active_model_id')"
                )
                return None
        provider = dict(provider_row)
        provider["has_key"] = bool(provider.get("has_key"))
        provider["requires_key"] = bool(provider.get("requires_key"))
        model = _model_record(model_row)
        return {"provider": provider, "model": model}

    def record_ai_usage(
        self,
        provider_id: str,
        provider_name: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        source: str = "",
        run_id: str | None = None,
        latency_ms: int = 0,
    ) -> None:
        prompt = max(0, int(prompt_tokens or 0))
        completion = max(0, int(completion_tokens or 0))
        total = max(0, int(total_tokens or 0)) or (prompt + completion)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ai_usage
                    (ts, provider_id, provider_name, model, prompt_tokens,
                     completion_tokens, total_tokens, source, run_id, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    provider_id[:64],
                    provider_name[:128],
                    model[:200],
                    prompt,
                    completion,
                    total,
                    source[:32],
                    run_id,
                    max(0, int(latency_ms or 0)),
                ),
            )

    def ai_usage_summary(self) -> dict[str, Any]:
        today = datetime.now(timezone.utc).date().isoformat() + "T"
        with self._connect() as conn:
            totals = conn.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens
                  FROM ai_usage
                 WHERE total_tokens > 0
                """
            ).fetchone()
            today_totals = conn.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens
                  FROM ai_usage
                 WHERE ts >= ? AND total_tokens > 0
                """,
                (today,),
            ).fetchone()
            by_model = conn.execute(
                """
                SELECT provider_id, provider_name, model,
                       COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens
                  FROM ai_usage
                 WHERE total_tokens > 0
                 GROUP BY provider_id, provider_name, model
                 ORDER BY total_tokens DESC, calls DESC
                """
            ).fetchall()
            by_provider = conn.execute(
                """
                SELECT provider_id, provider_name,
                       COUNT(*) AS calls,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens
                  FROM ai_usage
                 WHERE total_tokens > 0
                 GROUP BY provider_id, provider_name
                 ORDER BY total_tokens DESC
                """
            ).fetchall()
            recent = conn.execute(
                """
                SELECT ts, provider_name, model, prompt_tokens, completion_tokens,
                       total_tokens, source, run_id, latency_ms
                  FROM ai_usage
                 WHERE total_tokens > 0
                 ORDER BY id DESC
                 LIMIT 20
                """
            ).fetchall()
        return {
            "totals": dict(totals),
            "today": dict(today_totals),
            "by_model": [dict(row) for row in by_model],
            "by_provider": [dict(row) for row in by_provider],
            "recent": [dict(row) for row in recent],
        }

    @staticmethod
    def _profile_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["auto_confirm"] = bool(data.get("auto_confirm"))
        return data


__all__ = ["Storage", "DEFAULT_PROFILE"]
