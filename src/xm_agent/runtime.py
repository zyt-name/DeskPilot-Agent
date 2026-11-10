from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import replace
from typing import Any, Callable

from . import vision
from .adapters.base import Adapter
from .audit import AuditLog
from .channels import DESKTOP_KIND, ChannelHub, is_observe
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

MAX_PLAN_ACTIONS = 4
_TERMINAL_CAPABILITIES = {
    "task.finish",
    "task.await_user",
}

_LOGIN_TEXT = re.compile(r"(登录|登陆|sign\s*in|log\s*in|login)", re.I)
_PASSWORD_TEXT = re.compile(r"(密码|password|passwd|pwd)", re.I)
_ACCOUNT_TEXT = re.compile(
    r"(账号|帐号|用户名|手机号|邮箱|username|account|email|phone)", re.I
)
_LOGIN_PROMPT_TEXT = re.compile(
    r"(请先?登录|扫码登录|登录后|sign\s*in\s+to\s+continue|"
    r"log\s*in\s+to\s+continue|login\s+required)",
    re.I,
)
_LOGIN_GOAL_TEXT = re.compile(
    r"(登录|登陆|注册|密码|账号|帐号|用户名|sign\s*in|log\s*in|login|"
    r"register|password|username)",
    re.I,
)
_CREDENTIAL_VALUE = re.compile(
    r"(密码|password|passwd|pwd)\s*(?:是|为|[:：=])\s*[^\s，,。;；]{2,}",
    re.I,
)


class _Stop(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class AgentRuntime:
    """Observe -> decide -> audit -> act -> verify loop.

    The model may return a short batch of deterministic actions. A plan is
    observed once, executed in order, and verified as a whole, which removes
    the extra screenshot and model round trip per keystroke. Any failure or
    blocked action drops the rest of the batch and re-observes before deciding.
    """

    def __init__(
        self,
        adapter: Adapter | None,
        provider: Provider,
        policy: Policy,
        audit: AuditLog,
        lock: LeaseLock,
        max_steps: int = 30,
        max_consecutive_failures: int = 2,
        confirm: ConfirmCallback | None = None,
        on_event: EventCallback | None = None,
        gate: AuditGate | None = None,
        control: RunControl | None = None,
        max_audit_denials: int = 2,
        max_provider_retries: int = 2,
        provider_retry_delay: float = 1.2,
        hub: ChannelHub | None = None,
    ) -> None:
        self.adapter = adapter
        self.hub = hub if hub is not None else ChannelHub(desktop=adapter)
        self.provider = provider
        self.policy = policy
        self.audit = audit
        self.lock = lock
        self.max_steps = max(1, int(max_steps))
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))
        self.max_audit_denials = max(1, int(max_audit_denials))
        self.max_provider_retries = max(0, int(max_provider_retries))
        self.provider_retry_delay = max(0.0, float(provider_retry_delay))
        self.confirm = confirm
        self.on_event = on_event
        self.gate = gate
        self.control = control
        self._screen_hash = ""
        self._screen_card = ""
        self._decided_hash = ""
        self._user_notes: list[str] = []

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
        actions_done = 0
        pending: list[Action] = []
        observation: Observation | None = None
        observation_fresh = False
        fresh_plan = True
        force_screenshot = False
        self._screen_hash = ""
        self._screen_card = ""
        self._decided_hash = ""
        self._user_notes = []
        # 只把真能执行的能力告诉模型：没接浏览器通道就别让它调 browser.*
        try:
            self.provider.available_capabilities = self.hub.capabilities()
        except (AttributeError, TypeError):
            pass
        self.audit.event(trace_id, "start", {"goal": goal})
        self._emit("start", {"trace_id": trace_id, "goal": goal})

        try:
            while actions_done < self.max_steps:
                self._checkpoint_stop()
                step = actions_done + 1

                if pending:
                    action = pending.pop(0)
                else:
                    if not observation_fresh or observation is None:
                        observation = self._observe_with_retry(
                            trace_id,
                            step,
                            history,
                            include_screenshot=(
                                force_screenshot or self._vision_backend()
                            ),
                        )
                        observation_fresh = True
                        force_screenshot = False
                    view = self._decision_observation(observation, step)
                    action = self._decide_with_retry(
                        trace_id, goal, step, view, history, task_context
                    )
                    forced = self._login_wall_action(
                        goal, task_context, view, action
                    )
                    if forced is not None:
                        action = forced
                    self._remember_screen(observation, action)
                    actions = self._plan_actions(
                        action, self.max_steps - actions_done
                    )
                    if len(actions) > 1:
                        payload = {
                            "step": step,
                            "actions": [item.to_dict() for item in actions],
                        }
                        self.audit.event(trace_id, "plan", payload)
                        self._emit("plan", payload)
                    action = actions[0]
                    pending = actions[1:]
                    fresh_plan = True

                actions_done += 1
                last_in_plan = not pending
                action = replace(action, steps=(), screen_card="")
                step_observation = replace(observation, step=step)
                policy_decision = self.policy.check(action)
                record = StepRecord(
                    step=step,
                    observation=step_observation,
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
                            action,
                            policy_decision,
                            step_observation,
                            screenshot_path=(
                                step_observation.screenshot_path
                                if fresh_plan
                                else None
                            ),
                            screenshot_provider=self._capture_for_audit,
                        )
                    except RunPreempted:
                        raise _Stop("aborted", "preempted by queued task")
                    except RunCancelled:
                        raise _Stop("aborted", "cancelled by user")
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
                        pending.clear()
                        observation_fresh = False
                        fresh_plan = False
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
                        pending.clear()
                        observation_fresh = False
                        fresh_plan = False
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

                fresh_plan = False

                if action.capability == "task.await_user":
                    kind = str(action.args.get("kind") or "input").strip()
                    if kind not in {"login", "input"}:
                        kind = "input"
                    prompt = str(
                        action.args.get("prompt")
                        or action.args.get("summary")
                        or ""
                    ).strip()
                    if not prompt:
                        prompt = (
                            "当前窗口需要你先完成登录。"
                            if kind == "login"
                            else "任务需要你先处理当前窗口。"
                        )
                    declared = str(
                        action.args.get("summary") or ""
                    ).strip()
                    default_summary = (
                        f"任务已暂停，等待你登录。\n\n{prompt}\n\n"
                        "登录完成后在下方输入“继续”，我会从当前屏幕接着执行原任务。"
                        if kind == "login"
                        else f"任务已暂停，需要你处理：{prompt}\n\n"
                        "处理完成后在下方输入“继续”，我会从当前屏幕接着执行原任务。"
                    )
                    await_user = {
                        "kind": kind,
                        "prompt": prompt,
                        "resume_hint": "完成后输入“继续”",
                    }
                    record.receipt = Receipt(
                        True,
                        {"kind": kind, "prompt": prompt},
                    )
                    record.verification = Verification(True, "awaiting user")
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
                            "await_user": True,
                            "error": None,
                        },
                    )
                    return self._finish(
                        trace_id,
                        "needs_human",
                        prompt,
                        history,
                        summary=declared or default_summary,
                        await_user=await_user,
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
                    if not observation_fresh or observation.step != step:
                        observation = self._observe_with_retry(
                            trace_id,
                            step,
                            history,
                            include_screenshot=self._vision_backend(),
                        )
                        observation_fresh = True
                        step_observation = replace(observation, step=step)
                        record.observation = step_observation
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
                        observation_fresh = False
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

                if is_observe(action.capability):
                    receipt, refreshed = self._capture_observation(
                        step, action, action.args
                    )
                    record.receipt = receipt
                    record.verification = (
                        Verification(True, "observation refreshed")
                        if receipt.ok
                        else Verification(
                            False, receipt.error or "observe failed"
                        )
                    )
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
                            "verify_ok": receipt.ok,
                            "deferred": False,
                            "error": receipt.error,
                        },
                    )
                    if receipt.ok and refreshed is not None:
                        observation = refreshed
                        observation_fresh = True
                        failures = 0
                    else:
                        failures += 1
                        observation_fresh = False
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
                except RunPreempted:
                    raise _Stop("aborted", "preempted by queued task")
                except RunCancelled:
                    raise _Stop("aborted", "cancelled by user")
                try:
                    receipt = self.hub.execute(
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
                    if not last_in_plan:
                        # Deterministic batch step: the receipt is enough.
                        # The batch is verified once at the end, saving a tool
                        # round trip (and often a screenshot) per keystroke.
                        verification = Verification(
                            True,
                            "批量计划执行中，稍后统一验证",
                            {"batched": True},
                            deferred=True,
                        )
                    else:
                        try:
                            verification = self.hub.verify(action.expect)
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
                        "batched": not last_in_plan,
                        "error": receipt.error or record.error,
                    },
                )

                observation_fresh = False
                if not (receipt.ok and verification.ok):
                    failures += 1
                    pending.clear()
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
        except _Stop as stop:
            return self._finish(
                trace_id, stop.status, stop.reason, history
            )
        except Exception as exc:
            return self._finish(
                trace_id, "failed", f"runtime error: {exc}", history
            )
        finally:
            self.lock.release(token)
            self.audit.event(trace_id, "lock_released", {"released": True})

    _FATAL_PROVIDER_HINTS = (
        "401",
        "403",
        "unauthorized",
        "invalid api key",
        "authentication",
        "permission denied",
    )
    _TRANSIENT_PROVIDER_HINTS = (
        "429",
        "rate limit",
        "timeout",
        "timed out",
        "connection",
        "502",
        "503",
        "504",
        "overloaded",
        "server error",
        "temporarily",
    )

    @classmethod
    def _fatal_provider_error(cls, message: str) -> bool:
        lowered = str(message or "").casefold()
        return any(hint in lowered for hint in cls._FATAL_PROVIDER_HINTS)

    def _retry_delay(self, message: str, attempt: int) -> float:
        if not self.provider_retry_delay:
            return 0.0
        lowered = str(message or "").casefold()
        if not any(
            hint in lowered for hint in self._TRANSIENT_PROVIDER_HINTS
        ):
            return 0.0
        return min(8.0, self.provider_retry_delay * attempt)

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
        provider_vision = bool(
            getattr(
                self.provider,
                "provider_vision",
                getattr(self.provider, "vision", False),
            )
        )
        judge = getattr(self.provider, "verify_completion", None)
        if judge is None or not provider_vision:
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

    def _checkpoint_stop(self) -> None:
        try:
            self._checkpoint()
        except RunPreempted:
            raise _Stop("aborted", "preempted by queued task")
        except RunCancelled:
            raise _Stop("aborted", "cancelled by user")

    def _observe_with_retry(
        self,
        trace_id: str,
        step: int,
        history: list[StepRecord],
        *,
        include_screenshot: bool,
    ) -> Observation:
        attempts = 0
        while True:
            try:
                observation = self.hub.observe(
                    step, include_screenshot=include_screenshot
                )
                break
            except RunPreempted:
                raise _Stop("aborted", "preempted by queued task")
            except RunCancelled:
                raise _Stop("aborted", "cancelled by user")
            except Exception as exc:
                attempts += 1
                message = str(exc) or exc.__class__.__name__
                if attempts >= 2:
                    raise _Stop(
                        "failed", f"observation failed: {message}"
                    )
                self._emit(
                    "observation_retry",
                    {"step": step, "error": message[:300]},
                )
                time.sleep(0.6)
                self._checkpoint_stop()

        self.audit.event(
            trace_id, "observation", observation.to_dict()
        )
        self._emit(
            "observation",
            {
                "step": step,
                "summary": observation.summary,
                "elements": len(observation.elements),
                "screenshot_cached": observation.screenshot_cached,
            },
        )
        return observation

    def _decide_with_retry(
        self,
        trace_id: str,
        goal: str,
        step: int,
        observation: Observation,
        history: list[StepRecord],
        task_context: str,
    ) -> Action:
        instructions: list[str] = []
        if self.control is not None:
            instructions = self.control.drain_instructions()
            if instructions:
                self._user_notes.extend(instructions)
                self._emit(
                    "instructions",
                    {"step": step, "items": instructions},
                )
        attempts = 0
        retry_note = ""
        while True:
            try:
                kwargs: dict[str, Any] = {}
                attempt_instructions = list(instructions)
                if retry_note:
                    attempt_instructions.append(retry_note)
                if attempt_instructions:
                    kwargs["instructions"] = attempt_instructions
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
                if attempts:
                    self._emit(
                        "provider_recovered",
                        {"step": step, "attempts": attempts},
                    )
                return action
            except RunPreempted:
                raise _Stop("aborted", "preempted by queued task")
            except RunCancelled:
                raise _Stop("aborted", "cancelled by user")
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                attempts += 1
                retryable = (
                    attempts <= self.max_provider_retries
                    and not self._fatal_provider_error(message)
                )
                self._emit(
                    "provider_retry",
                    {
                        "step": step,
                        "attempt": attempts,
                        "error": message[:300],
                        "retrying": retryable,
                    },
                )
                if not retryable:
                    raise _Stop(
                        "failed",
                        "模型连续 "
                        f"{attempts} 次未能给出可执行结果："
                        f"{message[:300]}",
                    )
                retry_note = (
                    "上一次回复无法解析或调用失败。"
                    "请只输出一个合法 JSON 对象，不要输出任何解释、"
                    "思考过程或 Markdown，并缩短 rationale。"
                )
                delay = self._retry_delay(message, attempts)
                if delay:
                    time.sleep(delay)
                self._checkpoint_stop()

    def _vision_backend(self) -> bool:
        return bool(getattr(self.adapter, "vision_only", False))

    @staticmethod
    def _login_wall(observation: Observation) -> bool:
        parts = [observation.active_window or "", observation.screen_card or ""]
        parts.extend(element.name for element in observation.elements if element.name)
        text = " ".join(parts)
        if not text.strip():
            return False
        has_login = bool(_LOGIN_TEXT.search(text))
        if not has_login:
            return False
        if _PASSWORD_TEXT.search(text) or _ACCOUNT_TEXT.search(text):
            return True
        return bool(_LOGIN_PROMPT_TEXT.search(text))

    def _login_wall_action(
        self,
        goal: str,
        task_context: str,
        observation: Observation,
        action: Action,
    ) -> Action | None:
        """Fail closed on a login wall when the user gave no credentials.

        UIA observations expose the login form, so the runtime can stop before
        the model wastes steps trying to view the page without logging in.
        Vision backends rely on the prompt rule; their screen card is also
        checked, which covers cached screens.
        """
        if action.capability.startswith("task.") or is_observe(
            action.capability
        ):
            return None
        if _LOGIN_GOAL_TEXT.search(goal or ""):
            return None
        if not self._login_wall(observation):
            return None
        combined = "\n".join([task_context, *self._user_notes])
        if _CREDENTIAL_VALUE.search(combined):
            return None
        window = str(observation.active_window or "").strip()
        target = f"“{window}”" if window else "当前窗口"
        prompt = (
            f"{target}需要登录，而任务没有提供账号和密码。"
            "请先在桌面上完成登录，然后在输入框发送“继续”。"
        )
        return Action(
            "task.await_user",
            {
                "kind": "login",
                "prompt": prompt,
                "summary": (
                    f"任务已暂停，等待你登录。\n\n{prompt}\n\n"
                    "登录完成后输入“继续”，我会从当前屏幕接着执行原任务。"
                ),
            },
            rationale="登录页需要用户先登录",
        )

    @staticmethod
    def _observation_digest(observation: Observation) -> str:
        digest = str((observation.raw or {}).get("screenshot_hash") or "")
        if digest:
            return digest
        return vision.digest_path(observation.screenshot_path)

    def _decision_observation(
        self, observation: Observation, step: int
    ) -> Observation:
        digest = self._observation_digest(observation)
        self._screen_hash = digest
        cached = (
            bool(digest)
            and digest == self._decided_hash
            and bool(self._screen_card)
        )
        if not cached:
            return observation
        self._emit(
            "screen_cache",
            {"step": step, "hash": digest[:12], "reused": True},
        )
        return replace(
            observation,
            screenshot_path=None,
            screen_card=self._screen_card,
            screenshot_cached=True,
        )

    def _remember_screen(
        self, observation: Observation, action: Action
    ) -> None:
        digest = self._observation_digest(observation)
        if not digest:
            return
        if digest != self._decided_hash:
            self._decided_hash = digest
            self._screen_card = action.screen_card
        elif action.screen_card:
            self._screen_card = action.screen_card

    @staticmethod
    def _plan_actions(action: Action, budget: int) -> list[Action]:
        limit = max(1, min(int(budget), MAX_PLAN_ACTIONS))
        planned: list[Action] = []
        for item in [action, *action.steps]:
            planned.append(item)
            if item.capability in _TERMINAL_CAPABILITIES or is_observe(
                item.capability
            ):
                break
            if len(planned) >= limit:
                break
        return planned

    def _capture_for_audit(self) -> str | None:
        """审计要图：先问当前通道，再退回桌面截图。

        纯浏览器模式没有桌面通道，绝不能顺手截一张桌面——那既无用，
        也违背「不占用用户屏幕」的约定。
        """
        for candidate in (self.adapter, self.hub.active()):
            if candidate is None:
                continue
            capture = getattr(candidate, "capture_screenshot", None)
            if not callable(capture):
                continue
            try:
                path = capture()
            except Exception:
                path = None
            if path:
                return str(path)
        if self.hub.get(DESKTOP_KIND) is None:
            return None
        saved = vision.capture_primary_screen(
            getattr(self.adapter, "screenshot_dir", ".xm-agent/screenshots"),
            prefix="audit",
        )
        if not saved:
            return None
        return str(saved.get("path") or "") or None

    def _capture_observation(
        self,
        step: int,
        action: Action,
        args: dict[str, Any],
    ) -> tuple[Receipt, Observation | None]:
        kind = self.hub.kind_for(action.capability)
        try:
            observation = self.hub.observe(
                step,
                include_screenshot=bool(args.get("include_screenshot")),
                kind=kind,
            )
        except Exception as exc:
            error = f"observation failed: {exc}"
            return Receipt(False, error=error), None
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
            observation,
        )

    def _finish(
        self,
        trace_id: str,
        status: str,
        reason: str,
        history: list[StepRecord],
        *,
        summary: str | None = None,
        await_user: dict[str, Any] | None = None,
    ) -> TaskResult:
        summary = summary or final_summary(status, reason, history)
        result = TaskResult(
            trace_id, status, history, reason, summary, await_user
        )
        self.audit.event(trace_id, "result", result.to_dict())
        payload: dict[str, Any] = {
            "trace_id": trace_id,
            "status": status,
            "reason": reason,
            "summary": summary,
            "steps": len(history),
        }
        if await_user:
            payload["await_user"] = await_user
        self._emit("result", payload)
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
