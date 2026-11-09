"""Audit gate: deterministic rules + top-level AI + user confirmation."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

from ..types import Action, Observation, PolicyDecision
from .auditor import (
    AuditVerdict,
    TopLevelAuditor,
    fallback_verdict,
    rule_verdict,
)
from .operation import (
    OperationDescriptor,
    Sensitivity,
    detect_sensitivity,
    environment_profile,
    needs_rollback_review,
    needs_visual_review,
    normalize_operation,
)

ConfirmCallback = Callable[[Action, str, bool], bool]
EventCallback = Callable[[str, dict[str, Any]], None]
DecisionCallback = Callable[[dict[str, Any]], None]
ExecutionCallback = Callable[[str, bool, str], None]

AUDIT_MODES = {"ai", "confirm_all"}


@dataclass
class GateDecision:
    allowed: bool
    descriptor: OperationDescriptor
    sensitivity: Sensitivity
    verdict: AuditVerdict
    audited: bool = False
    user_confirmed: bool = False
    reason: str = ""
    rollback_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "audited": self.audited,
            "user_confirmed": self.user_confirmed,
            "reason": self.reason,
            "rollback_review": self.rollback_review,
            "operation": self.descriptor.to_dict(),
            "sensitivity": self.sensitivity.to_dict(),
            "verdict": self.verdict.to_dict(),
        }


class AuditGate:
    """Review one proposed action before it reaches the adapter.

    Order: policy deny -> hard block -> deterministic sensitivity -> top-level
    auditor -> user confirmation -> TOCTOU recheck. The gate can only tighten
    the policy result, never loosen it.
    """

    def __init__(
        self,
        *,
        mode: str = "ai",
        policy: Any = None,
        auditor: TopLevelAuditor | None = None,
        confirm: ConfirmCallback | None = None,
        on_event: EventCallback | None = None,
        on_decision: DecisionCallback | None = None,
        on_execution: ExecutionCallback | None = None,
        workspace_roots: Iterable[str | os.PathLike[str]] | None = None,
        project_root: str | os.PathLike[str] | None = None,
        protected_paths: Iterable[str] | None = None,
        hard_block_paths: Iterable[str] | None = None,
        always_confirm: Iterable[str] = (),
        cache_ttl_seconds: int = 120,
    ) -> None:
        self.mode = "confirm_all" if str(mode) in {
            "confirm_all",
            "confirm",
            "ask",
        } else "ai"
        self.policy = policy
        self.auditor = auditor
        self.confirm = confirm
        self.on_event = on_event
        self.on_decision = on_decision
        self.on_execution = on_execution
        self.workspace_roots = list(workspace_roots or [os.getcwd()])
        self.project_root = project_root or os.getcwd()
        self.protected_paths = list(protected_paths or [])
        self.hard_block_paths = list(hard_block_paths or [])
        self.always_confirm = set(always_confirm)
        self.cache_ttl_seconds = max(0, int(cache_ttl_seconds))
        self._cache: dict[str, tuple[float, AuditVerdict]] = {}

    def review(
        self,
        action: Action,
        policy_decision: PolicyDecision,
        observation: Observation | None = None,
        *,
        screenshot_path: str | None = None,
        screenshot_provider: Callable[[], str | None] | None = None,
    ) -> GateDecision:
        descriptor = self._normalize(action, observation)
        sensitivity = detect_sensitivity(
            descriptor, always_confirm=self.always_confirm
        )
        rollback_review = needs_rollback_review(descriptor, sensitivity)
        require_visual = needs_visual_review(descriptor, sensitivity)
        if require_visual and not screenshot_path and screenshot_provider:
            try:
                screenshot_path = screenshot_provider()
            except Exception:
                screenshot_path = None

        if policy_decision.decision == "deny":
            verdict = AuditVerdict(
                verdict="deny",
                risk="critical",
                categories=["policy"],
                scope={},
                reversible=None,
                purpose="确定性策略拒绝该能力或参数",
                impact=policy_decision.reason,
                steps=["不执行"],
                source="policy",
                reason=policy_decision.reason,
            )
            return self._finish(
                GateDecision(
                    False,
                    descriptor,
                    sensitivity,
                    verdict,
                    audited=False,
                    reason=policy_decision.reason,
                ),
                action,
                policy_decision,
                observation,
            )

        if descriptor.mutating and any(
            target.hard_blocked for target in descriptor.targets
        ):
            blocked = [
                target.path
                for target in descriptor.targets
                if target.hard_blocked
            ]
            verdict = AuditVerdict(
                verdict="deny",
                risk="critical",
                categories=["system_change"],
                scope={"paths": blocked},
                reversible=False,
                purpose="写入系统硬拦截路径",
                impact="可能破坏操作系统、程序安装目录或引导配置",
                steps=["不执行", *[f"阻止写入 {path}" for path in blocked[:5]]],
                source="rule",
                reason="系统硬拦截路径禁止写入",
            )
            return self._finish(
                GateDecision(
                    False,
                    descriptor,
                    sensitivity,
                    verdict,
                    audited=True,
                    reason=verdict.reason,
                ),
                action,
                policy_decision,
                observation,
            )

        needs_user = (
            policy_decision.decision == "confirm"
            or action.capability in self.always_confirm
        )
        if self.mode == "confirm_all" and descriptor.mutating:
            needs_user = True
        if sensitivity.requires_user:
            needs_user = True

        needs_ai = (
            self.mode == "ai" and sensitivity.sensitive
        ) or rollback_review
        if not needs_ai and not needs_user:
            verdict = rule_verdict(descriptor, sensitivity)
            return self._finish(
                GateDecision(
                    True,
                    descriptor,
                    sensitivity,
                    verdict,
                    audited=False,
                    rollback_review=False,
                ),
                action,
                policy_decision,
                observation,
                record=False,
            )

        environment = self._environment(descriptor, observation)
        if needs_ai:
            verdict = self._audit(
                descriptor,
                environment,
                policy_decision,
                sensitivity,
                screenshot_path=screenshot_path,
                require_visual=require_visual,
            )
        else:
            verdict = rule_verdict(descriptor, sensitivity)
            if needs_user and verdict.verdict == "allow":
                verdict = replace(
                    verdict,
                    verdict="ask_user",
                    risk=max(
                        [verdict.risk, "medium"],
                        key=lambda item: [
                            "low",
                            "medium",
                            "high",
                            "critical",
                        ].index(item),
                    ),
                    impact=verdict.impact
                    or "该操作会修改文件、系统状态或发送数据",
                )

        if sensitivity.requires_user and verdict.verdict == "allow":
            verdict = replace(
                verdict,
                verdict="ask_user",
                risk=max(
                    [verdict.risk, "high"],
                    key=lambda item: [
                        "low",
                        "medium",
                        "high",
                        "critical",
                    ].index(item),
                ),
                impact=verdict.impact
                or "该操作会访问或修改其他项目、工作区或用户数据",
                reason="涉及其他项目或用户数据，必须由用户确认",
            )

        if verdict.verdict == "deny":
            return self._finish(
                GateDecision(
                    False,
                    descriptor,
                    sensitivity,
                    verdict,
                    audited=True,
                    reason=verdict.impact or "被顶层审计阻止",
                    rollback_review=rollback_review,
                ),
                action,
                policy_decision,
                observation,
                environment=environment,
            )

        if verdict.verdict == "ask_user" or verdict.risk in {
            "high",
            "critical",
        }:
            needs_user = True

        user_confirmed = False
        allowed = True
        reason = verdict.reason
        if needs_user:
            prompt = self._prompt(descriptor, verdict, sensitivity)
            approved = False
            mandatory = bool(sensitivity.requires_user) or bool(
                require_visual and verdict.source != "ai"
            )
            if self.confirm is not None:
                try:
                    approved = bool(self.confirm(action, prompt, mandatory))
                except Exception:
                    approved = False
            if not approved:
                allowed = False
                reason = "用户拒绝该操作"
                verdict = replace(
                    verdict,
                    verdict="ask_user",
                    purpose=verdict.purpose or "用户需要审计该操作",
                    impact=verdict.impact or reason,
                    reason=reason,
                )
            else:
                user_confirmed = True

        if allowed and user_confirmed:
            refreshed = self._normalize(action, observation)
            if refreshed.operation_id != descriptor.operation_id:
                allowed = False
                reason = "操作在用户批准后发生变化，已阻止；请重新执行"
                verdict = replace(verdict, verdict="deny", reason=reason)

        decision = GateDecision(
            allowed,
            descriptor,
            sensitivity,
            verdict,
            audited=True,
            user_confirmed=user_confirmed,
            reason=reason,
            rollback_review=rollback_review,
        )
        return self._finish(
            decision,
            action,
            policy_decision,
            observation,
            environment=environment,
        )

    def mark_executed(
        self, decision: GateDecision, ok: bool, error: str = ""
    ) -> None:
        if self.on_execution is None:
            return
        try:
            self.on_execution(
                decision.descriptor.operation_id, bool(ok), error or ""
            )
        except Exception:
            return

    def _normalize(
        self, action: Action, observation: Observation | None
    ) -> OperationDescriptor:
        return normalize_operation(
            action,
            observation=observation,
            workspace_roots=self.workspace_roots,
            project_root=self.project_root,
            protected_paths=self.protected_paths,
            hard_block_paths=self.hard_block_paths,
        )

    def _environment(
        self,
        descriptor: OperationDescriptor,
        observation: Observation | None,
    ) -> dict[str, Any]:
        return environment_profile(
            descriptor,
            observation=observation,
            workspace_roots=self.workspace_roots,
            protected_paths=self.protected_paths,
        )

    def _cache_key(
        self,
        descriptor: OperationDescriptor,
        environment: dict[str, Any],
        policy_decision: PolicyDecision,
    ) -> str:
        raw = json.dumps(
            {
                "operation": descriptor.operation_id,
                "policy": policy_decision.decision,
                "mode": self.mode,
                "environment": {
                    "cwd": environment.get("cwd"),
                    "active_window": environment.get("active_window"),
                    "administrator": environment.get("administrator"),
                },
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _audit(
        self,
        descriptor: OperationDescriptor,
        environment: dict[str, Any],
        policy_decision: PolicyDecision,
        sensitivity: Sensitivity,
        *,
        screenshot_path: str | None = None,
        require_visual: bool = False,
    ) -> AuditVerdict:
        if require_visual:
            if self.auditor is None:
                return fallback_verdict(
                    "该操作可能难以回退，但未配置可识图的顶层审计模型，已强制人工确认"
                )
            if not getattr(self.auditor, "vision", False):
                return fallback_verdict(
                    "该操作可能难以回退，但顶层审计模型不支持识图，已强制人工确认"
                )
            if not screenshot_path:
                return fallback_verdict(
                    "该操作可能难以回退，但无法获取屏幕截图，已强制人工确认"
                )
        key = self._cache_key(descriptor, environment, policy_decision)
        cached = None if require_visual else self._cache.get(key)
        if cached is not None:
            timestamp, verdict = cached
            if (
                self.cache_ttl_seconds > 0
                and time.monotonic() - timestamp <= self.cache_ttl_seconds
                and verdict.verdict in {"allow", "deny"}
            ):
                return replace(verdict, latency_ms=0, reason=verdict.reason)
        if self.auditor is None:
            verdict = fallback_verdict(
                "未配置顶层审计模型，敏感操作已升级为用户确认"
            )
        else:
            verdict = self.auditor.audit(
                descriptor,
                environment,
                policy_decision,
                sensitivity,
                screenshot_path=screenshot_path,
                require_visual=require_visual,
            )
        if (
            not require_visual
            and verdict.source == "ai"
            and verdict.verdict in {"allow", "deny"}
        ):
            self._cache[key] = (time.monotonic(), verdict)
            if len(self._cache) > 256:
                oldest = sorted(
                    self._cache.items(), key=lambda item: item[1][0]
                )[:64]
                for stale_key, _ in oldest:
                    self._cache.pop(stale_key, None)
        return verdict

    @staticmethod
    def _prompt(
        descriptor: OperationDescriptor,
        verdict: AuditVerdict,
        sensitivity: Sensitivity,
    ) -> str:
        lines = [f"操作：{descriptor.summary()}"]
        if verdict.visual:
            lines.append("审查方式：顶部 AI 已对照屏幕截图审查（回退风险）")
        elif verdict.source != "ai":
            lines.append("审查方式：未能完成识图审查，请你确认")
        if verdict.purpose:
            lines.append(f"目的：{verdict.purpose}")
        if verdict.steps:
            lines.append("流程：" + " → ".join(verdict.steps[:6]))
        impact = verdict.impact or "；".join(sensitivity.reasons)
        lines.append(f"可能影响：{impact or '未评估'}")
        lines.append(f"风险：{verdict.risk}")
        reversible = (
            "未知"
            if verdict.reversible is None
            else ("可逆" if verdict.reversible else "不可逆")
        )
        lines.append(f"可逆性：{reversible}")
        if verdict.safer_alternative:
            lines.append(f"更安全的替代：{verdict.safer_alternative}")
        return "\n".join(lines)

    def _finish(
        self,
        decision: GateDecision,
        action: Action,
        policy_decision: PolicyDecision,
        observation: Observation | None,
        *,
        environment: dict[str, Any] | None = None,
        record: bool = True,
    ) -> GateDecision:
        if decision.audited:
            payload = decision.to_dict()
            payload["policy"] = policy_decision.to_dict()
            self._emit("audit", payload)
        if record and self.on_decision is not None:
            environment = environment or self._environment(
                decision.descriptor, observation
            )
            try:
                self.on_decision(
                    {
                        "operation": decision.descriptor.to_dict(),
                        "environment": environment,
                        "verdict": decision.verdict.to_dict(include_raw=True),
                        "sensitivity": decision.sensitivity.to_dict(),
                        "allowed": decision.allowed,
                        "user_confirmed": decision.user_confirmed,
                        "user_decision": (
                            "allow"
                            if decision.user_confirmed
                            else (
                                "deny"
                                if not decision.allowed
                                and decision.audited
                                else ""
                            )
                        ),
                        "reason": decision.reason,
                    }
                )
            except Exception:
                pass
        return decision

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event, payload)
        except Exception:
            return


__all__ = [
    "AUDIT_MODES",
    "AuditGate",
    "DecisionCallback",
    "ExecutionCallback",
    "GateDecision",
]
