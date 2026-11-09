from __future__ import annotations

import pytest

import xm_agent.ui.server as server_module
from xm_agent.config import AdapterConfig, AgentConfig
from xm_agent.control import RunControl
from xm_agent.ui.server import UiServer


def _config(tmp_path) -> AgentConfig:
    config = AgentConfig()
    config.adapters = {"mock": AdapterConfig()}
    config.default_adapter = "mock"
    config.audit.root = str(tmp_path / "audit")
    config.runtime.lock_path = str(tmp_path / "agent.lock")
    config.runtime.screenshot_dir = str(tmp_path / "shots")
    return config


def _server(tmp_path, desktop=None) -> UiServer:
    return UiServer(
        _config(tmp_path),
        db_path=tmp_path / "app.db",
        resources_root=tmp_path / "resource-root",
        desktop=desktop,
    )


def _running(app: UiServer, run_id: str = "run-ask", paused: bool = True):
    control = RunControl()
    if paused:
        control.pause("测试暂停")
    app._controls[run_id] = control
    with app._runs_lock:
        app._runs[run_id] = {
            "id": run_id,
            "goal": "",
            "adapter": "mock",
            "status": "running",
            "reason": "",
            "steps": 0,
            "model": "vision-test",
            "model_provider_id": "",
            "model_id": "",
            "conversation_id": None,
            "audit_mode": "ai",
            "created_at": "",
            "events": [],
        }
    return control


class _VisionProvider:
    provider_vision = True
    model = "vision-test"

    def __init__(self, text: str = "屏幕上有一个确认按钮。") -> None:
        self.text = text
        self.calls: list[dict] = []

    def complete(self, system, user, *, max_tokens=800, image_path=None):
        self.calls.append(
            {"system": system, "user": user, "image_path": image_path}
        )
        return self.text


class _TextProvider:
    provider_vision = False
    model = "text-only"

    def complete(self, system, user, *, max_tokens=800):
        raise AssertionError("纯文本模型不应被调用")


class _RaisingProvider(_VisionProvider):
    def complete(self, system, user, *, max_tokens=800, image_path=None):
        raise RuntimeError("模型超时")


def _shot(tmp_path, desktop=None):
    return {
        "path": str(tmp_path / "shots" / "ask-1.png"),
        "width": 1280,
        "height": 720,
    }


def test_ask_requires_a_question(tmp_path):
    app = _server(tmp_path)
    app._controls["run-ask"] = RunControl()
    with pytest.raises(ValueError, match="请输入要问的问题"):
        app.ask_screen("run-ask", "   ")


def test_ask_requires_a_known_run(tmp_path):
    app = _server(tmp_path)
    with pytest.raises(ValueError, match="run not found"):
        app.ask_screen("missing", "这是什么")


def test_ask_requires_a_paused_run(tmp_path):
    app = _server(tmp_path)
    _running(app, paused=False)
    with pytest.raises(ValueError, match="请先暂停"):
        app.ask_screen("run-ask", "这是什么")


def test_ask_rejects_finished_run(tmp_path):
    app = _server(tmp_path)
    with app._runs_lock:
        app._runs["run-ask"] = {
            "id": "run-ask",
            "adapter": "mock",
            "status": "success",
            "model_provider_id": "",
            "model_id": "",
        }
    with pytest.raises(ValueError, match="任务已结束"):
        app.ask_screen("run-ask", "这是什么")


def test_ask_rejects_text_only_model(tmp_path, monkeypatch):
    app = _server(tmp_path)
    _running(app)
    monkeypatch.setattr(
        server_module, "build_provider", lambda *a, **k: _TextProvider()
    )
    with pytest.raises(ValueError, match="不支持图像输入"):
        app.ask_screen("run-ask", "这是什么")


def test_ask_attaches_screenshot_and_returns_answer(tmp_path, monkeypatch):
    app = _server(tmp_path)
    _running(app)
    provider = _VisionProvider("截图里是「另存为」对话框，默认文件名未改。")
    monkeypatch.setattr(
        server_module, "build_provider", lambda *a, **k: provider
    )
    monkeypatch.setattr(
        server_module,
        "capture_primary_screen",
        lambda directory, prefix="screen", max_width=1280: _shot(tmp_path),
    )

    result = app.ask_screen("run-ask", "这个对话框在问什么？")

    assert result["answer"].startswith("截图里是")
    assert result["model"] == "vision-test"
    assert result["screenshot"].endswith("ask-1.png")
    assert len(provider.calls) == 1
    assert provider.calls[0]["image_path"].endswith("ask-1.png")
    assert "这个对话框在问什么？" in provider.calls[0]["user"]


def test_ask_hides_console_while_capturing(tmp_path, monkeypatch):
    class _Desktop:
        available = True

        def __init__(self) -> None:
            self.calls: list[str] = []

        def release(self) -> bool:
            self.calls.append("release")
            return True

        def reclaim(self) -> bool:
            self.calls.append("reclaim")
            return True

    desktop = _Desktop()
    app = _server(tmp_path, desktop)
    _running(app)
    monkeypatch.setattr(
        server_module, "build_provider", lambda *a, **k: _VisionProvider()
    )

    seen: list[list[str]] = []

    def capture(directory, prefix="screen", max_width=1280):
        seen.append(list(desktop.calls))
        return _shot(tmp_path)

    monkeypatch.setattr(server_module, "capture_primary_screen", capture)
    app.ask_screen("run-ask", "屏幕上有什么？")

    assert seen == [["release"]]
    assert desktop.calls == ["release", "reclaim"]


def test_capture_failure_is_reported(tmp_path, monkeypatch):
    app = _server(tmp_path)
    _running(app)
    monkeypatch.setattr(
        server_module, "build_provider", lambda *a, **k: _VisionProvider()
    )
    monkeypatch.setattr(
        server_module,
        "capture_primary_screen",
        lambda directory, prefix="screen", max_width=1280: None,
    )
    with pytest.raises(ValueError, match="截图失败"):
        app.ask_screen("run-ask", "屏幕上有什么？")


def test_model_failure_is_reported(tmp_path, monkeypatch):
    app = _server(tmp_path)
    _running(app)
    monkeypatch.setattr(
        server_module, "build_provider", lambda *a, **k: _RaisingProvider()
    )
    monkeypatch.setattr(
        server_module,
        "capture_primary_screen",
        lambda directory, prefix="screen", max_width=1280: _shot(tmp_path),
    )
    with pytest.raises(ValueError, match="截图提问失败"):
        app.ask_screen("run-ask", "屏幕上有什么？")


def test_empty_answer_is_reported(tmp_path, monkeypatch):
    app = _server(tmp_path)
    _running(app)
    monkeypatch.setattr(
        server_module, "build_provider", lambda *a, **k: _VisionProvider("  ")
    )
    monkeypatch.setattr(
        server_module,
        "capture_primary_screen",
        lambda directory, prefix="screen", max_width=1280: _shot(tmp_path),
    )
    with pytest.raises(ValueError, match="没有返回内容"):
        app.ask_screen("run-ask", "屏幕上有什么？")


def test_capture_restores_console_even_when_capture_raises(
    tmp_path, monkeypatch
):
    class _Desktop:
        available = True

        def __init__(self) -> None:
            self.calls: list[str] = []

        def release(self) -> bool:
            self.calls.append("release")
            return True

        def reclaim(self) -> bool:
            self.calls.append("reclaim")
            return True

    desktop = _Desktop()
    app = _server(tmp_path, desktop)

    def boom(directory, prefix="screen", max_width=1280):
        raise RuntimeError("grab failed")

    monkeypatch.setattr(server_module, "capture_primary_screen", boom)
    with pytest.raises(RuntimeError):
        app._capture_for_question()
    assert desktop.calls == ["release", "reclaim"]
