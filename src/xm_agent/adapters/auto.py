from __future__ import annotations

import json
import re
from typing import Any, Callable

from ..config import AgentConfig
from ..types import Observation, Receipt, Verification

AUTO_ADAPTER = "auto"
REAL_KINDS = ("windows_mcp", "computer_use_mcp", "xshell_mcp")
SCREEN_KINDS = {"windows_mcp", "computer_use_mcp"}
_BACKEND_LABEL = {
    "windows_mcp": "windows（Windows-MCP，本机 UIA 元素树，普通桌面任务默认）",
    "computer_use_mcp": "computer_use（截图视觉后端，仅在 Windows-MCP 不可用或任务必须依赖纯视觉/坐标时兜底）",
    "xshell_mcp": "xshell（远程终端，涉及 SSH、远程服务器、Linux 命令或 Xshell 会话）",
}
_REMOTE_HINTS = (
    "ssh",
    "xshell",
    "远程",
    "服务器",
    "云主机",
    "linux",
    "vps",
    "systemctl",
    "shell",
    "主机",
)
_ROUTE_SYSTEM = (
    "你是桌面 Agent 的后端路由器。根据用户目标选择唯一后端，只输出 JSON："
    '{"backend":"windows|computer_use|xshell","reason":"不超过30字"}。'
    "规则：普通本机桌面操作优先 windows；computer_use 只作为 Windows 后端"
    "不可用或必须依赖纯视觉坐标时的兜底；涉及远程 SSH/服务器/Linux 终端时选 xshell。"
)


def adapter_registry(config: AgentConfig) -> dict[str, str]:
    """Map adapter kind -> configured adapter name (first match wins)."""
    names: dict[str, str] = {}
    for name, adapter in config.adapters.items():
        if adapter.kind in REAL_KINDS and adapter.kind not in names:
            names[adapter.kind] = name
    return names


def auto_available(config: AgentConfig) -> bool:
    return bool(adapter_registry(config))


def adapter_kind(config: AgentConfig, name: str) -> str:
    adapter = config.adapters.get(name)
    return adapter.kind if adapter is not None else ""


def auto_chain(
    config: AgentConfig, primary: str, *, vision_available: bool
) -> list[str]:
    chain = [primary]
    if adapter_kind(config, primary) == "windows_mcp":
        fallback = adapter_registry(config).get("computer_use_mcp")
        if fallback and vision_available:
            chain.append(fallback)
    return chain


def choose_primary(
    config: AgentConfig,
    goal: str,
    provider: Any | None,
    *,
    vision_available: bool,
) -> tuple[str, str]:
    """Pick the primary adapter for an auto run and explain the choice."""
    registry = adapter_registry(config)
    if not registry:
        raise RuntimeError("auto 后端需要至少配置一个真实后端（Windows-MCP、computer_use 或 xshell）")

    text = str(goal or "").casefold()
    heuristic_kind = ""
    heuristic_reason = ""
    if "xshell_mcp" in registry and any(hint in text for hint in _REMOTE_HINTS):
        heuristic_kind = "xshell_mcp"
        heuristic_reason = "目标涉及远程终端"
    elif "windows_mcp" in registry:
        heuristic_kind = "windows_mcp"
        heuristic_reason = "本机桌面任务优先使用 Windows-MCP"
    elif "computer_use_mcp" in registry:
        heuristic_kind = "computer_use_mcp"
        heuristic_reason = "未配置 Windows-MCP，使用视觉后端"
    else:
        heuristic_kind = "xshell_mcp"
        heuristic_reason = "仅配置了终端后端"

    if provider is not None:
        offered = [
            kind
            for kind in REAL_KINDS
            if kind in registry and (kind != "computer_use_mcp" or vision_available)
        ]
        if offered and heuristic_kind in offered:
            try:
                raw = provider.complete(
                    _ROUTE_SYSTEM
                    + "可选后端："
                    + "；".join(_BACKEND_LABEL[kind] for kind in offered),
                    str(goal or ""),
                    max_tokens=120,
                )
                chosen, reason = _parse_route(raw, offered)
                if chosen:
                    label = {
                        "windows_mcp": "Windows-MCP",
                        "computer_use_mcp": "computer_use",
                        "xshell_mcp": "xshell",
                    }.get(chosen, chosen)
                    return registry[chosen], f"AI 选择 {label}" + (
                        f"：{reason}" if reason else ""
                    )
            except Exception:
                pass

    if heuristic_kind == "computer_use_mcp" and not vision_available:
        if "windows_mcp" in registry:
            return registry["windows_mcp"], "当前模型不支持图像输入，改用 Windows-MCP"
        raise RuntimeError(
            "auto 路由到 computer_use，但当前执行模型不支持图像输入；"
            "请改选可识图模型或配置 Windows-MCP 后端"
        )
    return registry[heuristic_kind], heuristic_reason


def _parse_route(raw: str, offered: list[str]) -> tuple[str, str]:
    text = str(raw or "").strip()
    if not text:
        return "", ""
    aliases = {
        "windows": "windows_mcp",
        "computer_use": "computer_use_mcp",
        "xshell": "xshell_mcp",
    }
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            backend = str(data.get("backend") or "").strip().casefold()
            backend = aliases.get(backend, backend)
            reason = str(data.get("reason") or "").strip()[:60]
            if backend in offered:
                return backend, reason
    lowered = text.casefold()
    for kind in offered:
        if kind in lowered:
            return kind, ""
    for alias, kind in aliases.items():
        if kind in offered and alias in lowered:
            return kind, ""
    return "", ""


class AutoAdapter:
    """Delegates to the routed backend and falls back on observe failures."""

    name = "auto"

    def __init__(
        self,
        factory: Callable[[str], Any],
        chain: list[str],
        on_switch: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._factory = factory
        self._chain = list(chain)
        self._index = -1
        self._adapter: Any | None = None
        self._errors: list[str] = []
        self._on_switch = on_switch

    @property
    def vision_only(self) -> bool:
        return bool(getattr(self._adapter, "vision_only", False))

    @property
    def active_name(self) -> str:
        return str(getattr(self._adapter, "name", self.name))

    def _emit(self, name: str, reason: str) -> None:
        if self._on_switch is None:
            return
        try:
            self._on_switch(
                {
                    "adapter": name,
                    "reason": reason,
                    "index": self._index,
                    "chain": list(self._chain),
                }
            )
        except Exception:
            return

    def _build_next(self, notice: str = "") -> Any:
        while self._index + 1 < len(self._chain):
            self._index += 1
            name = self._chain[self._index]
            try:
                adapter = self._factory(name)
            except Exception as exc:
                self._errors.append(f"{name}: {exc}")
                continue
            self._adapter = adapter
            role = "主后端" if self._index == 0 else "兜底后端"
            self._emit(name, notice or f"{role}已启动")
            return adapter
        detail = "；".join(self._errors) or "没有可用后端"
        raise RuntimeError(f"auto 后端全部启动失败：{detail}")

    def _ensure(self) -> Any:
        if self._adapter is not None:
            return self._adapter
        return self._build_next()

    def _switch(self, reason: str) -> bool:
        if self._index + 1 >= len(self._chain):
            return False
        previous = self.active_name
        if self._adapter is not None:
            try:
                self._adapter.close()
            except Exception:
                pass
        self._adapter = None
        try:
            self._build_next(f"{previous} 不可用，已切换兜底：{reason}")
        except Exception:
            return False
        return True

    def observe(self, step: int, include_screenshot: bool = False) -> Observation:
        adapter = self._ensure()
        try:
            return adapter.observe(step, include_screenshot)
        except Exception as exc:
            if not self._switch(f"观测失败：{exc}"):
                raise
            return self._adapter.observe(step, include_screenshot)

    def execute(self, capability: str, args: dict[str, Any]) -> Receipt:
        return self._ensure().execute(capability, args)

    def verify(self, expect: str | dict[str, Any] | None) -> Verification:
        return self._ensure().verify(expect)

    def capture_screenshot(self) -> str | None:
        capture = getattr(self._ensure(), "capture_screenshot", None)
        if not callable(capture):
            return None
        try:
            return capture()
        except Exception:
            return None

    def close(self) -> None:
        if self._adapter is not None:
            try:
                self._adapter.close()
            except Exception:
                pass
        self._adapter = None


__all__ = [
    "AUTO_ADAPTER",
    "AutoAdapter",
    "adapter_kind",
    "adapter_registry",
    "auto_available",
    "auto_chain",
    "choose_primary",
]
