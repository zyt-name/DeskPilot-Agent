from __future__ import annotations

import os
from typing import Any

from .adapters.computer_use_mcp import ComputerUseMcpAdapter
from .adapters.mcp_tool import McpToolAdapter
from .adapters.mock import MockAdapter
from .adapters.windows_mcp import WindowsMcpAdapter
from .adapters.xshell_mcp import XshellMcpAdapter
from .ai.catalog import is_vision_model, preset_endpoint
from .ai.crypto import decrypt_secret
from .config import AgentConfig
from .guard import AuditGate, TopLevelAuditor
from .mcp_stdio import MCPError, StdioMCPClient
from .ai.factory import (
    build_active_provider,
    build_ai_provider,
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

_VISION_ONLY_KINDS = {"computer_use_mcp"}


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


def _ensure_vision(config: AgentConfig, adapter_name: str, provider):
    adapter = config.adapters.get(adapter_name)
    kind = adapter.kind if adapter is not None else ""
    vision = bool(
        getattr(provider, "provider_vision", getattr(provider, "vision", False))
    )
    if kind in _VISION_ONLY_KINDS and not vision:
        model = str(
            getattr(provider, "provider_model", "")
            or getattr(provider, "model", "")
            or "当前模型"
        )
        raise RuntimeError(
            f"computer_use 后端只有截图、没有元素树，而 {model} 被识别为纯文本模型，"
            "无法看到屏幕。请改用支持图像输入的模型（如 GPT-4o、Claude、Gemini、"
            "GLM-4V、Qwen-VL），或改用 Windows-MCP 后端。"
        )
    return provider


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
            return _ensure_vision(config, adapter_name, active)
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
        provider = OpenAICompatProvider(
            base_url=config.model.base_url,
            model=config.model.model,
            api_key_env=config.model.api_key_env,
            timeout_seconds=config.model.timeout_seconds,
            temperature=config.model.temperature,
            vision=is_vision_model(config.model.model),
            on_usage=recorder,
        )
        return _ensure_vision(config, adapter_name, provider)
    raise RuntimeError(f"unsupported model provider: {config.model.provider}")


def policy_from_config(config: AgentConfig) -> Policy:
    return Policy(
        allowed_capabilities=config.policy.allowed_capabilities,
        allowed_command_prefixes=config.policy.allowed_command_prefixes,
        require_confirmation=config.policy.require_confirmation,
        allowed_paths=config.policy.allowed_paths,
    )


def _audit_provider(
    config: AgentConfig,
    storage: Storage | None,
    run_id: str | None,
) -> tuple[Any, str, str, bool]:
    audit_config = config.audit_ai
    active = storage.get_active_ai() if storage is not None else None
    if storage is not None and audit_config.model_provider_id:
        row = storage.get_ai_provider(
            audit_config.model_provider_id, include_secret=True
        )
        if row is not None:
            endpoint = preset_endpoint(
                str(row.get("preset_id") or ""),
                str(row.get("plan_id") or ""),
            )
            if endpoint is not None:
                row = {**row, **endpoint}
            models = {
                item["model_id"]: item
                for item in storage.list_ai_models(row["id"])
            }
            model_id = audit_config.model_id
            model = models.get(model_id)
            if model is None and models:
                model_id, model = next(iter(models.items()))
            if model is not None:
                api_key = decrypt_secret(str(row.get("api_key_enc") or ""))
                recorder = usage_recorder(
                    storage,
                    str(row.get("id") or ""),
                    str(row.get("name") or ""),
                    model_id,
                    "audit",
                    run_id,
                )
                provider = build_ai_provider(
                    row,
                    model_id,
                    api_key=api_key,
                    vision=False,
                    timeout_seconds=audit_config.timeout_seconds,
                    temperature=0.0,
                    on_usage=recorder,
                )
                same = bool(
                    active
                    and active["provider"]["id"] == row.get("id")
                    and active["model"]["model_id"] == model_id
                )
                return provider, model_id, str(row.get("name") or ""), same

    if storage is not None:
        provider = build_active_provider(
            storage,
            timeout_seconds=audit_config.timeout_seconds,
            temperature=0.0,
            source="audit",
            run_id=run_id,
        )
        if provider is not None:
            return (
                provider,
                str(getattr(provider, "provider_model", "")),
                str(getattr(provider, "provider_name", "")),
                True,
            )

    if config.model.provider in {"openai_compat", "openai"}:
        recorder = None
        if storage is not None:
            recorder = usage_recorder(
                storage,
                "config",
                "config",
                config.model.model,
                "audit",
                run_id,
            )
        provider = OpenAICompatProvider(
            base_url=config.model.base_url,
            model=config.model.model,
            api_key_env=config.model.api_key_env,
            timeout_seconds=audit_config.timeout_seconds,
            temperature=0.0,
            vision=False,
            on_usage=recorder,
        )
        return provider, config.model.model, "config", True

    return None, "", "", False


def build_audit_gate(
    config: AgentConfig,
    storage: Storage | None = None,
    *,
    run_id: str | None = None,
    conversation_id: str | None = None,
    confirm=None,
    on_event=None,
    mode: str | None = None,
    project_root: str | None = None,
    workspace_roots: list[str] | None = None,
) -> AuditGate:
    provider, model_id, provider_name, same = _audit_provider(
        config, storage, run_id
    )
    auditor = (
        TopLevelAuditor(
            provider,
            model=model_id,
            provider_name=provider_name,
            same_as_executor=same,
        )
        if provider is not None
        else None
    )

    def on_decision(payload: dict[str, Any]) -> None:
        if storage is None:
            return
        storage.record_audit_decision(
            payload, run_id=run_id, conversation_id=conversation_id
        )

    def on_execution(operation_id: str, ok: bool, error: str) -> None:
        if storage is None or run_id is None:
            return
        storage.mark_audit_executed(run_id, operation_id, ok, error)

    roots = workspace_roots or config.policy.allowed_paths or [os.getcwd()]
    return AuditGate(
        mode=mode or config.audit_ai.mode,
        auditor=auditor,
        confirm=confirm,
        on_event=on_event,
        on_decision=on_decision,
        on_execution=on_execution,
        workspace_roots=roots,
        project_root=project_root or os.getcwd(),
        protected_paths=config.audit_ai.protected_paths,
        always_confirm=set(config.audit_ai.always_confirm)
        | set(config.policy.require_confirmation),
    )


__all__ = [
    "build_adapter",
    "build_audit_gate",
    "build_provider",
    "policy_from_config",
]
