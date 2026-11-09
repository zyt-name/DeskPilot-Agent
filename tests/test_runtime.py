import threading
import time
import unittest
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
    Observation,
    PolicyDecision,
    Receipt,
    StepRecord,
    Verification,
)


class FixedAuditor:
    def __init__(self, verdict: str) -> None:
        self.verdict = verdict

    def audit(self, descriptor, environment, policy, sensitivity):
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


if __name__ == "__main__":
    unittest.main()
