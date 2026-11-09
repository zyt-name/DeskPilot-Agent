import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from xm_agent.adapters.mock import MockAdapter
from xm_agent.audit import AuditLog
from xm_agent.contracts import CAPABILITIES
from xm_agent.control import RunControl
from xm_agent.guard import AuditGate
from xm_agent.guard.auditor import AuditVerdict
from xm_agent.lock import LeaseLock
from xm_agent.policy import Policy
from xm_agent.providers.fake import ScriptedProvider
from xm_agent.runtime import AgentRuntime
from xm_agent.types import (
    Action,
    Element,
    Observation,
    PolicyDecision,
    Receipt,
    StepRecord,
    Verification,
)


class FixedAuditor:
    def __init__(self, verdict: str) -> None:
        self.verdict = verdict

    def audit(
        self,
        descriptor,
        environment,
        policy,
        sensitivity,
        **kwargs,
    ):
        return AuditVerdict(
            verdict=self.verdict,
            risk="high" if self.verdict != "allow" else "low",
            categories=["data_loss"],
            purpose="执行终端命令",
            impact="可能删除项目文件",
            steps=["执行命令"],
            source="ai",
            reason="",
        )


class RecordingProvider:
    name = "recording"

    def __init__(self) -> None:
        self.instructions: list[str] | None = None

    def decide(
        self,
        goal,
        observation: Observation,
        history: list[StepRecord],
        remaining_steps: int,
        *,
        instructions=None,
        on_delta=None,
    ) -> Action:
        self.instructions = instructions
        return Action("task.finish", {"summary": "done"})


class ContextProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.context = ""

    def decide(
        self,
        goal,
        observation: Observation,
        history: list[StepRecord],
        remaining_steps: int,
        *,
        instructions=None,
        on_delta=None,
        context="",
    ) -> Action:
        self.context = context
        return super().decide(
            goal,
            observation,
            history,
            remaining_steps,
            instructions=instructions,
            on_delta=on_delta,
        )


class FlakyProvider:
    name = "flaky"

    def __init__(
        self,
        failures: int,
        action: Action,
        message: str = "model did not return valid JSON: 当前屏幕显示登录页",
    ) -> None:
        self.failures = failures
        self.action = action
        self.message = message
        self.calls = 0
        self.last_instructions: list[str] | None = None

    def decide(
        self,
        goal,
        observation: Observation,
        history: list[StepRecord],
        remaining_steps: int,
        *,
        instructions=None,
        on_delta=None,
    ) -> Action:
        self.calls += 1
        self.last_instructions = (
            list(instructions) if instructions is not None else None
        )
        if self.calls <= self.failures:
            raise ValueError(self.message)
        return self.action


class FlakyAdapter(MockAdapter):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.observe_calls = 0

    def observe(self, step, include_screenshot=False):
        self.observe_calls += 1
        if self.observe_calls <= self.failures:
            raise RuntimeError("windows-mcp snapshot timed out")
        return super().observe(step, include_screenshot)


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = MockAdapter()
        self.audit = AuditLog(self.root / "audit")
        self.lock = LeaseLock(self.root / "agent.lock", ttl_seconds=10)
        self.policy = Policy(
            allowed_capabilities=list(CAPABILITIES),
            allowed_command_prefixes=["echo"],
            require_confirmation=["file.transfer"],
            allowed_paths=[str(self.root)],
        )

    def _runtime(
        self,
        actions: list[Action],
        confirm: bool = True,
        max_failures: int = 2,
        on_event=None,
    ) -> AgentRuntime:
        return AgentRuntime(
            adapter=self.adapter,
            provider=ScriptedProvider(actions),
            policy=self.policy,
            audit=self.audit,
            lock=self.lock,
            max_steps=6,
            max_consecutive_failures=max_failures,
            confirm=lambda action, reason, mandatory=False: confirm,
            on_event=on_event,
        )

    def test_done_flow_writes_audit_and_releases_lock(self):
        result = self._runtime(
            [
                Action(
                    "desktop.act",
                    {"action": "type", "text": "hello"},
                    expect="hello",
                ),
                Action(
                    "desktop.act",
                    {"action": "click", "element_id": "e2"},
                    expect="saved",
                ),
                Action("task.finish", {"summary": "done"}),
            ]
        ).run("type hello and save")

        self.assertEqual(result.status, "done")
        self.assertEqual(self.adapter.text, "hello")
        self.assertTrue(self.adapter.saved)
        self.assertFalse(self.lock.path.exists())

        audit_text = self.audit.path_for(result.trace_id).read_text(
            encoding="utf-8"
        )
        self.assertIn('"event": "start"', audit_text)
        self.assertIn('"event": "result"', audit_text)
        self.assertIn('"event": "lock_released"', audit_text)

    def test_denied_command_is_not_executed(self):
        result = self._runtime(
            [
                Action(
                    "terminal.exec",
                    {"command": "curl http://example.com"},
                )
            ]
        ).run("run a denied command")

        self.assertEqual(result.status, "aborted")
        self.assertEqual(self.adapter.commands, [])
        self.assertFalse(self.lock.path.exists())

    def test_denied_confirmation_aborts(self):
        result = self._runtime(
            [
                Action(
                    "file.transfer",
                    {
                        "direction": "upload",
                        "source": str(self.root / "a.txt"),
                        "destination": str(self.root / "b.txt"),
                    },
                )
            ],
            confirm=False,
        ).run("upload a file")

        self.assertEqual(result.status, "aborted")
        self.assertEqual(self.adapter.transfers, [])

    def test_consecutive_failures_stop_the_loop(self):
        actions = [
            Action(
                "desktop.act",
                {"action": "type", "text": "x"},
                expect="saved",
            )
            for _ in range(3)
        ]
        result = self._runtime(actions, max_failures=2).run("bad expectation")

        self.assertEqual(result.status, "failed")
        self.assertIn("consecutive failures", result.reason)

    def test_observe_action_refreshes_observation(self):
        result = self._runtime(
            [
                Action("desktop.observe", {"include_screenshot": True}),
                Action("task.finish", {"summary": "looked and finished"}),
            ]
        ).run("look at the screen")

        self.assertEqual(result.status, "done")
        observe = result.steps[0]
        self.assertTrue(observe.receipt.ok)
        self.assertTrue(observe.verification.ok)

    def test_busy_lock_returns_needs_human(self):
        token = self.lock.acquire()
        try:
            result = self._runtime(
                [Action("task.finish", {"summary": "done"})]
            ).run("task")
            self.assertEqual(result.status, "needs_human")
            self.assertIn("桌面正被另一个进程控制", result.summary)
        finally:
            self.lock.release(token)

    def test_declared_failure_ends_with_chinese_summary(self):
        events = []
        declared = "未能完成：登录窗口没有出现，账号仍未登录。"
        result = self._runtime(
            [
                Action(
                    "task.finish",
                    {"success": False, "summary": declared},
                )
            ],
            on_event=lambda event, payload: events.append((event, payload)),
        ).run("登录账号")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.summary, declared)
        self.assertEqual(result.reason, declared)
        step_event = [
            payload
            for event, payload in events
            if event == "step" and payload.get("declared_failure")
        ]
        self.assertTrue(step_event)
        self.assertFalse(step_event[0]["verify_ok"])
        result_event = [payload for event, payload in events if event == "result"]
        self.assertEqual(result_event[-1]["summary"], declared)

    def test_result_event_carries_final_summary(self):
        events = []
        self._runtime(
            [Action("task.finish", {"summary": "已在桌面创建 hello.txt。"})],
            on_event=lambda event, payload: events.append((event, payload)),
        ).run("创建文件")

        result_event = [payload for event, payload in events if event == "result"]
        self.assertEqual(result_event[-1]["status"], "done")
        self.assertEqual(
            result_event[-1]["summary"], "已在桌面创建 hello.txt。"
        )

    def _audited_runtime(
        self,
        actions,
        verdict="allow",
        confirm=True,
        control=None,
        provider=None,
        on_event=None,
        max_audit_denials=2,
    ) -> AgentRuntime:
        gate = AuditGate(
            auditor=FixedAuditor(verdict),
            confirm=lambda action, prompt, mandatory=False: confirm,
            workspace_roots=[str(self.root)],
            project_root=str(self.root),
        )
        policy = Policy(
            allowed_capabilities=list(CAPABILITIES),
            allowed_command_prefixes=["rm", "dir"],
            allowed_paths=[str(self.root)],
        )
        return AgentRuntime(
            adapter=self.adapter,
            provider=provider or ScriptedProvider(actions),
            policy=policy,
            audit=self.audit,
            lock=self.lock,
            max_steps=6,
            max_consecutive_failures=2,
            confirm=lambda action, reason, mandatory=False: confirm,
            on_event=on_event,
            gate=gate,
            control=control,
            max_audit_denials=max_audit_denials,
        )

    def test_audit_allow_executes_action(self):
        result = self._audited_runtime(
            [
                Action("terminal.exec", {"command": "rm -r build"}),
                Action("task.finish", {"summary": "done"}),
            ]
        ).run("clean build")

        self.assertEqual(result.status, "done")
        self.assertEqual(self.adapter.commands, ["rm -r build"])
        audited = [step for step in result.steps if step.audit]
        self.assertTrue(audited)
        self.assertEqual(audited[0].audit["verdict"]["verdict"], "allow")

    def test_audit_ask_user_confirm_executes_action(self):
        result = self._audited_runtime(
            [
                Action("terminal.exec", {"command": "rm -r build"}),
                Action("task.finish", {"summary": "done"}),
            ],
            verdict="ask_user",
            confirm=True,
        ).run("clean build")

        self.assertEqual(result.status, "done")
        self.assertEqual(self.adapter.commands, ["rm -r build"])
        self.assertTrue(result.steps[0].audit["user_confirmed"])

    def test_audit_denial_skips_action_and_continues(self):
        result = self._audited_runtime(
            [
                Action("terminal.exec", {"command": "rm -r build"}),
                Action("task.finish", {"summary": "alternative done"}),
            ],
            verdict="deny",
        ).run("clean build")

        self.assertEqual(result.status, "done")
        self.assertEqual(self.adapter.commands, [])
        self.assertTrue(result.steps[0].error)

    def test_max_audit_denials_aborts(self):
        result = self._audited_runtime(
            [
                Action("terminal.exec", {"command": "rm -r a"}),
                Action("terminal.exec", {"command": "rm -r b"}),
                Action("terminal.exec", {"command": "rm -r c"}),
            ],
            verdict="deny",
            max_audit_denials=2,
        ).run("clean")

        self.assertEqual(result.status, "aborted")
        self.assertIn("audit denied", result.reason)
        self.assertEqual(self.adapter.commands, [])

    def test_pause_then_cancel_aborts_run(self):
        control = RunControl()
        started = threading.Event()

        def on_event(event, payload):
            if event == "start":
                started.set()

        runtime = self._audited_runtime(
            [Action("task.finish", {"summary": "done"})],
            control=control,
            on_event=on_event,
        )
        control.pause("用户暂停")
        results = []
        thread = threading.Thread(target=lambda: results.append(runtime.run("task")))
        thread.start()
        self.assertTrue(started.wait(3))
        time.sleep(0.05)
        self.assertFalse(results)
        control.cancel()
        thread.join(timeout=5)

        self.assertEqual(results[0].status, "aborted")
        self.assertIn("cancelled", results[0].reason)
        self.assertFalse(self.lock.path.exists())

    def test_preempt_aborts_run(self):
        control = RunControl()
        started = threading.Event()

        def on_event(event, payload):
            if event == "start":
                started.set()

        runtime = self._audited_runtime(
            [Action("task.finish", {"summary": "done"})],
            control=control,
            on_event=on_event,
        )
        control.pause()
        results = []
        thread = threading.Thread(target=lambda: results.append(runtime.run("task")))
        thread.start()
        self.assertTrue(started.wait(3))
        control.preempt()
        thread.join(timeout=5)

        self.assertEqual(results[0].status, "aborted")
        self.assertIn("preempted", results[0].reason)

    def test_control_instructions_reach_provider(self):
        control = RunControl()
        control.add_instruction("改用其他目录")
        provider = RecordingProvider()
        events = []
        result = self._audited_runtime(
            [],
            provider=provider,
            control=control,
            on_event=lambda event, payload: events.append(event),
        ).run("task")

        self.assertEqual(result.status, "done")
        self.assertEqual(provider.instructions, ["改用其他目录"])
        self.assertIn("instructions", events)

    def test_task_context_reaches_provider(self):
        provider = ContextProvider()
        result = self._audited_runtime([], provider=provider).run(
            "继续整理", context="任务记忆：上次已整理下载目录"
        )

        self.assertEqual(result.status, "done")
        self.assertIn("下载目录", provider.context)

    def test_streaming_deltas_are_emitted(self):
        events = []
        result = self._audited_runtime(
            [
                Action(
                    "desktop.act",
                    {"action": "type", "text": "hello"},
                    expect="hello",
                ),
                Action("task.finish", {"summary": "done"}),
            ],
            on_event=lambda event, payload: events.append((event, payload)),
        ).run("type hello")

        self.assertEqual(result.status, "done")
        assistant = [
            payload
            for event, payload in events
            if event == "assistant" and payload.get("step") == 1
        ]
        self.assertTrue(assistant)
        self.assertIn("desktop.act", assistant[0]["text"])

    def _retry_runtime(self, provider, retries=2, on_event=None):
        return AgentRuntime(
            adapter=self.adapter,
            provider=provider,
            policy=self.policy,
            audit=self.audit,
            lock=self.lock,
            max_steps=6,
            max_consecutive_failures=2,
            max_provider_retries=retries,
            provider_retry_delay=0,
            confirm=lambda action, reason, mandatory=False: True,
            on_event=on_event,
        )

    def test_provider_error_is_retried_with_json_instruction(self):
        events = []
        provider = FlakyProvider(
            1, Action("task.finish", {"summary": "done"})
        )
        result = self._retry_runtime(
            provider,
            on_event=lambda event, payload: events.append((event, payload)),
        ).run("task")

        self.assertEqual(result.status, "done")
        self.assertEqual(provider.calls, 2)
        retries = [payload for name, payload in events if name == "provider_retry"]
        self.assertEqual(retries[0]["attempt"], 1)
        self.assertTrue(retries[0]["retrying"])
        self.assertTrue(
            any(name == "provider_recovered" for name, _ in events)
        )
        self.assertTrue(
            any(
                "只输出一个合法 JSON 对象" in note
                for note in (provider.last_instructions or [])
            )
        )

    def test_provider_error_stops_after_retry_budget(self):
        provider = FlakyProvider(
            10, Action("task.finish", {"summary": "done"})
        )
        result = self._retry_runtime(provider, retries=1).run("task")

        self.assertEqual(result.status, "failed")
        self.assertEqual(provider.calls, 2)
        self.assertIn("模型连续 2 次未能给出可执行结果", result.reason)

    def test_fatal_provider_error_is_not_retried(self):
        provider = FlakyProvider(
            10,
            Action("task.finish", {"summary": "done"}),
            message="401 invalid api key",
        )
        result = self._retry_runtime(provider).run("task")

        self.assertEqual(result.status, "failed")
        self.assertEqual(provider.calls, 1)
        self.assertIn("401", result.reason)

    def _observation_runtime(self, adapter, on_event=None):
        return AgentRuntime(
            adapter=adapter,
            provider=ScriptedProvider(
                [Action("task.finish", {"summary": "done"})]
            ),
            policy=self.policy,
            audit=self.audit,
            lock=self.lock,
            max_steps=6,
            on_event=on_event,
        )

    def test_observation_failure_is_retried(self):
        events = []
        adapter = FlakyAdapter(failures=1)
        result = self._observation_runtime(
            adapter,
            on_event=lambda event, payload: events.append((event, payload)),
        ).run("task")

        self.assertEqual(result.status, "done")
        self.assertEqual(adapter.observe_calls, 2)
        self.assertTrue(
            any(name == "observation_retry" for name, _ in events)
        )

    def test_observation_failure_twice_ends_task(self):
        adapter = FlakyAdapter(failures=10)
        result = self._observation_runtime(adapter).run("task")

        self.assertEqual(result.status, "failed")
        self.assertEqual(adapter.observe_calls, 2)
        self.assertIn("observation failed", result.reason)


class VisionAdapter:
    name = "vision"
    vision_only = True

    def __init__(self, screenshot: Path) -> None:
        self.screenshot = str(screenshot)

    def observe(self, step, include_screenshot=False) -> Observation:
        return Observation(
            step=step,
            source="vision",
            summary="screenshot",
            screenshot_path=self.screenshot,
        )

    def execute(self, capability, args) -> Receipt:
        return Receipt(True, {})

    def verify(self, expect) -> Verification:
        return Verification(True, "deferred", deferred=True)

    def close(self) -> None:
        return None


class VisionProvider:
    name = "vision"
    vision = True

    def __init__(self, verdicts: list[tuple[bool, str]]) -> None:
        self.verdicts = list(verdicts)
        self.calls: list[tuple[str, str, str]] = []

    def decide(
        self,
        goal,
        observation,
        history,
        remaining_steps,
        *,
        instructions=None,
        on_delta=None,
    ) -> Action:
        return Action("task.finish", {"summary": "claim done"})

    def verify_completion(self, goal, summary, screenshot_path):
        self.calls.append((goal, summary, screenshot_path))
        return self.verdicts.pop(0)


class NoJudgeProvider:
    name = "no-judge"
    vision = True

    def decide(
        self,
        goal,
        observation,
        history,
        remaining_steps,
        *,
        instructions=None,
        on_delta=None,
    ) -> Action:
        return Action("task.finish", {"summary": "claim done"})


class VisionCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.shot = self.root / "shot.png"
        self.shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.audit = AuditLog(self.root / "audit")
        self.lock = LeaseLock(self.root / "agent.lock", ttl_seconds=10)
        self.policy = Policy(
            allowed_capabilities=list(CAPABILITIES),
            allowed_command_prefixes=["echo"],
            require_confirmation=["file.transfer"],
            allowed_paths=[str(self.root)],
        )

    def _run(self, provider, max_steps=4, max_failures=2, on_event=None):
        runtime = AgentRuntime(
            adapter=VisionAdapter(self.shot),
            provider=provider,
            policy=self.policy,
            audit=self.audit,
            lock=self.lock,
            max_steps=max_steps,
            max_consecutive_failures=max_failures,
            on_event=on_event,
        )
        return runtime.run("open github and search zmsy")

    def test_finish_requires_vision_confirmation(self):
        provider = VisionProvider(
            [
                (False, "Run dialog still open"),
                (True, "GitHub results for zmsy are visible"),
            ]
        )
        result = self._run(provider)

        self.assertEqual(result.status, "done")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0][2], str(self.shot))
        self.assertIn("Run dialog still open", result.steps[0].error or "")
        self.assertTrue(result.steps[1].verification.ok)

    def test_finish_rejected_twice_fails(self):
        provider = VisionProvider(
            [
                (False, "Run dialog still open"),
                (False, "still not GitHub"),
            ]
        )
        result = self._run(provider)

        self.assertEqual(result.status, "failed")
        self.assertIn("completion not verified", result.reason)
        self.assertTrue(
            all(
                step.action.capability == "task.finish"
                for step in result.steps
            )
        )

    def test_finish_is_blocked_without_a_judge(self):
        result = self._run(NoJudgeProvider())

        self.assertEqual(result.status, "failed")
        self.assertIn("no vision", result.reason)

    def test_finish_blocked_event_is_emitted(self):
        events = []
        provider = VisionProvider(
            [(False, "Run dialog still open"), (False, "still not GitHub")]
        )
        self._run(
            provider,
            on_event=lambda event, payload: events.append((event, payload)),
        )

        blocked = [
            payload
            for event, payload in events
            if event == "step" and payload.get("finish_blocked")
        ]
        self.assertTrue(blocked)
        self.assertFalse(blocked[0]["verify_ok"])


class CountingProvider:
    name = "counting"

    def __init__(self, actions: list[Action]) -> None:
        self.actions = list(actions)
        self.calls = 0
        self.observed: list[Observation] = []

    def decide(
        self,
        goal,
        observation: Observation,
        history: list[StepRecord],
        remaining_steps: int,
        *,
        instructions=None,
        on_delta=None,
        context="",
    ) -> Action:
        self.calls += 1
        self.observed.append(observation)
        if not self.actions:
            return Action("task.finish", {"summary": "done"})
        return self.actions.pop(0)


class CountingAdapter(MockAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.observe_calls = 0

    def observe(self, step, include_screenshot=False):
        self.observe_calls += 1
        return super().observe(step, include_screenshot)


class CachedScreenAdapter(CountingAdapter):
    def observe(self, step, include_screenshot=False):
        observation = super().observe(step, include_screenshot)
        observation.screenshot_path = "mock://screenshot.png"
        observation.raw["screenshot_hash"] = "stable-hash"
        return observation


class LoginWallAdapter(CountingAdapter):
    def observe(self, step, include_screenshot=False):
        observation = super().observe(step, include_screenshot)
        observation.active_window = "登录 - 测试系统"
        observation.elements = [
            Element("e1", "Edit", "账号", True, (10, 10, 120, 24)),
            Element("e2", "Edit", "密码", True, (10, 40, 120, 24)),
            Element("e3", "Button", "登录", True, (10, 70, 80, 24)),
        ]
        return observation


class BatchedPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audit = AuditLog(self.root / "audit")
        self.lock = LeaseLock(self.root / "agent.lock", ttl_seconds=10)
        self.policy = Policy(
            allowed_capabilities=list(CAPABILITIES),
            allowed_paths=[str(self.root)],
        )

    def _run(
        self,
        adapter,
        provider,
        on_event=None,
        max_steps=6,
        goal="type hello and save",
        context="",
    ):
        runtime = AgentRuntime(
            adapter=adapter,
            provider=provider,
            policy=self.policy,
            audit=self.audit,
            lock=self.lock,
            max_steps=max_steps,
            max_consecutive_failures=2,
            confirm=lambda action, reason, mandatory=False: True,
            on_event=on_event,
        )
        return runtime.run(goal, context=context)

    def test_plan_observes_and_decides_once_then_batches(self):
        adapter = CountingAdapter()
        provider = CountingProvider(
            [
                Action(
                    "desktop.act",
                    {"action": "type", "text": "hello"},
                    steps=(
                        Action(
                            "desktop.act",
                            {"action": "click", "element_id": "e2"},
                        ),
                        Action("task.finish", {"summary": "done"}),
                    ),
                )
            ]
        )
        events = []
        result = self._run(
            adapter,
            provider,
            on_event=lambda event, payload: events.append((event, payload)),
        )

        self.assertEqual(result.status, "done")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(adapter.observe_calls, 2)
        self.assertEqual(adapter.text, "hello")
        self.assertTrue(adapter.saved)
        self.assertEqual(len(result.steps), 3)
        self.assertTrue(result.steps[0].verification.deferred)
        self.assertTrue(result.steps[1].verification.deferred)
        self.assertEqual(
            result.steps[2].action.capability, "task.finish"
        )
        plans = [payload for event, payload in events if event == "plan"]
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(plans[0]["actions"]), 3)

    def test_failed_batch_step_drops_remaining_actions(self):
        adapter = CountingAdapter()
        provider = CountingProvider(
            [
                Action(
                    "desktop.act",
                    {"action": "frobnicate"},
                    steps=(
                        Action(
                            "desktop.act",
                            {"action": "click", "element_id": "e2"},
                        ),
                    ),
                )
            ]
        )
        result = self._run(adapter, provider)

        self.assertEqual(result.status, "done")
        self.assertEqual(provider.calls, 2)
        self.assertEqual(adapter.clicks, [])
        self.assertFalse(adapter.saved)
        self.assertFalse(result.steps[0].verification.ok)
        self.assertEqual(
            result.steps[1].action.capability, "task.finish"
        )

    def test_plan_stops_at_terminal_action(self):
        adapter = CountingAdapter()
        provider = CountingProvider(
            [
                Action(
                    "desktop.act",
                    {"action": "type", "text": "hello"},
                    steps=(
                        Action("task.finish", {"summary": "done"}),
                        Action(
                            "desktop.act",
                            {"action": "click", "element_id": "e2"},
                        ),
                    ),
                )
            ]
        )
        result = self._run(adapter, provider)

        self.assertEqual(result.status, "done")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(adapter.clicks, [])
        self.assertEqual(len(result.steps), 2)

    def test_unchanged_screen_reuses_screen_card(self):
        adapter = CachedScreenAdapter()
        provider = CountingProvider(
            [
                Action(
                    "desktop.act",
                    {"action": "type", "text": "hello"},
                    expect="hello",
                    screen_card="卡片：记事本已打开",
                )
            ]
        )
        events = []
        result = self._run(
            adapter,
            provider,
            on_event=lambda event, payload: events.append((event, payload)),
        )

        self.assertEqual(result.status, "done")
        self.assertEqual(provider.calls, 2)
        first, second = provider.observed
        self.assertEqual(first.screenshot_path, "mock://screenshot.png")
        self.assertFalse(first.screenshot_cached)
        self.assertIsNone(second.screenshot_path)
        self.assertTrue(second.screenshot_cached)
        self.assertEqual(second.screen_card, "卡片：记事本已打开")
        cache_events = [
            payload for event, payload in events if event == "screen_cache"
        ]
        self.assertEqual(len(cache_events), 1)
        self.assertTrue(cache_events[0]["reused"])

    def test_await_user_stops_without_executing_actions(self):
        adapter = CountingAdapter()
        provider = CountingProvider(
            [
                Action(
                    "task.await_user",
                    {
                        "kind": "login",
                        "prompt": "请先登录测试系统",
                        "summary": "任务已暂停，等待你登录。请登录后输入“继续”。",
                    },
                )
            ]
        )
        result = self._run(adapter, provider, goal="查看测试系统通知")

        self.assertEqual(result.status, "needs_human")
        self.assertEqual(result.await_user["kind"], "login")
        self.assertEqual(result.await_user["prompt"], "请先登录测试系统")
        self.assertIn("等待你登录", result.summary)
        self.assertEqual(result.steps[0].action.capability, "task.await_user")
        self.assertEqual(result.steps[0].receipt.ok, True)
        self.assertEqual(adapter.clicks, [])
        self.assertEqual(adapter.text, "")

    def test_login_wall_forces_await_user_without_credentials(self):
        adapter = LoginWallAdapter()
        provider = CountingProvider(
            [Action("desktop.act", {"action": "click", "element_id": "e3"})]
        )
        result = self._run(adapter, provider, goal="查看测试系统通知")

        self.assertEqual(result.status, "needs_human")
        self.assertEqual(result.await_user["kind"], "login")
        self.assertIn("登录", result.summary)
        self.assertEqual(result.steps[0].action.capability, "task.await_user")
        self.assertEqual(adapter.clicks, [])

    def test_login_wall_ignored_when_goal_mentions_login(self):
        adapter = LoginWallAdapter()
        provider = CountingProvider(
            [
                Action("desktop.act", {"action": "click", "element_id": "e3"}),
                Action("task.finish", {"summary": "已登录"}),
            ]
        )
        result = self._run(adapter, provider, goal="打开登录页面并登录")

        self.assertEqual(result.status, "done")
        self.assertEqual(adapter.clicks, ["e3"])

    def test_login_wall_ignored_when_credentials_in_goal(self):
        adapter = LoginWallAdapter()
        provider = CountingProvider(
            [
                Action("desktop.act", {"action": "click", "element_id": "e3"}),
                Action("task.finish", {"summary": "已登录"}),
            ]
        )
        result = self._run(
            adapter,
            provider,
            goal="用账号 demo 密码 demo123 登录后查看通知",
        )

        self.assertEqual(result.status, "done")
        self.assertEqual(adapter.clicks, ["e3"])


if __name__ == "__main__":
    unittest.main()
