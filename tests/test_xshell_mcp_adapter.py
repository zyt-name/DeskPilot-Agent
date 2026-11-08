from __future__ import annotations

import unittest

from xm_agent.adapters.xshell_mcp import XshellMcpAdapter


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_wait = False

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        if name == "xshell_read_screen":
            return {"content": [{"type": "text", "text": "user@host:~$ ls\nfile.txt"}]}
        if name == "xshell_exec":
            return {"content": [{"type": "text", "text": "file.txt\n__DONE__"}]}
        if name == "xshell_wait_for":
            if self.fail_wait:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": "Timed out waiting for text"}],
                }
            return {"content": [{"type": "text", "text": "Matched."}]}
        return {"content": [{"type": "text", "text": "ok"}]}

    def close(self) -> None:
        pass


class XshellAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _StubClient()
        self.adapter = XshellMcpAdapter(self.client)

    def test_observe_reads_screen(self) -> None:
        observation = self.adapter.observe(1)
        self.assertEqual(observation.source, "terminal")
        self.assertIn("user@host", observation.summary)
        name, args = self.client.calls[0]
        self.assertEqual(name, "xshell_read_screen")
        self.assertEqual(args["sessionId"], "default")

    def test_exec_maps_session_and_timeout(self) -> None:
        receipt = self.adapter.execute(
            "terminal.exec",
            {"host": "server-a", "command": "ls", "timeout_s": 5},
        )
        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.detail["stdout"], "file.txt\n__DONE__")
        name, args = self.client.calls[-1]
        self.assertEqual(name, "xshell_exec")
        self.assertEqual(args["command"], "ls")
        self.assertEqual(args["sessionId"], "server-a")
        self.assertEqual(args["timeoutMs"], 5000)

    def test_file_transfer_is_unsupported(self) -> None:
        receipt = self.adapter.execute(
            "file.transfer",
            {"direction": "upload", "source": "a", "destination": "b"},
        )
        self.assertFalse(receipt.ok)
        self.assertIn("SFTP", receipt.error)

    def test_verify_wait_for(self) -> None:
        verification = self.adapter.verify({"text": "file.txt"})
        self.assertTrue(verification.ok)
        args = self.client.calls[-1][1]
        self.assertEqual(args["sessionId"], "default")
        self.assertEqual(args["timeoutMs"], 30000)

    def test_verify_timeout_is_failure(self) -> None:
        self.client.fail_wait = True
        verification = self.adapter.verify({"text": "missing"})
        self.assertFalse(verification.ok)
        self.assertIn("Timed out", verification.detail)


if __name__ == "__main__":
    unittest.main()
