"""Hand the desktop over to the agent while it drives the screen.

The console window sits on top of the very desktop the agent is about to
click, so a run that owns the screen would otherwise fight its own UI. The
console therefore minimises when such a run starts and comes back when the run
ends, fails, is cancelled, or pauses for the user.

Browser mode (``--server-only``) has no window to move; every method is then a
no-op that reports ``False`` instead of raising.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class DesktopHandoff:
    """Minimise and restore the console window around screen-owning runs."""

    def __init__(self, window: Any | None = None) -> None:
        self._window = window
        self._lock = threading.RLock()
        self._released = False

    @property
    def available(self) -> bool:
        with self._lock:
            return self._window is not None

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    def attach(self, window: Any | None) -> None:
        """Bind the real window once the WebView has created it."""
        with self._lock:
            self._window = window

    def release(self) -> bool:
        """Minimise the console. Idempotent; True when the desktop is free."""
        with self._lock:
            window = self._window
            if window is None:
                return False
            if self._released:
                return True
            try:
                window.minimize()
            except Exception as exc:  # pragma: no cover - window backend
                logger.warning("desktop handoff failed: %s", exc)
                return False
            self._released = True
            return True

    def reclaim(self) -> bool:
        """Bring the console back, but only if this object sent it away."""
        with self._lock:
            window = self._window
            if window is None or not self._released:
                return False
            try:
                window.restore()
            except Exception as exc:  # pragma: no cover - window backend
                logger.warning("desktop reclaim failed: %s", exc)
                return False
            self._released = False
            return True


__all__ = ["DesktopHandoff"]
