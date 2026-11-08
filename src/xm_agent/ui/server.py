from __future__ import annotations

import copy
import json
import mimetypes
import os
import secrets
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..ai.service import AiService
from ..audit import AuditLog
from ..bootstrap import build_adapter, build_provider, policy_from_config
from ..config import AgentConfig
from ..lock import LeaseLock
from ..resources import ResourceManager
from ..runtime import AgentRuntime
from ..storage import Storage

STATIC_DIR = Path(__file__).parent / "static"
_ASSETS = {
    "/": "index.html",
    "/index.html": "index.html",
    "/static/app.js": "app.js",
    "/static/styles.css": "styles.css",
}
_MAX_BODY = 64 * 1024
_MAX_EVENTS = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UiServer:
    """Loopback HTTP + desktop WebView backend for the local UI.

    The API stays shell-agnostic so a Tauri window can replace the WebView
    without touching the runtime.
    """

    def __init__(
        self,
        config: AgentConfig,
        db_path: str | Path = ".xm-agent/app.db",
        host: str = "127.0.0.1",
        port: int = 0,
        resources_root: str | Path | None = None,
    ) -> None:
        self.config = config
        self.storage = Storage(db_path)
        self.ai = AiService(self.storage)
        self.host = host
        self.port = int(port)
        self.token = secrets.token_urlsafe(24)
        self.resources = ResourceManager(resources_root or Path.cwd())
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._runs: dict[str, dict[str, Any]] = {}
        self._runs_lock = threading.Lock()
        self._active = threading.Lock()
        self._resource_lock = threading.Lock()
        self._resource_job: dict[str, Any] | None = None

    def start(self) -> str:
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()
        return f"http://{self.host}:{self.port}/"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def bootstrap(self) -> dict[str, Any]:
        api_key = os.environ.get(self.config.model.api_key_env)
        return {
            "token": self.token,
            "profile": self.storage.get_profile(),
            "runs": self.storage.list_runs(20),
            "ai": self.ai.bootstrap_summary(),
            "config": {
                "adapters": sorted(self.config.adapters),
                "default_adapter": self.config.default_adapter,
                "model": {
                    "provider": self.config.model.provider,
                    "model": self.config.model.model,
                    "vision": self.config.model.vision,
                    "api_key_env": self.config.model.api_key_env,
                    "api_key_present": bool(api_key),
                },
                "audit_root": self.config.audit.root,
                "db_path": str(self.storage.path.resolve()),
            },
        }

    def resources_snapshot(self) -> dict[str, Any]:
        states = [state.to_dict() for state in self.resources.status()]
        with self._resource_lock:
            job = copy.deepcopy(self._resource_job)
        return {
            "root": str(self.resources.root),
            "resources": states,
            "job": job,
        }

    def start_resource_sync(
        self,
        only: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        with self._resource_lock:
            if self._resource_job and self._resource_job.get("running"):
                return self.resources_snapshot()
            states = [state.to_dict() for state in self.resources.status()]
            job_id = uuid.uuid4().hex[:12]
            self._resource_job = {
                "id": job_id,
                "running": True,
                "phase": "detecting",
                "percent": 1,
                "message": "正在检测资源完整性",
                "started_at": _now(),
                "finished_at": None,
                "ok": None,
                "error": None,
                "resources": states,
            }
        threading.Thread(
            target=self._run_resource_sync,
            args=(job_id, list(only or []), bool(force)),
            daemon=True,
        ).start()
        return self.resources_snapshot()

    def _run_resource_sync(
        self, job_id: str, only: list[str], force: bool
    ) -> None:
        def on_progress(event: dict[str, Any]) -> None:
            with self._resource_lock:
                job = self._resource_job
                if job is None or job.get("id") != job_id:
                    return
                percent = event.get("percent")
                if isinstance(percent, (int, float)):
                    job["percent"] = max(
                        int(job["percent"]), min(100, int(percent))
                    )
                message = event.get("message")
                if message:
                    job["message"] = str(message)
                phase = event.get("phase")
                if phase:
                    job["phase"] = str(phase)
                resource_id = str(event.get("resource") or "")
                state = event.get("state")
                if not resource_id:
                    return
                rows = job["resources"]
                index = next(
                    (
                        position
                        for position, row in enumerate(rows)
                        if row.get("id") == resource_id
                    ),
                    None,
                )
                if isinstance(state, dict):
                    if index is None:
                        rows.append(state)
                    else:
                        rows[index] = state
                    return
                if index is not None and phase and phase not in {
                    "detect",
                    "result",
                    "error",
                }:
                    rows[index]["status"] = "installing"
                    if message:
                        rows[index]["detail"] = str(message)

        try:
            states = self.resources.ensure(
                only=only or None, force=force, progress=on_progress
            )
            failed = [
                state for state in states if not state.ok and state.required
            ]
            with self._resource_lock:
                job = self._resource_job
                if job is None or job.get("id") != job_id:
                    return
                job["running"] = False
                job["phase"] = "done"
                job["percent"] = 100
                job["finished_at"] = _now()
                job["ok"] = not failed
                if failed:
                    job["message"] = (
                        "检测完成，以下必需资源未就绪："
                        + "、".join(state.name for state in failed)
                    )
                else:
                    job["message"] = "检测完成，资源已就绪"
        except Exception as exc:
            with self._resource_lock:
                job = self._resource_job
                if job is None or job.get("id") != job_id:
                    return
                job["running"] = False
                job["phase"] = "error"
                job["finished_at"] = _now()
                job["ok"] = False
                job["error"] = str(exc)
                job["message"] = f"资源检测失败：{exc}"

    def save_profile(self, fields: dict[str, Any]) -> dict[str, Any]:
        adapter = str(
            fields.get("default_adapter", self.config.default_adapter)
        )
        if adapter not in self.config.adapters:
            raise ValueError(f"unknown adapter: {adapter}")
        allowed = {
            "display_name": fields.get("display_name"),
            "default_adapter": adapter,
            "default_goal": fields.get("default_goal"),
            "auto_confirm": fields.get("auto_confirm"),
        }
        return self.storage.save_profile(
            **{key: value for key, value in allowed.items() if value is not None}
        )

    def start_run(
        self, goal: str, adapter_name: str, auto_confirm: bool
    ) -> dict[str, Any]:
        goal = goal.strip()
        if not goal:
            raise ValueError("goal is required")
        if adapter_name not in self.config.adapters:
            raise ValueError(f"unknown adapter: {adapter_name}")
        if not self._active.acquire(blocking=False):
            raise RuntimeError("another task is already running")
        run_id = uuid.uuid4().hex[:12]
        active = self.ai.active_summary()
        if active is not None:
            run_model = active["model"]
        elif self.config.model.provider in {"openai_compat", "openai"}:
            run_model = self.config.model.model
        else:
            run_model = "heuristic-mock"
        entry = {
            "id": run_id,
            "goal": goal[:500],
            "adapter": adapter_name,
            "status": "running",
            "reason": "",
            "steps": 0,
            "model": run_model,
            "created_at": _now(),
            "events": [],
        }
        with self._runs_lock:
            self._runs[run_id] = entry
        try:
            self.storage.create_run(run_id, goal, adapter_name)
            threading.Thread(
                target=self._execute_run,
                args=(run_id, goal, adapter_name, bool(auto_confirm)),
                daemon=True,
            ).start()
        except Exception:
            self._active.release()
            with self._runs_lock:
                self._runs.pop(run_id, None)
            raise
        return {"id": run_id}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._runs_lock:
            entry = self._runs.get(run_id)
            if entry is None:
                stored = self.storage.get_run(run_id)
                if stored is None:
                    return None
                stored["events"] = []
                return stored
            return {
                "id": entry["id"],
                "goal": entry["goal"],
                "adapter": entry["adapter"],
                "status": entry["status"],
                "reason": entry["reason"],
                "steps": entry["steps"],
                "model": entry["model"],
                "created_at": entry["created_at"],
                "events": list(entry["events"]),
            }

    def _execute_run(
        self,
        run_id: str,
        goal: str,
        adapter_name: str,
        auto_confirm: bool,
    ) -> None:
        def on_event(event: str, payload: dict[str, Any]) -> None:
            with self._runs_lock:
                entry = self._runs.get(run_id)
                if entry is None:
                    return
                entry["events"].append(
                    {"event": event, "payload": payload, "ts": _now()}
                )
                del entry["events"][:-_MAX_EVENTS]

        adapter = None
        status, reason, steps = "failed", "", 0
        try:
            adapter = build_adapter(self.config, adapter_name)
            provider = build_provider(
                self.config,
                adapter_name,
                storage=self.storage,
                source="ui",
                run_id=run_id,
            )
            on_event(
                "model",
                {
                    "model": getattr(provider, "provider_model", "")
                    or getattr(provider, "model", "")
                    or provider.name,
                    "provider": getattr(provider, "provider_name", "")
                    or provider.name,
                    "vision": bool(
                        getattr(provider, "provider_vision", False)
                        or getattr(provider, "vision", False)
                    ),
                },
            )
            runtime = AgentRuntime(
                adapter=adapter,
                provider=provider,
                policy=policy_from_config(self.config),
                audit=AuditLog(
                    self.config.audit.root, self.config.audit.keep_days
                ),
                lock=LeaseLock(
                    self.config.runtime.lock_path,
                    self.config.runtime.lock_ttl_seconds,
                ),
                max_steps=self.config.runtime.max_steps,
                max_consecutive_failures=(
                    self.config.runtime.max_consecutive_failures
                ),
                confirm=(
                    (lambda action, why: True)
                    if auto_confirm
                    else (lambda action, why: False)
                ),
                on_event=on_event,
            )
            result = runtime.run(goal)
            status = result.status
            reason = result.reason
            steps = len(result.steps)
        except Exception as exc:
            reason = str(exc)
            on_event("error", {"message": reason})
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    pass
            self.storage.finish_run(run_id, status, reason, steps)
            with self._runs_lock:
                entry = self._runs.get(run_id)
                if entry is not None:
                    entry["status"] = status
                    entry["reason"] = reason
                    entry["steps"] = steps
            self._active.release()


def _make_handler(app: UiServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "XmAgentUI/0.1"

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                self._json({"error": "invalid host"}, 403)
                return
            path = urlparse(self.path).path
            if path == "/api/health":
                self._json(
                    {
                        "ok": True,
                        "db": str(app.storage.path),
                        "adapters": len(app.config.adapters),
                    }
                )
                return
            if path == "/api/bootstrap":
                self._json(app.bootstrap())
                return
            if path == "/api/runs":
                self._json({"runs": app.storage.list_runs(20)})
                return
            if path == "/api/ai":
                self._json(app.ai.overview())
                return
            if path == "/api/ai/usage":
                self._json(app.ai.usage())
                return
            if path == "/api/resources":
                self._json(app.resources_snapshot())
                return
            if path.startswith("/api/runs/"):
                run = app.get_run(path.rsplit("/", 1)[-1])
                if run is None:
                    self._json({"error": "run not found"}, 404)
                else:
                    self._json(run)
                return
            if path in _ASSETS:
                self._static(_ASSETS[path])
                return
            self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                self._json({"error": "invalid host"}, 403)
                return
            if self.headers.get("X-XM-Token") != app.token:
                self._json({"error": "missing or invalid token"}, 401)
                return
            path = urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/profile":
                    self._json(app.save_profile(body))
                    return
                if path == "/api/runs":
                    self._json(
                        app.start_run(
                            goal=str(body.get("goal", "")),
                            adapter_name=str(body.get("adapter", "")),
                            auto_confirm=bool(body.get("auto_confirm")),
                        ),
                        202,
                    )
                    return
                if path == "/api/ai/providers/save":
                    saved = app.ai.save_provider(body)
                    self._json({"provider": saved, "ai": app.ai.overview()})
                    return
                if path == "/api/ai/providers/delete":
                    app.ai.delete_provider(str(body.get("id", "")))
                    self._json({"ok": True, "ai": app.ai.overview()})
                    return
                if path == "/api/ai/models/fetch":
                    models = app.ai.fetch_models(
                        str(body.get("provider_id", "")),
                        str(body.get("api_key", "")),
                    )
                    self._json({"models": models, "ai": app.ai.overview()})
                    return
                if path == "/api/ai/models/add":
                    model = app.ai.add_model(
                        str(body.get("provider_id", "")),
                        str(body.get("model_id", "")),
                        str(body.get("display_name", "")),
                        bool(body.get("vision")),
                    )
                    self._json({"model": model, "ai": app.ai.overview()})
                    return
                if path == "/api/ai/models/delete":
                    app.ai.delete_model(
                        str(body.get("provider_id", "")),
                        str(body.get("model_id", "")),
                    )
                    self._json({"ok": True, "ai": app.ai.overview()})
                    return
                if path == "/api/ai/active":
                    active = app.ai.set_active(
                        str(body.get("provider_id", "")),
                        str(body.get("model_id", "")),
                    )
                    self._json({"active": active, "ai": app.ai.overview()})
                    return
                if path == "/api/ai/test":
                    result = app.ai.test_provider(
                        str(body.get("provider_id", "")),
                        str(body.get("model_id", "")),
                        str(body.get("api_key", "")),
                    )
                    self._json(result)
                    return
                if path == "/api/resources/sync":
                    only = body.get("only")
                    if only is not None and not isinstance(only, list):
                        raise ValueError("only must be a list of resource ids")
                    self._json(
                        app.start_resource_sync(
                            [str(item) for item in only or []],
                            bool(body.get("force")),
                        ),
                        202,
                    )
                    return
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            except RuntimeError as exc:
                self._json({"error": str(exc)}, 409)
                return
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json({"error": "invalid JSON body"}, 400)
                return
            self._json({"error": "not found"}, 404)

        def _host_ok(self) -> bool:
            host = self.headers.get("Host", "")
            return host in {
                f"{app.host}:{app.port}",
                f"localhost:{app.port}",
            }

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > _MAX_BODY:
                raise ValueError("request body too large")
            raw = self.rfile.read(length)
            if not raw:
                return {}
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("request body must be a JSON object")
            return data

        def _static(self, name: str) -> None:
            path = STATIC_DIR / name
            if not path.is_file():
                self._json({"error": "asset not found"}, 404)
                return
            data = path.read_bytes()
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if mime.startswith("text/") or mime == "application/javascript":
                mime += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, payload: Any, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


__all__ = ["UiServer", "STATIC_DIR"]
