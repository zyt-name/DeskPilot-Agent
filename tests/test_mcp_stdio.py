import sys
import unittest
from pathlib import Path

from xm_agent.mcp_stdio import StdioMCPClient

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "examples" / "fake_mcp_server.py"


class StdioMCPClientTests(unittest.TestCase):
    def test_initialize_list_and_call(self):
        client = StdioMCPClient([sys.executable, str(SERVER)], default_timeout=10)
        self.addCleanup(client.close)

        tools = client.list_tools()
        names = {tool["name"] for tool in tools}
        self.assertIn("Snapshot", names)
        self.assertIn("xshell_send", names)

        result = client.call_tool("Snapshot", {})
        text = result["content"][0]["text"]
        self.assertIn("Notepad", text)
        self.assertFalse(result.get("isError", False))

    def test_unknown_tool_is_error(self):
        client = StdioMCPClient([sys.executable, str(SERVER)], default_timeout=10)
        self.addCleanup(client.close)
        result = client.call_tool("NoSuchTool", {})
        self.assertTrue(result.get("isError"))


if __name__ == "__main__":
    unittest.main()
