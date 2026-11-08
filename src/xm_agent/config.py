from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import CAPABILITIES


@dataclass
class RuntimeConfig:
    max_steps: int = 12
    max_consecutive_failures: int = 2
    lock_path: str = ".xm-agent/agent.lock"
    lock_ttl_seconds: int = 60


@dataclass
class AuditConfig:
    root: str = ".xm-agent/audit"
    keep_days: int = 7


@dataclass
class ModelConfig:
    provider: str = "fake"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4.1"
    api_key_env: str = "OPENAI_API_KEY"
    timeout_seconds: int = 60
    temperature: float = 0.0
    vision: bool = False


@dataclass
class PolicyConfig:
    allowed_capabilities: list[str] = field(default_factory=lambda: list(CAPABILITIES))
    allowed_command_prefixes: list[str] = field(default_factory=list)
    require_confirmation: list[str] = field(default_factory=lambda: ["file.transfer"])
    allowed_paths: list[str] = field(default_factory=list)


@dataclass
class AdapterConfig:
    kind: str = "mock"
    command: list[str] = field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)
    tool_args: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class AgentConfig:
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    adapters: dict[str, AdapterConfig] = field(
        default_factory=lambda: {"mock": AdapterConfig()}
    )
    default_adapter: str = "mock"


def _str_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    return [str(item) for item in value]


def _adapter_from(name: str, raw: dict[str, Any]) -> AdapterConfig:
    tools = raw.get("tools", {})
    tool_args = raw.get("tool_args", {})
    env = raw.get("env", {})
    return AdapterConfig(
        kind=str(raw.get("kind", "mcp_stdio")),
        command=[str(part) for part in raw.get("command", [])],
        cwd=str(raw["cwd"]) if raw.get("cwd") else None,
        env={str(key): str(value) for key, value in env.items()},
        tools={str(key): str(value) for key, value in tools.items()},
        tool_args={
            str(tool): {
                str(source): str(target)
                for source, target in mapping.items()
            }
            for tool, mapping in tool_args.items()
        },
    )


def load_config(path: str | Path | None = None) -> AgentConfig:
    if path is None:
        return AgentConfig()

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    runtime_raw = data.get("runtime", {})
    audit_raw = data.get("audit", {})
    model_raw = data.get("model", {})
    policy_raw = data.get("policy", {})
    adapters_raw = data.get("adapters", {})

    defaults = AgentConfig()
    runtime = RuntimeConfig(
        max_steps=int(runtime_raw.get("max_steps", defaults.runtime.max_steps)),
        max_consecutive_failures=int(
            runtime_raw.get(
                "max_consecutive_failures",
                defaults.runtime.max_consecutive_failures,
            )
        ),
        lock_path=str(runtime_raw.get("lock_path", defaults.runtime.lock_path)),
        lock_ttl_seconds=int(
            runtime_raw.get("lock_ttl_seconds", defaults.runtime.lock_ttl_seconds)
        ),
    )
    audit = AuditConfig(
        root=str(audit_raw.get("root", defaults.audit.root)),
        keep_days=int(audit_raw.get("keep_days", defaults.audit.keep_days)),
    )
    model = ModelConfig(
        provider=str(model_raw.get("provider", defaults.model.provider)),
        base_url=str(model_raw.get("base_url", defaults.model.base_url)),
        model=str(model_raw.get("model", defaults.model.model)),
        api_key_env=str(model_raw.get("api_key_env", defaults.model.api_key_env)),
        timeout_seconds=int(
            model_raw.get("timeout_seconds", defaults.model.timeout_seconds)
        ),
        temperature=float(
            model_raw.get("temperature", defaults.model.temperature)
        ),
        vision=bool(model_raw.get("vision", defaults.model.vision)),
    )
    policy = PolicyConfig(
        allowed_capabilities=_str_list(
            policy_raw.get("allowed_capabilities"),
            defaults.policy.allowed_capabilities,
        ),
        allowed_command_prefixes=_str_list(
            policy_raw.get("allowed_command_prefixes"), []
        ),
        require_confirmation=_str_list(
            policy_raw.get("require_confirmation"),
            defaults.policy.require_confirmation,
        ),
        allowed_paths=_str_list(policy_raw.get("allowed_paths"), []),
    )
    adapters = {
        str(name): _adapter_from(str(name), raw)
        for name, raw in adapters_raw.items()
    }
    if not adapters:
        adapters = {"mock": AdapterConfig()}

    default_adapter = str(
        runtime_raw.get("default_adapter") or next(iter(adapters))
    )
    if default_adapter not in adapters:
        raise ValueError(
            f"default adapter {default_adapter!r} is not defined in [adapters]"
        )

    return AgentConfig(
        runtime=runtime,
        audit=audit,
        model=model,
        policy=policy,
        adapters=adapters,
        default_adapter=default_adapter,
    )
