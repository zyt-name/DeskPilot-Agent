"""Cross-backend capability contract tests.

Every backend must satisfy the same structural contract: ``observe`` returns a
well-formed ``Observation``, termination capabilities never reach a backend,
and ``verify`` returns a ``Verification`` (possibly deferred). Adding a backend
only requires one entry in ``_build`` — the cases below stay unchanged.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from xm_agent.adapters.computer_use_mcp import ComputerUseMcpAdapter
from xm_agent.adapters.mock import MockAdapter
from xm_agent.adapters.windows_mcp import WindowsMcpAdapter
from xm_agent.adapters.xshell_mcp import XshellMcpAdapter
from xm_agent.contracts import CAPABILITIES
from xm_agent.types import Element, Observation, Receipt, Verification

PNG = b"\x89PNG\r\n\x1a\nfake"

SNAPSHOT = """Focused Window:
Name                 Depth    Status    Width    Height    Handle
-------------------  -------  --------  -------  -------  --------
Untitled - Notepad   0        Normal    800      600      1234

UI Tree:
desktop
└── window "Untitled - Notepad"
    ├── (120,80) edit "Text editor"  [action: click]
    └── (10,20) button "保存"  [action: click]
"""


class _SnapshotClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": SNAPSHOT}]}

    def list_tools(self) -> list[dict]:
        return []

    def close(self) -> None:
        self.closed = True


class _ComputerClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"image_width": 1280, "image_height": 720}
                    ),
                },
                {
                    "type": "image",
                    "data": base64.b64encode(PNG).decode("ascii"),
                    "mimeType": "image/png",
                },
            ]
        }

    def list_tools(self) -> list[dict]:
        return []

    def close(self) -> None:
        self.closed = True


class _XshellClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        if name == "xshell_read_screen":
            return {
                "content": [{"type": "text", "text": "user@host:~$ ls\nfile.txt"}]
            }
        return {"content": [{"type": "text", "text": "ok"}]}

    def list_tools(self) -> list[dict]:
        return []

    def close(self) -> None:
        self.closed = True


def _build(name: str, shots: Path):
    if name == "mock":
        return MockAdapter()
    if name == "windows":
        return WindowsMcpAdapter(_SnapshotClient(), screenshot_dir=shots)
    if name == "computer_use":
        return ComputerUseMcpAdapter(_ComputerClient(), screenshot_dir=shots)
    if name == "xshell":
        return XshellMcpAdapter(_XshellClient())
    raise AssertionError(f"unknown backend: {name}")


@pytest.fixture(params=["mock", "windows", "computer_use", "xshell"])
def backend(request, tmp_path):
    adapter = _build(request.param, tmp_path)
    try:
        yield request.param, adapter
    finally:
        adapter.close()


def test_observe_matches_contract(backend):
    name, adapter = backend
    observation = adapter.observe(3)
    assert isinstance(observation, Observation), name
    assert observation.step == 3, name
    assert observation.source.strip(), name
    assert observation.summary.strip(), name
    assert isinstance(observation.elements, list), name
    assert all(isinstance(item, Element) for item in observation.elements), name
    assert observation.active_window is None or isinstance(
        observation.active_window, str
    ), name


def test_termination_capabilities_never_reach_backends(backend):
    name, adapter = backend
    for capability in ("task.finish", "task.await_user"):
        receipt = adapter.execute(capability, {})
        assert isinstance(receipt, Receipt), name
        assert receipt.ok is False, f"{name} must fail closed on {capability}"
        assert receipt.error, name


def test_verify_returns_contract_shape(backend):
    name, adapter = backend
    verification = adapter.verify(None)
    assert isinstance(verification, Verification), name
    assert verification.ok is True, name
    assert isinstance(verification.deferred, bool), name


def test_close_is_idempotent(backend):
    _, adapter = backend
    adapter.close()
    adapter.close()


def test_capability_contract_is_stable():
    assert len(CAPABILITIES) == 8
    assert "task.await_user" in CAPABILITIES
    assert "desktop.observe" in CAPABILITIES
