from __future__ import annotations

import sys
from pathlib import Path

from xm_agent.bootstrap import probe_adapter_kind
from xm_agent.config import AdapterConfig, AgentConfig

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "examples" / "fake_mcp_server.py"


def _config(kind: str, command: list[str]) -> AgentConfig:
    config = AgentConfig()
    config.adapters = {
        "probe": AdapterConfig(kind=kind, command=command),
    }
    config.default_adapter = "probe"
    return config


def test_probe_completes_real_mcp_handshake():
    result = probe_adapter_kind(
        _config("windows_mcp", [sys.executable, str(SERVER)]),
        "windows_mcp",
        timeout=10,
    )
    assert result["ok"] is True
    assert "Snapshot" in result["tools"]
    assert "握手成功" in result["detail"]


def test_probe_reports_startup_crash_with_stderr():
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('missing dependency\\n'); sys.exit(3)",
    ]
    result = probe_adapter_kind(
        _config("windows_mcp", command), "windows_mcp", timeout=10
    )
    assert result["ok"] is False
    assert "启动失败" in result["detail"]
    assert "missing dependency" in result["detail"]


def test_probe_reports_missing_adapter_and_command():
    config = _config("windows_mcp", [sys.executable, str(SERVER)])

    missing_kind = probe_adapter_kind(config, "xshell_mcp")
    assert missing_kind["ok"] is False
    assert "没有对应的后端" in missing_kind["detail"]

    config.adapters["probe"] = AdapterConfig(kind="windows_mcp")
    missing_command = probe_adapter_kind(config, "windows_mcp")
    assert missing_command["ok"] is False
    assert "没有配置启动命令" in missing_command["detail"]
