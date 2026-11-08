from __future__ import annotations

import os

from .adapters.computer_use_mcp import ComputerUseMcpAdapter
from .adapters.mcp_tool import McpToolAdapter
from .adapters.mock import MockAdapter
from .adapters.windows_mcp import WindowsMcpAdapter
from .adapters.xshell_mcp import XshellMcpAdapter
from .config import AgentConfig
from .mcp_stdio import MCPError, StdioMCPClient
from .ai.factory import (
    build_active_provider,
    usage_recorder,
)
from .policy import Policy
from .providers.fake import HeuristicMockProvider
from .providers.openai_compat import OpenAICompatProvider
from .storage import Storage

_ADAPTERS = {
    "windows_mcp": WindowsMcpAdapter,
    "xshell_mcp": XshellMcpAdapter,
    "computer_use_mcp": ComputerUseMcpAdapter,
}


def build_adapter(config: AgentConfig, name: str):
    adapter_config = config.adapters.get(name)
    if adapter_config is None:
        raise RuntimeError(f"adapter is not configured: {name}")
    if adapter_config.kind == "mock":
        return MockAdapter()
    if adapter_config.kind not in {"mcp_stdio", *_ADAPTERS}:
        raise RuntimeError(f"unsupported adapter kind: {adapter_config.kind}")
    if not adapter_config.command:
        raise RuntimeError(f"adapter {name!r} has no command")
    env = None
    if adapter_config.env:
        env = {**os.environ, **adapter_config.env}
    try:
        client = StdioMCPClient(
            adapter_config.command, env=env, cwd=adapter_config.cwd
        )
    except (OSError, MCPError) as exc:
        raise RuntimeError(f"failed to start MCP server: {exc}") from exc
    adapter_class = _ADAPTERS.get(adapter_config.kind, McpToolAdapter)
    return adapter_class(
        client,
        tools=adapter_config.tools,
        tool_args=adapter_config.tool_args,
    )


def build_provider(
    config: AgentConfig,
    adapter_name: str,
    storage: Storage | None = None,
    source: str = "ui",
    run_id: str | None = None,
):
    if storage is not None:
        active = build_active_provider(
            storage,
            timeout_seconds=config.model.timeout_seconds,
            temperature=config.model.temperature,
            source=source,
            run_id=run_id,
        )
        if active is not None:
            return active
    if config.model.provider == "fake":
        if adapter_name != "mock":
            raise RuntimeError(
                "model.provider=fake only supports the mock adapter; "
                "configure an active AI model in the desktop console or set "
                "[model] provider=openai_compat"
            )
        return HeuristicMockProvider()
    if adapter_name == "mock":
        return HeuristicMockProvider()
    if config.model.provider in {"openai_compat", "openai"}:
        recorder = None
        if storage is not None:
            recorder = usage_recorder(
                storage,
                "config",
                "config",
                config.model.model,
                source,
                run_id,
            )
        return OpenAICompatProvider(
            base_url=config.model.base_url,
            model=config.model.model,
            api_key_env=config.model.api_key_env,
            timeout_seconds=config.model.timeout_seconds,
            temperature=config.model.temperature,
            vision=config.model.vision,
            on_usage=recorder,
        )
    raise RuntimeError(f"unsupported model provider: {config.model.provider}")


def policy_from_config(config: AgentConfig) -> Policy:
    return Policy(
        allowed_capabilities=config.policy.allowed_capabilities,
        allowed_command_prefixes=config.policy.allowed_command_prefixes,
        require_confirmation=config.policy.require_confirmation,
        allowed_paths=config.policy.allowed_paths,
    )


__all__ = ["build_adapter", "build_provider", "policy_from_config"]
