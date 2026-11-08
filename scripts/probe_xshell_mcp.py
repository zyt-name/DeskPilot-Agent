"""Read-only probe for the xshell-mcp server.

Example:
    .venv/Scripts/python.exe scripts/probe_xshell_mcp.py --list
    .venv/Scripts/python.exe scripts/probe_xshell_mcp.py --sessions
    .venv/Scripts/python.exe scripts/probe_xshell_mcp.py --exec pwd
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xm_agent.mcp_stdio import MCPError, StdioMCPClient  # noqa: E402

SERVER_DIR = ROOT / "vendor" / "xshell-mcp"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="print tools/list")
    parser.add_argument(
        "--sessions", action="store_true", help="call xshell_list_sessions"
    )
    parser.add_argument("--status", action="store_true", help="call xshell_status")
    parser.add_argument(
        "--exec",
        metavar="COMMAND",
        help="run a read-only command via xshell_exec (allowDangerous stays off)",
    )
    args = parser.parse_args()

    if not (SERVER_DIR / "dist" / "server.js").exists():
        print(f"missing build: {SERVER_DIR / 'dist' / 'server.js'}", file=sys.stderr)
        return 2

    env = dict(os.environ)
    env["XSHELL_MCP_CONFIG"] = str(ROOT / "config" / "xshell-mcp.json")

    client = StdioMCPClient(
        ["node", "dist/server.js"],
        env=env,
        cwd=str(SERVER_DIR),
        default_timeout=60,
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
        if args.sessions:
            result = client.call_tool("xshell_list_sessions", {})
            print(_text(result))
        if args.status:
            result = client.call_tool("xshell_status", {})
            print(_text(result))
        if args.exec:
            result = client.call_tool(
                "xshell_exec",
                {
                    "command": args.exec,
                    "sessionId": "default",
                    "shellType": "posix",
                    "timeoutMs": 20000,
                },
            )
            print(_text(result))
    except (MCPError, TimeoutError) as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


def _text(result: dict) -> str:
    return "\n".join(
        str(part.get("text", ""))
        for part in result.get("content", [])
        if isinstance(part, dict) and part.get("type") == "text"
    )


if __name__ == "__main__":
    raise SystemExit(main())
