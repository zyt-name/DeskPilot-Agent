"""通道抽象：把桌面和浏览器统一成可路由的能力宿主。

Runtime 只认 Channel，不认具体后端。每个通道声明自己拥有哪些能力，
Hub 按能力名把动作、观测和校验路由过去。这样「桌面」和「浏览器」能同时
在线：谁刚执行过动作，下一次观测就看谁，另一边不被打扰。

约定很窄，只是 Runtime 真正用到的四件事：observe / execute / verify / close。
其余（截图落盘、窗口前置、空闲回收）由通道自己实现，Hub 不关心。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .types import Observation, Receipt, Verification

DESKTOP_KIND = "desktop"
BROWSER_KIND = "browser"

# 能力名前缀 -> 通道类型。桌面后端历史上同时承担终端、文件、等待，
# 所以它们都归桌面通道；浏览器自己一组。
_KIND_PREFIXES: tuple[tuple[str, str], ...] = (
    ("browser.", BROWSER_KIND),
    ("desktop.", DESKTOP_KIND),
    ("terminal.", DESKTOP_KIND),
    ("file.", DESKTOP_KIND),
    ("wait.", DESKTOP_KIND),
    ("task.", DESKTOP_KIND),
)

OBSERVE_SUFFIX = ".observe"


@runtime_checkable
class Channel(Protocol):
    """一个能执行能力的能力宿主。"""

    name: str
    kind: str
    capabilities: frozenset[str]

    def observe(
        self, step: int, include_screenshot: bool = False
    ) -> Observation:
        ...

    def execute(self, capability: str, args: dict[str, Any]) -> Receipt:
        ...

    def verify(self, expect: str | dict[str, Any] | None) -> Verification:
        ...

    def close(self) -> None:
        ...


def kind_of(capability: str) -> str:
    """能力名属于哪类通道。未知前缀按桌面处理，保持旧行为。"""
    for prefix, kind in _KIND_PREFIXES:
        if capability.startswith(prefix):
            return kind
    return DESKTOP_KIND


def is_observe(capability: str) -> bool:
    """`*.observe` 是显式刷新观测的能力，Runtime 需要单独分支。"""
    return capability.endswith(OBSERVE_SUFFIX)


def channel_kind(channel: Any, default: str = DESKTOP_KIND) -> str:
    return str(getattr(channel, "kind", "") or default)


def channel_available(channel: Any) -> bool:
    """通道现在能不能干活。没声明 available 的按常驻处理。"""
    probe = getattr(channel, "available", None)
    if callable(probe):
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 - 探测失败按不可用处理
            return False
    return True


def channel_capabilities(channel: Any) -> frozenset[str]:
    caps = getattr(channel, "capabilities", None)
    if isinstance(caps, (set, frozenset, list, tuple)):
        return frozenset(str(item) for item in caps)
    return frozenset()


class ChannelHub:
    """按能力路由通道，并记住「上一步是谁干的活」。

    记住 last_kind 是为了观测语义：浏览器的动作之后不该去截桌面，
    桌面的动作之后也不该去读网页。没有这一步，两个通道都会各自
    observe 一遍，既慢又会给模型看错的界面。
    """

    def __init__(
        self,
        desktop: Any | None = None,
        *,
        browser_only: Any | None = None,
    ) -> None:
        self._channels: dict[str, Any] = {}
        self._last_kind = DESKTOP_KIND
        if desktop is not None:
            self.register(desktop, kind=DESKTOP_KIND)
        if browser_only is not None:
            self.register(browser_only, kind=BROWSER_KIND)

    # ------------------------------------------------------------ 注册
    def register(self, channel: Any, kind: str | None = None) -> None:
        name = str(getattr(channel, "name", "") or kind or "")
        if not name:
            raise ValueError("channel needs a name")
        self._channels[name] = channel
        if kind:
            try:
                channel.kind = kind  # 后端可能没声明，补上便于路由
            except (AttributeError, TypeError):
                pass
        if self._last_kind not in self._kinds():
            self._last_kind = next(iter(self._kinds()), DESKTOP_KIND)

    def unregister(self, name: str) -> None:
        self._channels.pop(name, None)

    def _kinds(self) -> set[str]:
        return {channel_kind(channel) for channel in self._channels.values()}

    def get(self, kind: str) -> Any | None:
        for channel in self._channels.values():
            if channel_kind(channel) == kind:
                return channel
        return None

    def all(self) -> list[Any]:
        return list(self._channels.values())

    def capabilities(self) -> list[str]:
        """当前所有通道真能处理的能力，按契约顺序返回。"""
        from .contracts import CAPABILITIES

        declared: set[str] = set()
        for channel in self._channels.values():
            caps = channel_capabilities(channel)
            if caps:
                declared |= caps
            elif channel_kind(channel) == DESKTOP_KIND:
                # 历史后端没有声明能力清单，按桌面全量处理。
                declared |= {
                    name
                    for name in CAPABILITIES
                    if not name.startswith("browser.")
                }
        return [name for name in CAPABILITIES if name in declared]

    # ------------------------------------------------------------ 路由
    def channel_for(self, capability: str) -> Any | None:
        """显式声明了该能力的通道优先，其次按能力前缀选类型。"""
        for channel in self._channels.values():
            if capability in channel_capabilities(channel):
                return channel
        return self.get(kind_of(capability))

    def kind_for(self, capability: str) -> str:
        channel = self.channel_for(capability)
        if channel is not None:
            return channel_kind(channel)
        return kind_of(capability)

    @property
    def last_kind(self) -> str:
        return self._last_kind

    def focus(self, kind: str) -> None:
        if kind in self._kinds():
            self._last_kind = kind

    def active(self) -> Any | None:
        """当前应该被观测的通道。

        浏览器关闭后不能因为「上次是它」就去观测它——那会顺手把内核再拉起来。
        不可用的通道直接跳过，回落到桌面。
        """
        channel = self.get(self._last_kind)
        if channel is not None and channel_available(channel):
            return channel
        desktop = self.get(DESKTOP_KIND)
        if desktop is not None and channel_available(desktop):
            return desktop
        for candidate in self._channels.values():
            if channel_available(candidate):
                return candidate
        # 全都没就绪：纯浏览器模式的首帧，唤醒唯一通道（拉内核）
        if len(self._channels) == 1:
            channel = next(iter(self._channels.values()))
            wake = getattr(channel, "activate", None)
            if callable(wake):
                try:
                    wake()
                except Exception:  # noqa: BLE001 - 唤醒失败按无通道处理
                    return None
                if channel_available(channel):
                    return channel
        return None

    # ------------------------------------------------------------ 执行
    def execute(self, capability: str, args: dict[str, Any]) -> Receipt:
        channel = self.channel_for(capability)
        if channel is None:
            return Receipt(
                False,
                error=f"没有可处理 {capability} 的通道（浏览器通道未启用？）",
            )
        self._last_kind = channel_kind(channel)
        return channel.execute(capability, args)

    def observe(
        self,
        step: int,
        include_screenshot: bool = False,
        kind: str | None = None,
    ) -> Observation:
        channel = self.get(kind) if kind else self.active()
        if channel is None:
            raise RuntimeError("没有可用的观测通道")
        if kind:
            self._last_kind = kind
        observation = channel.observe(
            step, include_screenshot=include_screenshot
        )
        source = str(getattr(observation, "source", "") or "")
        if source == BROWSER_KIND:
            self._last_kind = BROWSER_KIND
        elif source in {"desktop", "terminal", "unknown"}:
            self._last_kind = DESKTOP_KIND
        return observation

    def verify(
        self,
        expect: str | dict[str, Any] | None,
        kind: str | None = None,
    ) -> Verification:
        channel = self.get(kind) if kind else self.active()
        if channel is None:
            return Verification(False, "没有可校验的通道")
        return channel.verify(expect)

    # ------------------------------------------------------------ 生命周期
    def close_all(self) -> None:
        for channel in list(self._channels.values()):
            try:
                channel.close()
            except Exception:  # noqa: BLE001 - 关闭失败不该拖垮退出流程
                pass

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": str(getattr(channel, "name", "")),
                "kind": channel_kind(channel),
                "capabilities": sorted(channel_capabilities(channel)),
                "active": channel_kind(channel) == self._last_kind,
            }
            for channel in self._channels.values()
        ]
