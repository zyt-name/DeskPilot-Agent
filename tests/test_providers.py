import json
import tempfile
import unittest
from pathlib import Path

from xm_agent.adapters.mock import MockAdapter
from xm_agent.providers.common import parse_action_plan, parse_verdict_json
from xm_agent.providers.fake import HeuristicMockProvider
from xm_agent.providers.openai_compat import OpenAICompatProvider, ProviderError
from xm_agent.types import Observation


class ProviderTests(unittest.TestCase):
    def test_parse_json_with_code_fence(self):
        parsed = OpenAICompatProvider._parse_json(
            '```json\n{"capability": "task.finish", "args": {}}\n```'
        )
        self.assertEqual(parsed["capability"], "task.finish")

    def test_parse_json_embedded_in_prose(self):
        parsed = OpenAICompatProvider._parse_json(
            "当前屏幕显示登录页，先清空密码框。\n"
            '{"capability": "desktop.act", "args": '
            '{"action": "key", "keys": ["ctrl+a"]}}\n'
            "执行完后再观察。"
        )
        self.assertEqual(parsed["capability"], "desktop.act")
        self.assertEqual(parsed["args"]["keys"], ["ctrl+a"])

    def test_parse_json_ignores_braces_in_strings(self):
        parsed = OpenAICompatProvider._parse_json(
            '说明：使用 {占位符} 表示。\n'
            '{"capability":"desktop.act","args":{"text":"a}b{c"}}'
        )
        self.assertEqual(parsed["args"]["text"], "a}b{c")

    def test_parse_json_rejects_unbalanced_output(self):
        with self.assertRaises(ProviderError):
            OpenAICompatProvider._parse_json(
                '{"capability": "desktop.act", "args": {'
            )

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
        text = text_only._user_content("goal", observation, [], 3)
        self.assertIsInstance(text, str)
        self.assertFalse(
            json.loads(text)["observation"]["screenshot_attached"]
        )

        vision = OpenAICompatProvider("http://model", "m", vision=True)
        content = vision._user_content("goal", observation, [], 3)
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertTrue(
            json.loads(content[0]["text"])["observation"]["screenshot_attached"]
        )
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(
            content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        )

    def test_user_content_without_screenshot_stays_text(self):
        provider = OpenAICompatProvider("http://model", "m", vision=True)
        self.assertIsInstance(
            provider._user_content("goal", Observation(step=1), [], 3), str
        )

    def test_verify_completion_needs_vision_and_screenshot(self):
        provider = OpenAICompatProvider("http://model", "m", vision=False)
        ok, reason = provider.verify_completion("goal", "summary", None)
        self.assertFalse(ok)
        self.assertIn("vision", reason)

    def test_verify_completion_sends_image_and_parses_verdict(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        shot = Path(tmp.name) / "shot.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        provider = OpenAICompatProvider("http://model", "m", vision=True)
        captured = {}

        def fake_chat(messages, *, max_tokens=None, on_delta=None):
            captured["messages"] = messages
            return (
                '{"done": false, "reason": "Run dialog still open"}',
                {},
                1,
            )

        provider._chat = fake_chat  # type: ignore[method-assign]
        ok, reason = provider.verify_completion(
            "open github", "done", str(shot)
        )

        self.assertFalse(ok)
        self.assertIn("Run dialog", reason)
        content = captured["messages"][1]["content"]
        self.assertEqual(content[1]["type"], "image_url")

    def test_user_content_injects_task_context(self):
        provider = OpenAICompatProvider("http://model", "m")
        text = provider._user_content(
            "goal",
            Observation(step=1),
            [],
            3,
            task_context="任务记忆：用户先整理了下载目录",
        )
        payload = json.loads(text)
        assert "下载目录" in payload["task_context"]

    def test_decide_sends_task_context_to_model(self):
        provider = OpenAICompatProvider("http://model", "m")
        captured = {}

        def fake_chat(messages, *, max_tokens=None, on_delta=None):
            captured["messages"] = messages
            return '{"capability": "task.finish", "args": {"summary": "ok"}}', {}, 1

        provider._chat = fake_chat  # type: ignore[method-assign]
        action = provider.decide(
            "整理桌面",
            Observation(step=1),
            [],
            3,
            context="任务记忆：上一次已完成下载目录整理",
        )
        assert action.capability == "task.finish"
        payload = json.loads(captured["messages"][1]["content"])
        assert "下载目录" in payload["task_context"]

    def test_parse_action_plan_batches_known_steps(self):
        action = parse_action_plan(
            {
                "capability": "desktop.act",
                "args": {"action": "type", "text": "hello"},
                "expect": "hello",
                "screen_card": "记事本已打开",
                "steps": [
                    {
                        "capability": "desktop.act",
                        "args": {"action": "click", "element_id": "e2"},
                    },
                    {"capability": "task.finish", "args": {"summary": "ok"}},
                    {"capability": "made.up", "args": {}},
                ],
            }
        )

        self.assertEqual(action.capability, "desktop.act")
        self.assertEqual(action.screen_card, "记事本已打开")
        self.assertEqual(
            [step.capability for step in action.steps],
            ["desktop.act", "task.finish"],
        )

    def test_parse_action_plan_rejects_unknown_first_action(self):
        with self.assertRaises(ValueError):
            parse_action_plan({"capability": "made.up", "args": {}})

    def test_parse_verdict_json_is_conservative(self):
        self.assertEqual(
            parse_verdict_json('{"done": true, "reason": "visible"}'),
            (True, "visible"),
        )
        self.assertFalse(parse_verdict_json("yes, it looks done")[0])
        self.assertFalse(parse_verdict_json('{"done": "maybe"}')[0])
        self.assertFalse(parse_verdict_json('{"reason": "no field"}')[0])


if __name__ == "__main__":
    unittest.main()
