import os
import errno
import re
import sys
import json
import time
import queue
import logging
import asyncio
import threading
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass
from typing import Dict, List, Set, Optional, Any

from telethon import TelegramClient, errors
from telegram_auth import AuthenticationMixin
from queue_state import CoalescingEventQueue, QueueViewIndex, QueueTotals as QueueStats
import transfer_scheduler as scheduler
from transfer_scheduler import TransferSchedulerMixin
from download_storage import get_storage, media_identity, AsyncDiskSink, IncompleteDownloadError
from telegram_transport import download_stream, ResumeRestartRequired
from engine.media import topic_id, is_forum, topics_request

# All app-owned paths resolve against the script directory, never the working
# directory: launching from elsewhere used to scatter config.json / downloader.log
# / the Telethon session into whatever CWD the launcher happened to pick.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOG_FILE = os.path.join(BASE_DIR, "downloader.log")

# Scheduler tuning lives in transfer_scheduler; aliases remain available
# to integrations and the extension preview scanner.
MESSAGE_SCAN_WINDOW = scheduler.SCAN_WINDOW
TARGET_BATCH_SIZE = scheduler.PER_CHAT_PENDING_LIMIT
QUEUE_REFILL_THRESHOLD = scheduler.REFILL_THRESHOLD
FETCH_BACKOFF = scheduler.FETCH_BACKOFF

# Telegram requires download offsets and chunk sizes to be 4 KiB aligned; 512 KiB
# is Telethon's MAX_CHUNK_SIZE and the largest legal request size.
RESUME_ALIGN = 512 * 1024

# Retry budget per item before it becomes terminally Failed.
MAX_ITEM_RETRIES = 3

MAX_FILENAME_LEN = 120
# Deny-list, deliberately not an allow-list: path separators, control characters
# and the Windows-reserved set are the only things that are actually unsafe.
# Stripping anything more (non-ASCII, '@', '%', ...) would rename files that are
# already on disk from earlier runs and re-download them.
_ILLEGAL_FILE_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"|?*\\/]')
# The original chat-folder scheme, reproduced exactly so existing per-chat
# folders keep matching: keep alphanumerics plus these, then strip whitespace.
_LEGACY_CHAT_EXTRAS = "._- "
_TERMINAL_STATUSES = ("Completed", "Skipped (Exists)", "Cancelled", "Failed")


def configure_logging(log_file=None):
    """Installs a size-capped rotating file log. Called from main(), not at import."""
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.DEBUG)

    file_handler = RotatingFileHandler(
        log_file or LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root.addHandler(file_handler)

    # A GUI app has no visible stdout when launched from Finder or an IDE; keep it
    # for warnings only so a terminal launch still surfaces real problems.
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root.addHandler(stream_handler)

    logging.getLogger("telethon").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


def _degenerate(name: str) -> bool:
    """True for names that are empty or resolve to a directory reference."""
    return not name or set(name) <= set("._- ")


def _truncate(name: str) -> str:
    if len(name) <= MAX_FILENAME_LEN:
        return name
    stem, ext = os.path.splitext(name)
    return stem[: MAX_FILENAME_LEN - len(ext)] + ext


def sanitize_file_name(raw_name: Optional[str], msg_id: int, ext: str) -> str:
    """Reduces an uploader-supplied file name to a single safe path component.

    msg.file.name is chosen by whoever uploaded the file, so it is hostile input.
    An absolute name such as '/app/VID_20240423_111309_573.mp4' made
    os.path.join discard the download folder entirely and wrote to the
    filesystem root; '../../x' escaped upwards the same way.
    """
    name = os.path.basename((raw_name or "").replace("\\", "/").rstrip("/"))
    name = _ILLEGAL_FILE_CHARS.sub("_", name)
    name = "".join(ch for ch in name if ch.isprintable() or ch == " ")
    name = name.strip(" .")
    if _degenerate(name):
        return f"msg_{msg_id}{ext or '.bin'}"
    return _truncate(name)


def sanitize_chat_dir(chat_name: str, chat_id: int) -> str:
    """Builds the per-chat folder name using the original scheme.

    Kept byte-for-byte compatible on purpose: changing it would orphan every
    folder already on disk and re-download everything in it. The guard below is
    the one addition, because the old filter could yield '' or '..'.
    """
    name = "".join(
        c for c in (chat_name or "")
        if c.isalnum() or c in _LEGACY_CHAT_EXTRAS
    ).strip()
    if _degenerate(name):
        return f"chat_{chat_id}"
    return _truncate(name)


def is_within(base: str, target: str) -> bool:
    """True when target resolves inside base. Last line of defence before any file op."""
    try:
        base_real = os.path.realpath(base)
        target_real = os.path.realpath(target)
        return os.path.commonpath([base_real, target_real]) == base_real
    except (ValueError, OSError):
        return False


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0 or unit == "TB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


DEFAULT_CONFIG = {
    "output_dir": os.getenv("OUTPUT_ROOT") or os.path.join(BASE_DIR, "telegram_downloads"),
    "max_parallel_files": 3,
    "scan_depth": 500,
}


def validate_config(data: dict) -> dict:
    """Coerces and clamps config values so a hand-edited config.json cannot wedge the app."""
    clean = dict(DEFAULT_CONFIG)

    out_dir = data.get("output_dir")
    if isinstance(out_dir, str) and out_dir.strip():
        clean["output_dir"] = os.path.abspath(os.path.expanduser(out_dir.strip()))
    else:
        clean["output_dir"] = os.path.abspath(clean["output_dir"])

    try:
        clean["max_parallel_files"] = max(1, min(10, int(data.get("max_parallel_files", 3))))
    except (TypeError, ValueError):
        pass
    try:
        depth = int(data.get("scan_depth", 500))
        clean["scan_depth"] = depth if depth >= 0 else 500
    except (TypeError, ValueError):
        pass
    return clean


def load_config(config_file=None) -> dict:
    """Loads configuration from config.json, falling back to validated defaults."""
    CONFIG_FILE = config_file or globals()["CONFIG_FILE"]
    data = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
                logger.info("Loaded configuration from %s", CONFIG_FILE)
            else:
                logger.warning("Ignoring %s: expected a JSON object.", CONFIG_FILE)
        except Exception as e:
            logger.warning("Failed to load %s: %s", CONFIG_FILE, e)
    return validate_config(data)


def save_config(config_data: dict, config_file=None) -> Optional[str]:
    """Atomically persists the configuration. Returns an error string on failure."""
    CONFIG_FILE = config_file or globals()["CONFIG_FILE"]
    tmp_path = f"{CONFIG_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_FILE)
        logger.info("Configuration saved to %s", CONFIG_FILE)
        return None
    except Exception as e:
        logger.error("Failed to save %s: %s", CONFIG_FILE, e)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return str(e)


def check_output_dir(path: str) -> Optional[str]:
    """Verifies the download directory exists and is writable. Returns an error string."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        return f"Cannot create '{path}': {e}"
    if not os.access(path, os.W_OK):
        return f"'{path}' is not writable."
    return None


@dataclass
class DownloadItem:
    id: str  # Format: f"{chat_id}_{msg_id}"
    chat_id: int
    chat_name: str
    message: Any
    file_name: str
    msg_id: int
    ext: str
    file_size: Optional[int] = None  # None means Telegram did not report a size
    status: str = "Queued"
    progress: float = 0.0
    downloaded_bytes: int = 0
    speed: float = 0.0
    retries: int = 0
    task: Optional[asyncio.Task] = None
    last_update_time: float = 0.0
    last_bytes: int = 0
    output_dir: Optional[str] = None
    topic_id: Optional[int] = None
    topic_name: Optional[str] = None
    expected_identity: Optional[str] = None
    job_id: Optional[str] = None


# Sort/display order for the chat picker. Groups are what this tool downloads
# from, so they come first; DMs and bots last.
_CHAT_KIND_ORDER = {"Group": 0, "Channel": 1, "DM": 2, "Bot": 3}


@dataclass
class ChatEntry:
    """One selectable chat, decoupled from Telethon's Dialog object.

    The Tk thread only ever touches these plain fields, so a surprising entity
    (deleted user, ChannelForbidden) cannot raise while the list is being built
    and silently truncate it. ``entity`` carries the original Dialog through to
    ``iter_messages``, which is what actually needs it.
    """
    id: int
    name: str
    kind: str
    archived: bool
    entity: Any = None

    @classmethod
    def from_dialog(cls, d: Any) -> "ChatEntry":
        if getattr(d, "is_group", False):
            kind = "Group"
        elif getattr(d, "is_channel", False):
            kind = "Channel"
        elif getattr(d, "is_user", False):
            kind = "Bot" if getattr(getattr(d, "entity", None), "bot", False) else "DM"
        else:
            kind = "Group"
        name = getattr(d, "name", None) or getattr(d, "title", None) or f"Chat {d.id}"
        return cls(id=d.id, name=str(name), kind=kind,
                   archived=bool(getattr(d, "archived", False)), entity=d)


@dataclass
class ChatPaginationState:
    chat_id: int
    chat_name: str
    entity: Any = None  # Preserved Telethon Dialog/Entity for reliable iteration
    last_msg_id: int = 0
    has_more: bool = True
    fetching_in_progress: bool = False
    total_discovered: int = 0
    total_completed: int = 0
    consecutive_errors: int = 0
    retry_armed: bool = False
    chosen_exts: frozenset = frozenset()
    all_formats: bool = True
    generation: int = 0
    paused: bool = False
    cancelled: bool = False
    discovery_started: bool = False
    last_error: str = ""
    newest_msg_id: int = 0
    stop_msg_id: int = 0
    output_dir: Optional[str] = None


@dataclass
class ChatSnapshot:
    """Immutable view of a ChatPaginationState, safe to read from the Tk thread."""
    chat_id: int
    chat_name: str
    total_discovered: int
    total_completed: int
    status: str = "Queued"
    queued: int = 0
    active: int = 0
    failed: int = 0
    paused: bool = False


class TelegramBackend(TransferSchedulerMixin, AuthenticationMixin):
    def __init__(self, api_id: int, api_hash: str, session_path: str, accounts=None, config_file=None):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self.configure_auth(accounts)
        self._account_resetting = False
        self._account_generation = 0

        self.event_queue = CoalescingEventQueue()
        self._config_file = config_file
        self.config = load_config(config_file)

        self.client: Optional[TelegramClient] = None
        self.download_queue: List[DownloadItem] = []
        # id -> item, so the per-message duplicate check is O(1) instead of a full
        # scan of download_queue for every message of every batch.
        self.queue_index: Dict[str, DownloadItem] = {}
        self.pagination_states: Dict[int, ChatPaginationState] = {}
        self.chosen_exts: Set[str] = set()
        self.all_formats_mode: bool = False

        self.queue_lock = threading.RLock()
        self.states_lock = threading.RLock()
        self._view_index = QueueViewIndex()
        self._view_source = self.download_queue
        self._view_length = 0
        self.latest_scan_generation = 0
        self._extension_scan_task = None
        self._config_save_lock = asyncio.Lock()
        self.last_dispatched_chat_id: Optional[int] = None

        # Global rate-limit gate. While flood_until is in the future no new
        # download is dispatched, so one FloodWait does not cascade the queue.
        self.flood_until: float = 0.0
        self._flood_timer_armed = False
        self._pending_tasks: Set[asyncio.Task] = set()
        self._shutting_down = False

        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="tg-loop", daemon=True)
        self.thread.start()

    # ---------------------------------------------------------------- plumbing

    def emit_event(self, event_type: str, data: Any = None):
        """Thread-safe event dispatch to the Tk thread."""
        if event_type in ("item_updated", "item_progress") and isinstance(data, DownloadItem):
            self._item_state_changed(data)
        self.event_queue.put((event_type, data))

    def _ensure_view_index(self):
        # Legacy integrations/tests may replace the list directly. Normal writes
        # use the hooks below and never need this O(history) rebuild.
        if self._view_source is not self.download_queue or self._view_length != len(self.download_queue):
            self._view_index.rebuild(self.download_queue)
            self._view_source = self.download_queue
            self._view_length = len(self.download_queue)

    def _queue_items_added(self, items):
        with self.queue_lock:
            if (self._view_source is not self.download_queue
                    or self._view_length + len(items) != len(self.download_queue)):
                self._view_index.rebuild(self.download_queue)
            for item in items:
                self._view_index.change(item)
            self._view_source = self.download_queue
            self._view_length = len(self.download_queue)

    def _queue_items_removed(self, items):
        with self.queue_lock:
            self._view_index.rebuild(self.download_queue)
            self._view_source = self.download_queue
            self._view_length = len(self.download_queue)

    def _item_state_changed(self, item):
        with self.queue_lock:
            self._ensure_view_index()
            if self.queue_index.get(item.id) is item or item.id in self._view_index.previous:
                self._view_index.change(item)

    def snapshot_queue_window(self, chat_filter=None, offset=0, limit=40):
        with self.queue_lock:
            self._ensure_view_index()
            return self._view_index.window(chat_filter, offset, limit)

    def report_error(self, message: str):
        logger.error(message)
        self.emit_event("error", message)

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)

        def handle_async_exception(loop, context):
            exc = context.get("exception")
            logger.error("Unhandled asyncio exception: %s",
                         exc or context.get("message"), exc_info=exc)

        self.loop.set_exception_handler(handle_async_exception)
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.close()

    def _spawn(self, coro, owner: Optional[DownloadItem] = None):
        """Schedules a coroutine as a real asyncio.Task on the loop thread.

        Always producing a Task (rather than a concurrent.futures.Future from
        run_coroutine_threadsafe) is what makes cancel() actually raise
        CancelledError inside the worker. Strong refs are held so in-flight
        tasks cannot be garbage collected.
        """
        generation = self._account_generation

        def _create():
            if self._shutting_down or self._account_resetting or generation != self._account_generation:
                coro.close()
                return
            task = asyncio.ensure_future(coro)
            if owner is not None:
                owner.task = task
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)

        if not self._call_on_loop(_create):
            coro.close()

    def _call_on_loop(self, fn) -> bool:
        """Runs fn on the loop thread. Returns False once the loop is gone."""
        if self._shutting_down:
            return False
        if threading.current_thread() is self.thread:
            fn()
            return True
        try:
            self.loop.call_soon_threadsafe(fn)
            return True
        except RuntimeError:
            # Loop already closed (app is exiting); nothing left to schedule onto.
            return False

    def run_coro(self, coro):
        generation = self._account_generation

        async def tracked():
            if generation != self._account_generation:
                coro.close()
                return
            task = asyncio.current_task()
            self._pending_tasks.add(task)
            try:
                return await coro
            finally:
                self._pending_tasks.discard(task)
        return asyncio.run_coroutine_threadsafe(tracked(), self.loop)

    async def _reset_account_work(self):
        """Drain old work before replacing a client; never mix account entities."""
        self._account_resetting = True
        self.authorized = False
        self._account_generation += 1
        try:
            self.cancel_extension_scan()
            await self._reset_scheduler()
            pending = [t for t in self._pending_tasks if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            with self.queue_lock:
                self.download_queue.clear()
                self.queue_index.clear()
                self._queue_items_removed([])
            with self.states_lock:
                self.pagination_states.clear()
            self.chosen_exts.clear()
            self.all_formats_mode = False
            self.last_dispatched_chat_id = None
            self.flood_until = 0
            self._flood_timer_armed = False
            # All producers belonging to the old account have now finished.
            while True:
                try:
                    self.event_queue.get_nowait()
                except queue.Empty:
                    break
            self.emit_event("account_reset")
        finally:
            self._account_resetting = False

    def snapshot_states(self) -> List[ChatSnapshot]:
        """Copies chat progress under lock.

        The Tk thread used to iterate pagination_states directly while the loop
        thread inserted into it, which raises RuntimeError mid-iteration and
        aborted the whole event-processing tick.
        """
        with self.states_lock:
            states = list(self.pagination_states.values())
        snapshots = []
        for state in states:
            counts = self.queue_stats(state.chat_id)
            if state.paused:
                status = "Paused"
            elif state.cancelled:
                status = "Cancelled"
            elif time.monotonic() < self.flood_until and (state.has_more or counts.queued):
                status = "Waiting for Telegram"
            elif counts.active:
                status = "Downloading"
            elif state.fetching_in_progress:
                status = "Discovering"
            elif state.retry_armed:
                status = "Retrying discovery"
            elif counts.queued or state.has_more:
                status = "Queued"
            elif counts.failed or state.last_error:
                status = "Failed"
            else:
                status = "Finished"
            snapshots.append(ChatSnapshot(state.chat_id, state.chat_name, state.total_discovered,
                                          state.total_completed, status, counts.queued,
                                          counts.active, counts.failed, state.paused))
        return snapshots

    def snapshot_queue(self, chat_filter: Optional[int] = None) -> List[DownloadItem]:
        with self.queue_lock:
            if chat_filter is None:
                return list(self.download_queue)
            return [it for it in self.download_queue if it.chat_id == chat_filter]

    def queue_stats(self, chat_filter=None) -> QueueStats:
        with self.queue_lock:
            self._ensure_view_index()
            return self._view_index.snapshot(chat_filter)

    def update_config(self, new_config: dict):
        """Synchronous facade for background callers; state commits on the loop."""
        if threading.current_thread() is self.thread:
            raise RuntimeError("Use await apply_config() on the backend loop.")
        return self.run_coro(self.apply_config(new_config)).result()

    async def apply_config(self, new_config):
        async with self._config_save_lock:
            updated = validate_config({**self.config, **new_config})
            error = await asyncio.to_thread(save_config, updated, self._config_file)
            if error:
                self.report_error(f"Could not save settings: {error}")
                return error
            self.config = updated
            self._schedule_downloads()
            return None

    # ------------------------------------------------------------------ client

    async def fetch_dialogs(self):
        """Enumerates every chat, channel and group the account can see.

        Two deliberate departures from a plain ``get_dialogs(limit=None)``:

        * The main list and the archive are walked as separate passes. Telethon
          documents an unspecified ``folder`` as returning everything, but that
          relies on Telegram honouring an omitted ``folder_id``; asking for
          folder 0 and folder 1 explicitly is a superset either way.
        * Partial results are always delivered. Telethon's own docstring warns
          that enumerating hundreds of dialogs draws a ``FloodWaitError``, and
          a single wrapping ``except`` around one big call turns that into an
          empty chat list, which is exactly the "not all groups load" symptom.
        """
        if self.accounts and not self.authorized:
            return

        collected: Dict[int, Any] = {}
        failures: List[str] = []

        for label, archived in (("main", False), ("archive", True)):
            try:
                await self._collect_dialogs(collected, archived, label)
            except (errors.UnauthorizedError, errors.AuthKeyError):
                await self.session_invalid()
                return
            except Exception as e:
                logger.exception("Dialog enumeration failed for the %s folder: %s", label, e)
                failures.append(f"{label} folder: {e}")

        entries = [ChatEntry.from_dialog(d) for d in collected.values()]
        # Groups and channels first: they are what this tool is for, and burying
        # them under hundreds of DMs is indistinguishable from not loading them.
        entries.sort(key=lambda e: (_CHAT_KIND_ORDER.get(e.kind, 9), e.name.lower()))

        counts: Dict[str, int] = {}
        for e in entries:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        logger.info("Fetched %d dialogs (%s), archived=%d",
                    len(entries),
                    ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none",
                    sum(1 for e in entries if e.archived))

        self.emit_event("dialogs_loaded", entries)
        if failures:
            self.report_error(
                "Some chats could not be listed ({}). {} loaded so far — reopen the "
                "app to try again.".format("; ".join(failures), len(entries)))

    async def _collect_dialogs(self, collected: Dict[int, Any], archived: bool, label: str):
        """Walks one folder into ``collected``, retrying through rate limits."""
        attempts = 0
        while True:
            before = len(collected)
            try:
                async for d in self.client.iter_dialogs(limit=None, archived=archived):
                    if d.id not in collected:
                        collected[d.id] = d
                        if len(collected) % 25 == 0:
                            self.emit_event("dialogs_progress", len(collected))
                logger.info("Walked %s folder: %d new, %d total",
                            label, len(collected) - before, len(collected))
                return
            except errors.FloodError as e:
                wait = int(getattr(e, "seconds", 0) or 0)
                attempts += 1
                if attempts > 3:
                    logger.warning("Giving up on the %s folder after %d rate limits; "
                                   "keeping %d dialogs.", label, attempts, len(collected))
                    return
                logger.warning("Rate limited listing the %s folder; waiting %ss (attempt %d).",
                               label, wait, attempts)
                self.emit_event("dialogs_progress",
                                f"Rate limited, waiting {wait}s ({len(collected)} chats so far)")
                await asyncio.sleep(wait + 1)
                # The restart re-walks from the top; dedupe by id makes that safe.

    def begin_extension_scan(self, selected_dialogs):
        self.latest_scan_generation += 1
        generation = self.latest_scan_generation
        selected = tuple(selected_dialogs)

        def begin():
            if self._extension_scan_task is not None:
                self._extension_scan_task.cancel()
            self._spawn(self.scan_extensions(selected, generation))
        self._call_on_loop(begin)
        return generation

    def cancel_extension_scan(self):
        self.latest_scan_generation += 1
        def cancel():
            if self._extension_scan_task is not None:
                self._extension_scan_task.cancel()
        self._call_on_loop(cancel)

    async def scan_extensions(self, selected_dialogs, generation=None):
        """One cancellable preview scan; obsolete results never replace selections."""
        if generation is None:
            self.latest_scan_generation += 1
            generation = self.latest_scan_generation
        if generation != self.latest_scan_generation or (self.accounts and not self.authorized):
            return
        self._extension_scan_task = asyncio.current_task()
        discovered = set()
        failures = []
        status = "completed"
        scanned = 0
        limit = self.config.get("scan_depth", 500) or None

        def event(kind, message, **extra):
            if generation == self.latest_scan_generation:
                self.emit_event(kind, {"generation": generation, "message": message,
                                      "extensions": sorted(discovered), "status": status, **extra})
        event("scan_started", f"Scanning {len(selected_dialogs)} chat(s)...")
        try:
            for index, dialog in enumerate(selected_dialogs, 1):
                cursor = 0
                seen = 0
                while limit is None or seen < limit:
                    while time.monotonic() < self.flood_until:
                        event("scan_progress", f"Waiting for Telegram ({max(1, int(self.flood_until-time.monotonic()))}s)")
                        await asyncio.sleep(min(1, max(0, self.flood_until-time.monotonic())))
                    count = 0
                    try:
                        page_limit = min(MESSAGE_SCAN_WINDOW, limit-seen) if limit else MESSAGE_SCAN_WINDOW
                        kwargs = {"limit": page_limit}
                        if cursor:
                            kwargs["offset_id"] = cursor
                        async for message in self.client.iter_messages(dialog.entity, **kwargs):
                            cursor = message.id
                            seen += 1
                            count += 1
                            scanned += 1
                            if message.file and message.file.ext:
                                discovered.add(message.file.ext.lower())
                            if count % 25 == 0:
                                event("scan_progress", f"Scanning {index}/{len(selected_dialogs)}: {dialog.name} ({seen} messages)")
                                await asyncio.sleep(0)
                        event("scan_progress", f"Scanned {index}/{len(selected_dialogs)}: {dialog.name} ({seen} messages)")
                        if count < page_limit:
                            break
                    except errors.FloodError as exc:
                        self._enter_flood_wait(getattr(exc, "seconds", 1) or 1)
                    except (errors.UnauthorizedError, errors.AuthKeyError):
                        await self.session_invalid()
                        return
                    except Exception as exc:
                        failures.append(f"{dialog.name}: {type(exc).__name__}")
                        break
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            if self._extension_scan_task is asyncio.current_task():
                self._extension_scan_task = None
            if failures:
                status = "partial"
            message = (f"Scan {status}: {len(discovered)} format(s), {scanned} messages."
                       + (" Failed chats: " + "; ".join(failures) if failures else ""))
            event("scan_finished", message)

    # ------------------------------------------------------------- enqueue/fetch

    def _make_chat_job(self, dialog, formats, all_formats):
        return ChatPaginationState(
            chat_id=dialog.id, chat_name=dialog.name, entity=dialog.entity,
            chosen_exts=frozenset(formats), all_formats=all_formats,
            output_dir=self.config["output_dir"])

    def _make_download_item(self, state, message, ext):
        return DownloadItem(
            id=f"{state.chat_id}_{message.id}", chat_id=state.chat_id,
            chat_name=state.chat_name, message=message,
            file_name=sanitize_file_name(message.file.name, message.id, ext),
            msg_id=message.id, ext=ext, file_size=message.file.size,
            output_dir=state.output_dir, topic_id=topic_id(message, is_forum(state.entity)))

    async def _resolve_item_topics(self, chat_id, entity, items):
        if not hasattr(self, "topic_titles"):
            self.topic_titles = {}
        titles = self.topic_titles.setdefault(chat_id, {})
        missing = {item.topic_id for item in items if item.topic_id is not None and item.topic_id not in titles}
        if missing:
            try:
                result = await self.client(topics_request(entity, sorted(missing)))
                titles.update({topic.id: topic.title for topic in result.topics if hasattr(topic, "title")})
            except (errors.FloodError, errors.UnauthorizedError, errors.AuthKeyError):
                raise
            except Exception:
                logger.info("Topic titles unavailable for chat %s; using stable IDs", chat_id)
            for id in missing:
                titles.setdefault(id, "General" if id == 1 else f"Topic_{id}")
        for item in items:
            item.topic_name = titles.get(item.topic_id)

    # ------------------------------------------------------------------ download

    def _resolve_paths(self, item: DownloadItem) -> tuple[str, str, str]:
        """Returns (output_root, save_dir, save_path), all guaranteed inside output_root."""
        root = os.path.abspath(item.output_dir or self.config["output_dir"])
        chat_dir = sanitize_chat_dir(item.chat_name, item.chat_id)
        save_dir = os.path.join(root, chat_dir)
        save_path = os.path.join(save_dir, item.file_name)
        if not is_within(root, save_dir) or not is_within(root, save_path):
            raise ValueError(f"Refusing to write outside the download folder: {save_path!r}")
        return root, save_dir, save_path

    async def _disk_operation(self, operation, *args):
        """Drain disk work before cancellation can release its file ownership."""
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._drain_disk_task(task)
            raise

    @staticmethod
    async def _drain_disk_task(task):
        # A second Stop/account-switch/shutdown request must not abandon the
        # underlying thread while it still owns a file or reservation.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        return task.result()

    async def _download_worker(self, item: DownloadItem, generation=None):
        """Transfer a reserved media identity without blocking the event loop."""
        generation = getattr(item, "worker_generation", 0) if generation is None else generation
        storage = reservation = None

        def owns_item():
            return generation == getattr(item, "worker_generation", 0)

        try:
            if not owns_item() or item.status in ("Cancelled", "Paused"):
                return
            item.last_error = ""
            if item.message is None and item.expected_identity:
                if not await self._refresh_reference(item):
                    raise ValueError("This message is no longer available. Rescan the chat to refresh it.")
            root, _, _ = await self._disk_operation(self._resolve_paths, item)
            account = (f"user:{self.current_user_id}" if getattr(self, "current_user_id", None)
                       else f"profile:{self.profile_id}" if getattr(self, "profile_id", None)
                       else f"session:{os.path.abspath(self.session_path)}")
            identity = media_identity(account, item)
            if item.expected_identity and identity.key != item.expected_identity:
                raise ValueError("The selected media changed on Telegram. Rescan before downloading it.")
            storage = await self._disk_operation(get_storage, root)
            prepare = asyncio.create_task(asyncio.to_thread(
                storage.prepare, account, item.chat_id, sanitize_chat_dir(item.chat_name, item.chat_id),
                item.file_name, identity, item.topic_id,
                sanitize_chat_dir(item.topic_name or ("General" if item.topic_id == 1 else
                                  f"Topic_{item.topic_id}"), item.topic_id or 0) if item.topic_id else None))
            try:
                reservation = await asyncio.shield(prepare)
            except asyncio.CancelledError:
                reservation = await self._drain_disk_task(prepare)
                raise
            if not owns_item() or item.status in ("Cancelled", "Paused"):
                raise asyncio.CancelledError()
            item.output_path = reservation.save_path
            if reservation.complete:
                item.status = "Skipped (Exists)"
                item.progress = 1.0
                item.downloaded_bytes = reservation.written
                item.message = None
                return
            await self._transfer(item, reservation.temp_path, reservation.save_path)
            if not owns_item() or item.status in ("Cancelled", "Paused"):
                raise asyncio.CancelledError()
            save_path, written = await self._disk_operation(storage.finalize, reservation)
            if not owns_item():
                return
            item.output_path = save_path
            item.status = "Completed"
            item.progress = 1.0
            item.downloaded_bytes = written
            item.message = None
            logger.debug("Downloaded '%s' -> '%s'", item.file_name, save_path)
        except asyncio.CancelledError:
            if owns_item():
                item.status = "Paused" if getattr(item, "_pause_requested", False) else "Cancelled"
            logger.info("Stopped '%s'; resumable bytes retained.", item.file_name)
            raise
        except errors.FloodError as e:
            wait = getattr(e, "seconds", 0) or 0
            if owns_item():
                item.last_error = f"Telegram rate limit: retry after {wait} seconds."
                self._enter_flood_wait(wait)
            if owns_item() and item.retries < MAX_ITEM_RETRIES:
                item.retries += 1
                item.status = "Queued"
            elif owns_item():
                item.status = "Failed (rate limit)"
        except errors.FileReferenceExpiredError:
            if owns_item():
                item.last_error = "Telegram file reference expired; refreshing the message."
            if owns_item() and item.retries < MAX_ITEM_RETRIES and await self._refresh_reference(item):
                item.retries += 1
                item.status = "Queued"
            elif owns_item():
                item.status = "Failed (stale reference)"
        except (errors.UnauthorizedError, errors.AuthKeyError):
            if owns_item():
                item.last_error = "Telegram session is no longer authorized. Sign in again."
                item.status = "Cancelled"
                await self.session_invalid()
        except (errors.RPCError, OSError) as e:
            logger.warning("Download of '%s' failed: %s", item.file_name, e)
            if owns_item():
                item.last_error = str(e)
            permanent = (isinstance(e, OSError) and e.errno in
                         (errno.ENOSPC, errno.EACCES, errno.EPERM, errno.EROFS))
            if owns_item() and not permanent and item.retries < MAX_ITEM_RETRIES:
                item.retries += 1
                item.status = "Queued"
            elif owns_item():
                item.status = "Failed"
                self.report_error(f"Could not download '{item.file_name}': {e}")
        except Exception as e:  # noqa: BLE001 - surface unexpected failures without stranding a worker
            logger.exception("Unexpected failure downloading '%s'", item.file_name)
            if owns_item():
                item.last_error = str(e)
                item.status = "Failed"
                self.report_error(f"Could not download '{item.file_name}': {e}")
        finally:
            if reservation is not None and storage is not None:
                try:
                    if owns_item() and item.status not in ("Completed", "Skipped (Exists)"):
                        item.downloaded_bytes = await self._disk_operation(storage.partial_bytes, reservation)
                        item.progress = min(1.0, item.downloaded_bytes / item.file_size) if item.file_size else 0.0
                finally:
                    await self._disk_operation(storage.release, reservation)
            if owns_item():
                item.speed = 0.0
                if item.status == "Downloading":
                    item.status = "Failed"
                if item.status in ("Completed", "Skipped (Exists)"):
                    with self.states_lock:
                        state = self.pagination_states.get(item.chat_id)
                        if state:
                            state.total_completed += 1
                if item.task is asyncio.current_task():
                    item.task = None
                self.emit_event("item_updated", item)
                self._check_and_trigger_next_batch(item.chat_id)
                self._schedule_downloads()

    async def _refresh_reference(self, item: DownloadItem) -> bool:
        """Re-fetches a message whose file reference expired during a long run."""
        try:
            with self.states_lock:
                state = self.pagination_states.get(item.chat_id)
                entity = state.entity if state else item.chat_id
            fresh = await self.client.get_messages(entity, ids=item.msg_id)
            if fresh and fresh.file:
                item.message = fresh
                item.file_size = fresh.file.size
                item.ext = (fresh.file.ext or "").lower()
                item.file_name = sanitize_file_name(fresh.file.name, item.msg_id, item.ext)
                logger.info("Refreshed stale file reference for '%s'.", item.file_name)
                return True
        except (errors.FloodError, errors.UnauthorizedError, errors.AuthKeyError):
            raise
        except Exception as e:  # noqa: BLE001 - a failed refresh should become a recoverable row error
            logger.warning("Could not refresh reference for '%s': %s", item.file_name, e)
        return False

    def _make_progress_cb(self, item: DownloadItem):
        """Progress callback throttled to ~10 Hz so the UI queue cannot be swamped."""
        item.last_update_time = time.monotonic()
        item.last_bytes = item.downloaded_bytes
        generation = getattr(item, "worker_generation", 0)

        def progress_cb(current, total):
            if (item.status in ("Cancelled", "Paused")
                    or generation != getattr(item, "worker_generation", 0)):
                return
            now = time.monotonic()
            elapsed = now - item.last_update_time
            if elapsed < 0.1:
                return
            item.speed = max(0.0, (current - item.last_bytes) / elapsed)
            item.downloaded_bytes = current
            item.progress = (current / total) if total else 0.0
            item.last_update_time = now
            item.last_bytes = current
            self.emit_event("item_progress", item)

        return progress_cb

    async def _transfer(self, item: DownloadItem, temp_path: str, save_path: str):
        """Resume only manifest-owned documents; photo sizes may be estimates."""
        progress_cb = self._make_progress_cb(item)
        exact_size = media_identity("transfer", item).exact_size

        def partial_size():
            return os.path.getsize(temp_path) if os.path.isfile(temp_path) else 0

        existing = await self._disk_operation(partial_size)
        if exact_size and item.file_size and existing == item.file_size:
            return
        resume_from = ((existing // RESUME_ALIGN) * RESUME_ALIGN
                       if exact_size and item.file_size and existing < item.file_size else 0)
        if resume_from:
            try:
                logger.info("Resuming '%s' at %s.", item.file_name, human_size(resume_from))
                await self._download_resumed(item, temp_path, resume_from, progress_cb)
                return
            except (AttributeError, TypeError, NotImplementedError,
                    IncompleteDownloadError, ResumeRestartRequired) as e:
                # Unsupported streams or a normal EOF before the advertised end
                # can restart once through Telethon's normal downloader. Network
                # exceptions retain the partial and retry the aligned stream.
                logger.warning("Resume could not complete for '%s' (%s); restarting from scratch.",
                               item.file_name, e)
                item.downloaded_bytes = 0
                item.progress = 0.0
                progress_cb = self._make_progress_cb(item)
        sink = await AsyncDiskSink.open(temp_path)
        try:
            await self.client.download_media(item.message, file=sink, progress_callback=progress_cb)
        finally:
            await sink.finish()

    async def _download_resumed(self, item: DownloadItem, temp_path: str,
                                resume_from: int, progress_cb):
        """Append aligned chunks; all local file operations stay off this loop."""
        total = item.file_size or 0
        downloaded = resume_from
        item.downloaded_bytes = downloaded
        sink = await AsyncDiskSink.open(temp_path, resume_from)
        try:
            async with download_stream(self.client,
                item.message, offset=resume_from, request_size=RESUME_ALIGN
            ) as stream:
                async for chunk in stream:
                    if item.status in ("Cancelled", "Paused"):
                        raise asyncio.CancelledError()
                    await sink.write(chunk)
                    downloaded += len(chunk)
                    progress_cb(downloaded, total or downloaded)
        finally:
            await sink.finish()
        if total and downloaded != total:
            raise IncompleteDownloadError(f"resume wrote {downloaded} of {total} bytes")

    # ------------------------------------------------------------------ actions

    def sweep_orphan_parts(self, output_dir=None) -> tuple[int, int]:
        """Count unowned fragments. GUI callers must dispatch this off the Tk thread."""
        root = self.config.get("output_dir", "") if output_dir is None else output_dir
        if not os.path.isdir(root):
            return 0, 0
        return get_storage(root).orphan_parts()

    def delete_orphan_parts(self, output_dir=None) -> tuple[int, int]:
        """Delete unowned fragments while retaining active and resumable files."""
        root = self.config.get("output_dir", "") if output_dir is None else output_dir
        if not os.path.isdir(root):
            return 0, 0
        count, total = get_storage(root).orphan_parts(delete=True)
        logger.info("Removed %d orphaned .part file(s), %s.", count, human_size(total))
        return count, total

    def shutdown(self, timeout: float = 5.0):
        """Cancel authentication and downloads before disconnecting and closing."""
        if self._shutting_down:
            return
        self._shutting_down = True

        async def drain():
            await self._reset_scheduler()
            pending = list(self._pending_tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if self.client is not None:
                await self.client.disconnect()

        try:
            asyncio.run_coroutine_threadsafe(drain(), self.loop).result(timeout=timeout)
        except Exception as exc:
            logger.warning("Shutdown did not complete cleanly: %s", type(exc).__name__)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=timeout)


