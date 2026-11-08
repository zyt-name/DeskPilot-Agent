import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from xm_agent.policy import Policy
from xm_agent.types import Action


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = Policy(
            allowed_capabilities=[
                "desktop.act",
                "terminal.exec",
                "file.transfer",
                "task.finish",
            ],
            allowed_command_prefixes=["echo", "rm"],
            require_confirmation=["file.transfer"],
            allowed_paths=[str(self.root)],
        )

    def test_unknown_capability_denied(self):
        decision = self.policy.check(Action("browser.open", {}))
        self.assertEqual(decision.decision, "deny")

    def test_command_not_in_allowlist_denied(self):
        decision = self.policy.check(
            Action("terminal.exec", {"command": "curl http://example.com"})
        )
        self.assertEqual(decision.decision, "deny")

    def test_safe_command_allowed(self):
        decision = self.policy.check(
            Action("terminal.exec", {"command": "echo hello"})
        )
        self.assertEqual(decision.decision, "allow")

    def test_destructive_command_requires_confirmation(self):
        decision = self.policy.check(
            Action("terminal.exec", {"command": "rm -rf /tmp/x"})
        )
        self.assertEqual(decision.decision, "confirm")

    def test_empty_command_denied(self):
        decision = self.policy.check(
            Action("terminal.exec", {"command": "   "})
        )
        self.assertEqual(decision.decision, "deny")

    def test_file_transfer_inside_root_requires_confirmation(self):
        decision = self.policy.check(
            Action(
                "file.transfer",
                {
                    "direction": "upload",
                    "source": str(self.root / "a.txt"),
                    "destination": str(self.root / "b.txt"),
                },
            )
        )
        self.assertEqual(decision.decision, "confirm")

    def test_file_transfer_outside_root_denied(self):
        decision = self.policy.check(
            Action(
                "file.transfer",
                {
                    "direction": "upload",
                    "source": str(self.root.parent / "outside.txt"),
                    "destination": str(self.root / "b.txt"),
                },
            )
        )
        self.assertEqual(decision.decision, "deny")


if __name__ == "__main__":
    unittest.main()
