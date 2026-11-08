import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from xm_agent.adapters.mock import MockAdapter
from xm_agent.audit import AuditLog
from xm_agent.contracts import CAPABILITIES
from xm_agent.lock import LeaseLock
from xm_agent.policy import Policy
from xm_agent.providers.fake import ScriptedProvider
from xm_agent.runtime import AgentRuntime
from xm_agent.types import Action


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
    ) -> AgentRuntime:
        return AgentRuntime(
            adapter=self.adapter,
            provider=ScriptedProvider(actions),
            policy=self.policy,
            audit=self.audit,
            lock=self.lock,
            max_steps=6,
            max_consecutive_failures=max_failures,
            confirm=lambda action, reason: confirm,
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

    def test_busy_lock_returns_needs_human(self):
        token = self.lock.acquire()
        try:
            result = self._runtime(
                [Action("task.finish", {"summary": "done"})]
            ).run("task")
            self.assertEqual(result.status, "needs_human")
        finally:
            self.lock.release(token)


if __name__ == "__main__":
    unittest.main()
