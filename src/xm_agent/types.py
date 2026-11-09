from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

PolicyKind = Literal["allow", "confirm", "deny"]
TaskStatus = Literal["done", "failed", "aborted", "needs_human"]


@dataclass(frozen=True)
class Element:
    id: str
    role: str = ""
    name: str = ""
    enabled: bool = True
    bounds: tuple[int, int, int, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "enabled": self.enabled,
        }
        if self.bounds is not None:
            data["bounds"] = list(self.bounds)
        return data


@dataclass
class Observation:
    step: int
    source: str = "unknown"
    summary: str = ""
    elements: list[Element] = field(default_factory=list)
    active_window: str | None = None
    screenshot_path: str | None = None
    screen_card: str = ""
    screenshot_cached: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, max_elements: int = 40) -> dict[str, Any]:
        return {
            "step": self.step,
            "source": self.source,
            "summary": self.summary,
            "active_window": self.active_window,
            "screenshot_path": self.screenshot_path,
            "screen_card": self.screen_card,
            "screenshot_cached": self.screenshot_cached,
            "elements": [element.to_dict() for element in self.elements[:max_elements]],
        }


@dataclass(frozen=True)
class Action:
    capability: str
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    expect: str | None = None
    steps: tuple["Action", ...] = ()
    screen_card: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "args": self.args,
            "rationale": self.rationale,
            "expect": self.expect,
            "steps": [step.to_dict() for step in self.steps],
            "screen_card": self.screen_card,
        }


@dataclass
class Receipt:
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    screenshot_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "error": self.error,
            "screenshot_path": self.screenshot_path,
        }


@dataclass
class Verification:
    ok: bool
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    deferred: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "evidence": self.evidence,
            "deferred": self.deferred,
        }


@dataclass(frozen=True)
class PolicyDecision:
    decision: PolicyKind
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"decision": self.decision, "reason": self.reason}


@dataclass
class StepRecord:
    step: int
    observation: Observation
    action: Action
    policy: PolicyDecision | None = None
    audit: dict[str, Any] | None = None
    receipt: Receipt | None = None
    verification: Verification | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "observation": self.observation.to_dict(),
            "action": self.action.to_dict(),
            "policy": self.policy.to_dict() if self.policy else None,
            "audit": self.audit,
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "verification": self.verification.to_dict() if self.verification else None,
            "error": self.error,
        }


@dataclass
class TaskResult:
    trace_id: str
    status: TaskStatus
    steps: list[StepRecord] = field(default_factory=list)
    reason: str = ""
    summary: str = ""
    await_user: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "trace_id": self.trace_id,
            "status": self.status,
            "reason": self.reason,
            "summary": self.summary,
            "steps": [step.to_dict() for step in self.steps],
        }
        if self.await_user:
            data["await_user"] = self.await_user
        return data
