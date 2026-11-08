from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from xm_agent.adapters.computer_use_mcp import ComputerUseMcpAdapter
from xm_agent.mcp_stdio import MCPError

PNG = b"\x89PNG\r\n\x1a\nfake"


class FakeComputerClient:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return self.results.pop(0)

    def list_tools(self) -> list[dict[str, Any]]:
        return []

    def close(self) -> None:
        self.closed = True


def shot_result(width: int = 1280, height: int = 720) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"image_width": width, "image_height": height}
                ),
            },
            {
                "type": "image",
                "data": base64.b64encode(PNG).decode("ascii"),
                "mimeType": "image/png",
            },
        ]
    }


def ok_result() -> dict[str, Any]:
    return {"content": [{"type": "text", "text": '{"ok":true}'}]}


class ComputerUseAdapterTests(unittest.TestCase):
    def make(self, results: list[dict[str, Any]]) -> tuple[ComputerUseMcpAdapter, FakeComputerClient, tempfile.TemporaryDirectory]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        client = FakeComputerClient(results)
        adapter = ComputerUseMcpAdapter(
            client,  # type: ignore[arg-type]
            screenshot_dir=Path(tmp.name) / "shots",
        )
        return adapter, client, tmp

    def test_observe_saves_screenshot_and_dimensions(self) -> None:
        adapter, client, tmp = self.make([shot_result(1536, 864)])
        obs = adapter.observe(3)
        self.assertEqual(client.calls, [("computer", {"action": "get_screenshot"})])
        self.assertEqual(obs.source, "vision")
        self.assertEqual(obs.raw["image_width"], 1536)
        self.assertEqual(obs.raw["image_height"], 864)
        self.assertIn("1536x864", obs.summary)
        self.assertEqual(obs.elements, [])
        assert obs.screenshot_path is not None
        saved = Path(obs.screenshot_path)
        self.assertTrue(saved.exists())
        self.assertEqual(saved.read_bytes(), PNG)
        self.assertTrue(str(saved).startswith(str(Path(tmp.name).resolve())))

    def test_click_maps_buttons_to_actions(self) -> None:
        adapter, client, _ = self.make([ok_result(), ok_result(), ok_result()])
        for button, expected in (
            ("left", "left_click"),
            ("right", "right_click"),
            ("double", "double_click"),
        ):
            receipt = adapter.execute(
                "desktop.act",
                {"action": "click", "x": 10, "y": 20, "button": button},
            )
            self.assertTrue(receipt.ok)
        self.assertEqual(
            client.calls,
            [
                ("computer", {"action": "left_click", "coordinate": [10, 20]}),
                ("computer", {"action": "right_click", "coordinate": [10, 20]}),
                ("computer", {"action": "double_click", "coordinate": [10, 20]}),
            ],
        )

    def test_type_key_and_move(self) -> None:
        adapter, client, _ = self.make([ok_result(), ok_result(), ok_result()])
        self.assertTrue(
            adapter.execute("desktop.act", {"action": "type", "text": "hello"}).ok
        )
        self.assertTrue(
            adapter.execute("desktop.act", {"action": "key", "keys": "ctrl+s"}).ok
        )
        self.assertTrue(
            adapter.execute("desktop.act", {"action": "move", "x": 5, "y": 6}).ok
        )
        self.assertEqual(
            client.calls,
            [
                ("computer", {"action": "type", "text": "hello"}),
                ("computer", {"action": "key", "text": "ctrl+s"}),
                ("computer", {"action": "mouse_move", "coordinate": [5, 6]}),
            ],
        )

    def test_type_clear_and_press_enter(self) -> None:
        adapter, client, _ = self.make(
            [ok_result(), ok_result(), ok_result()]
        )
        receipt = adapter.execute(
            "desktop.act",
            {
                "action": "type",
                "text": "https://github.com/search?q=zmsy",
                "clear": True,
                "press_enter": True,
            },
        )
        self.assertTrue(receipt.ok)
        self.assertEqual(
            client.calls,
            [
                ("computer", {"action": "key", "text": "ctrl+a"}),
                (
                    "computer",
                    {
                        "action": "type",
                        "text": "https://github.com/search?q=zmsy",
                    },
                ),
                ("computer", {"action": "key", "text": "return"}),
            ],
        )

    def test_scroll_uses_given_coordinate(self) -> None:
        adapter, client, _ = self.make([ok_result(), ok_result()])
        adapter.execute("desktop.act", {"action": "scroll", "amount": 2, "x": 8, "y": 9})
        adapter.execute("desktop.act", {"action": "scroll", "amount": -1, "x": 8, "y": 9})
        self.assertEqual(
            client.calls,
            [
                (
                    "computer",
                    {"action": "scroll", "coordinate": [8, 9], "text": "down:200"},
                ),
                (
                    "computer",
                    {"action": "scroll", "coordinate": [8, 9], "text": "up:100"},
                ),
            ],
        )

    def test_scroll_without_coordinate_falls_back_to_cursor(self) -> None:
        adapter, client, _ = self.make(
            [
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"x": 640, "y": 360}),
                        }
                    ]
                },
                ok_result(),
            ]
        )
        receipt = adapter.execute("desktop.act", {"action": "scroll"})
        self.assertTrue(receipt.ok)
        self.assertEqual(
            client.calls,
            [
                ("computer", {"action": "get_cursor_position"}),
                (
                    "computer",
                    {
                        "action": "scroll",
                        "coordinate": [640, 360],
                        "text": "down:300",
                    },
                ),
            ],
        )

    def test_element_id_and_missing_coordinate_are_rejected(self) -> None:
        adapter, client, _ = self.make([])
        by_element = adapter.execute(
            "desktop.act", {"action": "click", "element_id": "e1"}
        )
        self.assertFalse(by_element.ok)
        self.assertIn("no element ids", by_element.error or "")
        by_missing = adapter.execute("desktop.act", {"action": "move"})
        self.assertFalse(by_missing.ok)
        self.assertEqual(client.calls, [])

    def test_verify_captures_screenshot_and_defers_semantics(self) -> None:
        adapter, client, _ = self.make([shot_result()])
        verification = adapter.verify("saved")
        self.assertTrue(verification.ok)
        self.assertTrue(verification.deferred)
        self.assertTrue(verification.evidence["deferred"])
        self.assertEqual(verification.evidence["expect"], "saved")
        self.assertTrue(Path(verification.evidence["screenshot_path"]).exists())
        self.assertEqual(
            client.calls, [("computer", {"action": "get_screenshot"})]
        )

    def test_verify_without_expectation_skips_capture(self) -> None:
        adapter, client, _ = self.make([])
        verification = adapter.verify(None)
        self.assertTrue(verification.ok)
        self.assertEqual(client.calls, [])

    def test_wait_for_sleeps_and_captures(self) -> None:
        adapter, _, _ = self.make([shot_result()])
        receipt = adapter.execute("wait.for", {"timeout_s": 0})
        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.detail["deferred"])

    def test_error_result_is_reported(self) -> None:
        adapter, _, _ = self.make(
            [{"isError": True, "content": [{"type": "text", "text": "boom"}]}]
        )
        with self.assertRaises(MCPError):
            adapter.observe(1)
        adapter2, _, _ = self.make(
            [{"isError": True, "content": [{"type": "text", "text": "boom"}]}]
        )
        receipt = adapter2.execute("desktop.act", {"action": "type", "text": "x"})
        self.assertFalse(receipt.ok)
        self.assertIn("boom", receipt.error or "")

    def test_unsupported_capability(self) -> None:
        adapter, _, _ = self.make([])
        receipt = adapter.execute("terminal.exec", {"command": "pwd"})
        self.assertFalse(receipt.ok)
        self.assertIn("unsupported", receipt.error or "")


if __name__ == "__main__":
    unittest.main()
