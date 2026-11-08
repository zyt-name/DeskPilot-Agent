"""Read-only probe for a stdio MCP server.

Example:
    .venv/Scripts/python.exe scripts/probe_windows_mcp.py --list
    .venv/Scripts/python.exe scripts/probe_windows_mcp.py --snapshot
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xm_agent.mcp_stdio import MCPError, StdioMCPClient  # noqa: E402

SERVER = ROOT / "vendor" / "Windows-MCP" / ".venv" / "Scripts" / "python.exe"
SERVER_ARGS = ["-m", "windows_mcp", "serve"]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="print tools/list")
    parser.add_argument("--snapshot", action="store_true", help="call Snapshot read-only")
    parser.add_argument("--parse", action="store_true", help="parse snapshot with adapter")
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="run one observe-only runtime step (finishes immediately)",
    )
    parser.add_argument("--limit", type=int, default=40, help="snapshot lines to print")
    args = parser.parse_args()

    if not SERVER.exists():
        print(f"missing server interpreter: {SERVER}", file=sys.stderr)
        return 2

    env = dict(os.environ)
    env["POSTHOG_API_KEY"] = ""  # disable upstream telemetry
    env["PYTHONIOENCODING"] = "utf-8"

    client = StdioMCPClient(
        [str(SERVER), *SERVER_ARGS],
        env=env,
        cwd=str(ROOT / "vendor" / "Windows-MCP"),
        default_timeout=120,
    )
    try:
        print("initialize: ok")

        if args.list:
            tools = client.list_tools()
            print(f"tools: {len(tools)}")
            for tool in tools:
                schema = tool.get("inputSchema") or {}
                required = schema.get("required") or []
                props = list((schema.get("properties") or {}).keys())
                print(f"- {tool.get('name')} required={required} props={props}")

        if args.snapshot:
            result = client.call_tool(
                "Snapshot",
                {
                    "use_vision": False,
                    "use_ui_tree": True,
                    "use_annotation": False,
                },
            )
            text = "\n".join(
                str(part.get("text", ""))
                for part in result.get("content", [])
                if isinstance(part, dict) and part.get("type") == "text"
            )
            stripped = text.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    text = "\n".join(str(part) for part in parsed)
            print(f"snapshot: {len(text)} chars")
            if args.parse:
                from xm_agent.adapters.windows_mcp import parse_snapshot

                elements, windows, active = parse_snapshot(text)
                print(
                    f"parsed: elements={len(elements)} windows={len(windows)} "
                    f"active={active!r}"
                )
                for element in elements[:10]:
                    print(
                        f"  {element.id} {element.bounds[:2]} "
                        f"{element.role} {element.name[:40]!r}"
                    )
            lines = text.splitlines()
            for line in lines[: args.limit]:
                print(line)
            if len(lines) > args.limit:
                print(f"... ({len(lines) - args.limit} more lines)")

        if args.runtime:
            from xm_agent.adapters.windows_mcp import WindowsMcpAdapter
            from xm_agent.audit import AuditLog
            from xm_agent.contracts import CAPABILITIES
            from xm_agent.lock import LeaseLock
            from xm_agent.policy import Policy
            from xm_agent.providers.fake import ScriptedProvider
            from xm_agent.runtime import AgentRuntime
            from xm_agent.types import Action

            adapter = WindowsMcpAdapter(client)
            runtime = AgentRuntime(
                adapter=adapter,
                provider=ScriptedProvider(
                    [Action("task.finish", {"summary": "observe-only probe"})]
                ),
                policy=Policy(
                    allowed_capabilities=list(CAPABILITIES),
                    allowed_command_prefixes=[],
                    require_confirmation=[],
                    allowed_paths=[str(ROOT)],
                ),
                audit=AuditLog(ROOT / ".xm-agent" / "audit"),
                lock=LeaseLock(ROOT / ".xm-agent" / "agent.lock"),
                max_steps=2,
                confirm=lambda action, reason: True,
            )
            result = runtime.run("probe the desktop read-only")
            observation = result.steps[0].observation if result.steps else None
            print(
                f"runtime: status={result.status} steps={len(result.steps)} "
                f"elements={len(observation.elements) if observation else 0} "
                f"active={observation.active_window if observation else None!r}"
            )
    except (MCPError, TimeoutError) as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
