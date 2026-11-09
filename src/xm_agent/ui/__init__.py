from __future__ import annotations

import time

from ..config import AgentConfig
from ..notify import SystemNotifier
from .hotkey import GlobalHotkey
from .overlay import ScreenIndicator
from .server import STATIC_DIR, UiServer
from .window import open_window


def run_gui(
    config: AgentConfig,
    db_path: str = ".xm-agent/app.db",
    port: int = 0,
    debug: bool = False,
    server_only: bool = False,
) -> int:
    indicator = ScreenIndicator()
    indicator.start()
    server = UiServer(
        config,
        db_path=db_path,
        port=port,
        indicator=indicator,
        notifier=SystemNotifier(),
    )
    url = server.start()
    hotkey = GlobalHotkey(server.toggle_pause_active)
    server.native_hotkey = hotkey.start()
    print(f"[ui] local api: {url}")
    print(f"[ui] database: {server.storage.path.resolve()}")
    if server.native_hotkey:
        print("[ui] global F8 pause enabled")
    try:
        if server_only:
            print("[ui] server-only mode, press Ctrl+C to stop")
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                return 0
        return open_window(url, debug=debug)
    finally:
        hotkey.stop()
        indicator.close()
        server.stop()


__all__ = ["UiServer", "STATIC_DIR", "run_gui", "open_window"]
