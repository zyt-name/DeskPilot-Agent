from __future__ import annotations

from xm_agent.storage import DEFAULT_PROFILE, Storage


def test_profile_defaults_are_created(tmp_path):
    storage = Storage(tmp_path / "app.db")
    profile = storage.get_profile()
    assert profile["display_name"] == DEFAULT_PROFILE["display_name"]
    assert profile["default_adapter"] == "mock"
    assert profile["auto_confirm"] is False


def test_profile_roundtrip_persists(tmp_path):
    path = tmp_path / "app.db"
    storage = Storage(path)
    storage.save_profile(
        display_name="  测试用户  ",
        default_adapter="windows",
        default_goal="打开记事本",
        auto_confirm=True,
    )

    reopened = Storage(path).get_profile()
    assert reopened["display_name"] == "测试用户"
    assert reopened["default_adapter"] == "windows"
    assert reopened["default_goal"] == "打开记事本"
    assert reopened["auto_confirm"] is True


def test_profile_partial_update_keeps_other_fields(tmp_path):
    storage = Storage(tmp_path / "app.db")
    storage.save_profile(default_goal="目标一", auto_confirm=True)
    profile = storage.save_profile(display_name="新名字")
    assert profile["default_goal"] == "目标一"
    assert profile["auto_confirm"] is True


def test_run_lifecycle_and_history(tmp_path):
    storage = Storage(tmp_path / "app.db")
    storage.create_run("run-1", "输入 hello 并保存", "mock")
    storage.finish_run("run-1", "done", "mock task complete", 3)

    stored = storage.get_run("run-1")
    assert stored is not None
    assert stored["status"] == "done"
    assert stored["steps"] == 3
    assert stored["reason"] == "mock task complete"
    assert stored["finished_at"]

    storage.create_run("run-2", "第二个任务", "windows")
    runs = storage.list_runs()
    assert [run["id"] for run in runs] == ["run-2", "run-1"]
    assert storage.get_run("missing") is None


def test_run_goal_is_truncated(tmp_path):
    storage = Storage(tmp_path / "app.db")
    storage.create_run("run-long", "x" * 900, "mock")
    assert len(storage.get_run("run-long")["goal"]) == 500
