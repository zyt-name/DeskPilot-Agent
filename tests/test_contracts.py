import unittest

from xm_agent.contracts import CAPABILITIES, capabilities_prompt, get_capability


class ContractTests(unittest.TestCase):
    def test_required_capabilities_present(self):
        required = {
            "desktop.observe",
            "desktop.act",
            "desktop.verify",
            "terminal.exec",
            "file.transfer",
            "wait.for",
            "task.finish",
        }
        self.assertTrue(required.issubset(CAPABILITIES))

    def test_each_capability_has_version_and_schema(self):
        for name, spec in CAPABILITIES.items():
            self.assertEqual(spec.name, name)
            self.assertTrue(spec.version)
            self.assertEqual(spec.input_schema.get("type"), "object")
            self.assertEqual(spec.output_schema.get("type"), "object")

    def test_unknown_capability_raises(self):
        with self.assertRaises(KeyError):
            get_capability("browser.open")

    def test_prompt_lists_every_capability(self):
        prompt = capabilities_prompt()
        for name in CAPABILITIES:
            self.assertIn(name, prompt)


if __name__ == "__main__":
    unittest.main()
