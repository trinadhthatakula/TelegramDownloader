"""Format scans must be cancellable and obsolete results must not reach the UI."""
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import interactive_downloader as dl


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = patch.object(dl, "CONFIG_FILE", os.path.join(self.tmp.name, "config.json"))
        self.config.start()
        self.backend = dl.TelegramBackend(1, "fake", os.path.join(self.tmp.name, "unused"))

    def tearDown(self):
        self.backend.shutdown()
        self.config.stop()
        self.tmp.cleanup()

    def test_superseded_scan_cannot_publish_results(self):
        backend = self.backend
        first_started = asyncio.Event()
        class Client:
            async def disconnect(self):
                pass
            async def iter_messages(self, entity, **kwargs):
                if entity == 1:
                    first_started.set()
                    await asyncio.Event().wait()
                yield SimpleNamespace(id=1, file=SimpleNamespace(ext=".jpg"))
        backend.client = Client()
        old = backend.begin_extension_scan([SimpleNamespace(entity=1, name="Old")])
        backend.run_coro(first_started.wait()).result(2)
        new = backend.begin_extension_scan([SimpleNamespace(entity=2, name="New")])
        backend.run_coro(asyncio.sleep(0.03)).result(2)
        results = [data for kind, data in backend.event_queue.queue if kind == "scan_finished"]
        self.assertNotEqual(old, new)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["generation"], new)
        self.assertEqual(results[0]["extensions"], [".jpg"])

    def test_cancel_invalidates_late_scan_events(self):
        backend = self.backend
        started, cancelled = asyncio.Event(), asyncio.Event()
        class Client:
            async def disconnect(self):
                pass
            async def iter_messages(self, entity, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
                yield
        backend.client = Client()
        generation = backend.begin_extension_scan([SimpleNamespace(entity=1, name="One")])
        backend.run_coro(started.wait()).result(2)
        backend.cancel_extension_scan()
        backend.run_coro(cancelled.wait()).result(2)
        self.assertGreater(backend.latest_scan_generation, generation)
        self.assertFalse(any(kind == "scan_finished" for kind, _ in backend.event_queue.queue))


if __name__ == "__main__":
    unittest.main()
