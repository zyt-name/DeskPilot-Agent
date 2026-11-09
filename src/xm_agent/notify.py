from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from typing import Any

from .summary import one_line

_TOAST_COMMAND = r"""
$ErrorActionPreference = 'Stop'
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
  $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
  $texts = $xml.GetElementsByTagName('text')
  $texts.Item(0).AppendChild($xml.CreateTextNode($env:XM_NOTIFY_TITLE)) | Out-Null
  $texts.Item(1).AppendChild($xml.CreateTextNode($env:XM_NOTIFY_BODY)) | Out-Null
  $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:XM_NOTIFY_APP).Show($toast)
} catch {
  exit 1
}
""".strip()


class SystemNotifier:
    """Send a Windows toast without adding a runtime dependency."""

    def __init__(
        self,
        app_id: str = "DeskPilot Agent",
        platform: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.app_id = app_id
        self.platform = platform or sys.platform
        self.timeout = timeout

    def notify(self, title: Any, body: Any = "") -> None:
        if self.platform != "win32":
            return
        threading.Thread(
            target=self.send,
            args=(title, body),
            daemon=True,
        ).start()

    def send(self, title: Any, body: Any = "") -> None:
        if self.platform != "win32":
            return
        title_text = one_line(title, 80) or "DeskPilot Agent"
        body_text = one_line(body, 240)
        powershell = shutil.which("powershell") or "powershell.exe"
        env = {
            **os.environ,
            "XM_NOTIFY_TITLE": title_text,
            "XM_NOTIFY_BODY": body_text,
            "XM_NOTIFY_APP": self.app_id,
        }
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _TOAST_COMMAND,
                ],
                env=env,
                capture_output=True,
                timeout=self.timeout,
                creationflags=flags,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return


__all__ = ["SystemNotifier"]
