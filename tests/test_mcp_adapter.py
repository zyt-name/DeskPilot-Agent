import sys
import unittest
from pathlib import Path

from xm_agent.adapters.mcp_tool import McpToolAdapter
from xm_agent.mcp_stdio import StdioMCPClient

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "examples" / "fake_mcp_server.py"


class McpToolAdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = StdioMCPClient(
            [sys.executable, str(SERVER)], default_timeout=10
        )
        self.adapter = McpToolAdapter(self.client)
        self.addCleanup(self.adapter.close)

    def test_observe_returns_structured_elements(self):
        observation = self.adapter.observe(1)
        self.assertEqual(observation.active_window, "Notepad")
        self.assertGreaterEqual(len(observation.elements), 2)
        self.assertEqual(observation.elements[0].id, "e1")
        self.assertEqual(observation.elements[0].role, "Edit")

    def test_click_returns_receipt(self):
        receipt = self.adapter.execute(
            "desktop.act", {"action": "click", "element_id": "e2"}
        )
        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.detail.get("clicked"), "e2")
        self.assertTrue(receipt.detail.get("saved"))

    def test_terminal_exec_maps_to_configured_tool(self):
        receipt = self.adapter.execute(
            "terminal.exec", {"command": "echo hi"}
        )
        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.detail.get("exit_code"), 0)
        self.assertIn("echo hi", receipt.detail.get("stdout", ""))

    def test_verify_uses_wait_for(self):
        self.assertTrue(self.adapter.verify("ready").ok)
        self.assertFalse(self.adapter.verify("missing-text").ok)


if __name__ == "__main__":
    unittest.main()
