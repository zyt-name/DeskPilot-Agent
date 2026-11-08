import tomllib
import unittest
from pathlib import Path

from xm_agent.config import load_config

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = load_config()
        self.assertEqual(config.default_adapter, "mock")
        self.assertEqual(config.runtime.max_steps, 12)
        self.assertIn("mock", config.adapters)

    def test_example_config(self):
        config = load_config(ROOT / "config" / "agent.example.toml")
        self.assertEqual(config.default_adapter, "mock")
        self.assertIn("windows", config.adapters)
        self.assertIn("xshell", config.adapters)
        self.assertEqual(config.adapters["windows"].kind, "windows_mcp")
        self.assertEqual(config.adapters["windows"].env["POSTHOG_API_KEY"], "")
        self.assertIn("--config", config.adapters["windows"].command)
        windows_config = tomllib.loads(
            (ROOT / "config" / "windows-mcp.toml").read_text(encoding="utf-8")
        )
        self.assertFalse(windows_config["control"]["enabled"])
        self.assertEqual(config.adapters["xshell"].kind, "xshell_mcp")
        self.assertEqual(config.adapters["xshell"].tools["exec"], "xshell_exec")
        self.assertIn("computer_use", config.adapters)
        self.assertEqual(
            config.adapters["computer_use"].kind, "computer_use_mcp"
        )
        self.assertEqual(
            config.adapters["computer_use"].tools["computer"], "computer"
        )
        for name in ("xshell", "computer_use"):
            adapter = config.adapters[name]
            self.assertEqual(adapter.command[0], "node")
            # Node resolves the script argument against cwd, so it must not
            # repeat the vendor/<name> prefix already implied by cwd.
            self.assertFalse(adapter.command[1].startswith("vendor/"))
            self.assertFalse(adapter.command[1].startswith("vendor\\"))
            self.assertTrue(adapter.cwd.startswith("vendor/"))
        self.assertIn("echo", config.policy.allowed_command_prefixes)
        self.assertEqual(config.model.provider, "openai_compat")


if __name__ == "__main__":
    unittest.main()
