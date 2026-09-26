"""Verify incremental UI state and backpressure without a Tk desktop."""
import unittest
from types import SimpleNamespace

from queue_state import CoalescingEventQueue, QueueViewIndex


def item(identifier, chat=1, status="Queued"):
    return SimpleNamespace(id=str(identifier), chat_id=chat, status=status,
                           file_size=1024, downloaded_bytes=0, speed=0)


class QueueStateTests(unittest.TestCase):
    def test_incremental_global_and_filtered_counts(self):
        index = QueueViewIndex()
        first, second = item(1), item(2, chat=2)
        index.change(first)
        index.change(second)
        self.assertEqual(index.snapshot().queued, 2)
        first.status = "Downloading"
        first.downloaded_bytes, first.speed = 100, 25
        index.change(first)
        self.assertEqual(index.snapshot(1).downloaded, 100)
        self.assertEqual(index.snapshot(1).speed, 25)
        self.assertEqual(index.snapshot(2).active, 0)
        first.status = "Completed"
        index.change(first)
        index.change(first)
        self.assertEqual(index.snapshot().done, 1)
        self.assertEqual(index.snapshot().queued, 1)
        self.assertEqual(index.snapshot().downloaded, 1024)
        second.status = "Failed"
        index.change(second)
        self.assertEqual(index.snapshot().failed, 1)
        self.assertEqual(index.snapshot().active, 0)

    def test_snapshots_do_not_iterate_entire_history(self):
        class NoIterList(list):
            def __iter__(self):
                raise AssertionError("A UI snapshot scanned history")
        index = QueueViewIndex()
        for n in range(10000):
            index.change(item(n, chat=n % 5, status="Completed"))
        index.all_items = NoIterList(index.all_items)
        index.by_chat[1] = NoIterList(index.by_chat[1])
        total, page = index.window(None, 9990, 10)
        self.assertEqual((total, len(page), page[-1].id), (10000, 10, "9999"))
        self.assertEqual(index.window(1, 0, 8)[0], 2000)
        self.assertEqual(index.snapshot().done, 10000)

    def test_notifications_are_coalesced_and_control_is_prioritized(self):
        events = CoalescingEventQueue(update_limit=8)
        for n in range(10000):
            events.put(("item_progress", item(n)))
        self.assertEqual(events.qsize(), 8)
        events.put(("account_reset", None))
        self.assertEqual(events.get_nowait(), ("account_reset", None))
        latest = item(20000)
        for n in range(1000):
            latest.downloaded_bytes = n
            events.put(("item_progress", latest))
        retained = [data for kind, data in events.queue if data.id == "20000"]
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].downloaded_bytes, 999)


if __name__ == "__main__":
    unittest.main()
