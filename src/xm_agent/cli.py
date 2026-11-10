from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .adapters.mock import MockAdapter
from .audit import AuditLog
from .bootstrap import (
    build_adapter,
    build_hub,
    build_provider,
    policy_from_config,
)
from .channels import ChannelHub
from .config import AgentConfig, load_config
from .contracts import CAPABILITIES
from .lock import LeaseLock
from .maintenance import cleanup as cleanup_files
from .notify import SystemNotifier
from .policy import Policy
from .providers.fake import ScriptedProvider
from .runtime import AgentRuntime
from .storage import Storage
from .summary import FINISH_LABELS, one_line
from .types import Action


def _print_event(event: str, payload: dict[str, Any]) -> None:
    if event == "start":
        print(f"[start] {payload['goal']} (trace={payload['trace_id']})")
    elif event == "observation":
        summary = str(payload["summary"])[:180]
        print(
            f"[observe] step {payload['step']}: {summary} "
            f"({payload['elements']} elements)"
        )
    elif event == "step":
        error = f" error={payload['error']}" if payload.get("error") else ""
        print(
            f"[step {payload['step']}] {payload['capability']} "
            f"receipt={payload['receipt_ok']} verify={payload['verify_ok']}{error}"
        )
    elif event == "result":
        print(
            f"[result] {payload['status']}: {payload['reason']} "
            f"(steps={payload['steps']}, trace={payload['trace_id']})"
        )
    elif event == "lock_busy":
        print("[busy] desktop lock is held by another process")


def _interactive_confirm(
    action: Action, reason: str, mandatory: bool = False
) -> bool:
    print(f"\n[confirm] {reason}")
    print(f"  capability: {action.capability}")
    print(f"  args: {json.dumps(action.args, ensure_ascii=False)}")
    try:
        answer = input("允许执行? [y/N] ").strip().casefold()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _build_adapter(config: AgentConfig, name: str):
    try:
        return build_adapter(config, name)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


def _build_provider(
    config: AgentConfig,
    adapter_name: str,
    storage: Storage | None = None,
):
    try:
        return build_provider(
            config,
            adapter_name,
            storage=storage,
            source="cli",
            run_id=None,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


def _policy_from_config(config: AgentConfig) -> Policy:
    return policy_from_config(config)


def _safe_cleanup(config: AgentConfig) -> None:
    """Prune expired audit logs and screenshots before a long-running command."""

    try:
        report = cleanup_files(config)
    except Exception:
        return
    if report["removed"]:
        print(
            f"[cleanup] 已清理 {report['removed']} 个过期文件"
            f"（保留 {report['keep_days']} 天）"
        )


def _cmd_cleanup(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    report = cleanup_files(
        config, keep_days=args.keep_days, dry_run=args.dry_run
    )
    verb = "将删除" if report["dry_run"] else "已删除"
    print(
        f"保留 {report['keep_days']} 天；{verb} {report['removed']} 个文件，"
        f"释放 {report['freed_bytes']} 字节"
    )
    print(f"  审计日志: {report['audit']['path']} → {report['audit']['removed']}")
    screenshots = report["screenshots"]
    print(
        f"  截图缓存: {screenshots['path']} → {screenshots['removed']}"
        f"（保留 {screenshots['kept']} 个）"
    )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _safe_cleanup(config)
    adapter_name = args.adapter or config.default_adapter
    db_path = args.db
    if db_path is None and Path(".xm-agent/app.db").is_file():
        db_path = ".xm-agent/app.db"
    storage = Storage(db_path) if db_path else None
    adapter = _build_adapter(config, adapter_name)
    provider = _build_provider(config, adapter_name, storage)
    if getattr(provider, "provider_model", ""):
        print(
            f"[model] {provider.provider_name} · {provider.provider_model}"
            f" (vision={'on' if provider.provider_vision else 'off'})"
        )
    policy = _policy_from_config(config)
    audit = AuditLog(config.audit.root, config.audit.keep_days)
    lock = LeaseLock(
        config.runtime.lock_path, config.runtime.lock_ttl_seconds
    )
    confirm = (
        (
            lambda action, reason, mandatory=False: (
                _interactive_confirm(action, reason) if mandatory else True
            )
        )
        if args.yes
        else _interactive_confirm
    )
    hub = build_hub(config, adapter)
    runtime = AgentRuntime(
        adapter=adapter,
        provider=provider,
        policy=policy,
        audit=audit,
        lock=lock,
        max_steps=config.runtime.max_steps,
        max_consecutive_failures=config.runtime.max_consecutive_failures,
        max_provider_retries=config.runtime.max_provider_retries,
        provider_retry_delay=config.runtime.provider_retry_delay,
        confirm=confirm,
        on_event=_print_event,
        hub=hub,
    )
    try:
        result = runtime.run(args.goal)
    finally:
        hub.close_all()

    print(f"trace: {result.trace_id}")
    print(f"audit: {audit.path_for(result.trace_id)}")
    label = FINISH_LABELS.get(result.status, "任务结束")
    goal_text = one_line(args.goal, 28)
    SystemNotifier().send(
        f"{label} · {goal_text}" if goal_text else label,
        result.summary or result.reason,
    )
    if result.status == "done":
        return 0
    if result.status == "needs_human":
        return 2
    return 1


def _cmd_resources(args: argparse.Namespace) -> int:
    from .resources import ResourceManager

    manager = ResourceManager(args.root)
    if args.action in {"status", "check"}:
        states = manager.status()
        if args.action == "check":
            from .bootstrap import probe_adapter_kind
            from .config import load_config

            config_path = getattr(args, "config", None)
            if config_path is None and Path("config/agent.example.toml").is_file():
                config_path = "config/agent.example.toml"
            config = load_config(config_path)
            for state in states:
                if not state.ok:
                    continue
                result = probe_adapter_kind(config, state.id, timeout=20)
                state.runtime_ok = bool(result.get("ok"))
                state.runtime_detail = str(result.get("detail") or "")
                state.runtime_tools = list(result.get("tools") or [])
        if args.json:
            print(
                json.dumps(
                    [state.to_dict() for state in states],
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"资源根目录: {manager.root}")
            print(f"{'资源':<22}{'版本':<10}{'状态':<10}说明")
            for state in states:
                detail = state.detail
                if state.runtime_ok is True:
                    detail = f"{detail}；{state.runtime_detail}"
                elif state.runtime_ok is False:
                    detail = f"{detail}；运行不可用：{state.runtime_detail}"
                print(
                    f"{state.name:<22}{state.version:<10}"
                    f"{_resource_status_label(state.status):<10}{detail}"
                )
        return (
            0
            if all(
                not state.required
                or (state.ok and state.runtime_ok is not False)
                for state in states
            )
            else 1
        )

    last: dict[str, str] = {}

    def on_progress(event: dict[str, Any]) -> None:
        resource_id = str(event.get("resource") or "")
        message = str(event.get("message") or "")
        if not message:
            return
        if event.get("event") in {"detect", "result", "error"}:
            print(f"[resources] {message}")
            last[resource_id] = message
        elif last.get(resource_id) != message:
            print(f"[resources] {message}")
            last[resource_id] = message

    states = manager.ensure(
        only=args.only, force=args.force, progress=on_progress
    )
    if args.json:
        print(
            json.dumps(
                [state.to_dict() for state in states],
                ensure_ascii=False,
                indent=2,
            )
        )
    failed = [state for state in states if state.required and not state.ok]
    if failed:
        print(
            "[resources] 必需资源未就绪："
            + "，".join(state.name for state in failed)
        )
        return 1
    return 0


def _resource_status_label(status: str) -> str:
    return {
        "ready": "就绪",
        "missing": "未安装",
        "incomplete": "需补全",
        "outdated": "版本不符",
        "error": "失败",
    }.get(status, status)


def _cmd_gui(args: argparse.Namespace) -> int:
    config_path = args.config
    if config_path is None and Path("config/agent.example.toml").is_file():
        config_path = "config/agent.example.toml"
    config = load_config(config_path)
    _safe_cleanup(config)
    from .ui import run_gui

    return run_gui(
        config,
        db_path=args.db,
        port=args.port,
        debug=args.debug,
        server_only=args.server_only,
    )


def _cmd_demo(_: argparse.Namespace) -> int:
    adapter = MockAdapter()
    provider = ScriptedProvider(
        [
            Action(
                "desktop.act",
                {"action": "type", "text": "hello"},
                "type the requested text",
                expect="hello",
            ),
            Action(
                "desktop.act",
                {"action": "click", "element_id": "e2"},
                "click the Save button",
                expect="saved",
            ),
            Action(
                "task.finish",
                {"summary": "typed hello and saved"},
                "goal complete",
            ),
        ]
    )
    policy = Policy(
        allowed_capabilities=list(CAPABILITIES),
        allowed_command_prefixes=["echo"],
        require_confirmation=["file.transfer"],
        allowed_paths=[str(Path.cwd())],
    )
    audit = AuditLog(".xm-agent/audit")
    lock = LeaseLock(".xm-agent/agent.lock")
    hub = ChannelHub(desktop=adapter)
    runtime = AgentRuntime(
        adapter=adapter,
        provider=provider,
        policy=policy,
        audit=audit,
        lock=lock,
        max_steps=6,
        max_consecutive_failures=2,
        confirm=lambda action, reason, mandatory=False: True,
        on_event=_print_event,
        hub=hub,
    )
    result = runtime.run("在记事本中输入 hello 并保存")
    print(f"trace: {result.trace_id}")
    print(f"audit: {audit.path_for(result.trace_id)}")
    return 0 if result.status == "done" else 1


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            continue


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(
        prog="xm-agent",
        description="Capability-contract computer-control agent runtime",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run one task")
    run_parser.add_argument("--goal", required=True)
    run_parser.add_argument("--config")
    run_parser.add_argument("--adapter")
    run_parser.add_argument(
        "--db",
        help="SQLite path; defaults to .xm-agent/app.db when it exists",
    )
    run_parser.add_argument(
        "--yes",
        action="store_true",
        help="auto-approve actions that require confirmation",
    )
    run_parser.set_defaults(handler=_cmd_run)

    demo_parser = subparsers.add_parser(
        "demo", help="run the offline mock demo"
    )
    demo_parser.set_defaults(handler=_cmd_demo)

    cleanup_parser = subparsers.add_parser(
        "cleanup", help="prune old audit logs and screenshots"
    )
    cleanup_parser.add_argument("--config")
    cleanup_parser.add_argument(
        "--keep-days",
        type=int,
        help="override the audit retention window (defaults to [audit].keep_days)",
    )
    cleanup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be removed without deleting anything",
    )
    cleanup_parser.set_defaults(handler=_cmd_cleanup)

    resources_parser = subparsers.add_parser(
        "resources", help="check or install the bundled MCP resources"
    )
    resources_sub = resources_parser.add_subparsers(
        dest="action", required=True
    )
    status_parser = resources_sub.add_parser(
        "status", help="show local resource status"
    )
    status_parser.add_argument("--json", action="store_true")
    status_parser.add_argument(
        "--root", default=".", help="project root (defaults to cwd)"
    )
    status_parser.set_defaults(handler=_cmd_resources)

    check_parser = resources_sub.add_parser(
        "check", help="check structure and run a real MCP handshake"
    )
    check_parser.add_argument("--json", action="store_true")
    check_parser.add_argument(
        "--root", default=".", help="project root (defaults to cwd)"
    )
    check_parser.add_argument(
        "--config",
        help="config file (defaults to config/agent.example.toml if present)",
    )
    check_parser.set_defaults(handler=_cmd_resources)

    ensure_parser = resources_sub.add_parser(
        "ensure", help="download and build missing resources"
    )
    ensure_parser.add_argument("--json", action="store_true")
    ensure_parser.add_argument(
        "--root", default=".", help="project root (defaults to cwd)"
    )
    ensure_parser.add_argument(
        "--only",
        action="append",
        help="limit to a resource id; repeat for multiple",
    )
    ensure_parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild even when the resource is already ready",
    )
    ensure_parser.set_defaults(handler=_cmd_resources)

    gui_parser = subparsers.add_parser(
        "gui", help="launch the local desktop console"
    )
    gui_parser.add_argument(
        "--config",
        help="config file (defaults to config/agent.example.toml if present)",
    )
    gui_parser.add_argument("--db", default=".xm-agent/app.db")
    gui_parser.add_argument("--port", type=int, default=0)
    gui_parser.add_argument("--debug", action="store_true")
    gui_parser.add_argument(
        "--server-only",
        action="store_true",
        help="print the local URL instead of opening a desktop window",
    )
    gui_parser.set_defaults(handler=_cmd_gui)

    args = parser.parse_args(argv)
    return int(args.handler(args))
