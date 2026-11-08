import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from xm_agent.lock import LeaseLock


class LeaseLockTests(unittest.TestCase):
    def test_acquire_release_and_contention(self):
        with TemporaryDirectory() as temp:
            lock = LeaseLock(Path(temp) / "agent.lock", ttl_seconds=10)
            token = lock.acquire()
            self.assertIsNotNone(token)
            self.assertIsNone(lock.acquire())
            self.assertFalse(lock.release("wrong-token"))
            self.assertTrue(lock.path.exists())
            self.assertTrue(lock.release(token))
            self.assertFalse(lock.path.exists())
            self.assertFalse(lock.release(token))

            second = lock.acquire()
            self.assertIsNotNone(second)
            self.assertTrue(lock.release(second))

    def test_stale_lock_is_taken_over(self):
        with TemporaryDirectory() as temp:
            lock = LeaseLock(Path(temp) / "agent.lock", ttl_seconds=10)
            lock.path.write_text(
                json.dumps(
                    {
                        "owner": "old",
                        "token": "old-token",
                        "expires_at": time.time() - 5,
                    }
                ),
                encoding="utf-8",
            )
            token = lock.acquire()
            self.assertIsNotNone(token)
            self.assertEqual(lock.holder()["token"], token)
            self.assertTrue(lock.release(token))

    def test_heartbeat_extends_lease(self):
        with TemporaryDirectory() as temp:
            lock = LeaseLock(Path(temp) / "agent.lock", ttl_seconds=10)
            token = lock.acquire()
            self.assertTrue(lock.heartbeat(token))
            self.assertTrue(lock.holder()["expires_at"] > time.time())
            self.assertTrue(lock.release(token))


if __name__ == "__main__":
    unittest.main()
