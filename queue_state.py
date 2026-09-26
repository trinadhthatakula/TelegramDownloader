"""Small UI snapshots and coalesced notifications for the download backend."""
import queue
import threading
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, fields


@dataclass
class QueueTotals:
    total: int = 0
    active: int = 0
    queued: int = 0
    done: int = 0
    failed: int = 0
    cancelled: int = 0
    paused: int = 0
    speed: float = 0.0
    downloaded: int = 0
    total_bytes: int = 0


def contribution(item):
    status = item.status
    done = status in ("Completed", "Skipped (Exists)")
    return QueueTotals(total=1, active=int(status == "Downloading"),
                       queued=int(status == "Queued"), cancelled=int(status == "Cancelled"),
                       paused=int(status in ("Paused", "Cancelling")),
                       done=int(done), failed=int(status.startswith("Failed")),
                       downloaded=(item.file_size or item.downloaded_bytes or 0) if done else 0,
                       total_bytes=item.file_size or 0)


class QueueViewIndex:
    """Caller holds the backend queue lock; updates are O(1), windows O(page size)."""
    def __init__(self):
        self.by_chat = defaultdict(list)
        self.all_items = []
        self.previous = {}
        self.totals = defaultdict(QueueTotals)
        self.active = {}

    def change(self, item):
        previous = self.previous.get(item.id)
        if previous is None:
            self.all_items.append(item)
            self.by_chat[item.chat_id].append(item)
        current = contribution(item)
        for key in (None, item.chat_id):
            totals = self.totals[key]
            for field in fields(QueueTotals):
                name = field.name
                setattr(totals, name, getattr(totals, name) + getattr(current, name)
                        - (getattr(previous, name) if previous else 0))
        self.previous[item.id] = current
        if item.status == "Downloading":
            self.active[item.id] = item
        else:
            self.active.pop(item.id, None)

    def rebuild(self, items):
        self.__init__()
        for item in items:
            self.change(item)

    def snapshot(self, chat_filter=None):
        counts = self.totals[chat_filter]
        result = QueueTotals(**vars(counts))
        for item in self.active.values():
            if chat_filter is None or item.chat_id == chat_filter:
                result.downloaded += item.downloaded_bytes
                result.speed += item.speed
        return result

    def window(self, chat_filter, offset, limit):
        items = self.all_items if chat_filter is None else self.by_chat[chat_filter]
        return len(items), list(items[max(0, offset):max(0, offset) + max(0, limit)])


class CoalescingEventQueue:
    """Preserve control events; supersede noisy view updates before they reach Tk."""
    _coalesced = frozenset({"queue_updated", "item_updated", "item_progress", "dialogs_progress", "scan_progress"})

    def __init__(self, update_limit=2048):
        self._control = deque()
        self._updates = OrderedDict()
        self._lock = threading.Lock()
        self.update_limit = update_limit

    def put(self, event, block=True, timeout=None):
        event_type, data = event
        with self._lock:
            if event_type in self._coalesced:
                identity = getattr(data, "id", None)
                if isinstance(data, dict):
                    identity = data.get("generation")
                key = (event_type, identity)
                self._updates[key] = event
                if len(self._updates) > self.update_limit:
                    self._updates.popitem(last=False)
            else:
                self._control.append(event)

    def get_nowait(self):
        with self._lock:
            if self._control:
                return self._control.popleft()
            if self._updates:
                return self._updates.popitem(last=False)[1]
        raise queue.Empty

    def empty(self):
        return self.qsize() == 0

    def qsize(self):
        with self._lock:
            return len(self._control) + len(self._updates)

    @property
    def queue(self):
        """Read-only snapshot, also useful to deterministic fake-client tests."""
        with self._lock:
            return deque([*self._control, *self._updates.values()])
