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
        self.assertEqual(config.adapters["xshell"].kind, "xshell_mcp")
        self.assertEqual(config.adapters["xshell"].tools["exec"], "xshell_exec")
        self.assertIn("computer_use", config.adapters)
        self.assertEqual(
            config.adapters["computer_use"].kind, "computer_use_mcp"
        )
        self.assertEqual(
            config.adapters["computer_use"].tools["computer"], "computer"
        )
        self.assertIn("echo", config.policy.allowed_command_prefixes)
        self.assertEqual(config.model.provider, "openai_compat")
        self.assertFalse(config.model.vision)


if __name__ == "__main__":
    unittest.main()
