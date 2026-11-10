from __future__ import annotations

import copy
import inspect
import json
import mimetypes
import os
import queue as queue_module
import re
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..adapters.auto import (
    AUTO_ADAPTER,
    SCREEN_KINDS,
    AutoAdapter,
    adapter_kind,
    auto_available,
    auto_chain,
    choose_primary,
)
from ..ai.catalog import is_vision_model
from ..ai.service import AiService
from ..audit import AuditLog
from ..bootstrap import (
    BROWSER_ADAPTER,
    build_adapter,
    build_audit_gate,
    build_browser_channel,
    build_provider,
    ensure_vision,
    policy_from_config,
    probe_adapter_kind,
)
from ..channels import ChannelHub
from ..config import AgentConfig
from ..context import compact_context, snapshot_context
from ..control import RunControl
from ..lock import LeaseLock
from ..resources import ResourceManager, UpdateChecker, Upgrader, make_prober
from ..runtime import AgentRuntime
from ..storage import Storage
from ..summary import FINISH_LABELS, final_summary, one_line
from ..vision import capture_primary_screen

STATIC_DIR = Path(__file__).parent / "static"
_ASSETS = {
    "/": "index.html",
    "/index.html": "index.html",
    "/static/app.js": "app.js",
    "/static/task.js": "task.js",
    "/static/browser.js": "browser.js",
    "/static/styles.css": "styles.css",
}
_MAX_BODY = 64 * 1024
_MAX_EVENTS = 300
_TITLE_SYSTEM_PROMPT = (
    "你是任务命名助手。根据用户目标和执行结果，用不超过 16 个汉字概括任务。"
    "只输出任务名称，不要引号、标点或解释。"
)
_SCREEN_ASK_PROMPT = (
    "你是桌面任务的屏幕问答助手。用户暂停了任务，就当前屏幕截图提出问题。"
    "只依据截图里看得见的内容回答；看不到或不确定的信息直接说明看不出来，"
    "不要猜测，也不要建议执行未确认的操作。"
    "用简体中文回答，2–4 句话；需要列举时用短列表。不要输出 JSON。"
)
_SCREEN_ASK_HIDE_DELAY = 0.35
_RESUME_TEXT = re.compile(
    r"^(继续|继续任务|继续吧|可以继续|已登录|已经登录|登录好了|登录完成|"
    r"我登录好了|我登录完成了|好了|弄好了|ok|okay|continue|resume)"
    r"\s*[。.!！]?$",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _accepts_image(complete: Any) -> bool:
    try:
        parameters = inspect.signature(complete).parameters
    except (TypeError, ValueError):
        return False
    if "image_path" in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _clean_task_title(text: str) -> str:
    line = " ".join(str(text or "").strip().split())
    if not line:
        return ""
    if line.startswith(("{", "[")) or '"verdict"' in line or "```" in line:
        return ""
    line = line.strip(" \t\"'“”‘’《》【】[]()（）:：-—.")
    if len(line) < 2:
        return ""
    return line[:20]


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
        notifier: Any | None = None,
        desktop: Any | None = None,
    ) -> None:
        self.config = config
        self.storage = Storage(db_path)
        self.indicator = indicator
        self.notifier = notifier
        self.desktop = desktop
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
        self._resource_probes: dict[str, dict[str, Any]] = {}
        self._run_conversations: dict[str, str | None] = {}
        self._controls: dict[str, RunControl] = {}
        self._control_lock = threading.Lock()
        self._screen_runs: set[str] = set()
        self._screen_lock = threading.Lock()
        self._subscribers: dict[int, tuple[str | None, queue_module.Queue]] = {}
        self._subscriber_lock = threading.Lock()
        self._subscriber_seq = 0
        self._confirm_lock = threading.Lock()
        self._confirm_requests: dict[str, dict[str, Any]] = {}
        self._confirm_events: dict[str, threading.Event] = {}
        self._confirm_timeout = 300.0
        self._stopping = threading.Event()
        self.updates = UpdateChecker()
        self._update_lock = threading.Lock()
        self._update_cache: dict[str, Any] = {"checked_at": "", "candidates": []}
        # 升级链路：旁路装 → 探针 → 切换，探针用的命令会被改到旁路目录。
        self.upgrader = Upgrader(
            self.resources,
            self.updates,
            prober=make_prober(config, self.resources),
        )
        # 浏览器跨任务复用：profile 常驻，登录态与窗口都留着。
        self.browser = build_browser_channel(config)
        self._browser_lock = threading.Lock()

    def start(self) -> str:
        handler = _make_handler(self)
        self._httpd = _QuietThreadingHTTPServer((self.host, self.port), handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()
        self._start_browser_reaper()
        return f"http://{self.host}:{self.port}/"

    # ------------------------------------------------------------ 浏览器回收
    def _tick_browser_reaper(self) -> bool:
        """空闲回收的一拍。任务在跑、或有人在看镜像时都不动它。"""
        channel = self.browser
        if channel is None or self._stopping.is_set():
            return False
        if self._active.locked():
            return False
        try:
            retired = bool(channel.retire_if_idle())
        except Exception:  # noqa: BLE001 - 回收失败不该影响面板
            return False
        if retired:
            self._publish(
                "browser",
                {"event": "retired", **channel.status()},
                persist=False,
            )
        return retired

    def _start_browser_reaper(self) -> None:
        """后台定时回收空闲的浏览器内核。

        内核常驻会白占几百 MB；`keep_alive_seconds` 就是「上次用完还能留多久」。
        配成负数表示永不自动回收（调试或希望窗口一直开着时用）。
        """
        channel = self.browser
        if channel is None:
            return
        keep = int(getattr(channel, "keep_alive_seconds", 0) or 0)
        if keep < 0:
            return
        interval = max(5, min(60, keep // 3 or 10))

        def loop() -> None:
            while not self._stopping.wait(interval):
                self._tick_browser_reaper()

        threading.Thread(
            target=loop, daemon=True, name="browser-reaper"
        ).start()

    def browser_snapshot(self) -> dict[str, Any]:
        channel = self.browser
        if channel is None:
            return {"enabled": False, "started": False}
        data = channel.status()
        data["enabled"] = True
        return data

    def browser_action(
        self, action: str, url: str = ""
    ) -> dict[str, Any]:
        channel = self.browser
        if channel is None:
            return {"ok": False, "error": "浏览器通道未启用"}
        name = str(action or "").strip().lower()
        if name == "open":
            receipt = channel.execute(
                "browser.open", {"url": url or "about:blank"}
            )
        elif name in {"handoff", "close"}:
            receipt = channel.execute("browser.session", {"action": name})
        else:
            return {"ok": False, "error": f"未知动作: {name}"}
        data = dict(receipt.detail or {})
        data["ok"] = receipt.ok
        if receipt.error:
            data["error"] = receipt.error
        return data

    def browser_frames(
        self,
    ) -> tuple[int, queue_module.Queue] | tuple[None, None]:
        """给 SSE 连接挂一个帧订阅，返回 (订阅号, 帧队列)。"""
        channel = self.browser
        if channel is None:
            return None, None
        frames: queue_module.Queue = queue_module.Queue(maxsize=4)

        def push(data_url: str, meta: dict[str, Any]) -> None:
            # 只留最新几帧：用户看的是「现在」，积压只会制造延迟
            if frames.full():
                try:
                    frames.get_nowait()
                except queue_module.Empty:
                    pass
            try:
                frames.put_nowait((data_url, meta))
            except queue_module.Full:
                pass
        subscriber = channel.subscribe(push)
        return subscriber, frames

    def resource_updates(self, refresh: bool = False) -> dict[str, Any]:
        """查上游有没有新版本；`auto` 档顺手把可升级的排队升掉。

        `auto` 不是「静默换掉」——升级本身还要过探针，失败保留旧版，
        进度在资源面板里可见（见 TECH_DESIGN 4.22.15）。
        """
        mode = str(getattr(self.config.resources, "update_check", "notify"))
        if mode == "off":
            return {"mode": "off", "checked_at": "", "candidates": []}
        with self._update_lock:
            cached = self._update_cache
            fresh = bool(cached.get("checked_at")) and (
                time.time() - float(cached.get("ts") or 0) < 600
            )
            if fresh and not refresh:
                return {
                    "mode": mode,
                    "checked_at": cached["checked_at"],
                    "candidates": list(cached["candidates"]),
                }
            try:
                found = self.updates.check(
                    self.resources.resources, refresh=refresh
                )
                payload = [item.to_dict() for item in found]
                error = ""
            except Exception as exc:  # noqa: BLE001 - 查版本失败不该拖垮面板
                found, payload = [], []
                error = f"{exc.__class__.__name__}: {exc}"
            self._update_cache = {
                "ts": time.time(),
                "checked_at": _now(),
                "candidates": payload,
            }
        data: dict[str, Any] = {
            "mode": mode,
            "checked_at": self._update_cache["checked_at"],
            "candidates": payload,
        }
        if error:
            data["error"] = error
        if mode == "auto":
            data["started"] = self._auto_upgrade(found)
        return data

    def _auto_upgrade(self, candidates: list[Any]) -> list[str]:
        """自动升级的入口：只挑同大版本、且不在失败冷却期里的候选。

        有任务在跑就不动——切换入口会影响下一次拉起后端。
        """
        if self._active.locked():
            return []
        try:
            pending = self.upgrader.pending(candidates)
        except Exception:  # noqa: BLE001 - 判断失败就当没有可升级项
            return []
        if not pending:
            return []
        snapshot = self.start_upgrade(
            only=[item.resource_id for item in pending], candidates=pending
        )
        job = snapshot.get("job") or {}
        return list(job.get("upgrades") or [])

    def start_upgrade(
        self,
        only: list[str] | None = None,
        force: bool = False,
        *,
        version: str = "",
        candidates: list[Any] | None = None,
    ) -> dict[str, Any]:
        """后台升级：一次一条资源，进度写进与安装检测同一个 job。"""
        ids = [str(item) for item in only or []]
        with self._resource_lock:
            if self._resource_job and self._resource_job.get("running"):
                return self.resources_snapshot()
            states = [self._resource_dict(state) for state in self.resources.status()]
            job_id = uuid.uuid4().hex[:12]
            self._resource_job = {
                "id": job_id,
                "kind": "upgrade",
                "running": True,
                "phase": "detecting",
                "percent": 1,
                "message": "正在检查上游版本",
                "started_at": _now(),
                "finished_at": None,
                "ok": None,
                "error": None,
                "upgrades": ids,
                "resources": states,
            }
        threading.Thread(
            target=self._run_upgrade,
            args=(job_id, ids, bool(force), version, list(candidates or [])),
            daemon=True,
        ).start()
        return self.resources_snapshot()

    def _run_upgrade(
        self,
        job_id: str,
        only: list[str],
        force: bool,
        version: str,
        candidates: list[Any],
    ) -> None:
        def update(**fields: Any) -> None:
            with self._resource_lock:
                job = self._resource_job
                if job is None or job.get("id") != job_id:
                    return
                for key, value in fields.items():
                    if key == "percent" and isinstance(value, (int, float)):
                        job["percent"] = max(
                            int(job.get("percent") or 0),
                            min(100, int(value)),
                        )
                    else:
                        job[key] = value

        def on_progress(event: dict[str, Any]) -> None:
            resource_id = str(event.get("resource") or "")
            message = str(event.get("message") or "")
            if not message:
                return
            with self._resource_lock:
                job = self._resource_job
                if job is None or job.get("id") != job_id:
                    return
                job["phase"] = str(event.get("phase") or job.get("phase") or "")
                job["message"] = message
                rows = job.get("resources") or []
                for row in rows:
                    if row.get("id") == resource_id:
                        row["status"] = "installing"
                        row["detail"] = message
                        break

        try:
            pool = list(candidates)
            if not pool:
                update(message="正在检查上游版本")
                pool = self.upgrader.candidates()
            if version and only:
                item = self.upgrader.candidate_for(only[0], version)
                picked = [item] if item is not None else []
            elif only:
                wanted = set(only)
                picked = [item for item in pool if item.resource_id in wanted]
            elif force:
                # 强制升级 = 绕过兼容门禁，用户已经明确要求了
                picked = [item for item in pool if self.upgrader.is_newer(item.resource_id, item.latest)]
            else:
                picked = self.upgrader.pending(pool)

            results: list[dict[str, Any]] = []
            total = max(1, len(picked))
            for index, item in enumerate(picked):
                update(
                    message=f"正在升级 {item.name} 到 {item.latest}",
                    percent=int(index / total * 90) + 2,
                )
                outcome = self.upgrader.upgrade(
                    item.resource_id,
                    candidate=item,
                    force=force,
                    progress=on_progress,
                )
                payload = outcome.to_dict()
                results.append(payload)
                update(
                    message=outcome.detail
                    or f"{item.name} {payload['status']}",
                    percent=int((index + 1) / total * 90) + 2,
                )
                fresh = next(
                    (
                        self._resource_dict(one)
                        for one in self.resources.status()
                        if one.id == item.resource_id
                    ),
                    None,
                )
                if fresh is not None:
                    with self._resource_lock:
                        job = self._resource_job
                        if job is not None and job.get("id") == job_id:
                            for row in job.get("resources") or []:
                                if row.get("id") == item.resource_id:
                                    row.update(fresh)
                                    break
            with self._resource_lock:
                job = self._resource_job
                if job is None or job.get("id") != job_id:
                    return
                job["running"] = False
                job["phase"] = "done"
                job["percent"] = 100
                job["finished_at"] = _now()
                job["upgrades"] = results
                if not picked:
                    job["ok"] = True
                    job["message"] = "没有可升级的新版本"
                else:
                    failed = [
                        item for item in results if item["status"] == "failed"
                    ]
                    job["ok"] = not failed
                    if failed:
                        job["message"] = (
                            "升级未完成："
                            + "、".join(
                                f"{item['name']}（{item['detail']}）"
                                for item in failed
                            )
                            + "；旧版本仍在使用"
                        )
                    else:
                        job["message"] = "；".join(
                            f"{item['name']} → {item['to']}"
                            for item in results
                            if item["status"] == "upgraded"
                        ) or "已是最新版本"
        except Exception as exc:  # noqa: BLE001 - 升级失败不能让面板卡住
            update(
                running=False,
                phase="error",
                finished_at=_now(),
                ok=False,
                error=str(exc),
                message=f"升级失败：{exc}",
            )

    def _hub_for(self, adapter: Any) -> ChannelHub:
        hub = ChannelHub(desktop=adapter)
        with self._browser_lock:
            if self.browser is not None:
                self.browser.retire_if_idle()
                # 只留一个标签页：任务过程中开出来的多余标签不带到下一次，
                # 当前这一页留着——用户可能正站在上面等 AI 接手
                self.browser.focus_single_tab()
                hub.register(self.browser)
        return hub

    def stop(self) -> None:
        self._stopping.set()
        with self._browser_lock:
            if self.browser is not None:
                try:
                    self.browser.close()
                except Exception:
                    pass
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

    def _vision_models(self) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for provider in self.storage.list_ai_providers():
            for model in provider.get("models", []):
                if model.get("vision"):
                    found.append(
                        (str(provider["id"]), str(model["model_id"]))
                    )
        return found

    def _is_vision_model(self, provider_id: str, model_id: str) -> bool:
        return any(
            item["model_id"] == model_id and item.get("vision")
            for item in self.storage.list_ai_models(provider_id)
        )

    def audit_snapshot(self) -> dict[str, Any]:
        provider_id, model_id = self.storage.get_audit_model()
        explicit = bool(provider_id) and self._is_vision_model(
            provider_id, model_id
        )
        follow = False
        if not explicit:
            provider_id = model_id = ""
            active = self.storage.get_active_ai()
            if (
                active is not None
                and active["model"].get("vision")
            ):
                provider_id = str(active["provider"]["id"])
                model_id = str(active["model"]["model_id"])
                follow = True
        if not provider_id:
            vision_models = self._vision_models()
            if vision_models:
                provider_id, model_id = vision_models[0]
        return {
            "mode": self.config.audit_ai.mode,
            "modes": ["ai", "confirm_all"],
            "model_provider_id": provider_id,
            "model_id": model_id,
            "follow_executor": follow,
            "vision": bool(provider_id),
            "vision_required": True,
            "vision_available": bool(self._vision_models()),
            "fail_closed": self.config.audit_ai.fail_closed,
        }

    def ai_choices(self) -> list[dict[str, Any]]:
        return [
            {
                "id": provider["id"],
                "name": provider["name"],
                "models": [
                    {
                        "model_id": model["model_id"],
                        "display_name": model.get("display_name")
                        or model["model_id"],
                        "vision": bool(model.get("vision")),
                    }
                    for model in provider.get("models", [])
                ],
            }
            for provider in self.storage.list_ai_providers()
        ]

    def bootstrap(self) -> dict[str, Any]:
        api_key = os.environ.get(self.config.model.api_key_env)
        return {
            "token": self.token,
            "profile": self.storage.get_profile(),
            "runs": self.storage.list_runs(20),
            "conversations": self.storage.list_conversations(30),
            "queue": self.queue_snapshot(),
            "ai": self.ai.bootstrap_summary(),
            "ai_choices": self.ai_choices(),
            "hotkey": {
                "enabled": bool(self.native_hotkey),
                "key": "F8",
            },
            "screen_indicator": bool(
                self.indicator is not None and self.indicator.available
            ),
            "desktop_handoff": bool(
                self.config.runtime.release_desktop
                and self.desktop is not None
                and self.desktop.available
            ),
            "audit": self.audit_snapshot(),
            "config": {
                "adapters": sorted(
                    set(self.config.adapters)
                    | (
                        {BROWSER_ADAPTER}
                        if self.config.browser.enabled
                        else set()
                    )
                ),
                "auto_available": auto_available(self.config),
                "default_adapter": self.config.default_adapter,
                "model": {
                    "provider": self.config.model.provider,
                    "model": self.config.model.model,
                    "vision": is_vision_model(self.config.model.model),
                    "api_key_env": self.config.model.api_key_env,
                    "api_key_present": bool(api_key),
                    "context_window": self.config.model.context_window,
                },
                "audit_root": self.config.audit.root,
                "db_path": str(self.storage.path.resolve()),
            },
        }

    def _resource_dict(self, state: Any) -> dict[str, Any]:
        data = state.to_dict()
        probe = self._resource_probes.get(state.id) if state.ok else None
        if probe:
            data["runtime_ok"] = probe.get("ok")
            data["runtime_detail"] = probe.get("detail", "")
            data["runtime_tools"] = list(probe.get("tools") or [])
            data["runtime_checked_at"] = probe.get("checked_at", "")
        return data

    def resources_snapshot(self) -> dict[str, Any]:
        states = [self._resource_dict(state) for state in self.resources.status()]
        with self._resource_lock:
            job = copy.deepcopy(self._resource_job)
        return {
            "root": str(self.resources.root),
            "resources": states,
            "pins": self.resources.pins(),
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
            states = [self._resource_dict(state) for state in self.resources.status()]
            job_id = uuid.uuid4().hex[:12]
            self._resource_job = {
                "id": job_id,
                "kind": "sync",
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
            with self._resource_lock:
                job = self._resource_job
                if job is not None and job.get("id") == job_id:
                    job["percent"] = 90
                    job["phase"] = "probe"
                    job["message"] = "结构检查完成，正在检测运行可用性"
            self._probe_resources(states, job_id, on_progress)
            failed = [
                state
                for state in states
                if state.required
                and (not state.ok or state.runtime_ok is False)
            ]
            optional_failed = [
                state
                for state in states
                if not state.required
                and (not state.ok or state.runtime_ok is False)
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
                        "检测完成，以下必需资源不可用："
                        + "、".join(state.name for state in failed)
                        + "（可强制重装失败项）"
                    )
                elif optional_failed:
                    job["message"] = (
                        "检测完成，必需资源可用；可选资源未就绪："
                        + "、".join(state.name for state in optional_failed)
                    )
                else:
                    job["message"] = "检测完成，资源结构完整且运行可用"
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

    def _probe_resources(
        self,
        states: list[Any],
        job_id: str,
        on_progress: Any,
    ) -> None:
        ready = [state for state in states if state.ok]
        total = len(ready)
        for index, state in enumerate(ready):
            with self._resource_lock:
                job = self._resource_job
                if job is None or job.get("id") != job_id:
                    return
                job["message"] = f"正在检测 {state.name} 运行可用性"
                job["percent"] = max(
                    int(job.get("percent") or 0),
                    90 + int(index / max(1, total) * 9),
                )
            try:
                resource = next(
                    (
                        item
                        for item in self.resources.resources
                        if item.id == state.id
                    ),
                    None,
                )
                if resource is not None and resource.kind == "browser":
                    # 内核不是 MCP 服务，探针要跑 --version，不能当适配器启动
                    ok, detail = self.resources.probe_browser(state.id)
                    result = {"ok": ok, "detail": detail, "tools": []}
                else:
                    result = probe_adapter_kind(
                        self.config, state.id, timeout=20
                    )
            except Exception as exc:
                result = {
                    "ok": False,
                    "detail": f"检测异常：{exc}",
                    "tools": [],
                }
            state.runtime_ok = bool(result.get("ok"))
            state.runtime_detail = str(result.get("detail") or "")
            state.runtime_tools = list(result.get("tools") or [])
            state.runtime_checked_at = _now()
            self._resource_probes[state.id] = {
                "ok": state.runtime_ok,
                "detail": state.runtime_detail,
                "tools": list(state.runtime_tools),
                "checked_at": state.runtime_checked_at,
            }
            on_progress(
                {
                    "event": "probe",
                    "resource": state.id,
                    "state": state.to_dict(),
                    "message": f"{state.name} 运行可用性："
                    + ("通过" if state.runtime_ok else "失败"),
                    "percent": min(99, 90 + int((index + 1) / max(1, total) * 9)),
                }
            )

    def _adapter_allowed(self, name: str) -> bool:
        if name in self.config.adapters:
            return True
        if name == BROWSER_ADAPTER:
            return self.config.browser.enabled and self.browser is not None
        return name == AUTO_ADAPTER and auto_available(self.config)

    def _validate_model_selection(
        self, provider_id: str, model_id: str
    ) -> None:
        if not provider_id and not model_id:
            return
        if not provider_id or not model_id:
            raise ValueError("model provider and model must be selected together")
        if self.storage.get_ai_provider(provider_id) is None:
            raise ValueError("unknown AI provider")
        models = {
            item["model_id"]
            for item in self.storage.list_ai_models(provider_id)
        }
        if model_id not in models:
            raise ValueError("unknown model for this provider")

    def _validate_audit_model_selection(
        self, provider_id: str, model_id: str
    ) -> None:
        if not provider_id and not model_id:
            return
        self._validate_model_selection(provider_id, model_id)
        if not self._is_vision_model(provider_id, model_id):
            raise ValueError("顶层审计模型必须支持图像输入（识图）")

    def save_profile(self, fields: dict[str, Any]) -> dict[str, Any]:
        adapter = str(
            fields.get("default_adapter", self.config.default_adapter)
        )
        if not self._adapter_allowed(adapter):
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
        adapter = str(fields.get("adapter") or self.config.default_adapter)
        if not self._adapter_allowed(adapter):
            raise ValueError(f"unknown adapter: {adapter}")
        return self.storage.create_conversation(
            title=str(fields.get("title") or ""),
            adapter=adapter,
            audit_mode=str(fields.get("audit_mode") or self.config.audit_ai.mode),
        )

    def update_conversation(
        self, conversation_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        if fields.get("audit_mode") not in {None, "ai", "confirm_all"}:
            raise ValueError("audit_mode must be ai or confirm_all")
        if "adapter" in fields and not self._adapter_allowed(
            str(fields["adapter"])
        ):
            raise ValueError(f"unknown adapter: {fields['adapter']}")
        if "title" in fields:
            title = str(fields.get("title") or "").strip()
            if not title:
                raise ValueError("title must not be empty")
            fields = {**fields, "title": title[:60]}
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

    def conversation_context(
        self, conversation_id: str
    ) -> dict[str, Any] | None:
        conversation = self.storage.get_conversation(
            conversation_id, include_messages=False
        )
        if conversation is None:
            return None
        snapshot = snapshot_context(
            self.storage,
            conversation_id,
            context_window=self.config.model.context_window,
        )
        return {"conversation_id": conversation_id, **snapshot.to_dict()}

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
        # The user cannot answer from a minimised window, so bring the
        # console back for the duration of the prompt.
        if self.desktop is not None and self._run_owns_screen(run_id):
            try:
                self.desktop.reclaim()
            except Exception:
                pass
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
        control = self._control_for(run_id)
        if (
            self.desktop is not None
            and self._run_owns_screen(run_id)
            and (control is None or not control.paused)
        ):
            try:
                self.desktop.release()
            except Exception:
                pass
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
        if self.indicator is not None:
            try:
                self.indicator.set_paused(True)
            except Exception:
                pass
        if self.desktop is not None and self._run_owns_screen(run_id):
            try:
                self.desktop.reclaim()
            except Exception:
                pass
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
        if self.indicator is not None:
            try:
                self.indicator.set_paused(False)
            except Exception:
                pass
        if self.desktop is not None and self._run_owns_screen(run_id):
            try:
                self.desktop.release()
            except Exception:
                pass
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
        if adapter_name == AUTO_ADAPTER:
            return any(
                adapter.kind in SCREEN_KINDS
                for adapter in self.config.adapters.values()
            )
        adapter = self.config.adapters.get(adapter_name)
        return adapter is not None and adapter.kind in SCREEN_KINDS

    # -- desktop handoff ----------------------------------------------

    def _release_desktop(self, run_id: str) -> None:
        """Minimise the console so a screen run owns the desktop."""
        if not self.config.runtime.release_desktop:
            return
        with self._screen_lock:
            self._screen_runs.add(run_id)
        if self.desktop is None:
            return
        try:
            self.desktop.release()
        except Exception:
            pass

    def _reclaim_desktop(self, run_id: str) -> None:
        """Bring the console back once no screen run is left."""
        if not self.config.runtime.release_desktop:
            return
        with self._screen_lock:
            self._screen_runs.discard(run_id)
            busy = bool(self._screen_runs)
        if busy or self.desktop is None:
            return
        try:
            self.desktop.reclaim()
        except Exception:
            pass

    def _run_owns_screen(self, run_id: str) -> bool:
        with self._screen_lock:
            return run_id in self._screen_runs

    def instruct_run(self, run_id: str, text: str) -> str:
        control = self._control_for(run_id)
        if control is None:
            raise ValueError("run is not active")
        instruction = control.add_instruction(text)
        if not instruction:
            raise ValueError("instruction is required")
        return instruction

    # -- screenshot question ------------------------------------------

    def _run_entry(self, run_id: str) -> dict[str, Any] | None:
        with self._runs_lock:
            return self._runs.get(run_id)

    def _capture_for_question(self) -> dict[str, Any] | None:
        """Grab the screen without the console in the way, then put it back."""
        hidden = False
        if (
            self.config.runtime.release_desktop
            and self.desktop is not None
            and self.desktop.available
        ):
            try:
                hidden = bool(self.desktop.release())
            except Exception:
                hidden = False
            if hidden:
                time.sleep(_SCREEN_ASK_HIDE_DELAY)
        try:
            return capture_primary_screen(
                self.config.runtime.screenshot_dir, prefix="ask"
            )
        finally:
            if hidden:
                try:
                    self.desktop.reclaim()
                except Exception:
                    pass

    def ask_screen(self, run_id: str, question: str) -> dict[str, Any]:
        """Answer a question about the current screen while the run is paused.

        Only a paused run may ask: the screen must be still, otherwise the
        answer describes a frame the agent already changed.
        """
        text = " ".join(str(question or "").split()).strip()
        if not text:
            raise ValueError("请输入要问的问题")
        entry = self._run_entry(run_id)
        if entry is None:
            raise ValueError("run not found")
        control = self._control_for(run_id)
        if control is None:
            raise ValueError("任务已结束，无法对屏幕提问")
        if not control.paused:
            raise ValueError("请先暂停任务（F8），再对屏幕提问")
        provider = build_provider(
            self.config,
            str(entry.get("adapter") or self.config.default_adapter),
            storage=self.storage,
            source="ui",
            run_id=run_id,
            model_provider_id=str(entry.get("model_provider_id") or ""),
            model_id=str(entry.get("model_id") or ""),
        )
        vision = bool(
            getattr(provider, "provider_vision", False)
            or getattr(provider, "vision", False)
        )
        if not vision:
            raise ValueError("当前模型不支持图像输入，请切换到可识图的模型")
        shot = self._capture_for_question()
        if shot is None:
            raise ValueError("截图失败：当前环境无法访问桌面")
        complete = getattr(provider, "complete", None)
        if complete is None:
            raise ValueError("当前模型不支持问答")
        try:
            if _accepts_image(complete):
                answer = complete(
                    _SCREEN_ASK_PROMPT,
                    f"用户的问题：{text}",
                    max_tokens=800,
                    image_path=shot["path"],
                )
            else:
                answer = complete(
                    _SCREEN_ASK_PROMPT, f"用户的问题：{text}", max_tokens=800
                )
        except Exception as exc:
            raise ValueError(f"截图提问失败：{exc}") from exc
        answer = str(answer or "").strip()
        if not answer:
            raise ValueError("模型没有返回内容")
        return {
            "answer": answer,
            "model": str(
                getattr(provider, "provider_model", "")
                or getattr(provider, "model", "")
                or entry.get("model")
                or ""
            ),
            "screenshot": shot.get("path"),
            "width": shot.get("width"),
            "height": shot.get("height"),
        }

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

    def _pending_await_user(
        self, conversation_id: str
    ) -> dict[str, Any] | None:
        runs = self.storage.list_runs(1, conversation_id)
        if not runs or runs[0].get("status") != "needs_human":
            return None
        for message in reversed(self.storage.list_messages(conversation_id)):
            if message.get("role") == "user":
                return None
            if message.get("role") == "assistant":
                info = (message.get("payload") or {}).get("await_user")
                if info:
                    return {"run": runs[0], "await_user": info}
        return None

    def start_run(
        self,
        goal: str,
        adapter_name: str,
        auto_confirm: bool,
        *,
        conversation_id: str | None = None,
        audit_mode: str | None = None,
        model_provider_id: str = "",
        model_id: str = "",
        resume: bool = False,
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
        pending_await = None
        if conversation_id and (resume or _RESUME_TEXT.match(goal)):
            pending_await = self._pending_await_user(conversation_id)
        if resume and pending_await is None:
            raise ValueError("当前没有等待你处理的任务")
        run_goal = goal
        context_note = ""
        if pending_await is not None:
            run_goal = str(pending_await["run"].get("goal") or goal)
            kind = str(pending_await["await_user"].get("kind") or "input")
            label = "登录" if kind == "login" else "人工处理"
            context_note = (
                f"用户已完成暂停事项（{label}），并回复：{goal}。"
                "请从当前屏幕继续原任务，不要从头开始。"
            )
        if not self._adapter_allowed(adapter_name):
            raise ValueError(f"unknown adapter: {adapter_name}")
        mode = (
            audit_mode
            or (conversation["audit_mode"] if conversation else None)
            or self.config.audit_ai.mode
        )
        if mode not in {"ai", "confirm_all"}:
            raise ValueError("audit_mode must be ai or confirm_all")
        model_provider_id = str(model_provider_id or "")
        model_id = str(model_id or "")
        self._validate_model_selection(model_provider_id, model_id)

        if not self._active.acquire(blocking=False):
            task = self.storage.enqueue_task(
                conversation_id or "",
                run_goal,
                adapter_name,
                mode,
                model_provider_id,
                model_id,
                user_message=goal,
                context_note=context_note,
            )
            self._publish(
                "queue_added",
                {"task": task},
                conversation_id=conversation_id,
            )
            return {"queued": True, "task": task}

        try:
            run_id = self._launch_run(
                run_goal,
                adapter_name,
                bool(auto_confirm),
                conversation_id,
                mode,
                model_provider_id=model_provider_id,
                model_id=model_id,
                message=goal,
                context_note=context_note,
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
        model_provider_id: str = "",
        model_id: str = "",
        message: str | None = None,
        context_note: str = "",
    ) -> str:
        run_id = uuid.uuid4().hex[:12]
        active = self.ai.active_summary()
        if model_id:
            run_model = model_id
        elif active is not None:
            run_model = active["model"]
        elif self.config.model.provider in {"openai_compat", "openai"}:
            run_model = self.config.model.model
        else:
            run_model = "heuristic-mock"
        entry = {
            "id": run_id,
            "goal": goal[:500],
            "adapter": adapter_name,
            "resolved_adapter": "",
            "status": "running",
            "reason": "",
            "steps": 0,
            "model": run_model,
            "model_provider_id": model_provider_id,
            "model_id": model_id,
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
                    self.storage.set_auto_title(
                        conversation_id, goal.strip()[:24]
                    )
                self.storage.add_message(
                    conversation_id,
                    "user",
                    message if message is not None else goal,
                    "goal",
                    {
                        "run_id": run_id,
                        "adapter": adapter_name,
                        "audit_mode": audit_mode,
                        "model_provider_id": model_provider_id,
                        "model_id": model_id,
                        "resume": bool(context_note),
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
                    model_provider_id,
                    model_id,
                    context_note,
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
                model_provider_id=task.get("model_provider_id") or "",
                model_id=task.get("model_id") or "",
                message=task.get("user_message") or None,
                context_note=task.get("context_note") or "",
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
                "resolved_adapter": entry.get("resolved_adapter", ""),
                "status": entry["status"],
                "reason": entry["reason"],
                "steps": entry["steps"],
                "model": entry["model"],
                "model_provider_id": entry.get("model_provider_id", ""),
                "model_id": entry.get("model_id", ""),
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
        model_provider_id: str = "",
        model_id: str = "",
        context_note: str = "",
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
                persist=event not in {"thinking", "assistant", "context"},
            )

        def confirm(action, prompt: str, mandatory: bool = False) -> bool:
            return self._confirm_action(
                run_id, action, prompt, mandatory, auto_confirm
            )

        control = RunControl()
        with self._control_lock:
            self._controls[run_id] = control
        adapter = None
        status, reason, steps, summary = "failed", "", 0, ""
        await_user: dict[str, Any] | None = None
        screen = False
        needs_screen = False
        try:
            provider = build_provider(
                self.config,
                adapter_name,
                storage=self.storage,
                source="ui",
                run_id=run_id,
                model_provider_id=model_provider_id,
                model_id=model_id,
            )
            provider_model = str(
                getattr(provider, "provider_model", "")
                or getattr(provider, "model", "")
                or provider.name
            )
            provider_name = str(
                getattr(provider, "provider_name", "") or provider.name
            )
            vision = bool(
                getattr(provider, "provider_vision", False)
                or getattr(provider, "vision", False)
            )
            on_event(
                "model",
                {
                    "model": provider_model,
                    "provider": provider_name,
                    "vision": vision,
                },
            )
            if adapter_name == BROWSER_ADAPTER:
                # 纯浏览器模式：不挂桌面通道，不截屏、不占屏、不让出桌面
                needs_screen = False
                on_event(
                    "route",
                    {
                        "mode": "browser",
                        "adapter": BROWSER_ADAPTER,
                        "reason": "内置浏览器通道，全程不占用桌面",
                        "chain": [],
                    },
                )
            elif adapter_name == AUTO_ADAPTER:
                primary, route_reason = choose_primary(
                    self.config, goal, provider, vision_available=vision
                )
                ensure_vision(self.config, primary, provider)
                chain = auto_chain(
                    self.config, primary, vision_available=vision
                )
                with self._runs_lock:
                    entry = self._runs.get(run_id)
                    if entry is not None:
                        entry["resolved_adapter"] = primary
                on_event(
                    "route",
                    {
                        "mode": "auto",
                        "adapter": primary,
                        "reason": route_reason,
                        "chain": chain,
                    },
                )
                adapter = AutoAdapter(
                    lambda name: build_adapter(self.config, name),
                    chain,
                    on_switch=lambda payload: on_event(
                        "route", {**payload, "mode": "auto"}
                    ),
                )
                needs_screen = any(
                    adapter_kind(self.config, name) in SCREEN_KINDS
                    for name in chain
                )
            else:
                adapter = build_adapter(self.config, adapter_name)
                needs_screen = self._uses_screen(adapter_name)
            if needs_screen:
                self._release_desktop(run_id)
            if self.indicator is not None and needs_screen:
                screen = True
                try:
                    self.indicator.show()
                except Exception:
                    pass
            task_context = ""
            if conversation_id:
                try:
                    snapshot = compact_context(
                        self.storage,
                        conversation_id,
                        goal=goal,
                        context_window=self.config.model.context_window,
                        provider=provider,
                    )
                    task_context = snapshot.prompt()
                    on_event("context", snapshot.to_dict())
                except Exception:
                    task_context = ""
            if context_note:
                task_context = (
                    f"{task_context}\n\n{context_note}".strip()
                    if task_context
                    else context_note
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
                executor=(provider, provider_name, provider_model),
            )
            hub = self._hub_for(adapter)
            runtime = AgentRuntime(
                adapter=adapter,
                provider=provider,
                hub=hub,
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
                max_provider_retries=self.config.runtime.max_provider_retries,
                provider_retry_delay=(
                    self.config.runtime.provider_retry_delay
                ),
                confirm=confirm,
                on_event=on_event,
                gate=gate,
                control=control,
            )
            result = runtime.run(goal, context=task_context)
            status = result.status
            reason = result.reason
            steps = len(result.steps)
            await_user = result.await_user
            summary = result.summary or final_summary(
                status, reason, result.steps
            )
        except Exception as exc:
            reason = str(exc)
            summary = final_summary("failed", reason, [])
            on_event("error", {"message": reason})
        finally:
            if screen:
                try:
                    self.indicator.hide()
                except Exception:
                    pass
            if needs_screen:
                self._reclaim_desktop(run_id)
            with self._control_lock:
                self._controls.pop(run_id, None)
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    pass
            # 任务结束不关浏览器：把真窗口提到最前，用户可以直接在里面接着操作
            if self.browser is not None:
                try:
                    self.browser.bring_to_front()
                except Exception:  # noqa: BLE001 - 前置失败不影响任务结果
                    pass
            if conversation_id:
                text = (
                    summary
                    or "".join(assistant).strip()
                    or reason
                    or status
                )
                self.storage.add_message(
                    conversation_id,
                    "assistant",
                    text,
                    "result",
                    {
                        "run_id": run_id,
                        "status": status,
                        "reason": reason,
                        "summary": summary,
                        "steps": steps,
                        "thinking": "".join(thinking)[:8000],
                        **({"await_user": await_user} if await_user else {}),
                    },
                )
                self.storage.touch_conversation(conversation_id)
            # 顺序要紧：先落消息、再宣告 run 终态。反过来的话，轮询方一看到
            # needs_human 就来读对话，很可能读到的最后一条还是用户那句「继续」，
            # 界面上就变成「任务暂停了但没有 AI 的提示」。这是真的踩到过的。
            self.storage.finish_run(run_id, status, reason, steps)
            with self._runs_lock:
                entry = self._runs.get(run_id)
                if entry is not None:
                    entry["status"] = status
                    entry["reason"] = reason
                    entry["steps"] = steps
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
            self._notify_finished(goal, status, summary, reason)
            self._active.release()
            self._start_next_queued()
            if conversation_id:
                threading.Thread(
                    target=self._after_run,
                    args=(run_id, conversation_id, goal),
                    daemon=True,
                ).start()

    def _notify_finished(
        self,
        goal: str,
        status: str,
        summary: str,
        reason: str,
    ) -> None:
        if self.notifier is None:
            return
        label = FINISH_LABELS.get(status, "任务结束")
        goal_text = one_line(goal, 28)
        title = f"{label} · {goal_text}" if goal_text else label
        body = one_line(summary or reason or "任务已结束", 200)
        try:
            self.notifier.notify(title, body)
        except Exception:
            return

    def _after_run(
        self, run_id: str, conversation_id: str, goal: str
    ) -> None:
        provider = None
        try:
            conversation = self.storage.get_conversation(
                conversation_id, include_messages=False
            )
            if conversation is not None:
                provider = build_provider(
                    self.config,
                    conversation["adapter"],
                    storage=self.storage,
                    source="context",
                    run_id=run_id,
                )
        except Exception:
            provider = None
        try:
            snapshot = compact_context(
                self.storage,
                conversation_id,
                context_window=self.config.model.context_window,
                provider=provider,
            )
            self._publish(
                "context",
                {"conversation_id": conversation_id, **snapshot.to_dict()},
                conversation_id=conversation_id,
                persist=False,
            )
        except Exception:
            pass
        self._suggest_title(run_id, conversation_id, goal, provider)

    def _suggest_title(
        self,
        run_id: str,
        conversation_id: str,
        goal: str,
        provider: Any | None = None,
    ) -> None:
        try:
            conversation = self.storage.get_conversation(
                conversation_id, include_messages=False
            )
            if (
                conversation is None
                or conversation.get("title_source") == "user"
                or len(conversation.get("runs") or []) != 1
            ):
                return
            messages = self.storage.list_messages(conversation_id)
            result = ""
            for message in reversed(messages):
                if message["role"] == "assistant":
                    result = str(message.get("content") or "")
                    break
            if provider is None:
                provider = build_provider(
                    self.config,
                    conversation["adapter"],
                    storage=self.storage,
                    source="title",
                    run_id=run_id,
                )
            prompt = f"目标：{goal.strip()[:500]}\n结果：{result.strip()[:600]}"
            text = provider.complete(
                _TITLE_SYSTEM_PROMPT, prompt, max_tokens=32
            )
            title = _clean_task_title(text)
            if not title:
                return
            self.storage.set_auto_title(conversation_id, title)
            self._publish(
                "conversation_title",
                {"conversation_id": conversation_id, "title": title},
                conversation_id=conversation_id,
                persist=False,
            )
        except Exception:
            return


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
            if path == "/api/browser":
                self._json(app.browser_snapshot())
                return
            if path == "/api/resources/updates":
                refresh = (query.get("refresh") or [""])[0] not in {"", "0"}
                self._json(app.resource_updates(refresh=refresh))
                return
            if path == "/api/browser/stream":
                self._browser_stream(query)
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
                if len(parts) == 4 and parts[3] == "context":
                    snapshot = app.conversation_context(parts[2])
                    if snapshot is None:
                        self._json({"error": "conversation not found"}, 404)
                    else:
                        self._json(snapshot)
                    return
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
                self._drain_body()
                self._json({"error": "invalid host"}, 403)
                return
            if self.headers.get("X-XM-Token") != app.token:
                self._drain_body()
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
                        model_provider_id=str(
                            body.get("model_provider_id") or ""
                        ),
                        model_id=str(body.get("model_id") or ""),
                        resume=bool(body.get("resume")),
                    )
                    self._json(result, 202)
                    return
                if path == "/api/browser":
                    self._json(
                        app.browser_action(
                            str(body.get("action") or ""),
                            str(body.get("url") or ""),
                        )
                    )
                    return
                if path == "/api/audit/model":
                    provider_id = str(body.get("provider_id") or "")
                    model_id = str(body.get("model_id") or "")
                    if provider_id:
                        app._validate_audit_model_selection(
                            provider_id, model_id
                        )
                    app.storage.set_audit_model(
                        provider_id, model_id if provider_id else ""
                    )
                    self._json({"audit": app.audit_snapshot()})
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
                        if verb == "ask_screen":
                            self._json(
                                app.ask_screen(
                                    run_id, str(body.get("question", ""))
                                )
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
                if path == "/api/resources/upgrade":
                    only = body.get("only")
                    if only is not None and not isinstance(only, list):
                        raise ValueError("only must be a list of resource ids")
                    self._json(
                        app.start_upgrade(
                            [str(item) for item in only or []],
                            bool(body.get("force")),
                            version=str(body.get("version") or ""),
                        ),
                        202,
                    )
                    return
                if path == "/api/resources/rollback":
                    resource_id = str(body.get("id") or "")
                    if not resource_id:
                        raise ValueError("id is required")
                    outcome = app.upgrader.rollback(resource_id)
                    self._json(
                        {**outcome.to_dict(), "resources": app.resources_snapshot()}
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

        def _browser_stream(self, query: dict[str, list[str]]) -> None:
            """浏览器画面流。只在抽屉/全屏视图打开时连接，断开即停流。"""
            token = (query.get("token") or [""])[0]
            if token != app.token:
                self._json({"error": "invalid token"}, 401)
                return
            subscriber, frames = app.browser_frames()
            if subscriber is None or frames is None:
                self._json({"error": "browser channel disabled"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                self._sse_write(
                    {"event": "browser_ready", "payload": app.browser_snapshot()}
                )
                while True:
                    try:
                        data_url, meta = frames.get(timeout=15.0)
                    except queue_module.Empty:
                        self._sse_write({"event": "browser_ping", "payload": {}})
                        continue
                    self._sse_write(
                        {
                            "event": "browser_frame",
                            "payload": {
                                "image": data_url,
                                "ts": (meta or {}).get("timestamp"),
                            },
                        }
                    )
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                channel = app.browser
                if channel is not None:
                    channel.unsubscribe(subscriber)

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

        def _drain_body(self) -> None:
            """把请求体读干净再回错误。

            不读就关连接，客户端可能还没发完 body，会收到 TCP RST
            （表现为 ConnectionAbortedError 而不是干净的 401）。
            """
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except (TypeError, ValueError):
                length = 0
            if 0 < length <= _MAX_BODY:
                try:
                    self.rfile.read(length)
                except OSError:
                    pass

        def _drain_body(self) -> None:
            """把请求体读干净再回错误。

            不读就关连接，客户端可能还没发完 body，会收到 TCP RST
            （表现为 ConnectionAbortedError 而不是干净的 401）。
            """
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except (TypeError, ValueError):
                length = 0
            if 0 < length <= _MAX_BODY:
                try:
                    self.rfile.read(length)
                except OSError:
                    pass

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
