import tempfile
import unittest
from pathlib import Path

from xm_agent.adapters.mock import MockAdapter
from xm_agent.providers.fake import HeuristicMockProvider
from xm_agent.providers.openai_compat import OpenAICompatProvider
from xm_agent.types import Observation


class ProviderTests(unittest.TestCase):
    def test_parse_json_with_code_fence(self):
        parsed = OpenAICompatProvider._parse_json(
            '```json\n{"capability": "task.finish", "args": {}}\n```'
        )
        self.assertEqual(parsed["capability"], "task.finish")

    def test_heuristic_provider_types_requested_text(self):
        provider = HeuristicMockProvider()
        observation = MockAdapter().observe(1)
        action = provider.decide("输入 hello 并保存", observation, [], 5)
        self.assertEqual(action.capability, "desktop.act")
        self.assertEqual(action.args["text"], "hello")
        self.assertEqual(action.expect, "hello")

    def test_user_content_attaches_screenshot_only_when_vision_enabled(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        shot = Path(tmp.name) / "shot.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        observation = Observation(step=1, screenshot_path=str(shot))

        text_only = OpenAICompatProvider("http://model", "m")
        self.assertIsInstance(
            text_only._user_content("goal", observation, [], 3), str
        )

        vision = OpenAICompatProvider("http://model", "m", vision=True)
        content = vision._user_content("goal", observation, [], 3)
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(
            content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        )

    def test_user_content_without_screenshot_stays_text(self):
        provider = OpenAICompatProvider("http://model", "m", vision=True)
        self.assertIsInstance(
            provider._user_content("goal", Observation(step=1), [], 3), str
        )


if __name__ == "__main__":
    unittest.main()
