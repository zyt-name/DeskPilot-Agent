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
    build_model_provider,
    usage_recorder,
)
from .browser.channel import BrowserChannel
from .browser.session import BrowserSettings
from .channels import ChannelHub
from .policy import Policy
from .providers.fake import HeuristicMockProvider
from .providers.openai_compat import OpenAICompatProvider
from .storage import Storage

# 虚拟后端名：不是 MCP 适配器，而是「只用浏览器通道」的模式。
BROWSER_ADAPTER = "browser"

_ADAPTERS = {
    "windows_mcp": WindowsMcpAdapter,
    "xshell_mcp": XshellMcpAdapter,
    "computer_use_mcp": ComputerUseMcpAdapter,
}

_VISION_ONLY_KINDS = {"computer_use_mcp"}


def build_adapter(config: AgentConfig, name: str):
    if name == BROWSER_ADAPTER:
        raise RuntimeError(
            "browser 是通道模式，不是 MCP 适配器；请由 build_hub 挂载浏览器通道"
        )
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


def ensure_vision(config: AgentConfig, adapter_name: str, provider):
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
    model_provider_id: str = "",
    model_id: str = "",
):
    if storage is not None and model_provider_id and model_id:
        selected = build_model_provider(
            storage,
            model_provider_id,
            model_id,
            timeout_seconds=config.model.timeout_seconds,
            temperature=config.model.temperature,
            source=source,
            run_id=run_id,
        )
        return ensure_vision(config, adapter_name, selected)
    if storage is not None:
        active = build_active_provider(
            storage,
            timeout_seconds=config.model.timeout_seconds,
            temperature=config.model.temperature,
            source=source,
            run_id=run_id,
        )
        if active is not None:
            return ensure_vision(config, adapter_name, active)
    if config.model.provider == "fake":
        if adapter_name not in {"mock", BROWSER_ADAPTER}:
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
        return ensure_vision(config, adapter_name, provider)
    raise RuntimeError(f"unsupported model provider: {config.model.provider}")


def probe_adapter_kind(
    config: AgentConfig, kind: str, timeout: float = 20.0
) -> dict[str, Any]:
    """Start the configured MCP backend and complete a real MCP handshake."""
    adapter = next(
        (item for item in config.adapters.values() if item.kind == kind),
        None,
    )
    if adapter is None:
        return {"ok": False, "detail": "配置中没有对应的后端", "tools": []}
    if not adapter.command:
        return {"ok": False, "detail": "后端没有配置启动命令", "tools": []}
    env = {**os.environ, **(adapter.env or {})} if adapter.env else None
    try:
        client = StdioMCPClient(
            adapter.command,
            env=env,
            cwd=adapter.cwd,
            default_timeout=timeout,
        )
    except (OSError, MCPError) as exc:
        return {"ok": False, "detail": f"启动失败：{exc}", "tools": []}
    try:
        tools = [
            str(tool.get("name"))
            for tool in client.list_tools()
            if tool.get("name")
        ]
        return {
            "ok": True,
            "detail": f"MCP 握手成功，共 {len(tools)} 个工具",
            "tools": tools,
        }
    except (OSError, MCPError) as exc:
        return {"ok": False, "detail": f"握手失败：{exc}", "tools": []}
    finally:
        client.close()


def policy_from_config(config: AgentConfig) -> Policy:
    return Policy(
        allowed_capabilities=config.policy.allowed_capabilities,
        allowed_command_prefixes=config.policy.allowed_command_prefixes,
        require_confirmation=config.policy.require_confirmation,
        allowed_paths=config.policy.allowed_paths,
    )


def _provider_vision(provider: Any) -> bool:
    return bool(
        getattr(provider, "provider_vision", False)
        or getattr(provider, "vision", False)
    )


def _vision_model(
    models: dict[str, dict[str, Any]], preferred: str = ""
) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    if preferred and bool(models.get(preferred, {}).get("vision")):
        return preferred, models[preferred]
    for model_id, model in models.items():
        if bool(model.get("vision")):
            return model_id, model
    return None, None


def _audit_provider_from_row(
    config: AgentConfig,
    storage: Storage,
    row: dict[str, Any],
    model_id: str,
    model: dict[str, Any],
    run_id: str | None,
    executor: tuple[Any, str, str] | None,
    active: dict[str, Any] | None,
) -> tuple[Any, str, str, bool, bool] | None:
    if not bool(model.get("vision")):
        return None
    endpoint = preset_endpoint(
        str(row.get("preset_id") or ""),
        str(row.get("plan_id") or ""),
    )
    if endpoint is not None:
        row = {**row, **endpoint}
    try:
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
            vision=True,
            timeout_seconds=config.audit_ai.timeout_seconds,
            temperature=0.0,
            on_usage=recorder,
        )
    except Exception:
        return None
    same = bool(
        executor is not None
        and executor[2] == model_id
        and str(row.get("id") or "")
        == str(getattr(executor[0], "provider_id", "") or "")
    )
    if executor is not None and not same:
        same = bool(
            active
            and active["provider"]["id"] == row.get("id")
            and active["model"]["model_id"] == model_id
        )
    return provider, model_id, str(row.get("name") or ""), same, True


def _audit_provider(
    config: AgentConfig,
    storage: Storage | None,
    run_id: str | None,
    executor: tuple[Any, str, str] | None = None,
) -> tuple[Any, str, str, bool, bool]:
    """Select a vision-capable top-level auditor.

    The audit model must be able to see screenshots because rollback-risk UI
    actions are reviewed against the screen. Selection order: explicit audit
    model, explicit config, executor, active model, any configured vision
    model, then the config model.
    """
    audit_config = config.audit_ai
    active = storage.get_active_ai() if storage is not None else None

    selections: list[tuple[str, str]] = []
    if storage is not None:
        stored = storage.get_audit_model()
        if stored[0]:
            selections.append(stored)
    if audit_config.model_provider_id:
        selections.append(
            (audit_config.model_provider_id, audit_config.model_id)
        )
    if storage is not None:
        for provider_id, preferred in selections:
            row = storage.get_ai_provider(provider_id, include_secret=True)
            if row is None:
                continue
            models = {
                item["model_id"]: item
                for item in storage.list_ai_models(row["id"])
            }
            model_id, model = _vision_model(models, preferred)
            if model_id is None or model is None:
                continue
            chosen = _audit_provider_from_row(
                config, storage, row, model_id, model, run_id, executor, active
            )
            if chosen is not None:
                return chosen

    if executor is not None and _provider_vision(executor[0]):
        provider, model_id, provider_name = executor
        return provider, model_id, provider_name, True, True

    if storage is not None:
        active_provider = build_active_provider(
            storage,
            timeout_seconds=audit_config.timeout_seconds,
            temperature=0.0,
            source="audit",
            run_id=run_id,
        )
        if active_provider is not None and _provider_vision(active_provider):
            return (
                active_provider,
                str(getattr(active_provider, "provider_model", "")),
                str(getattr(active_provider, "provider_name", "")),
                True,
                True,
            )

    if storage is not None:
        for summary in storage.list_ai_providers():
            row = storage.get_ai_provider(
                str(summary.get("id") or ""), include_secret=True
            )
            if row is None:
                continue
            models = {
                item["model_id"]: item
                for item in storage.list_ai_models(row["id"])
            }
            model_id, model = _vision_model(models)
            if model_id is None or model is None:
                continue
            chosen = _audit_provider_from_row(
                config, storage, row, model_id, model, run_id, executor, active
            )
            if chosen is not None:
                return chosen

    if config.model.provider in {"openai_compat", "openai"} and is_vision_model(
        config.model.model
    ):
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
            vision=True,
            on_usage=recorder,
        )
        return provider, config.model.model, "config", True, True

    return None, "", "", False, False


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
    executor: tuple[Any, str, str] | None = None,
) -> AuditGate:
    provider, model_id, provider_name, same, vision = _audit_provider(
        config, storage, run_id, executor
    )
    auditor = (
        TopLevelAuditor(
            provider,
            model=model_id,
            provider_name=provider_name,
            same_as_executor=same,
            vision=vision,
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


def build_browser_channel(
    config: AgentConfig,
    *,
    screenshot_dir: str = ".xm-agent/screenshots",
) -> BrowserChannel | None:
    """按配置构造浏览器通道。关闭时返回 None，能力清单里就不会出现 browser.*。"""
    browser = config.browser
    if not browser.enabled:
        return None
    headless: bool | None = None
    if browser.headless == "always":
        headless = True
    elif browser.headless == "never":
        headless = False
    settings = BrowserSettings(
        engine=browser.engine,
        profile_root=browser.profile_root,
        headless=browser.headless,
        keep_alive_seconds=browser.keep_alive_seconds,
        mirror_fps=browser.mirror_fps,
        screenshot_max_width=browser.screenshot_max_width,
        connect_timeout=browser.connect_timeout,
    )
    return BrowserChannel(
        settings, screenshot_dir=screenshot_dir, headless=headless
    )


def build_hub(
    config: AgentConfig,
    adapter: Any | None,
    *,
    screenshot_dir: str = ".xm-agent/screenshots",
) -> ChannelHub:
    """桌面通道 + 浏览器通道。

    adapter 为 None 时表示纯浏览器模式：不挂桌面通道，全程不截屏、
    不让出桌面。此时能力清单只剩 browser.* 与 task.*。
    """
    hub = ChannelHub(desktop=adapter)
    channel = build_browser_channel(config, screenshot_dir=screenshot_dir)
    if channel is not None:
        hub.register(channel)
    return hub


__all__ = [
    "BROWSER_ADAPTER",
    "build_adapter",
    "build_audit_gate",
    "build_browser_channel",
    "build_hub",
    "build_provider",
    "ensure_vision",
    "policy_from_config",
]
