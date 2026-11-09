from __future__ import annotations

import pytest

from xm_agent.adapters.auto import (
    AutoAdapter,
    adapter_registry,
    auto_available,
    auto_chain,
    choose_primary,
)
from xm_agent.config import AdapterConfig, AgentConfig
from xm_agent.types import Observation


def _config(*kinds: str) -> AgentConfig:
    names = {
        "windows_mcp": "windows",
        "computer_use_mcp": "computer_use",
        "xshell_mcp": "xshell",
    }
    config = AgentConfig()
    config.adapters = {
        names[kind]: AdapterConfig(kind=kind) for kind in kinds
    }
    config.default_adapter = next(iter(config.adapters))
    return config


class _Router:
    def __init__(self, reply: str | Exception = "") -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, user, *, max_tokens=800, on_delta=None):
        self.calls.append((system, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_registry_and_chain_follow_configured_backends():
    config = _config("windows_mcp", "computer_use_mcp", "xshell_mcp")
    assert adapter_registry(config) == {
        "windows_mcp": "windows",
        "computer_use_mcp": "computer_use",
        "xshell_mcp": "xshell",
    }
    assert auto_available(config) is True
    assert auto_chain(config, "windows", vision_available=True) == [
        "windows",
        "computer_use",
    ]
    assert auto_chain(config, "windows", vision_available=False) == ["windows"]
    assert auto_chain(config, "xshell", vision_available=True) == ["xshell"]

    mock_only = AgentConfig()
    assert auto_available(mock_only) is False


def test_heuristic_prefers_windows_for_desktop_and_xshell_for_remote():
    config = _config("windows_mcp", "computer_use_mcp", "xshell_mcp")
    adapter, reason = choose_primary(
        config, "打开记事本并输入 hello", None, vision_available=True
    )
    assert adapter == "windows"
    assert "Windows-MCP" in reason

    adapter, reason = choose_primary(
        config,
        "ssh 到服务器执行 systemctl restart nginx",
        None,
        vision_available=True,
    )
    assert adapter == "xshell"
    assert "远程" in reason


def test_ai_route_wins_when_backend_is_offered():
    config = _config("windows_mcp", "computer_use_mcp", "xshell_mcp")
    router = _Router('{"backend": "xshell", "reason": "需要远程终端"}')
    adapter, reason = choose_primary(
        config, "整理一下这台电脑的桌面", router, vision_available=True
    )
    assert adapter == "xshell"
    assert reason == "AI 选择 xshell：需要远程终端"
    assert router.calls and "可选后端" in router.calls[0][0]


def test_ai_route_alias_and_invalid_reply_fall_back_to_heuristic():
    config = _config("windows_mcp", "computer_use_mcp")
    assert choose_primary(
        config, "record a macro", _Router('{"backend":"windows"}'), vision_available=False
    )[0] == "windows"

    broken = choose_primary(
        config, "打开设置", _Router("not-json"), vision_available=False
    )
    assert broken[0] == "windows"

    raising = choose_primary(
        config, "打开设置", _Router(RuntimeError("timeout")), vision_available=False
    )
    assert raising[0] == "windows"


def test_computer_use_requires_vision():
    config = _config("computer_use_mcp")
    with pytest.raises(RuntimeError, match="不支持图像输入"):
        choose_primary(config, "点击屏幕上的按钮", None, vision_available=False)
    adapter, _ = choose_primary(
        config, "点击屏幕上的按钮", None, vision_available=True
    )
    assert adapter == "computer_use"

    both = _config("windows_mcp", "computer_use_mcp")
    router = _Router('{"backend": "computer_use"}')
    adapter, _ = choose_primary(
        both, "打开设置", router, vision_available=False
    )
    assert adapter == "windows"


class _Backend:
    def __init__(self, name: str, *, fail_observe: bool = False) -> None:
        self.name = name
        self.vision_only = name == "computer_use"
        self.fail_observe = fail_observe
        self.closed = False
        self.observed = 0

    def observe(self, step: int, include_screenshot: bool = False) -> Observation:
        self.observed += 1
        if self.fail_observe:
            raise RuntimeError("uia unavailable")
        return Observation(step=step, source=self.name, summary="ok")

    def close(self) -> None:
        self.closed = True


def test_auto_adapter_switches_to_fallback_on_observe_failure():
    events: list[dict] = []
    built: list[str] = []

    def factory(name: str):
        built.append(name)
        return _Backend(name, fail_observe=name == "windows")

    adapter = AutoAdapter(factory, ["windows", "computer_use"], events.append)
    view = adapter.observe(1)
    assert view.source == "computer_use"
    assert adapter.active_name == "computer_use"
    assert built == ["windows", "computer_use"]
    assert events[-1]["adapter"] == "computer_use"
    assert "已切换兜底" in events[-1]["reason"]

    adapter.observe(2)
    assert built == ["windows", "computer_use"]
    adapter.close()
    assert adapter.active_name == "auto"


def test_auto_adapter_raises_when_whole_chain_fails():
    adapter = AutoAdapter(
        lambda name: _Backend(name, fail_observe=True), ["windows"]
    )
    with pytest.raises(RuntimeError, match="uia unavailable"):
        adapter.observe(1)
