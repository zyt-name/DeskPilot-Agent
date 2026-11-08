from __future__ import annotations

import threading
import time
from typing import Any


class RunCancelled(RuntimeError):
    """Raised at the next safe checkpoint after the user cancels a run."""


class RunPreempted(RunCancelled):
    """Raised when another task has been placed in front of this one."""


class RunControl:
    """Thread-safe pause/resume/cancel/instruction channel for one run."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._paused = False
        self._cancelled = False
        self._preempt = False
        self._instructions: list[str] = []
        self.pause_reason = ""

    @property
    def paused(self) -> bool:
        with self._condition:
            return self._paused

    @property
    def cancelled(self) -> bool:
        with self._condition:
            return self._cancelled

    def pause(self, reason: str = "用户暂停") -> bool:
        with self._condition:
            if self._cancelled:
                return False
            self._paused = True
            self.pause_reason = reason
            self._condition.notify_all()
            return True

    def resume(self) -> bool:
        with self._condition:
            if self._cancelled or not self._paused:
                return False
            self._paused = False
            self.pause_reason = ""
            self._condition.notify_all()
            return True

    def cancel(self) -> None:
        with self._condition:
            self._cancelled = True
            self._paused = False
            self._condition.notify_all()

    def preempt(self) -> None:
        with self._condition:
            if self._cancelled:
                return
            self._preempt = True
            self._paused = True
            self.pause_reason = "插队任务等待执行"
            self._condition.notify_all()

    def add_instruction(self, text: str) -> str:
        text = text.strip()[:2000]
        if not text:
            return ""
        with self._condition:
            self._instructions.append(text)
            return text

    def drain_instructions(self) -> list[str]:
        with self._condition:
            items = self._instructions[:]
            self._instructions.clear()
        return items

    def checkpoint(self) -> None:
        """Block while paused; raise when cancelled or preempted."""
        with self._condition:
            if self._preempt:
                self._preempt = False
                raise RunPreempted("preempted by queued task")
            while self._paused and not self._cancelled:
                self._condition.wait(timeout=0.5)
                if self._preempt:
                    self._preempt = False
                    raise RunPreempted("preempted by queued task")
            if self._cancelled:
                raise RunCancelled("cancelled by user")

    def sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            self.checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "paused": self._paused,
                "cancelled": self._cancelled,
                "pause_reason": self.pause_reason,
                "pending_instructions": len(self._instructions),
            }


__all__ = ["RunCancelled", "RunControl", "RunPreempted"]
