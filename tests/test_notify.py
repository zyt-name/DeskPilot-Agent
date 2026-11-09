import subprocess
import unittest
from unittest import mock

from xm_agent.notify import SystemNotifier


class SystemNotifierTests(unittest.TestCase):
    def test_non_windows_platform_is_a_noop(self):
        notifier = SystemNotifier(platform="linux")
        with mock.patch.object(subprocess, "run") as run:
            notifier.send("title", "body")
        run.assert_not_called()

    def test_windows_send_passes_text_through_environment(self):
        notifier = SystemNotifier(platform="win32", app_id="DeskPilot Test")
        with mock.patch.object(
            subprocess, "run", return_value=None
        ) as run:
            notifier.send("任务完成 · 测试 目标", "已完成。\n第二行")
        self.assertEqual(run.call_count, 1)
        args, kwargs = run.call_args
        self.assertIn("ToastNotificationManager", args[0][-1])
        env = kwargs["env"]
        self.assertEqual(env["XM_NOTIFY_TITLE"], "任务完成 · 测试 目标")
        self.assertEqual(env["XM_NOTIFY_BODY"], "已完成。 第二行")
        self.assertEqual(env["XM_NOTIFY_APP"], "DeskPilot Test")
        self.assertTrue(kwargs["capture_output"])
        self.assertFalse(kwargs["check"])

    def test_powershell_failures_are_swallowed(self):
        notifier = SystemNotifier(platform="win32")
        with mock.patch.object(
            subprocess, "run", side_effect=OSError("no powershell")
        ):
            notifier.send("title", "body")

    def test_notify_spawns_a_daemon_thread(self):
        import time

        notifier = SystemNotifier(platform="win32")
        with mock.patch.object(SystemNotifier, "send") as send:
            notifier.notify("title", "body")
            deadline = time.time() + 2
            while send.call_count == 0 and time.time() < deadline:
                time.sleep(0.01)
        send.assert_called_once_with("title", "body")


if __name__ == "__main__":
    unittest.main()
