from __future__ import annotations

import copy
import json
import mimetypes
import os
import queue as queue_module
import secrets
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..ai.catalog import is_vision_model
from ..ai.service import AiService
from ..audit import AuditLog
from ..bootstrap import (
    build_adapter,
    build_audit_gate,
    build_provider,
    policy_from_config,
)
from ..config import AgentConfig
from ..control import RunControl
from ..lock import LeaseLock
from ..resources import ResourceManager
from ..runtime import AgentRuntime
from ..storage import Storage

STATIC_DIR = Path(__file__).parent / "static"
_ASSETS = {
    "/": "index.html",
    "/index.html": "index.html",
    "/static/app.js": "app.js",
    "/static/task.js": "task.js",
    "/static/styles.css": "styles.css",
}
_MAX_BODY = 64 * 1024
_MAX_EVENTS = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Suppress tracebacks for clients that close SSE/HTTP sockets early."""

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(
            exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)
        ):
            return
        super().handle_error(request, client_address)


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
        indicator: Any | None = None,
    ) -> None:
        self.config = config
        self.storage = Storage(db_path)
        self.indicator = indicator
        self.native_hotkey = False
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
        self._run_conversations: dict[str, str | None] = {}
        self._controls: dict[str, RunControl] = {}
        self._control_lock = threading.Lock()
        self._subscribers: dict[int, tuple[str | None, queue_module.Queue]] = {}
        self._subscriber_lock = threading.Lock()
        self._subscriber_seq = 0
        self._confirm_lock = threading.Lock()
        self._confirm_requests: dict[str, dict[str, Any]] = {}
        self._confirm_events: dict[str, threading.Event] = {}
        self._confirm_timeout = 300.0
        self._stopping = threading.Event()

    def start(self) -> str:
        handler = _make_handler(self)
        self._httpd = _QuietThreadingHTTPServer((self.host, self.port), handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()
        return f"http://{self.host}:{self.port}/"

    def stop(self) -> None:
        self._stopping.set()
        with self._subscriber_lock:
            subscribers = list(self._subscribers.items())
            self._subscribers.clear()
        for _, (_, channel) in subscribers:
            try:
                channel.put_nowait(None)
            except queue_module.Full:
                pass
        with self._confirm_lock:
            for event in self._confirm_events.values():
                event.set()
            self._confirm_events.clear()
            self._confirm_requests.clear()
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
            "conversations": self.storage.list_conversations(30),
            "queue": self.queue_snapshot(),
            "ai": self.ai.bootstrap_summary(),
            "hotkey": {
                "enabled": bool(self.native_hotkey),
                "key": "F8",
            },
            "screen_indicator": bool(
                self.indicator is not None and self.indicator.available
            ),
            "audit": {
                "mode": self.config.audit_ai.mode,
                "modes": ["ai", "confirm_all"],
                "model_provider_id": self.config.audit_ai.model_provider_id,
                "model_id": self.config.audit_ai.model_id,
                "fail_closed": self.config.audit_ai.fail_closed,
            },
            "config": {
                "adapters": sorted(self.config.adapters),
                "default_adapter": self.config.default_adapter,
                "model": {
                    "provider": self.config.model.provider,
                    "model": self.config.model.model,
                    "vision": is_vision_model(self.config.model.model),
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

    # -- conversations ------------------------------------------------

    def create_conversation(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self.storage.create_conversation(
            title=str(fields.get("title") or ""),
            adapter=str(fields.get("adapter") or self.config.default_adapter),
            audit_mode=str(fields.get("audit_mode") or self.config.audit_ai.mode),
        )

    def update_conversation(
        self, conversation_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        if fields.get("audit_mode") not in {None, "ai", "confirm_all"}:
            raise ValueError("audit_mode must be ai or confirm_all")
        if "adapter" in fields and fields["adapter"] not in self.config.adapters:
            raise ValueError(f"unknown adapter: {fields['adapter']}")
        updated = self.storage.update_conversation(conversation_id, fields)
        if updated is None:
            raise ValueError("conversation not found")
        return updated

    def delete_conversation(self, conversation_id: str) -> None:
        self.storage.delete_conversation(conversation_id)

    def conversation_detail(self, conversation_id: str) -> dict[str, Any] | None:
        conversation = self.storage.get_conversation(conversation_id)
        if conversation is None:
            return None
        tasks = [
            task
            for task in self.storage.list_queue("queued")
            + self.storage.list_queue("running")
            if task["conversation_id"] == conversation_id
        ]
        conversation["queue"] = tasks
        for run in conversation.get("runs", []):
            control = self._control_for(run["id"])
            if control is not None:
                snapshot = control.snapshot()
                run["paused"] = bool(snapshot["paused"])
                run["pause_reason"] = snapshot["pause_reason"]
                run["pending_instructions"] = snapshot["pending_instructions"]
        return conversation

    # -- live events --------------------------------------------------

    def subscribe(
        self, conversation_id: str | None
    ) -> tuple[int, queue_module.Queue]:
        channel: queue_module.Queue = queue_module.Queue(maxsize=2000)
        with self._subscriber_lock:
            self._subscriber_seq += 1
            subscriber_id = self._subscriber_seq
            self._subscribers[subscriber_id] = (conversation_id, channel)
        return subscriber_id, channel

    def unsubscribe(self, subscriber_id: int) -> None:
        with self._subscriber_lock:
            self._subscribers.pop(subscriber_id, None)

    def _publish(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        run_id: str | None = None,
        conversation_id: str | None = None,
        persist: bool = True,
    ) -> None:
        message = {
            "id": None,
            "event": event,
            "payload": payload,
            "run_id": run_id,
            "conversation_id": conversation_id,
            "ts": _now(),
        }
        if persist and run_id:
            try:
                message["id"] = self.storage.append_run_event(
                    run_id, conversation_id, event, payload
                )
            except Exception:
                message["id"] = None
        with self._subscriber_lock:
            subscribers = list(self._subscribers.values())
        for filter_id, channel in subscribers:
            if filter_id is not None and filter_id != conversation_id:
                continue
            try:
                channel.put_nowait(message)
            except queue_module.Full:
                try:
                    channel.get_nowait()
                except queue_module.Empty:
                    pass
                try:
                    channel.put_nowait(message)
                except queue_module.Full:
                    pass

    # -- user confirmation --------------------------------------------

    def _confirm_action(
        self,
        run_id: str,
        action: Any,
        prompt: str,
        mandatory: bool,
        auto_confirm: bool,
    ) -> bool:
        if auto_confirm and not mandatory:
            return True
        return self.request_confirmation(run_id, action, prompt)

    def request_confirmation(self, run_id: str, action, prompt: str) -> bool:
        conversation_id = self._run_conversations.get(run_id)
        request_id = uuid.uuid4().hex[:12]
        signal = threading.Event()
        record: dict[str, Any] = {
            "id": request_id,
            "run_id": run_id,
            "conversation_id": conversation_id,
            "prompt": prompt,
            "action": action.to_dict(),
            "created_at": _now(),
            "approved": None,
            "timeout_seconds": self._confirm_timeout,
        }
        with self._confirm_lock:
            self._confirm_requests[request_id] = record
            self._confirm_events[request_id] = signal
        self._publish(
            "confirm_request",
            {key: record[key] for key in (
                "id",
                "run_id",
                "conversation_id",
                "prompt",
                "action",
                "timeout_seconds",
                "created_at",
            )},
            run_id=run_id,
            conversation_id=conversation_id,
        )
        signalled = signal.wait(timeout=self._confirm_timeout)
        with self._confirm_lock:
            self._confirm_requests.pop(request_id, None)
            self._confirm_events.pop(request_id, None)
        approved = bool(signalled and record.get("approved"))
        self._publish(
            "confirm_result",
            {"id": request_id, "approved": approved},
            run_id=run_id,
            conversation_id=conversation_id,
        )
        return approved

    def resolve_confirmation(self, request_id: str, approved: bool) -> bool:
        with self._confirm_lock:
            record = self._confirm_requests.get(request_id)
            signal = self._confirm_events.get(request_id)
            if record is None or signal is None:
                return False
            record["approved"] = bool(approved)
            signal.set()
            return True

    def _deny_pending_confirmations(self, run_id: str) -> None:
        with self._confirm_lock:
            pending = [
                (request_id, signal)
                for request_id, signal in self._confirm_events.items()
                if self._confirm_requests.get(request_id, {}).get("run_id")
                == run_id
            ]
            for request_id, signal in pending:
                record = self._confirm_requests.get(request_id)
                if record is not None:
                    record["approved"] = False
                signal.set()

    # -- run control --------------------------------------------------

    def _control_for(self, run_id: str) -> RunControl | None:
        with self._control_lock:
            return self._controls.get(run_id)

    def pause_run(self, run_id: str) -> bool:
        control = self._control_for(run_id)
        if control is None:
            raise ValueError("run is not active")
        control.pause("用户暂停")
        self._publish(
            "pause",
            {"run_id": run_id, "paused": True},
            run_id=run_id,
            conversation_id=self._run_conversations.get(run_id),
            persist=False,
        )
        return True

    def resume_run(self, run_id: str) -> bool:
        control = self._control_for(run_id)
        if control is None:
            raise ValueError("run is not active")
        control.resume()
        self._publish(
            "pause",
            {"run_id": run_id, "paused": False},
            run_id=run_id,
            conversation_id=self._run_conversations.get(run_id),
            persist=False,
        )
        return True

    def cancel_run(self, run_id: str) -> bool:
        control = self._control_for(run_id)
        if control is None:
            raise ValueError("run is not active")
        control.cancel()
        self._deny_pending_confirmations(run_id)
        return True

    def toggle_pause_active(self) -> bool:
        """Toggle the foreground run for the global F8 hotkey."""
        with self._control_lock:
            controls = list(self._controls.items())
        if len(controls) != 1:
            return False
        run_id, control = controls[0]
        try:
            if control.paused:
                self.resume_run(run_id)
            else:
                self.pause_run(run_id)
        except ValueError:
            return False
        return True

    def _uses_screen(self, adapter_name: str) -> bool:
        adapter = self.config.adapters.get(adapter_name)
        return adapter is not None and adapter.kind in {
            "windows_mcp",
            "computer_use_mcp",
        }

    def instruct_run(self, run_id: str, text: str) -> str:
        control = self._control_for(run_id)
        if control is None:
            raise ValueError("run is not active")
        instruction = control.add_instruction(text)
        if not instruction:
            raise ValueError("instruction is required")
        return instruction

    # -- task queue ---------------------------------------------------

    def queue_snapshot(self) -> dict[str, Any]:
        return {
            "queued": self.storage.list_queue("queued"),
            "running": self.storage.list_queue("running"),
        }

    def jump_queue(self, task_id: str, preempt: bool = False) -> dict[str, Any]:
        if not self.storage.jump_queue_task(task_id):
            raise ValueError("task is not queued")
        if preempt:
            with self._runs_lock:
                active_ids = [
                    run_id
                    for run_id, entry in self._runs.items()
                    if entry.get("status") == "running"
                ]
            for run_id in active_ids:
                control = self._control_for(run_id)
                if control is not None:
                    control.preempt()
                    self._deny_pending_confirmations(run_id)
        snapshot = self.queue_snapshot()
        task = self.storage.get_queue_task(task_id)
        self._publish(
            "queue",
            snapshot,
            conversation_id=(
                str(task["conversation_id"]) if task else None
            ),
            persist=False,
        )
        return snapshot

    def remove_queue_task(self, task_id: str) -> dict[str, Any]:
        task = self.storage.get_queue_task(task_id)
        if task is None or task["status"] != "queued":
            raise ValueError("task is not queued")
        self.storage.remove_queue_task(task_id)
        snapshot = self.queue_snapshot()
        self._publish(
            "queue",
            snapshot,
            conversation_id=str(task["conversation_id"]),
            persist=False,
        )
        return snapshot

    # -- runs ---------------------------------------------------------

    def start_run(
        self,
        goal: str,
        adapter_name: str,
        auto_confirm: bool,
        *,
        conversation_id: str | None = None,
        audit_mode: str | None = None,
    ) -> dict[str, Any]:
        goal = goal.strip()
        if not goal:
            raise ValueError("goal is required")
        conversation = None
        if conversation_id:
            conversation = self.storage.get_conversation(
                conversation_id, include_messages=False
            )
            if conversation is None:
                raise ValueError("conversation not found")
            if not adapter_name:
                adapter_name = conversation["adapter"]
        if adapter_name not in self.config.adapters:
            raise ValueError(f"unknown adapter: {adapter_name}")
        mode = (
            audit_mode
            or (conversation["audit_mode"] if conversation else None)
            or self.config.audit_ai.mode
        )
        if mode not in {"ai", "confirm_all"}:
            raise ValueError("audit_mode must be ai or confirm_all")

        if not self._active.acquire(blocking=False):
            task = self.storage.enqueue_task(
                conversation_id or "", goal, adapter_name, mode
            )
            self._publish(
                "queue_added",
                {"task": task},
                conversation_id=conversation_id,
            )
            return {"queued": True, "task": task}

        try:
            run_id = self._launch_run(
                goal,
                adapter_name,
                bool(auto_confirm),
                conversation_id,
                mode,
            )
        except Exception:
            self._active.release()
            raise
        return {"id": run_id, "queued": False}

    def _launch_run(
        self,
        goal: str,
        adapter_name: str,
        auto_confirm: bool,
        conversation_id: str | None,
        audit_mode: str,
        task_id: str | None = None,
    ) -> str:
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
            "conversation_id": conversation_id,
            "audit_mode": audit_mode,
            "created_at": _now(),
            "events": [],
        }
        with self._runs_lock:
            self._runs[run_id] = entry
        self._run_conversations[run_id] = conversation_id
        try:
            if conversation_id:
                conversation = self.storage.get_conversation(
                    conversation_id, include_messages=False
                )
                if conversation is None:
                    raise ValueError("conversation not found")
                if not conversation["title"]:
                    self.storage.update_conversation(
                        conversation_id, {"title": goal[:60]}
                    )
                self.storage.add_message(
                    conversation_id,
                    "user",
                    goal,
                    "goal",
                    {
                        "run_id": run_id,
                        "adapter": adapter_name,
                        "audit_mode": audit_mode,
                    },
                )
            self.storage.create_run(
                run_id, goal, adapter_name, conversation_id
            )
            if task_id:
                self.storage.set_queue_status(task_id, "running", run_id)
                entry["queue_task_id"] = task_id
            threading.Thread(
                target=self._execute_run,
                args=(
                    run_id,
                    goal,
                    adapter_name,
                    auto_confirm,
                    conversation_id,
                    audit_mode,
                    task_id,
                ),
                daemon=True,
            ).start()
        except Exception:
            with self._runs_lock:
                self._runs.pop(run_id, None)
            self._run_conversations.pop(run_id, None)
            raise
        return run_id

    def _start_next_queued(self) -> None:
        if not self._active.acquire(blocking=False):
            return
        task = self.storage.next_queued_task()
        if task is None:
            self._active.release()
            return
        conversation_id = task["conversation_id"] or None
        try:
            self._launch_run(
                task["goal"],
                task["adapter"],
                False,
                conversation_id,
                task["audit_mode"],
                task_id=task["id"],
            )
            self._publish(
                "queue",
                self.queue_snapshot(),
                conversation_id=conversation_id,
                persist=False,
            )
        except Exception:
            self.storage.set_queue_status(task["id"], "failed")
            self._active.release()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        control = self._control_for(run_id)
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
                "conversation_id": entry.get("conversation_id"),
                "audit_mode": entry.get("audit_mode"),
                "created_at": entry["created_at"],
                "paused": bool(control.paused) if control is not None else False,
                "events": list(entry["events"]),
            }

    def _execute_run(
        self,
        run_id: str,
        goal: str,
        adapter_name: str,
        auto_confirm: bool,
        conversation_id: str | None = None,
        audit_mode: str | None = None,
        task_id: str | None = None,
    ) -> None:
        thinking: list[str] = []
        assistant: list[str] = []

        def on_event(event: str, payload: dict[str, Any]) -> None:
            with self._runs_lock:
                entry = self._runs.get(run_id)
                if entry is not None:
                    entry["events"].append(
                        {"event": event, "payload": payload, "ts": _now()}
                    )
                    del entry["events"][:-_MAX_EVENTS]
            if event == "thinking":
                thinking.append(str(payload.get("text", "")))
            elif event == "assistant":
                assistant.append(str(payload.get("text", "")))
            self._publish(
                event,
                payload,
                run_id=run_id,
                conversation_id=conversation_id,
                persist=event not in {"thinking", "assistant"},
            )

        def confirm(action, prompt: str, mandatory: bool = False) -> bool:
            return self._confirm_action(
                run_id, action, prompt, mandatory, auto_confirm
            )

        control = RunControl()
        with self._control_lock:
            self._controls[run_id] = control
        adapter = None
        status, reason, steps = "failed", "", 0
        screen = self.indicator is not None and self._uses_screen(adapter_name)
        if screen:
            try:
                self.indicator.show()
            except Exception:
                pass
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
            gate = build_audit_gate(
                self.config,
                self.storage,
                run_id=run_id,
                conversation_id=conversation_id,
                confirm=confirm,
                mode=audit_mode,
                project_root=os.getcwd(),
                workspace_roots=list(self.config.policy.allowed_paths)
                or None,
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
                confirm=confirm,
                on_event=on_event,
                gate=gate,
                control=control,
            )
            result = runtime.run(goal)
            status = result.status
            reason = result.reason
            steps = len(result.steps)
        except Exception as exc:
            reason = str(exc)
            on_event("error", {"message": reason})
        finally:
            if screen:
                try:
                    self.indicator.hide()
                except Exception:
                    pass
            with self._control_lock:
                self._controls.pop(run_id, None)
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
            if conversation_id:
                text = "".join(assistant).strip() or reason or status
                self.storage.add_message(
                    conversation_id,
                    "assistant",
                    text,
                    "result",
                    {
                        "run_id": run_id,
                        "status": status,
                        "reason": reason,
                        "steps": steps,
                        "thinking": "".join(thinking)[:8000],
                    },
                )
                self.storage.touch_conversation(conversation_id)
            if task_id:
                self.storage.set_queue_status(
                    task_id,
                    {
                        "done": "done",
                        "failed": "failed",
                        "aborted": "cancelled",
                    }.get(status, "failed"),
                )
            self._run_conversations.pop(run_id, None)
            self._publish(
                "queue",
                self.queue_snapshot(),
                conversation_id=conversation_id,
                persist=False,
            )
            self._active.release()
            self._start_next_queued()


def _make_handler(app: UiServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "XmAgentUI/0.1"
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                self._json({"error": "invalid host"}, 403)
                return
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
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
            if path == "/api/events":
                self._events(query)
                return
            if path == "/api/runs":
                self._json({"runs": app.storage.list_runs(20)})
                return
            if path == "/api/conversations":
                self._json(
                    {"conversations": app.storage.list_conversations(50)}
                )
                return
            if path == "/api/queue":
                self._json(app.queue_snapshot())
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
            if path.startswith("/api/conversations/"):
                parts = path.strip("/").split("/")
                if len(parts) == 3:
                    conversation = app.conversation_detail(parts[2])
                    if conversation is None:
                        self._json({"error": "conversation not found"}, 404)
                    else:
                        self._json(conversation)
                    return
            if path.startswith("/api/runs/"):
                parts = path.strip("/").split("/")
                if len(parts) == 3:
                    run = app.get_run(parts[2])
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
                    conversation_id = body.get("conversation_id") or None
                    audit_mode = body.get("audit_mode") or None
                    result = app.start_run(
                        goal=str(body.get("goal", "")),
                        adapter_name=str(body.get("adapter", "")),
                        auto_confirm=bool(body.get("auto_confirm")),
                        conversation_id=(
                            str(conversation_id) if conversation_id else None
                        ),
                        audit_mode=str(audit_mode) if audit_mode else None,
                    )
                    self._json(result, 202)
                    return
                if path == "/api/conversations":
                    self._json(
                        {"conversation": app.create_conversation(body)},
                        201,
                    )
                    return
                if path == "/api/conversations/update":
                    conversation_id = str(body.get("id", ""))
                    self._json(
                        {
                            "conversation": app.update_conversation(
                                conversation_id, body
                            )
                        }
                    )
                    return
                if path == "/api/conversations/delete":
                    app.delete_conversation(str(body.get("id", "")))
                    self._json({"ok": True})
                    return
                if path == "/api/confirm":
                    request_id = str(
                        body.get("request_id") or body.get("id") or ""
                    )
                    resolved = app.resolve_confirmation(
                        request_id, bool(body.get("approve"))
                    )
                    if not resolved:
                        self._json({"error": "confirmation not found"}, 404)
                    else:
                        self._json({"ok": True})
                    return
                if path == "/api/queue/jump":
                    self._json(
                        app.jump_queue(
                            str(body.get("id", "")),
                            bool(body.get("preempt")),
                        )
                    )
                    return
                if path == "/api/queue/remove":
                    self._json(app.remove_queue_task(str(body.get("id", ""))))
                    return
                if path.startswith("/api/runs/"):
                    parts = path.strip("/").split("/")
                    if len(parts) == 4:
                        run_id, verb = parts[2], parts[3]
                        if verb == "pause":
                            app.pause_run(run_id)
                            self._json({"ok": True})
                            return
                        if verb == "resume":
                            app.resume_run(run_id)
                            self._json({"ok": True})
                            return
                        if verb == "cancel":
                            app.cancel_run(run_id)
                            self._json({"ok": True})
                            return
                        if verb == "instruct":
                            instruction = app.instruct_run(
                                run_id, str(body.get("text", ""))
                            )
                            self._json({"ok": True, "instruction": instruction})
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

        def _events(self, query: dict[str, list[str]]) -> None:
            token = (query.get("token") or [""])[0]
            if token != app.token:
                self._json({"error": "invalid token"}, 401)
                return
            conversation_id = (query.get("conversation_id") or [None])[0]
            conversation_id = conversation_id or None
            after_raw = self.headers.get("Last-Event-ID") or (
                query.get("after") or [None]
            )[0]
            try:
                after_id = int(after_raw or 0)
            except (TypeError, ValueError):
                after_id = 0
            replay = (
                app.storage.list_conversation_events(
                    conversation_id, after_id=after_id
                )
                if conversation_id
                else []
            )
            subscriber_id, channel = app.subscribe(conversation_id)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            last_id = after_id
            try:
                for item in replay:
                    if item["id"] <= last_id:
                        continue
                    last_id = item["id"]
                    self._sse_write(
                        {
                            "id": item["id"],
                            "event": item["event"],
                            "payload": item["payload"],
                            "run_id": item["run_id"],
                            "conversation_id": item["conversation_id"],
                            "ts": item["created_at"],
                        }
                    )
                while not app._stopping.is_set():
                    try:
                        message = channel.get(timeout=15)
                    except queue_module.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    if message is None:
                        break
                    message_id = message.get("id")
                    if message_id is not None and message_id <= last_id:
                        continue
                    if message_id is not None:
                        last_id = int(message_id)
                    self._sse_write(message)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.unsubscribe(subscriber_id)

        def _sse_write(self, message: dict[str, Any]) -> None:
            lines: list[str] = []
            if message.get("id") is not None:
                lines.append(f"id: {message['id']}")
            lines.append(f"event: {message.get('event') or 'message'}")
            lines.append(
                "data: " + json.dumps(message, ensure_ascii=False)
            )
            lines.append("")
            lines.append("")
            self.wfile.write("\n".join(lines).encode("utf-8"))
            self.wfile.flush()

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
