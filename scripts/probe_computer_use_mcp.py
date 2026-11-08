"""Read-only probe for domdomegg/computer-use-mcp.

Examples:
    .venv/Scripts/python.exe scripts/probe_computer_use_mcp.py --list
    .venv/Scripts/python.exe scripts/probe_computer_use_mcp.py --screenshot --save
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xm_agent.mcp_stdio import MCPError, StdioMCPClient  # noqa: E402

SERVER_DIR = ROOT / "vendor" / "computer-use-mcp"
SHOT_DIR = ROOT / ".xm-agent" / "probe"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="print tools/list")
    parser.add_argument(
        "--screenshot",
        action="store_true",
        help="call computer with action=get_screenshot (read-only)",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="write the screenshot under .xm-agent/probe/",
    )
    args = parser.parse_args()

    entry = SERVER_DIR / "dist" / "main.js"
    if not entry.exists():
        print(f"missing build: {entry}", file=sys.stderr)
        print(
            "run: cd vendor/computer-use-mcp && npm install && npm run build",
            file=sys.stderr,
        )
        return 2

    env = dict(os.environ)
    env.pop("MCP_TRANSPORT", None)
    client = StdioMCPClient(
        ["node", "dist/main.js"],
        env=env,
        cwd=str(SERVER_DIR),
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
        if args.screenshot:
            result = client.call_tool("computer", {"action": "get_screenshot"})
            text, image = _split(result)
            print(text or "(no text part)")
            if image is None:
                print("image: missing", file=sys.stderr)
                return 1
            print(f"image: {len(image)} bytes")
            if args.save:
                SHOT_DIR.mkdir(parents=True, exist_ok=True)
                path = SHOT_DIR / f"computer-use-{time.time_ns()}.png"
                path.write_bytes(image)
                print(f"saved: {path}")
    except (MCPError, TimeoutError) as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


def _split(result: dict) -> tuple[str, bytes | None]:
    texts: list[str] = []
    image: bytes | None = None
    for item in result.get("content", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            texts.append(str(item.get("text", "")))
        elif item.get("type") == "image" and item.get("data"):
            image = base64.b64decode(item["data"])
    text = "\n".join(part for part in texts if part).strip()
    if text:
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    return text, image


if __name__ == "__main__":
    raise SystemExit(main())
