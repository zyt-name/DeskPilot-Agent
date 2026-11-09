from __future__ import annotations

import pytest

from xm_agent.config import AdapterConfig, AgentConfig
from xm_agent.control import RunControl
from xm_agent.ui.desktop import DesktopHandoff
from xm_agent.ui.server import UiServer


class _FakeWindow:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: str | None = None

    def minimize(self) -> None:
        if self.fail == "minimize":
            raise RuntimeError("no window")
        self.calls.append("minimize")

    def restore(self) -> None:
        if self.fail == "restore":
            raise RuntimeError("no window")
        self.calls.append("restore")


def test_handoff_is_a_noop_without_a_window():
    handoff = DesktopHandoff()
    assert handoff.available is False
    assert handoff.released is False
    assert handoff.release() is False
    assert handoff.reclaim() is False


def test_handoff_minimises_once_and_restores_once():
    window = _FakeWindow()
    handoff = DesktopHandoff(window)
    assert handoff.available is True

    assert handoff.release() is True
    assert handoff.released is True
    assert handoff.release() is True
    assert window.calls == ["minimize"]

    assert handoff.reclaim() is True
    assert handoff.released is False
    assert handoff.reclaim() is False
    assert window.calls == ["minimize", "restore"]


def test_handoff_attaches_window_after_start():
    window = _FakeWindow()
    handoff = DesktopHandoff()
    handoff.attach(window)
    assert handoff.release() is True
    assert window.calls == ["minimize"]


def test_handoff_swallows_window_errors():
    window = _FakeWindow()
    window.fail = "minimize"
    handoff = DesktopHandoff(window)
    assert handoff.release() is False
    assert handoff.released is False

    window.fail = None
    assert handoff.release() is True
    window.fail = "restore"
    assert handoff.reclaim() is False


def test_release_desktop_config_defaults_on_and_can_be_disabled(tmp_path):
    assert AgentConfig().runtime.release_desktop is True
    path = tmp_path / "agent.toml"
    path.write_text("[runtime]\nrelease_desktop = false\n", encoding="utf-8")
    from xm_agent.config import load_config

    assert load_config(path).runtime.release_desktop is False


def _config(tmp_path) -> AgentConfig:
    config = AgentConfig()
    config.adapters = {"mock": AdapterConfig()}
    config.default_adapter = "mock"
    config.audit.root = str(tmp_path / "audit")
    config.runtime.lock_path = str(tmp_path / "agent.lock")
    return config


class _RecordingDesktop:
    available = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def release(self) -> bool:
        self.calls.append("release")
        return True

    def reclaim(self) -> bool:
        self.calls.append("reclaim")
        return True


def _server(tmp_path, desktop=None) -> UiServer:
    return UiServer(
        _config(tmp_path),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
        desktop=desktop,
    )


def test_server_reclaims_only_after_the_last_screen_run(tmp_path):
    desktop = _RecordingDesktop()
    app = _server(tmp_path, desktop)
    app._release_desktop("run-a")
    app._release_desktop("run-b")
    assert desktop.calls == ["release", "release"]

    app._reclaim_desktop("run-a")
    assert desktop.calls == ["release", "release"]
    app._reclaim_desktop("run-b")
    assert desktop.calls == ["release", "release", "reclaim"]


def test_server_skips_handoff_when_disabled(tmp_path):
    desktop = _RecordingDesktop()
    app = _server(tmp_path, desktop)
    app.config.runtime.release_desktop = False
    app._release_desktop("run-a")
    app._reclaim_desktop("run-a")
    assert desktop.calls == []
    assert app.bootstrap()["desktop_handoff"] is False


def test_pause_restores_console_and_resume_releases_it(tmp_path):
    desktop = _RecordingDesktop()
    app = _server(tmp_path, desktop)
    app._release_desktop("run-pause")
    app._controls["run-pause"] = RunControl()
    with app._runs_lock:
        app._runs["run-pause"] = {
            "id": "run-pause",
            "goal": "",
            "adapter": "mock",
            "status": "running",
            "reason": "",
            "steps": 0,
            "model": "",
            "conversation_id": None,
            "audit_mode": "ai",
            "created_at": "",
            "events": [],
        }
    try:
        app.pause_run("run-pause")
        assert desktop.calls == ["release", "reclaim"]
        app.resume_run("run-pause")
        assert desktop.calls == ["release", "reclaim", "release"]
    finally:
        app._controls.pop("run-pause", None)


def test_confirmation_brings_console_back(tmp_path):
    desktop = _RecordingDesktop()
    app = _server(tmp_path, desktop)
    app._release_desktop("run-confirm")
    app._confirm_timeout = 5.0

    result: dict[str, bool] = {}

    def approve() -> None:
        with app._confirm_lock:
            request = next(iter(app._confirm_requests.values()))
        app.resolve_confirmation(request["id"], True)
        result["approved"] = True

    import threading

    import xm_agent.ui.server as server_module

    original_publish = app._publish

    def publish(event, payload, **kwargs):
        original_publish(event, payload, **kwargs)
        if event == "confirm_request":
            threading.Thread(target=approve, daemon=True).start()

    app._publish = publish  # type: ignore[method-assign]
    try:
        assert app.request_confirmation("run-confirm", _Action(), "确认执行") is True
    finally:
        app._publish = original_publish  # type: ignore[method-assign]
    assert desktop.calls == ["release", "reclaim", "release"]


class _Action:
    capability = "input.click"

    def to_dict(self) -> dict:
        return {"capability": self.capability, "args": {}}


@pytest.mark.parametrize("available", [True, False])
def test_bootstrap_reports_handoff_availability(tmp_path, available):
    class _Desktop:
        def __init__(self) -> None:
            self.available = available

        def release(self) -> bool:
            return True

        def reclaim(self) -> bool:
            return True

    app = _server(tmp_path, _Desktop())
    assert app.bootstrap()["desktop_handoff"] is available
