"""空闲回收：内核不该一直占着内存，也不该在看的时候被关掉。

三个判据：
1. 在 keep_alive 窗口内不回收；
2. 超时后优雅关闭（profile 留在磁盘，登录态不丢）；
3. 有人在看镜像、或配置成负数时，一律不动它。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from xm_agent.browser.channel import BrowserChannel
from xm_agent.browser.session import BrowserSettings


class _Page:
    def __init__(self) -> None:
        self.stopped = 0
        self.started = 0

    @property
    def url(self) -> str:
        return "https://a.test/"

    @property
    def title(self) -> str:
        return "示例"

    def start_mirror(self, on_frame=None, fps=None, quality=None) -> None:
        self.started += 1

    def stop_mirror(self) -> None:
        self.stopped += 1


class _Session:
    def __init__(self) -> None:
        self.headless = True
        self.started = True
        self._closed = False
        self.close_calls = 0
        self._page = _Page()

    def first_page(self) -> _Page:
        return self._page

    def close(self, reason: str = "关闭") -> None:
        self.close_calls += 1
        self._closed = True
        self.started = False


class IdleRetireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.page = _Page()
        self.session = _Session()
        self.channel = BrowserChannel(
            BrowserSettings(keep_alive_seconds=30),
            session=self.session,
            page=self.page,
            screenshot_dir=str(self.root / "shots"),
        )

    def _idle_for(self, seconds: int) -> None:
        self.channel._last_used = time.time() - seconds

    def test_kept_within_window(self):
        self._idle_for(5)
        self.assertFalse(self.channel.retire_if_idle())
        self.assertEqual(self.session.close_calls, 0)

    def test_retired_after_window(self):
        self._idle_for(31)
        self.assertTrue(self.channel.retire_if_idle())
        self.assertEqual(self.session.close_calls, 1)
        self.assertFalse(self.channel.started)
        self.assertEqual(self.page.stopped, 1)

    def test_negative_keep_alive_never_retires(self):
        channel = BrowserChannel(
            BrowserSettings(keep_alive_seconds=-1),
            session=self.session,
            page=self.page,
            screenshot_dir=str(self.root / "shots"),
        )
        channel._last_used = time.time() - 9999
        self.assertFalse(channel.retire_if_idle())
        self.assertEqual(self.session.close_calls, 0)

    def test_watching_mirror_blocks_retire(self):
        self._idle_for(9999)
        subscriber = self.channel.subscribe(lambda image, meta: None)
        self.assertTrue(self.channel.mirror_active)
        self.assertFalse(self.channel.retire_if_idle())
        self.assertEqual(self.session.close_calls, 0)

        self.channel.unsubscribe(subscriber)
        self.assertFalse(self.channel.mirror_active)
        self.assertTrue(self.channel.retire_if_idle())

    def test_force_retires_even_when_watched(self):
        self.channel.subscribe(lambda image, meta: None)
        self.assertTrue(self.channel.retire_if_idle(force=True))
        self.assertEqual(self.session.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
