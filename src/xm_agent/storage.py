from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


class Storage:
    """Small SQLite store for local profile and task history."""

    def __init__(self, path: str | Path = ".xm-agent/app.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

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

    def create_run(self, run_id: str, goal: str, adapter: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, goal, adapter, status, created_at)
                VALUES (?, ?, ?, 'running', ?)
                """,
                (run_id, goal[:500], adapter[:64], _now()),
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

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

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
                SELECT provider_id, model_id, display_name, vision, source
                  FROM ai_models
                 ORDER BY vision DESC, model_id COLLATE NOCASE
                """
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for model in models:
            item = dict(model)
            item["vision"] = bool(item["vision"])
            grouped.setdefault(item.pop("provider_id"), []).append(item)
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
                conn.execute(
                    """
                    INSERT INTO ai_models
                        (provider_id, model_id, display_name, vision, source, created_at)
                    VALUES (?, ?, ?, ?, 'api', ?)
                    ON CONFLICT(provider_id, model_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        vision = excluded.vision
                    """,
                    (
                        provider_id,
                        model_id[:200],
                        str(model.get("display_name") or model_id)[:200],
                        int(bool(model.get("vision"))),
                        now,
                    ),
                )
        return self.list_ai_models(provider_id)

    def list_ai_models(self, provider_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT model_id, display_name, vision, source
                  FROM ai_models
                 WHERE provider_id = ?
                 ORDER BY vision DESC, model_id COLLATE NOCASE
                """,
                (provider_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["vision"] = bool(item["vision"])
            result.append(item)
        return result

    def add_ai_model(
        self,
        provider_id: str,
        model_id: str,
        display_name: str = "",
        vision: bool = False,
    ) -> dict[str, Any] | None:
        model_id = model_id.strip()
        if not model_id:
            raise ValueError("model id is required")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ai_models
                    (provider_id, model_id, display_name, vision, source, created_at)
                VALUES (?, ?, ?, ?, 'custom', ?)
                ON CONFLICT(provider_id, model_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    vision = excluded.vision,
                    source = 'custom'
                """,
                (
                    provider_id,
                    model_id[:200],
                    (display_name.strip() or model_id)[:200],
                    int(bool(vision)),
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
                SELECT model_id, display_name, vision, source
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
        model = dict(model_row)
        model["vision"] = bool(model["vision"])
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
