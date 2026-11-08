from __future__ import annotations

import threading
import time

import pytest

from xm_agent.control import RunCancelled, RunControl, RunPreempted


def test_pause_blocks_until_resume():
    control = RunControl()
    control.pause("stop")
    reached = threading.Event()
    released = threading.Event()

    def worker():
        reached.set()
        control.checkpoint()
        released.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert reached.wait(2)
    time.sleep(0.05)
    assert not released.is_set()

    control.resume()
    thread.join(timeout=2)
    assert released.is_set()


def test_cancel_raises_at_checkpoint():
    control = RunControl()
    control.cancel()
    with pytest.raises(RunCancelled):
        control.checkpoint()


def test_preempt_raises_without_waiting():
    control = RunControl()
    control.preempt()
    with pytest.raises(RunPreempted):
        control.checkpoint()


def test_preempt_wakes_a_paused_checkpoint():
    control = RunControl()
    control.pause()
    raised: list[BaseException] = []

    def worker():
        try:
            control.checkpoint()
        except BaseException as exc:
            raised.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.05)
    control.preempt()
    thread.join(timeout=2)
    assert raised and isinstance(raised[0], RunPreempted)


def test_instructions_are_drained_once():
    control = RunControl()
    control.add_instruction("改用其他目录")
    control.add_instruction("   ")
    assert control.drain_instructions() == ["改用其他目录"]
    assert control.drain_instructions() == []


def test_snapshot_reflects_state():
    control = RunControl()
    control.pause("waiting")
    control.add_instruction("hello")
    snapshot = control.snapshot()
    assert snapshot["paused"] is True
    assert snapshot["pause_reason"] == "waiting"
    assert snapshot["pending_instructions"] == 1
