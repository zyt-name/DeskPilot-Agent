from __future__ import annotations

import json
import unittest

from xm_agent.adapters.windows_mcp import WindowsMcpAdapter, parse_snapshot

SNAPSHOT = """Cursor Position: (100,200)
Screenshot Size: (1920,1080)
Visible Displays: 0:\\\\.\\DISPLAY1 (0,0,1920,1080) primary

Active Desktop:
Name
--------
Desktop 1

All Desktops:
Name
--------
Desktop 1

Focused Window:
Name                 Depth    Status    Width    Height    Handle
-------------------  -------  --------  -------  -------  --------
Untitled - Notepad   0        Normal    800      600      1234

Opened Windows:
Name                 Depth    Status    Width    Height    Handle
-------------------  -------  --------  -------  -------  --------
Untitled - Notepad   0        Normal    800      600      1234
Program Manager      0        Normal    1920     1080     5678

UI Tree:
desktop
└── window "Untitled - Notepad"
    ├── menu "Application"
    ├── (120,80) edit "Text editor"  [action: click]
    ├── (10,20) button "保存"  [action: click]focused
    └── (300,20) button ""  [action: click]
    └── (400,10) 按钮 "开始"  [action: click]
"""


class SnapshotParseTests(unittest.TestCase):
    def test_parses_elements_and_windows(self) -> None:
        elements, windows, active = parse_snapshot(SNAPSHOT)
        self.assertEqual(len(elements), 4)
        self.assertEqual(elements[0].id, "e0")
        self.assertEqual(elements[0].role, "edit")
        self.assertEqual(elements[0].name, "Text editor")
        self.assertEqual(elements[0].bounds, (120, 80, 1, 1))
        self.assertEqual(elements[1].name, "保存")
        self.assertEqual(elements[2].name, "")
        self.assertEqual(elements[3].role, "按钮")
        self.assertEqual(elements[3].name, "开始")
        self.assertEqual(windows, ["Untitled - Notepad"])
        self.assertEqual(active, "Untitled - Notepad")

    def test_survives_missing_sections(self) -> None:
        elements, windows, active = parse_snapshot("UI Tree:\ndesktop\n")
        self.assertEqual(elements, [])
        self.assertEqual(windows, [])
        self.assertIsNone(active)


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.snapshot = SNAPSHOT
        self.wait_text = "WaitFor condition 'text_exists' satisfied after 0.10s: found"

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        if name == "Snapshot":
            return {
                "content": [
                    {"type": "text", "text": json.dumps([self.snapshot])}
                ]
            }
        if name == "WaitFor":
            return {"content": [{"type": "text", "text": self.wait_text}]}
        return {"content": [{"type": "text", "text": "ok"}]}

    def close(self) -> None:
        pass


class WindowsAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _StubClient()
        self.adapter = WindowsMcpAdapter(self.client)

    def test_observe_read_only_snapshot(self) -> None:
        observation = self.adapter.observe(1)
        self.assertEqual(observation.source, "uia")
        self.assertEqual(observation.active_window, "Untitled - Notepad")
        self.assertEqual(len(observation.elements), 4)
        self.assertTrue(observation.summary.startswith("4 elements"))
        name, args = self.client.calls[0]
        self.assertEqual(name, "Snapshot")
        self.assertFalse(args["use_vision"])

    def test_click_resolves_observed_element(self) -> None:
        self.adapter.observe(1)
        receipt = self.adapter.execute(
            "desktop.act", {"action": "click", "element_id": "e1"}
        )
        self.assertTrue(receipt.ok)
        name, args = self.client.calls[-1]
        self.assertEqual(name, "Click")
        self.assertEqual(args["loc"], [10, 20])

    def test_click_uses_explicit_coordinates(self) -> None:
        receipt = self.adapter.execute(
            "desktop.act", {"action": "click", "x": 5, "y": 6}
        )
        self.assertTrue(receipt.ok)
        self.assertEqual(self.client.calls[-1][1]["loc"], [5, 6])

    def test_type_and_key_map_to_windows_tools(self) -> None:
        self.adapter.observe(1)
        self.adapter.execute(
            "desktop.act",
            {"action": "type", "element_id": "e0", "text": "hello", "clear": True},
        )
        name, args = self.client.calls[-1]
        self.assertEqual(name, "Type")
        self.assertEqual(args["loc"], [120, 80])
        self.assertEqual(args["text"], "hello")
        self.assertTrue(args["clear"])

        self.adapter.execute("desktop.act", {"action": "key", "keys": "ctrl+s"})
        name, args = self.client.calls[-1]
        self.assertEqual(name, "Shortcut")
        self.assertEqual(args["shortcut"], "ctrl+s")

    def test_scroll_direction_from_amount(self) -> None:
        self.adapter.execute(
            "desktop.act", {"action": "scroll", "x": 1, "y": 2, "amount": -3}
        )
        name, args = self.client.calls[-1]
        self.assertEqual(name, "Scroll")
        self.assertEqual(args["direction"], "up")
        self.assertEqual(args["wheel_times"], 3)

    def test_unknown_element_returns_error(self) -> None:
        receipt = self.adapter.execute(
            "desktop.act", {"action": "click", "element_id": "e99"}
        )
        self.assertFalse(receipt.ok)
        self.assertIn("observe", receipt.error)

    def test_verify_uses_waitfor_condition(self) -> None:
        self.adapter.observe(1)
        verification = self.adapter.verify({"text": "保存"})
        self.assertTrue(verification.ok)
        name, args = self.client.calls[-1]
        self.assertEqual(name, "WaitFor")
        self.assertEqual(args["condition"], "text_exists")
        self.assertEqual(args["text"], "保存")

        verification = self.adapter.verify({"element_id": "e1"})
        self.assertTrue(verification.ok)
        args = self.client.calls[-1][1]
        self.assertEqual(args["condition"], "element_exists")
        self.assertEqual(args["text"], "保存")

    def test_numeric_element_id_is_not_a_label(self) -> None:
        self.adapter.observe(1)
        receipt = self.adapter.execute(
            "desktop.act", {"action": "click", "element_id": "e0"}
        )
        self.assertTrue(receipt.ok)
        self.assertNotIn("label", self.client.calls[-1][1])


if __name__ == "__main__":
    unittest.main()
