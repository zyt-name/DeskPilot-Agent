from __future__ import annotations

import os
import uuid
from typing import Any, Callable

from .adapters.base import Adapter
from .audit import AuditLog
from .control import RunCancelled, RunControl, RunPreempted
from .guard.gate import AuditGate, GateDecision
from .lock import LeaseLock
from .policy import Policy
from .providers.base import Provider
from .summary import final_summary
from .types import (
    Action,
    Observation,
    Receipt,
    StepRecord,
    TaskResult,
    Verification,
)

ConfirmCallback = Callable[[Action, str, bool], bool]
EventCallback = Callable[[str, dict[str, Any]], None]


class AgentRuntime:
    """Observe -> decide -> audit -> act -> verify loop."""

    def __init__(
        self,
        adapter: Adapter,
        provider: Provider,
        policy: Policy,
        audit: AuditLog,
        lock: LeaseLock,
        max_steps: int = 12,
        max_consecutive_failures: int = 2,
        confirm: ConfirmCallback | None = None,
        on_event: EventCallback | None = None,
        gate: AuditGate | None = None,
        control: RunControl | None = None,
        max_audit_denials: int = 2,
    ) -> None:
        self.adapter = adapter
        self.provider = provider
        self.policy = policy
        self.audit = audit
        self.lock = lock
        self.max_steps = max(1, int(max_steps))
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))
        self.max_audit_denials = max(1, int(max_audit_denials))
        self.confirm = confirm
        self.on_event = on_event
        self.gate = gate
        self.control = control

    def run(self, goal: str, context: str = "") -> TaskResult:
        trace_id = uuid.uuid4().hex[:12]
        token = self.lock.acquire(owner=f"agent:{os.getpid()}")
        if token is None:
            result = TaskResult(
                trace_id,
                "needs_human",
                [],
                "desktop lock is held by another process",
                "任务未启动：桌面正被另一个进程控制，请稍后重试。",
            )
            self.audit.event(trace_id, "result", result.to_dict())
            self._emit("lock_busy", {"trace_id": trace_id})
            return result

        task_context = str(context or "").strip()
        history: list[StepRecord] = []
        failures = 0
        denials = 0
        self.audit.event(trace_id, "start", {"goal": goal})
        self._emit("start", {"trace_id": trace_id, "goal": goal})

        try:
            for step in range(1, self.max_steps + 1):
                try:
                    self._checkpoint()
                except RunPreempted:
                    return self._finish(
                        trace_id, "aborted", "preempted by queued task", history
                    )
                except RunCancelled:
                    return self._finish(
                        trace_id, "aborted", "cancelled by user", history
                    )
                try:
                    observation = self.adapter.observe(step)
                except Exception as exc:
                    return self._finish(
                        trace_id, "failed", f"observation failed: {exc}", history
                    )

                self.audit.event(
                    trace_id, "observation", observation.to_dict()
                )
                self._emit(
                    "observation",
                    {
                        "step": step,
                        "summary": observation.summary,
                        "elements": len(observation.elements),
                    },
                )

                instructions: list[str] = []
                if self.control is not None:
                    instructions = self.control.drain_instructions()
                    if instructions:
                        self._emit(
                            "instructions",
                            {"step": step, "items": instructions},
                        )
                try:
                    kwargs: dict[str, Any] = {}
                    if instructions:
                        kwargs["instructions"] = instructions
                    if task_context:
                        kwargs["context"] = task_context
                    if self.on_event is not None:
                        kwargs["on_delta"] = self._delta_callback(step)
                    action = self.provider.decide(
                        goal,
                        observation,
                        history,
                        self.max_steps - step + 1,
                        **kwargs,
                    )
                except RunCancelled:
                    return self._finish(
                        trace_id, "aborted", "cancelled by user", history
                    )
                except Exception as exc:
                    return self._finish(
                        trace_id, "failed", f"provider error: {exc}", history
                    )

                policy_decision = self.policy.check(action)
                record = StepRecord(
                    step=step,
                    observation=observation,
                    action=action,
                    policy=policy_decision,
                )
                self.audit.event(
                    trace_id,
                    "decision",
                    {
                        "step": step,
                        "action": action.to_dict(),
                        "policy": policy_decision.to_dict(),
                    },
                )

                gate_decision: GateDecision | None = None
                if self.gate is not None:
                    try:
                        gate_decision = self.gate.review(
                            action, policy_decision, observation
                        )
                    except RunCancelled:
                        return self._finish(
                            trace_id, "aborted", "cancelled by user", history
                        )
                    except Exception as exc:
                        record.error = f"audit failed closed: {exc}"
                        history.append(record)
                        self.audit.event(trace_id, "step", record.to_dict())
                        self._emit(
                            "step",
                            {
                                "step": step,
                                "capability": action.capability,
                                "audit_blocked": True,
                                "error": record.error,
                            },
                        )
                        denials += 1
                        if denials >= self.max_audit_denials:
                            return self._finish(
                                trace_id, "aborted", record.error, history
                            )
                        continue

                    record.audit = gate_decision.to_dict()
                    self.audit.event(
                        trace_id, "audit", gate_decision.to_dict()
                    )
                    if gate_decision.audited:
                        self._emit(
                            "audit",
                            {
                                **gate_decision.to_dict(),
                                "policy": policy_decision.to_dict(),
                                "step": step,
                            },
                        )
                    if not gate_decision.allowed:
                        record.error = (
                            gate_decision.reason or "blocked by audit"
                        )
                        history.append(record)
                        self.audit.event(trace_id, "step", record.to_dict())
                        self._emit(
                            "step",
                            {
                                "step": step,
                                "capability": action.capability,
                                "audit_blocked": True,
                                "error": record.error,
                            },
                        )
                        if gate_decision.verdict.source == "policy":
                            return self._finish(
                                trace_id, "aborted", record.error, history
                            )
                        denials += 1
                        if denials >= self.max_audit_denials:
                            return self._finish(
                                trace_id,
                                "aborted",
                                f"audit denied: {record.error}",
                                history,
                            )
                        continue
                else:
                    if policy_decision.decision == "deny":
                        record.error = policy_decision.reason
                        history.append(record)
                        self.audit.event(trace_id, "step", record.to_dict())
                        return self._finish(
                            trace_id, "aborted", policy_decision.reason, history
                        )

                    if policy_decision.decision == "confirm":
                        approved = False
                        if self.confirm is not None:
                            try:
                                approved = bool(
                                    self.confirm(
                                        action, policy_decision.reason, False
                                    )
                                )
                            except Exception as exc:
                                record.error = (
                                    f"confirmation callback failed: {exc}"
                                )
                        if not approved:
                            record.error = (
                                record.error or "confirmation denied"
                            )
                            history.append(record)
                            self.audit.event(
                                trace_id, "step", record.to_dict()
                            )
                            return self._finish(
                                trace_id,
                                "aborted",
                                "confirmation denied",
                                history,
                            )

                if action.capability == "task.finish":
                    declared_summary = str(
                        action.args.get("summary", "")
                    ).strip()
                    if action.args.get("success") is False:
                        record.error = (
                            declared_summary or "模型判定任务未完成"
                        )
                        record.receipt = Receipt(
                            True,
                            {"summary": declared_summary, "success": False},
                        )
                        record.verification = Verification(
                            False, record.error
                        )
                        if gate_decision is not None:
                            self.gate.mark_executed(gate_decision, True)
                        history.append(record)
                        self.audit.event(
                            trace_id, "step", record.to_dict()
                        )
                        self._emit(
                            "step",
                            {
                                "step": step,
                                "capability": action.capability,
                                "receipt_ok": True,
                                "verify_ok": False,
                                "declared_failure": True,
                                "error": record.error,
                            },
                        )
                        return self._finish(
                            trace_id, "failed", record.error, history
                        )
                    completion = self._completion_check(
                        goal, action, observation
                    )
                    if completion is not None and not completion.ok:
                        record.error = (
                            f"completion not verified: {completion.detail}"
                        )
                        record.verification = completion
                        if gate_decision is not None:
                            self.gate.mark_executed(
                                gate_decision, False, record.error
                            )
                        history.append(record)
                        self.audit.event(
                            trace_id, "step", record.to_dict()
                        )
                        self._emit(
                            "step",
                            {
                                "step": step,
                                "capability": action.capability,
                                "receipt_ok": True,
                                "verify_ok": False,
                                "finish_blocked": True,
                                "error": record.error,
                            },
                        )
                        failures += 1
                        if failures >= self.max_consecutive_failures:
                            return self._finish(
                                trace_id, "failed", record.error, history
                            )
                        continue
                    record.receipt = Receipt(
                        True,
                        {"summary": action.args.get("summary", "")},
                    )
                    record.verification = (
                        completion
                        or Verification(True, "task finished")
                    )
                    if gate_decision is not None:
                        self.gate.mark_executed(gate_decision, True)
                    history.append(record)
                    self.audit.event(trace_id, "step", record.to_dict())
                    self._emit(
                        "step",
                        {
                            "step": step,
                            "capability": action.capability,
                            "receipt_ok": True,
                            "verify_ok": True,
                            "error": None,
                        },
                    )
                    return self._finish(
                        trace_id,
                        "done",
                        declared_summary,
                        history,
                    )

                if action.capability == "desktop.observe":
                    receipt, verification = self._reobserve(
                        step, action.args
                    )
                    record.receipt = receipt
                    record.verification = verification
                    if gate_decision is not None:
                        self.gate.mark_executed(
                            gate_decision, receipt.ok, receipt.error or ""
                        )
                    history.append(record)
                    self.audit.event(trace_id, "step", record.to_dict())
                    self._emit(
                        "step",
                        {
                            "step": step,
                            "capability": action.capability,
                            "receipt_ok": receipt.ok,
                            "verify_ok": verification.ok,
                            "deferred": verification.deferred,
                            "error": receipt.error,
                        },
                    )
                    if receipt.ok:
                        failures = 0
                    else:
                        failures += 1
                        if failures >= self.max_consecutive_failures:
                            return self._finish(
                                trace_id,
                                "failed",
                                f"consecutive failures: {failures}",
                                history,
                            )
                    continue

                try:
                    self._checkpoint()
                except RunCancelled:
                    return self._finish(
                        trace_id, "aborted", "cancelled by user", history
                    )
                try:
                    receipt = self.adapter.execute(
                        action.capability, action.args
                    )
                except Exception as exc:
                    receipt = Receipt(False, error=f"adapter error: {exc}")
                record.receipt = receipt
                if gate_decision is not None:
                    self.gate.mark_executed(
                        gate_decision, receipt.ok, receipt.error or ""
                    )

                if receipt.ok:
                    try:
                        verification = self.adapter.verify(action.expect)
                    except Exception as exc:
                        verification = Verification(
                            False, f"verify error: {exc}"
                        )
                else:
                    verification = Verification(
                        False, receipt.error or "action failed"
                    )
                record.verification = verification
                history.append(record)
                self.audit.event(trace_id, "step", record.to_dict())
                self._emit(
                    "step",
                    {
                        "step": step,
                        "capability": action.capability,
                        "receipt_ok": receipt.ok,
                        "verify_ok": verification.ok,
                        "deferred": verification.deferred,
                        "error": receipt.error or record.error,
                    },
                )

                if not (receipt.ok and verification.ok):
                    failures += 1
                    if failures >= self.max_consecutive_failures:
                        return self._finish(
                            trace_id,
                            "failed",
                            f"consecutive failures: {failures}",
                            history,
                        )
                else:
                    failures = 0

            return self._finish(
                trace_id,
                "failed",
                f"max steps reached: {self.max_steps}",
                history,
            )
        except Exception as exc:
            return self._finish(
                trace_id, "failed", f"runtime error: {exc}", history
            )
        finally:
            self.lock.release(token)
            self.audit.event(trace_id, "lock_released", {"released": True})

    def _completion_check(
        self,
        goal: str,
        action: Action,
        observation: Observation,
    ) -> Verification | None:
        """Verify completion for backends that cannot check text themselves.

        Returns None when the backend verifies semantically on its own, so the
        existing adapters keep their behaviour.
        """
        if not getattr(self.adapter, "vision_only", False):
            return None
        screenshot = getattr(observation, "screenshot_path", None)
        if not screenshot:
            return Verification(
                False, "no screenshot available to confirm completion"
            )
        vision = bool(
            getattr(
                self.provider,
                "provider_vision",
                getattr(self.provider, "vision", False),
            )
        )
        judge = getattr(self.provider, "verify_completion", None)
        if judge is None or not vision:
            return Verification(
                False, "active model has no vision to confirm the screen"
            )
        summary = str(action.args.get("summary", ""))
        try:
            ok, detail = judge(goal, summary, screenshot)
        except Exception as exc:
            return Verification(False, f"completion check error: {exc}")
        return Verification(
            bool(ok),
            str(detail) or "vision model returned no reason",
            {"screenshot_path": screenshot},
        )

    def _reobserve(
        self, step: int, args: dict[str, Any]
    ) -> tuple[Receipt, Verification]:
        try:
            observation = self.adapter.observe(
                step,
                include_screenshot=bool(args.get("include_screenshot")),
            )
        except Exception as exc:
            error = f"observation failed: {exc}"
            return Receipt(False, error=error), Verification(False, error)
        return (
            Receipt(
                True,
                {
                    "summary": observation.summary,
                    "elements": len(observation.elements),
                    "active_window": observation.active_window,
                    "screenshot_path": observation.screenshot_path,
                },
            ),
            Verification(True, "observation refreshed"),
        )

    def _finish(
        self,
        trace_id: str,
        status: str,
        reason: str,
        history: list[StepRecord],
    ) -> TaskResult:
        summary = final_summary(status, reason, history)
        result = TaskResult(trace_id, status, history, reason, summary)
        self.audit.event(trace_id, "result", result.to_dict())
        self._emit(
            "result",
            {
                "trace_id": trace_id,
                "status": status,
                "reason": reason,
                "summary": summary,
                "steps": len(history),
            },
        )
        return result

    def _delta_callback(self, step: int):
        def on_delta(kind: str, text: str) -> None:
            self._checkpoint()
            self._emit(
                "thinking" if kind == "thinking" else "assistant",
                {"step": step, "text": text},
            )

        return on_delta

    def _checkpoint(self) -> None:
        if self.control is not None:
            self.control.checkpoint()

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event, payload)
        except Exception:
            return
