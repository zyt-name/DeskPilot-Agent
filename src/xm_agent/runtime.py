from __future__ import annotations

import os
import uuid
from typing import Any, Callable

from .adapters.base import Adapter
from .audit import AuditLog
from .lock import LeaseLock
from .policy import Policy
from .providers.base import Provider
from .types import Action, Receipt, StepRecord, TaskResult, Verification

ConfirmCallback = Callable[[Action, str], bool]
EventCallback = Callable[[str, dict[str, Any]], None]


class AgentRuntime:
    """Observe -> decide -> policy -> act -> verify loop."""

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
    ) -> None:
        self.adapter = adapter
        self.provider = provider
        self.policy = policy
        self.audit = audit
        self.lock = lock
        self.max_steps = max(1, int(max_steps))
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))
        self.confirm = confirm
        self.on_event = on_event

    def run(self, goal: str) -> TaskResult:
        trace_id = uuid.uuid4().hex[:12]
        token = self.lock.acquire(owner=f"agent:{os.getpid()}")
        if token is None:
            result = TaskResult(
                trace_id,
                "needs_human",
                [],
                "desktop lock is held by another process",
            )
            self.audit.event(trace_id, "result", result.to_dict())
            self._emit("lock_busy", {"trace_id": trace_id})
            return result

        history: list[StepRecord] = []
        failures = 0
        self.audit.event(trace_id, "start", {"goal": goal})
        self._emit("start", {"trace_id": trace_id, "goal": goal})

        try:
            for step in range(1, self.max_steps + 1):
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

                try:
                    action = self.provider.decide(
                        goal,
                        observation,
                        history,
                        self.max_steps - step + 1,
                    )
                except Exception as exc:
                    return self._finish(
                        trace_id, "failed", f"provider error: {exc}", history
                    )

                decision = self.policy.check(action)
                record = StepRecord(
                    step=step,
                    observation=observation,
                    action=action,
                    policy=decision,
                )
                self.audit.event(
                    trace_id,
                    "decision",
                    {
                        "step": step,
                        "action": action.to_dict(),
                        "policy": decision.to_dict(),
                    },
                )

                if decision.decision == "deny":
                    record.error = decision.reason
                    history.append(record)
                    self.audit.event(trace_id, "step", record.to_dict())
                    return self._finish(
                        trace_id, "aborted", decision.reason, history
                    )

                if decision.decision == "confirm":
                    approved = False
                    if self.confirm is not None:
                        try:
                            approved = bool(
                                self.confirm(action, decision.reason)
                            )
                        except Exception as exc:
                            record.error = (
                                f"confirmation callback failed: {exc}"
                            )
                    if not approved:
                        record.error = record.error or "confirmation denied"
                        history.append(record)
                        self.audit.event(trace_id, "step", record.to_dict())
                        return self._finish(
                            trace_id, "aborted", "confirmation denied", history
                        )

                if action.capability == "task.finish":
                    record.receipt = Receipt(
                        True,
                        {"summary": action.args.get("summary", "")},
                    )
                    record.verification = Verification(
                        True, "task finished"
                    )
                    history.append(record)
                    self.audit.event(trace_id, "step", record.to_dict())
                    return self._finish(
                        trace_id,
                        "done",
                        str(action.args.get("summary", "")),
                        history,
                    )

                try:
                    receipt = self.adapter.execute(
                        action.capability, action.args
                    )
                except Exception as exc:
                    receipt = Receipt(False, error=f"adapter error: {exc}")
                record.receipt = receipt

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

    def _finish(
        self,
        trace_id: str,
        status: str,
        reason: str,
        history: list[StepRecord],
    ) -> TaskResult:
        result = TaskResult(trace_id, status, history, reason)
        self.audit.event(trace_id, "result", result.to_dict())
        self._emit(
            "result",
            {
                "trace_id": trace_id,
                "status": status,
                "reason": reason,
                "steps": len(history),
            },
        )
        return result

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event, payload)
        except Exception:
            return
