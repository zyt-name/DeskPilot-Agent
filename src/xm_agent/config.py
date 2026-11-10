from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import CAPABILITIES


@dataclass
class RuntimeConfig:
    max_steps: int = 30
    max_consecutive_failures: int = 2
    max_provider_retries: int = 2
    provider_retry_delay: float = 1.2
    lock_path: str = ".xm-agent/agent.lock"
    lock_ttl_seconds: int = 60
    screenshot_dir: str = ".xm-agent/screenshots"
    release_desktop: bool = True


@dataclass
class AuditConfig:
    root: str = ".xm-agent/audit"
    keep_days: int = 7


@dataclass
class AuditAiConfig:
    mode: str = "ai"
    model_provider_id: str = ""
    model_id: str = ""
    timeout_seconds: int = 12
    fail_closed: bool = True
    protected_paths: list[str] = field(default_factory=list)
    always_confirm: list[str] = field(default_factory=list)


@dataclass
class ModelConfig:
    provider: str = "fake"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4.1"
    api_key_env: str = "OPENAI_API_KEY"
    timeout_seconds: int = 60
    temperature: float = 0.0
    context_window: int = 32768


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
class BrowserConfig:
    """内置浏览器通道。见 TECH_DESIGN 4.21。"""

    engine: str = "auto"  # auto | edge | chrome | chromium
    profile_root: str = ".xm-agent/browser/profiles"
    headless: str = "auto"  # auto | always | never
    keep_alive_seconds: int = 300
    mirror_fps: int = 15
    screenshot_max_width: int = 1280
    connect_timeout: float = 20.0


@dataclass
class AgentConfig:
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    audit_ai: AuditAiConfig = field(default_factory=AuditAiConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
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
    audit_ai_raw = data.get("audit_ai", {})
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
        max_provider_retries=int(
            runtime_raw.get(
                "max_provider_retries",
                defaults.runtime.max_provider_retries,
            )
        ),
        provider_retry_delay=float(
            runtime_raw.get(
                "provider_retry_delay",
                defaults.runtime.provider_retry_delay,
            )
        ),
        lock_path=str(runtime_raw.get("lock_path", defaults.runtime.lock_path)),
        lock_ttl_seconds=int(
            runtime_raw.get("lock_ttl_seconds", defaults.runtime.lock_ttl_seconds)
        ),
        screenshot_dir=str(
            runtime_raw.get(
                "screenshot_dir", defaults.runtime.screenshot_dir
            )
        ),
        release_desktop=bool(
            runtime_raw.get(
                "release_desktop", defaults.runtime.release_desktop
            )
        ),
    )
    audit = AuditConfig(
        root=str(audit_raw.get("root", defaults.audit.root)),
        keep_days=int(audit_raw.get("keep_days", defaults.audit.keep_days)),
    )
    audit_ai_mode = str(
        audit_ai_raw.get("mode", defaults.audit_ai.mode)
    ).strip().casefold()
    if audit_ai_mode not in {"ai", "confirm_all"}:
        raise ValueError(
            "audit_ai.mode must be 'ai' or 'confirm_all'; "
            "turning auditing off is not supported"
        )
    audit_ai = AuditAiConfig(
        mode=audit_ai_mode,
        model_provider_id=str(
            audit_ai_raw.get(
                "model_provider_id", defaults.audit_ai.model_provider_id
            )
        ),
        model_id=str(
            audit_ai_raw.get("model_id", defaults.audit_ai.model_id)
        ),
        timeout_seconds=int(
            audit_ai_raw.get(
                "timeout_seconds", defaults.audit_ai.timeout_seconds
            )
        ),
        fail_closed=bool(
            audit_ai_raw.get(
                "fail_closed", defaults.audit_ai.fail_closed
            )
        ),
        protected_paths=_str_list(
            audit_ai_raw.get("protected_paths"),
            defaults.audit_ai.protected_paths,
        ),
        always_confirm=_str_list(
            audit_ai_raw.get("always_confirm"),
            defaults.audit_ai.always_confirm,
        ),
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
        context_window=int(
            model_raw.get("context_window", defaults.model.context_window)
        ),
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

    browser_raw = runtime_raw.get("browser", {})
    if not isinstance(browser_raw, dict):
        browser_raw = {}
    browser_engine = str(
        browser_raw.get("engine", defaults.browser.engine)
    ).strip().lower()
    if browser_engine not in {"auto", "edge", "chrome", "chromium"}:
        raise ValueError(
            "runtime.browser.engine must be one of auto / edge / chrome / chromium"
        )
    browser_headless = str(
        browser_raw.get("headless", defaults.browser.headless)
    ).strip().lower()
    if browser_headless not in {"auto", "always", "never"}:
        raise ValueError("runtime.browser.headless must be one of auto / always / never")
    browser = BrowserConfig(
        engine=browser_engine,
        profile_root=str(
            browser_raw.get("profile_root", defaults.browser.profile_root)
        ),
        headless=browser_headless,
        keep_alive_seconds=int(
            browser_raw.get(
                "keep_alive_seconds", defaults.browser.keep_alive_seconds
            )
        ),
        mirror_fps=int(
            browser_raw.get("mirror_fps", defaults.browser.mirror_fps)
        ),
        screenshot_max_width=int(
            browser_raw.get(
                "screenshot_max_width", defaults.browser.screenshot_max_width
            )
        ),
        connect_timeout=float(
            browser_raw.get("connect_timeout", defaults.browser.connect_timeout)
        ),
    )

    return AgentConfig(
        runtime=runtime,
        audit=audit,
        audit_ai=audit_ai,
        model=model,
        policy=policy,
        browser=browser,
        adapters=adapters,
        default_adapter=default_adapter,
    )
