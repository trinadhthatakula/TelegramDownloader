import os
import re
import sys
import json
import time
import queue
import logging
import asyncio
import threading
import subprocess
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass
from typing import Dict, List, Set, Tuple, Optional, Any

import customtkinter as ctk
from tkinter import filedialog, messagebox
from dotenv import load_dotenv
from telethon import TelegramClient, errors
from telegram_auth import AccountStore, AccountPage, AuthenticationMixin, validate_credentials

# All app-owned paths resolve against the script directory, never the working
# directory: launching from elsewhere used to scatter config.json / downloader.log
# / the Telethon session into whatever CWD the launcher happened to pick.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOG_FILE = os.path.join(BASE_DIR, "downloader.log")

# Downloads are dispatched in fixed slots; a batch fetch scans this many messages
# looking for at most TARGET_BATCH_SIZE matches before yielding.
MESSAGE_SCAN_WINDOW = 200
TARGET_BATCH_SIZE = 50
QUEUE_REFILL_THRESHOLD = 15

# Telegram requires download offsets and chunk sizes to be 4 KiB aligned; 512 KiB
# is Telethon's MAX_CHUNK_SIZE and the largest legal request size.
RESUME_ALIGN = 512 * 1024

# Retry budget per item before it becomes terminally Failed.
MAX_ITEM_RETRIES = 3
# Backoff schedule (seconds) for repeated batch-fetch failures on one chat.
FETCH_BACKOFF = (2, 5, 15, 45, 120)

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


def configure_logging():
    """Installs a size-capped rotating file log. Called from main(), not at import."""
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.DEBUG)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
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


def load_config() -> dict:
    """Loads configuration from config.json, falling back to validated defaults."""
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


def save_config(config_data: dict) -> Optional[str]:
    """Atomically persists the configuration. Returns an error string on failure."""
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


@dataclass
class ChatSnapshot:
    """Immutable view of a ChatPaginationState, safe to read from the Tk thread."""
    chat_id: int
    chat_name: str
    total_discovered: int
    total_completed: int


@dataclass
class QueueStats:
    total: int = 0
    active: int = 0
    queued: int = 0
    done: int = 0
    failed: int = 0
    speed: float = 0.0
    downloaded: int = 0
    total_bytes: int = 0


class TelegramBackend(AuthenticationMixin):
    def __init__(self, api_id: int, api_hash: str, session_path: str, accounts=None):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self.configure_auth(accounts)
        self._account_resetting = False
        self._account_generation = 0

        self.event_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.config = load_config()

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
        self.event_queue.put((event_type, data))

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
            self.cancel_all(exclude_task=asyncio.current_task())
            pending = [t for t in self._pending_tasks if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            with self.queue_lock:
                self.download_queue.clear()
                self.queue_index.clear()
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
            return [
                ChatSnapshot(s.chat_id, s.chat_name, s.total_discovered, s.total_completed)
                for s in self.pagination_states.values()
            ]

    def snapshot_queue(self, chat_filter: Optional[int] = None) -> List[DownloadItem]:
        with self.queue_lock:
            if chat_filter is None:
                return list(self.download_queue)
            return [it for it in self.download_queue if it.chat_id == chat_filter]

    def queue_stats(self) -> QueueStats:
        stats = QueueStats()
        with self.queue_lock:
            for it in self.download_queue:
                stats.total += 1
                stats.total_bytes += it.file_size or 0
                if it.status == "Downloading":
                    stats.active += 1
                    stats.speed += it.speed
                    stats.downloaded += it.downloaded_bytes
                elif it.status == "Queued":
                    stats.queued += 1
                elif it.status in ("Completed", "Skipped (Exists)"):
                    stats.done += 1
                    stats.downloaded += it.file_size or 0
                elif it.status.startswith("Failed") or it.status == "Cancelled":
                    stats.failed += 1
        return stats

    def update_config(self, new_config: dict):
        """Validates, persists and applies new settings."""
        self.config = validate_config({**self.config, **new_config})
        err = save_config(self.config)
        if err:
            self.report_error(f"Could not save settings: {err}")
        logger.info("Configuration updated: %s", self.config)
        self._schedule_downloads()

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

    async def scan_extensions(self, selected_dialogs: "List[ChatEntry]"):
        """Scans recent history across selected chats to discover media extensions."""
        if self.accounts and not self.authorized:
            return

        discovered: Set[str] = set()
        try:
            scan_limit = self.config.get("scan_depth", 500)
            limit_val = None if scan_limit == 0 else scan_limit
            logger.info("Scanning %d chats (depth: %s)...", len(selected_dialogs), limit_val or "unlimited")

            for idx, d in enumerate(selected_dialogs, start=1):
                self.emit_event("scan_progress", f"Scanning {idx}/{len(selected_dialogs)}: {d.name}")
                try:
                    async for msg in self.client.iter_messages(d.entity, limit=limit_val):
                        if msg.file and msg.file.ext:
                            discovered.add(msg.file.ext.lower())
                except (errors.UnauthorizedError, errors.AuthKeyError):
                    await self.session_invalid()
                    return
                except errors.FloodError as e:
                    wait = getattr(e, "seconds", 0) or 0
                    logger.warning("Rate limited while scanning '%s': waiting %ss", d.name, wait)
                    self.emit_event("scan_progress", f"Rate limited, waiting {wait}s...")
                    await asyncio.sleep(wait + 1)
                except Exception as e:
                    logger.warning("Skipping scan of '%s': %s", d.name, e)

            sorted_exts = sorted(discovered)
            logger.info("Discovered extensions: %s", sorted_exts)
            self.emit_event("extensions_scanned", sorted_exts)
        except Exception as e:
            logger.exception("Error scanning extensions: %s", e)
            self.report_error(f"Scan failed: {e}")
            self.emit_event("extensions_scanned", [])

    # ------------------------------------------------------------- enqueue/fetch

    async def enqueue_selected_chats(self, selected_dialogs: "List[ChatEntry]", chosen_exts: set,
                                     all_formats: bool = False):
        """Prepares per-chat state and kicks off the first batch fetch for each."""
        if self.accounts and not self.authorized:
            return

        self.chosen_exts = set(chosen_exts)
        self.all_formats_mode = all_formats or ("*" in chosen_exts) or (len(chosen_exts) == 0)

        logger.info("Enqueueing %d chats. exts=%s all_formats=%s",
                    len(selected_dialogs), chosen_exts, self.all_formats_mode)

        with self.states_lock:
            for d in selected_dialogs:
                state = self.pagination_states.get(d.id)
                if not state:
                    state = ChatPaginationState(chat_id=d.id, chat_name=d.name, entity=d.entity)
                    self.pagination_states[d.id] = state
                else:
                    state.entity = d.entity
                    state.last_msg_id = 0
                    state.has_more = True
                    state.fetching_in_progress = False
                    state.consecutive_errors = 0

        self.emit_event("queue_updated", None)
        for d in selected_dialogs:
            self._spawn(self._fetch_next_chat_batch(d.id))

    async def _fetch_next_chat_batch(self, chat_id: int):
        """Fetches the next batch of matching files for one chat."""
        with self.states_lock:
            state = self.pagination_states.get(chat_id)
            if not state or state.fetching_in_progress or not state.has_more:
                logger.debug("Fetch skipped for %s (exists=%s fetching=%s has_more=%s)", chat_id,
                             state is not None,
                             state.fetching_in_progress if state else None,
                             state.has_more if state else None)
                return
            state.fetching_in_progress = True
            state.retry_armed = False

        new_items: List[DownloadItem] = []
        failed = False
        try:
            logger.debug("Fetching batch for '%s' (%s), offset_id=%s",
                         state.chat_name, chat_id, state.last_msg_id)
            fetched_matching = 0
            scanned = 0

            kwargs: Dict[str, Any] = {"limit": MESSAGE_SCAN_WINDOW}
            if state.last_msg_id > 0:
                kwargs["offset_id"] = state.last_msg_id

            target_entity = state.entity if state.entity is not None else chat_id

            async for msg in self.client.iter_messages(target_entity, **kwargs):
                state.last_msg_id = msg.id
                scanned += 1

                if not msg.file:
                    continue

                ext = (msg.file.ext or "").lower()
                if not (self.all_formats_mode or ext in self.chosen_exts):
                    continue

                item_id = f"{chat_id}_{msg.id}"
                with self.queue_lock:
                    if item_id in self.queue_index:
                        continue

                new_items.append(DownloadItem(
                    id=item_id,
                    chat_id=chat_id,
                    chat_name=state.chat_name,
                    message=msg,
                    file_name=sanitize_file_name(msg.file.name, msg.id, ext),
                    msg_id=msg.id,
                    ext=ext,
                    file_size=msg.file.size,
                ))
                fetched_matching += 1
                if fetched_matching >= TARGET_BATCH_SIZE:
                    break

            logger.info("Batch for '%s': scanned %d, queued %d new.",
                        state.chat_name, scanned, len(new_items))

            if scanned < MESSAGE_SCAN_WINDOW and fetched_matching < TARGET_BATCH_SIZE:
                state.has_more = False
                logger.info("Reached end of history for '%s'.", state.chat_name)

            if new_items:
                with self.queue_lock:
                    for item in new_items:
                        self.download_queue.append(item)
                        self.queue_index[item.id] = item
                with self.states_lock:
                    state.total_discovered += len(new_items)
                self.emit_event("queue_updated", None)

            state.consecutive_errors = 0
            self._schedule_downloads()

        except asyncio.CancelledError:
            raise
        except (errors.UnauthorizedError, errors.AuthKeyError):
            await self.session_invalid()
        except errors.FloodError as e:
            failed = True
            wait = getattr(e, "seconds", 0) or 0
            logger.warning("Rate limited fetching '%s': %ss", state.chat_name, wait)
            self._enter_flood_wait(wait)
        except Exception as e:
            failed = True
            logger.exception("Error fetching batch for '%s' (%s): %s", state.chat_name, chat_id, e)
        finally:
            with self.states_lock:
                state.fetching_in_progress = False
            if failed:
                self._arm_fetch_retry(chat_id)
            else:
                self._check_and_trigger_next_batch(chat_id)

    def _arm_fetch_retry(self, chat_id: int):
        """Backs off after a failed batch fetch.

        Previously the finally-block re-triggered the fetch immediately, so a
        persistent failure (revoked access, network down) became an unbounded
        zero-delay retry loop that hammered the API and flooded the log.
        """
        with self.states_lock:
            state = self.pagination_states.get(chat_id)
            if not state or state.retry_armed:
                return
            state.consecutive_errors += 1
            attempt = state.consecutive_errors
            if attempt > len(FETCH_BACKOFF):
                state.has_more = False
                logger.error("Giving up on '%s' after %d consecutive fetch failures.",
                             state.chat_name, attempt)
                self.report_error(
                    f"Stopped scanning '{state.chat_name}' after {attempt} failures."
                )
                return
            delay = FETCH_BACKOFF[attempt - 1]
            state.retry_armed = True
            name = state.chat_name

        logger.info("Retrying '%s' in %ss (attempt %d).", name, delay, attempt)

        generation = self._account_generation
        self._call_on_loop(
            lambda: self.loop.call_later(
                delay, lambda: self._spawn(self._fetch_next_chat_batch(chat_id))
                if generation == self._account_generation else None)
        )

    def _check_and_trigger_next_batch(self, chat_id: int):
        """Fetches the next batch when a chat's pending queue runs low."""
        try:
            with self.states_lock:
                state = self.pagination_states.get(chat_id)
                if not state or not state.has_more or state.fetching_in_progress:
                    return
                name = state.chat_name

            with self.queue_lock:
                pending = sum(
                    1 for it in self.download_queue
                    if it.chat_id == chat_id and it.status in ("Queued", "Downloading")
                )

            if pending < QUEUE_REFILL_THRESHOLD:
                logger.debug("Queue low for '%s' (%d pending); fetching next batch.", name, pending)
                self._spawn(self._fetch_next_chat_batch(chat_id))
        except Exception as e:
            logger.exception("Error in _check_and_trigger_next_batch(%s): %s", chat_id, e)

    # ---------------------------------------------------------------- scheduling

    def _enter_flood_wait(self, seconds: int):
        """Pauses all dispatch until the rate limit expires."""
        seconds = max(1, int(seconds or 1))
        deadline = time.monotonic() + seconds
        if deadline <= self.flood_until:
            return
        self.flood_until = deadline
        logger.warning("Rate limited by Telegram; pausing downloads for %ss.", seconds)
        self.emit_event("flood_wait", seconds)
        self._arm_flood_timer()

    def _arm_flood_timer(self):
        if self._flood_timer_armed:
            return
        self._flood_timer_armed = True
        delay = max(0.5, self.flood_until - time.monotonic() + 0.5)

        generation = self._account_generation

        def _clear():
            if generation != self._account_generation:
                return
            self._flood_timer_armed = False
            if time.monotonic() >= self.flood_until:
                logger.info("Rate limit expired; resuming downloads.")
                self.emit_event("flood_cleared", None)
                self._schedule_downloads()
            else:
                self._arm_flood_timer()

        if not self._call_on_loop(lambda: self.loop.call_later(delay, _clear)):
            self._flood_timer_armed = False

    def _schedule_downloads(self):
        """Dispatches queued items using fair round-robin across active chats."""
        try:
            if self._shutting_down or self._account_resetting:
                return
            if time.monotonic() < self.flood_until:
                self._arm_flood_timer()
                return

            to_start: List[DownloadItem] = []
            with self.queue_lock:
                active = sum(1 for it in self.download_queue if it.status == "Downloading")
                max_parallel = self.config.get("max_parallel_files", 3)
                slots = max_parallel - active
                if slots <= 0:
                    return

                # Bucket once, then round-robin out of the buckets. The previous
                # version rebuilt this map from the whole queue for every slot.
                queued_by_chat: Dict[int, List[DownloadItem]] = {}
                for it in self.download_queue:
                    if it.status == "Queued":
                        queued_by_chat.setdefault(it.chat_id, []).append(it)
                if not queued_by_chat:
                    return

                chat_ids = list(queued_by_chat.keys())
                if self.last_dispatched_chat_id in chat_ids:
                    cursor = chat_ids.index(self.last_dispatched_chat_id)
                else:
                    cursor = -1

                while slots > 0 and chat_ids:
                    cursor = (cursor + 1) % len(chat_ids)
                    chat_id = chat_ids[cursor]
                    bucket = queued_by_chat[chat_id]
                    item = bucket.pop(0)
                    if not bucket:
                        chat_ids.pop(cursor)
                        cursor -= 1
                    item.status = "Downloading"
                    self.last_dispatched_chat_id = chat_id
                    to_start.append(item)
                    slots -= 1

            logger.debug("Dispatching %d download(s); %d active.", len(to_start), active)
            for item in to_start:
                self._spawn(self._download_worker(item), owner=item)
                self.emit_event("item_updated", item)
        except Exception as e:
            logger.exception("Error in _schedule_downloads: %s", e)

    # ------------------------------------------------------------------ download

    def _resolve_paths(self, item: DownloadItem) -> Tuple[str, str, str]:
        """Returns (output_root, save_dir, save_path), all guaranteed inside output_root."""
        root = os.path.abspath(self.config["output_dir"])
        chat_dir = sanitize_chat_dir(item.chat_name, item.chat_id)
        save_dir = os.path.join(root, chat_dir)
        save_path = os.path.join(save_dir, item.file_name)
        if not is_within(root, save_dir) or not is_within(root, save_path):
            raise ValueError(f"Refusing to write outside the download folder: {save_path!r}")
        return root, save_dir, save_path

    def _pick_free_path(self, save_path: str) -> str:
        """Finds a non-colliding variant instead of deleting an existing file."""
        stem, ext = os.path.splitext(save_path)
        for n in range(2, 1000):
            candidate = f"{stem} ({n}){ext}"
            if not os.path.exists(candidate):
                return candidate
        return f"{stem} ({int(time.time())}){ext}"

    async def _download_worker(self, item: DownloadItem):
        """Downloads one file. Every path out of here leaves a terminal status."""
        temp_path: Optional[str] = None
        try:
            # NOTE: all of this used to sit outside the try. An exception here
            # (unwritable dir, bad name) left the item stuck "Downloading"
            # forever, permanently consuming one parallel slot, silently.
            root, save_dir, save_path = self._resolve_paths(item)
            os.makedirs(save_dir, exist_ok=True)

            logger.debug("Worker start: '%s' (%s) in '%s'",
                         item.file_name, human_size(item.file_size or 0), item.chat_name)

            if item.status == "Cancelled":
                logger.info("'%s' was cancelled before starting.", item.file_name)
                return

            # Existing complete file -> skip. Keeps prior downloads valid.
            if os.path.exists(save_path):
                existing = os.path.getsize(save_path)
                if item.file_size is not None and existing == item.file_size:
                    logger.debug("Skipping '%s' - already on disk (%d bytes).", item.file_name, existing)
                    item.status = "Skipped (Exists)"
                    item.progress = 1.0
                    return
                if item.file_size is None and existing > 0:
                    item.status = "Skipped (Exists)"
                    item.progress = 1.0
                    return
                # Different file sharing a name: never delete the existing one.
                save_path = self._pick_free_path(save_path)
                logger.info("Name collision for '%s'; saving as '%s'.",
                            item.file_name, os.path.basename(save_path))

            # Per-message temp name: a shared "<name>.part" let two workers write
            # the same file and both report Completed.
            temp_path = os.path.join(save_dir, f".{item.chat_id}_{item.msg_id}.part")

            await self._transfer(item, temp_path, save_path)

            if not os.path.exists(temp_path):
                raise IOError("download produced no file")

            written = os.path.getsize(temp_path)
            if written == 0:
                raise IOError("download produced an empty file")
            if item.file_size is not None and written != item.file_size:
                # Not fatal: msg.file.size is an estimate for progressive photos,
                # so a mismatch here does not mean the file is truncated. The
                # resume path, where the byte accounting is ours, checks strictly.
                logger.warning("'%s': expected %s, wrote %s.", item.file_name,
                               human_size(item.file_size), human_size(written))

            os.replace(temp_path, save_path)
            temp_path = None
            item.status = "Completed"
            item.progress = 1.0
            item.downloaded_bytes = written
            logger.debug("Downloaded '%s' -> '%s'", item.file_name, save_path)

        except asyncio.CancelledError:
            item.status = "Cancelled"
            logger.info("Cancelled '%s'.", item.file_name)
            self._discard_temp(temp_path)
            raise
        except errors.FloodError as e:
            wait = getattr(e, "seconds", 0) or 0
            self._discard_temp(temp_path)
            self._enter_flood_wait(wait)
            # Requeue rather than dead-end: the old code left a permanent
            # "Rate Limited" row that could never run, clear, or re-enqueue.
            if item.retries < MAX_ITEM_RETRIES:
                item.retries += 1
                item.status = "Queued"
                item.progress = 0.0
                item.downloaded_bytes = 0
            else:
                item.status = "Failed (rate limit)"
        except errors.FileReferenceExpiredError:
            self._discard_temp(temp_path)
            if item.retries < MAX_ITEM_RETRIES and await self._refresh_reference(item):
                item.retries += 1
                item.status = "Queued"
            else:
                item.status = "Failed (stale reference)"
        except (errors.UnauthorizedError, errors.AuthKeyError):
            self._discard_temp(temp_path)
            item.status = "Cancelled"
            await self.session_invalid()
        except (errors.RPCError, OSError, IOError) as e:
            self._discard_temp(temp_path)
            logger.warning("Download of '%s' failed: %s", item.file_name, e)
            if item.retries < MAX_ITEM_RETRIES:
                item.retries += 1
                item.status = "Queued"
                item.progress = 0.0
                item.downloaded_bytes = 0
            else:
                item.status = "Failed"
                self.report_error(f"Could not download '{item.file_name}': {e}")
        except Exception as e:
            self._discard_temp(temp_path)
            logger.exception("Unexpected failure downloading '%s': %s", item.file_name, e)
            item.status = "Failed"
            self.report_error(f"Could not download '{item.file_name}': {e}")
        finally:
            if item.status == "Downloading":
                # Belt and braces: never leave a slot consumed.
                item.status = "Failed"
            if item.status in ("Completed", "Skipped (Exists)"):
                with self.states_lock:
                    state = self.pagination_states.get(item.chat_id)
                    if state:
                        state.total_completed += 1
            item.task = None
            self.emit_event("item_updated", item)
            self._check_and_trigger_next_batch(item.chat_id)
            self._schedule_downloads()

    @staticmethod
    def _discard_temp(temp_path: Optional[str]):
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError as e:
                logger.debug("Could not remove temp file %s: %s", temp_path, e)

    async def _refresh_reference(self, item: DownloadItem) -> bool:
        """Re-fetches a message whose file reference expired during a long run."""
        try:
            with self.states_lock:
                state = self.pagination_states.get(item.chat_id)
                entity = state.entity if state else item.chat_id
            fresh = await self.client.get_messages(entity, ids=item.msg_id)
            if fresh and fresh.file:
                item.message = fresh
                logger.info("Refreshed stale file reference for '%s'.", item.file_name)
                return True
        except Exception as e:
            logger.warning("Could not refresh reference for '%s': %s", item.file_name, e)
        return False

    def _make_progress_cb(self, item: DownloadItem):
        """Progress callback throttled to ~10 Hz so the UI queue cannot be swamped."""
        item.last_update_time = time.monotonic()
        item.last_bytes = item.downloaded_bytes

        def progress_cb(current, total):
            if item.status == "Cancelled":
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
        """Downloads the file, resuming from a partial .part when one is usable."""
        progress_cb = self._make_progress_cb(item)

        resume_from = 0
        if os.path.exists(temp_path) and item.file_size:
            existing = os.path.getsize(temp_path)
            aligned = (min(existing, item.file_size) // RESUME_ALIGN) * RESUME_ALIGN
            if aligned > 0 and aligned < item.file_size:
                resume_from = aligned

        if resume_from:
            try:
                logger.info("Resuming '%s' at %s.", item.file_name, human_size(resume_from))
                await self._download_resumed(item, temp_path, resume_from, progress_cb)
                return
            except (asyncio.CancelledError, errors.FloodError, errors.FileReferenceExpiredError):
                raise
            except Exception as e:
                # An earlier iteration of this app broke on chunked downloads;
                # if resume misbehaves for any reason, start over rather than fail.
                logger.warning("Resume failed for '%s' (%s); restarting from scratch.",
                               item.file_name, e)
                self._discard_temp(temp_path)
                item.downloaded_bytes = 0
                item.progress = 0.0
                progress_cb = self._make_progress_cb(item)

        await self.client.download_media(item.message, file=temp_path, progress_callback=progress_cb)

    async def _download_resumed(self, item: DownloadItem, temp_path: str,
                                resume_from: int, progress_cb):
        """Appends to an existing .part using a byte-offset chunk stream."""
        total = item.file_size or 0
        downloaded = resume_from
        item.downloaded_bytes = downloaded
        with open(temp_path, "r+b") as fh:
            fh.truncate(resume_from)
            fh.seek(resume_from)
            async for chunk in self.client.iter_download(
                item.message, offset=resume_from, request_size=RESUME_ALIGN
            ):
                if item.status == "Cancelled":
                    raise asyncio.CancelledError()
                fh.write(chunk)
                downloaded += len(chunk)
                progress_cb(downloaded, total or downloaded)
            fh.flush()
            os.fsync(fh.fileno())

        # Strict here on purpose: these bytes were appended by us, so a short
        # result is a real truncation. _transfer turns this into a clean restart.
        if total and downloaded != total:
            raise IOError(f"resume wrote {downloaded} of {total} bytes")

    # ------------------------------------------------------------------ actions

    def cancel_download(self, item_id: str):
        """Cancels an active or queued item."""
        with self.queue_lock:
            item = self.queue_index.get(item_id)
            if not item or item.status in _TERMINAL_STATUSES:
                return
            task = item.task
            item.status = "Cancelled"
        if task is not None:
            self._call_on_loop(task.cancel)
        logger.info("Cancelled '%s' by user.", item.file_name)
        self.emit_event("item_updated", item)
        self._schedule_downloads()

    def cancel_all(self, exclude_task=None):
        """Cancels everything still pending or in flight."""
        tasks = []
        with self.queue_lock:
            for item in self.download_queue:
                if item.status in ("Queued", "Downloading"):
                    if item.task is not None and item.task is not exclude_task:
                        tasks.append(item.task)
                    item.status = "Cancelled"
        with self.states_lock:
            for state in self.pagination_states.values():
                state.has_more = False
        for task in tasks:
            self._call_on_loop(task.cancel)
        logger.info("Cancelled all pending downloads (%d in flight).", len(tasks))
        self.emit_event("queue_updated", None)

    def retry_failed(self) -> int:
        """Requeues every failed or cancelled item."""
        count = 0
        with self.queue_lock:
            for item in self.download_queue:
                if item.status.startswith("Failed") or item.status == "Cancelled":
                    item.status = "Queued"
                    item.retries = 0
                    item.progress = 0.0
                    item.downloaded_bytes = 0
                    item.speed = 0.0
                    count += 1
        if count:
            logger.info("Requeued %d failed/cancelled item(s).", count)
            self.emit_event("queue_updated", None)
            self._schedule_downloads()
        return count

    def clear_completed(self) -> int:
        """Drops every terminal item from memory, freeing retained Message objects."""
        with self.queue_lock:
            keep = [it for it in self.download_queue if it.status not in _TERMINAL_STATUSES]
            removed = len(self.download_queue) - len(keep)
            self.download_queue = keep
            self.queue_index = {it.id: it for it in keep}
        logger.info("Cleared %d finished item(s) from the queue.", removed)
        self.emit_event("queue_updated", None)
        return removed

    def sweep_orphan_parts(self) -> Tuple[int, int]:
        """Counts leftover .part files from previous runs. Returns (count, bytes)."""
        root = self.config.get("output_dir", "")
        count = total = 0
        if not os.path.isdir(root):
            return 0, 0
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name.endswith(".part"):
                    try:
                        total += os.path.getsize(os.path.join(dirpath, name))
                        count += 1
                    except OSError:
                        pass
        return count, total

    def delete_orphan_parts(self) -> Tuple[int, int]:
        """Deletes leftover .part files. Returns (count, bytes) actually removed."""
        root = self.config.get("output_dir", "")
        count = total = 0
        if not os.path.isdir(root):
            return 0, 0
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if not name.endswith(".part"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                    os.remove(path)
                    count += 1
                    total += size
                except OSError as e:
                    logger.warning("Could not remove %s: %s", path, e)
        logger.info("Removed %d orphaned .part file(s), %s.", count, human_size(total))
        return count, total

    def shutdown(self, timeout: float = 5.0):
        """Cancel authentication and downloads before disconnecting and closing."""
        if self._shutting_down:
            return
        self._shutting_down = True

        async def drain():
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


# Number of reusable row widgets kept alive in the queue view. Rendering cost is
# bound to this, not to the queue length.
ROW_POOL_SIZE = 40

# Same idea for the chat picker. An account with a few hundred dialogs used to
# build one CTkCheckBox each inside a single Tk callback, freezing the window
# for seconds while it appeared to be stuck loading.
CHAT_POOL_SIZE = 40

# Kinds offered as filter toggles, in display order.
CHAT_KINDS = ("Group", "Channel", "DM", "Bot")


class App(ctk.CTk):
    def __init__(self, backend: TelegramBackend):
        super().__init__()
        self.title("Telegram Multi-Chat Stream Downloader")
        self.geometry("1150x760")
        ctk.set_appearance_mode("Dark")

        self.backend = backend

        # Virtualized chat picker state. Selection lives in a set of chat ids,
        # not in the widgets, so it survives row recycling and filtering.
        self.all_chats: List[ChatEntry] = []
        self.filtered_chats: List[ChatEntry] = []
        self.selected_chat_ids: Set[int] = set()
        self.chat_rows: List[Dict[str, Any]] = []
        self.chat_offset = 0
        self.kind_filter_boxes: Dict[str, ctk.CTkCheckBox] = {}
        self.show_archived_box: Optional[ctk.CTkCheckBox] = None

        self.ext_checkboxes: Dict[str, ctk.CTkCheckBox] = {}
        self.group_widgets: Dict[Optional[int], ctk.CTkButton] = {}
        self.active_group_filter: Optional[int] = None  # None means "ALL"

        # Virtualized queue view state.
        self.queue_rows: List[Dict[str, Any]] = []
        self.visible_rows: Dict[str, Dict[str, Any]] = {}
        self.queue_offset = 0
        self._queue_dirty = True
        self._sidebar_dirty = True
        self._flood_notice = ""

        self._build_main_layout()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_queue()
        self._tick_refresh()
        self.page_accounts.busy = self.backend.accounts is not None
        self.page_accounts.update_buttons()
        self.backend.run_coro(self.backend.init_client())
        self.after(1200, self._offer_orphan_cleanup)

    # ------------------------------------------------------------------ plumbing

    def _poll_queue(self):
        """Drains backend events on the Tk thread, coalescing progress updates."""
        max_events_per_tick = 200
        processed = 0
        progress_updates: Dict[str, DownloadItem] = {}
        other_events: List[Tuple[str, Any]] = []

        try:
            while processed < max_events_per_tick:
                try:
                    event_type, data = self.backend.event_queue.get_nowait()
                except queue.Empty:
                    break
                processed += 1
                if event_type == "item_progress" and isinstance(data, DownloadItem):
                    progress_updates[data.id] = data
                else:
                    other_events.append((event_type, data))

            for event_type, data in other_events:
                # Isolated so one bad event cannot discard the rest of the tick.
                try:
                    self._process_ui_update(event_type, data)
                except Exception as e:
                    logger.exception("Error handling UI event %r: %s", event_type, e)

            for item in progress_updates.values():
                self.update_single_item_progress(item)
        except Exception as e:
            logger.exception("Error in UI poll loop: %s", e)
        finally:
            self.after(50, self._poll_queue)

    def _tick_refresh(self):
        """Coalesced repaint. Keeps per-event work off the hot path."""
        try:
            if self._queue_dirty:
                self._queue_dirty = False
                self._render_queue_window()
            if self._sidebar_dirty:
                self._sidebar_dirty = False
                self.refresh_group_filter_sidebar()
            self._update_summary()
        except Exception as e:
            logger.exception("Error in refresh tick: %s", e)
        finally:
            self.after(400, self._tick_refresh)

    def _process_ui_update(self, event_type: str, data: Any):
        if event_type == "login_state":
            self.page_accounts.handle(data)
            self.set_status(data["message"])
            if data["stage"] != "authorized":
                self.select_page("accounts")
        elif event_type == "login_finished":
            self.page_accounts.finished()
        elif event_type == "account_profile":
            self.page_accounts.show_profile(data)
        elif event_type == "account_reset":
            self.page_accounts.reset_profile()
            self.all_chats.clear()
            self.filtered_chats.clear()
            self.selected_chat_ids.clear()
            self.active_group_filter = None
            self.chat_offset = self.queue_offset = 0
            self._flood_notice = ""
            self.on_filter_chats()
            self.populate_scanned_extensions()
            self._queue_dirty = self._sidebar_dirty = True
        elif event_type == "auth_status":
            if data:
                self.set_status("Connected. Loading your chats...")
                self.backend.run_coro(self.backend.fetch_dialogs())
            else:
                self.select_page("accounts")
        elif event_type == "connect_failed":
            self.set_status(f"Could not reach Telegram: {data}")
            self.page_accounts.status.configure(text="Connection failed. Check your network and reconnect here.")
            self.select_page("accounts")
        elif event_type == "dialogs_progress":
            if isinstance(data, str):
                self.set_status(data)
            else:
                self.set_status(f"Loading your chats... {data} found so far.")
        elif event_type == "dialogs_loaded":
            # Only the pool is touched here, so this stays O(CHAT_POOL_SIZE)
            # however many dialogs the account has.
            self.all_chats = list(data or [])
            self.selected_chat_ids.intersection_update({e.id for e in self.all_chats})
            self.on_filter_chats()
            groups = sum(1 for e in self.all_chats if e.kind in ("Group", "Channel"))
            self.set_status(f"Loaded {len(self.all_chats)} chats "
                            f"({groups} groups/channels).")
        elif event_type == "scan_progress":
            self.set_status(str(data))
        elif event_type == "extensions_scanned":
            self.populate_scanned_extensions(extra_exts=data)
            self.set_status(f"Scan complete: {len(data or [])} format(s) found.")
        elif event_type == "queue_updated":
            self._queue_dirty = True
            self._sidebar_dirty = True
        elif event_type == "item_updated":
            if isinstance(data, DownloadItem):
                self.update_single_item_view(data)
            self._sidebar_dirty = True
            self._queue_dirty = True
        elif event_type == "flood_wait":
            self._flood_notice = f"⏳ Rate limited by Telegram — paused for {data}s"
            self.set_status(self._flood_notice)
        elif event_type == "flood_cleared":
            self._flood_notice = ""
            self.set_status("Rate limit expired. Resuming downloads.")
        elif event_type == "error":
            self.set_status(f"⚠️ {data}")

    def set_status(self, text: str):
        self.status_label.configure(text=text[:160])

    # -------------------------------------------------------------------- layout

    def _build_main_layout(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar_frame = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(5, weight=1)

        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="Telegram Downloader",
                                       font=ctk.CTkFont(size=16, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=15, pady=(20, 10))

        self.btn_nav_groups = ctk.CTkButton(
            self.sidebar_frame, text="📁 Groups & Formats", anchor="w",
            command=lambda: self.select_page("groups")
        )
        self.btn_nav_groups.grid(row=1, column=0, padx=10, pady=5, sticky="ew")

        self.btn_nav_queue = ctk.CTkButton(
            self.sidebar_frame, text="⬇️ Download Queue", anchor="w",
            command=lambda: self.select_page("queue")
        )
        self.btn_nav_queue.grid(row=2, column=0, padx=10, pady=5, sticky="ew")

        self.btn_nav_settings = ctk.CTkButton(
            self.sidebar_frame, text="⚙️ Settings", anchor="w",
            command=lambda: self.select_page("settings")
        )
        self.btn_nav_settings.grid(row=3, column=0, padx=10, pady=5, sticky="ew")

        self.btn_nav_accounts = ctk.CTkButton(
            self.sidebar_frame, text="Accounts", anchor="w",
            command=lambda: self.select_page("accounts"))
        self.btn_nav_accounts.grid(row=4, column=0, padx=10, pady=5, sticky="ew")
        self.page_accounts = AccountPage(self, self)

        self.page_groups = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.page_queue = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.page_settings = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")

        self._build_page_groups()
        self._build_page_queue()
        self._build_page_settings()

        # Persistent status bar: backend failures used to be log-only, so an
        # outage looked identical to an idle window.
        self.status_label = ctk.CTkLabel(self, text="Starting up...", anchor="w",
                                         text_color="gray70")
        self.status_label.grid(row=1, column=0, columnspan=2, sticky="ew", padx=15, pady=(0, 8))

        self.select_page("groups")

    def _build_page_groups(self):
        self.page_groups.grid_columnconfigure(0, weight=1)
        self.page_groups.grid_columnconfigure(1, weight=1)
        self.page_groups.grid_rowconfigure(1, weight=1)

        left_box = ctk.CTkFrame(self.page_groups)
        left_box.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=5, pady=5)
        left_box.grid_rowconfigure(5, weight=1)
        left_box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(left_box, text="Step 1: Select Target Chats",
                     font=("Arial", 15, "bold")).grid(row=0, column=0, pady=(8, 4), padx=10, sticky="w")

        self.search_chat_var = ctk.StringVar()
        self.search_chat_var.trace_add("write", self.on_filter_chats)
        self.entry_chat_search = ctk.CTkEntry(left_box, placeholder_text="🔍 Search chats...",
                                              textvariable=self.search_chat_var)
        self.entry_chat_search.grid(row=1, column=0, padx=8, pady=(0, 5), sticky="ew")

        # Type filters. Every chat is loaded; these only narrow what is listed,
        # so a group can never be missing because of how it is classified.
        filter_row = ctk.CTkFrame(left_box, fg_color="transparent")
        filter_row.grid(row=2, column=0, padx=6, pady=(0, 2), sticky="ew")
        for kind in CHAT_KINDS:
            box = ctk.CTkCheckBox(filter_row, text=kind, width=70,
                                  command=self.on_filter_chats)
            if kind in ("Group", "Channel"):
                box.select()
            else:
                box.deselect()
            box.pack(side="left", padx=(2, 6))
            self.kind_filter_boxes[kind] = box

        self.show_archived_box = ctk.CTkCheckBox(left_box, text="Include archived",
                                                 command=self.on_filter_chats)
        self.show_archived_box.select()
        self.show_archived_box.grid(row=3, column=0, padx=10, pady=(0, 4), sticky="w")

        action_row = ctk.CTkFrame(left_box, fg_color="transparent")
        action_row.grid(row=4, column=0, padx=6, pady=(0, 4), sticky="ew")
        action_row.grid_columnconfigure(2, weight=1)
        ctk.CTkButton(action_row, text="Select all shown", width=120, height=26,
                      command=self.on_select_all_shown).grid(row=0, column=0, padx=2)
        ctk.CTkButton(action_row, text="Clear", width=60, height=26, fg_color="gray30",
                      command=self.on_clear_chat_selection).grid(row=0, column=1, padx=2)
        self.chat_counts_label = ctk.CTkLabel(action_row, text="No chats loaded yet.",
                                              anchor="e", text_color="gray70")
        self.chat_counts_label.grid(row=0, column=2, sticky="ew", padx=(6, 4))

        chat_body = ctk.CTkFrame(left_box, fg_color="transparent")
        chat_body.grid(row=5, column=0, sticky="nsew", padx=8, pady=5)
        chat_body.grid_columnconfigure(0, weight=1)
        chat_body.grid_rowconfigure(CHAT_POOL_SIZE, weight=1)

        for i in range(CHAT_POOL_SIZE):
            self.chat_rows.append(self._build_chat_row(chat_body, i))

        self.chat_scrollbar = ctk.CTkScrollbar(chat_body, command=self._on_chat_scroll)
        self.chat_scrollbar.grid(row=0, column=1, rowspan=CHAT_POOL_SIZE + 1,
                                 sticky="ns", padx=(4, 0))
        self.chat_scrollbar.set(0.0, 1.0)

        for target in (chat_body, left_box):
            target.bind("<MouseWheel>", self._on_chat_wheel)
            target.bind("<Button-4>", self._on_chat_wheel)
            target.bind("<Button-5>", self._on_chat_wheel)

        right_box = ctk.CTkFrame(self.page_groups)
        right_box.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=5, pady=5)
        right_box.grid_rowconfigure(1, weight=1)
        right_box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(right_box, text="Step 2: Discovered Formats",
                     font=("Arial", 15, "bold")).grid(row=0, column=0, pady=8, padx=10, sticky="w")
        self.ext_scroll = ctk.CTkScrollableFrame(right_box)
        self.ext_scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=5)

        self.lbl_ext_placeholder = ctk.CTkLabel(
            self.ext_scroll,
            text="⭐ ALL Formats selected by default.\n\n"
                 "Click '🔍 Scan Selected Chats' to discover specific file extensions.",
            justify="center",
            text_color="gray70"
        )
        self.lbl_ext_placeholder.pack(padx=20, pady=40)

        btn_frame = ctk.CTkFrame(self.page_groups)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=10)

        ctk.CTkButton(btn_frame, text="🔍 Scan Selected Chats",
                      command=self.on_scan_extensions).pack(side="left", padx=15, pady=8)
        ctk.CTkButton(btn_frame, text="🚀 Start Downloads", fg_color="green",
                      command=self.on_start_downloads).pack(side="right", padx=15, pady=8)

    def populate_scanned_extensions(self, extra_exts: Optional[List[str]] = None):
        """Populates Step 2 with the formats discovered by a scan."""
        for widget in self.ext_scroll.winfo_children():
            widget.destroy()
        self.ext_checkboxes.clear()

        cb_all = ctk.CTkCheckBox(self.ext_scroll, text="⭐ ALL Formats (Any File)",
                                 font=("Arial", 13, "bold"))
        cb_all.select()
        cb_all.pack(anchor="w", padx=5, pady=6)
        self.ext_checkboxes["*"] = cb_all

        scanned_list = sorted(set(extra_exts or []))
        if not scanned_list:
            ctk.CTkLabel(self.ext_scroll, text="No file extensions found in the scanned messages.",
                         text_color="gray60").pack(padx=10, pady=10)
            return

        for ext in scanned_list:
            cb = ctk.CTkCheckBox(self.ext_scroll, text=ext)
            cb.select()
            cb.pack(anchor="w", padx=5, pady=4)
            self.ext_checkboxes[ext] = cb

    def _build_page_queue(self):
        self.page_queue.grid_columnconfigure(0, weight=1)
        self.page_queue.grid_columnconfigure(1, weight=3)
        self.page_queue.grid_rowconfigure(0, weight=1)

        left_panel = ctk.CTkFrame(self.page_queue)
        left_panel.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        ctk.CTkLabel(left_panel, text="Chat Filters", font=("Arial", 14, "bold")).pack(pady=(8, 4))

        self.search_queue_group_var = ctk.StringVar()
        self.search_queue_group_var.trace_add("write", lambda *_: self._mark_sidebar_dirty())
        self.entry_queue_group_search = ctk.CTkEntry(left_panel, placeholder_text="🔍 Search filter...",
                                                     textvariable=self.search_queue_group_var)
        self.entry_queue_group_search.pack(fill="x", padx=10, pady=(0, 4))

        ctk.CTkButton(left_panel, text="🧹 Clear Finished", fg_color="gray30",
                      command=self.on_clear_completed).pack(fill="x", padx=10, pady=3)
        ctk.CTkButton(left_panel, text="🔁 Retry Failed", fg_color="gray30",
                      command=self.on_retry_failed).pack(fill="x", padx=10, pady=3)
        ctk.CTkButton(left_panel, text="🛑 Cancel All", fg_color="gray30",
                      command=self.on_cancel_all).pack(fill="x", padx=10, pady=3)
        ctk.CTkButton(left_panel, text="📂 Open Folder", fg_color="gray30",
                      command=self.on_open_output_dir).pack(fill="x", padx=10, pady=3)

        self.group_filter_scroll = ctk.CTkScrollableFrame(left_panel)
        self.group_filter_scroll.pack(fill="both", expand=True, padx=5, pady=5)

        right_panel = ctk.CTkFrame(self.page_queue)
        right_panel.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)
        right_panel.grid_columnconfigure(0, weight=1)
        right_panel.grid_rowconfigure(2, weight=1)

        self.queue_header = ctk.CTkLabel(right_panel, text="Download Queue: ALL Groups",
                                         font=("Arial", 14, "bold"))
        self.queue_header.grid(row=0, column=0, sticky="w", padx=10, pady=(8, 0))

        self.queue_summary = ctk.CTkLabel(right_panel, text="Queue is empty.",
                                          anchor="w", text_color="gray70")
        self.queue_summary.grid(row=1, column=0, sticky="ew", padx=10, pady=(2, 6))

        # A fixed pool of reusable rows replaces the old one-card-per-item
        # CTkScrollableFrame. Building 5 CTk widgets per file made every queued
        # batch a synchronous relayout of the entire list: ~54 ms at 50 rows but
        # ~313 ms at 1,200 and seconds at this project's ~8,900 files, which is
        # what made multi-group downloads unusable.
        body = ctk.CTkFrame(right_panel, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=5, pady=(0, 5))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(ROW_POOL_SIZE, weight=1)

        for i in range(ROW_POOL_SIZE):
            row = self._build_queue_row(body, i)
            self.queue_rows.append(row)

        self.queue_scrollbar = ctk.CTkScrollbar(body, command=self._on_queue_scroll)
        self.queue_scrollbar.grid(row=0, column=1, rowspan=ROW_POOL_SIZE + 1,
                                  sticky="ns", padx=(4, 0))
        self.queue_scrollbar.set(0.0, 1.0)

        for target in (body, right_panel):
            target.bind("<MouseWheel>", self._on_queue_wheel)
            target.bind("<Button-4>", self._on_queue_wheel)
            target.bind("<Button-5>", self._on_queue_wheel)

    def _build_chat_row(self, parent, index: int) -> Dict[str, Any]:
        """Creates one reusable chat row. Called exactly CHAT_POOL_SIZE times."""
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=index, column=0, sticky="ew", padx=2)
        frame.grid_remove()
        frame.grid_columnconfigure(0, weight=1)

        row: Dict[str, Any] = {"frame": frame, "chat_id": None, "shown": False}
        cb = ctk.CTkCheckBox(frame, text="", command=lambda r=row: self._on_chat_toggle(r))
        cb.grid(row=0, column=0, sticky="w", padx=(4, 4), pady=3)
        row["cb"] = cb

        kind = ctk.CTkLabel(frame, text="", width=90, anchor="e", text_color="gray60")
        kind.grid(row=0, column=1, sticky="e", padx=(4, 6))
        row["kind"] = kind

        for widget in (frame, kind):
            widget.bind("<MouseWheel>", self._on_chat_wheel)
            widget.bind("<Button-4>", self._on_chat_wheel)
            widget.bind("<Button-5>", self._on_chat_wheel)
        return row

    def _on_chat_toggle(self, row: Dict[str, Any]):
        chat_id = row.get("chat_id")
        if chat_id is None:
            return
        if row["cb"].get() == 1:
            self.selected_chat_ids.add(chat_id)
        else:
            self.selected_chat_ids.discard(chat_id)
        self._update_chat_counts()

    def _on_chat_scroll(self, *args):
        if not args:
            return
        total = len(self.filtered_chats)
        if args[0] == "moveto":
            self.chat_offset = int(round(float(args[1]) * total))
        elif args[0] == "scroll":
            step = CHAT_POOL_SIZE if len(args) > 2 and args[2] == "pages" else 1
            self.chat_offset += int(args[1]) * step
        self._render_chat_window()

    def _on_chat_wheel(self, event):
        if getattr(event, "num", None) == 4:
            delta = 3
        elif getattr(event, "num", None) == 5:
            delta = -3
        else:
            raw = getattr(event, "delta", 0)
            delta = (raw if abs(raw) < 30 else raw // 120) * 3
        if not delta:
            return
        self.chat_offset -= delta
        self._render_chat_window()

    def _render_chat_window(self):
        """Repaints the visible chat rows by rebinding the pool. Cost is O(pool)."""
        total = len(self.filtered_chats)
        max_offset = max(0, total - CHAT_POOL_SIZE)
        self.chat_offset = max(0, min(self.chat_offset, max_offset))

        for i, row in enumerate(self.chat_rows):
            idx = self.chat_offset + i
            if idx < total:
                entry = self.filtered_chats[idx]
                row["chat_id"] = entry.id
                label = entry.name if len(entry.name) <= 42 else entry.name[:39] + "..."
                if row["cb"].cget("text") != label:
                    row["cb"].configure(text=label)
                # select()/deselect() never fire the row command, so rebinding a
                # recycled row cannot corrupt the selection set.
                if entry.id in self.selected_chat_ids:
                    row["cb"].select()
                else:
                    row["cb"].deselect()
                tag = f"{entry.kind} 📦" if entry.archived else entry.kind
                if row["kind"].cget("text") != tag:
                    row["kind"].configure(text=tag)
                if not row["shown"]:
                    row["frame"].grid()
                    row["shown"] = True
            elif row["shown"]:
                row["frame"].grid_remove()
                row["shown"] = False
                row["chat_id"] = None

        if total <= CHAT_POOL_SIZE:
            self.chat_scrollbar.set(0.0, 1.0)
        else:
            self.chat_scrollbar.set(self.chat_offset / total,
                                    min(1.0, (self.chat_offset + CHAT_POOL_SIZE) / total))

    def _update_chat_counts(self):
        """Shows what was loaded, so 'are all my groups here' is answerable."""
        if not self.all_chats:
            self.chat_counts_label.configure(text="No chats loaded yet.")
            return
        counts: Dict[str, int] = {}
        for e in self.all_chats:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        parts = [f"{k} {counts[k]}" for k in CHAT_KINDS if counts.get(k)]
        archived = sum(1 for e in self.all_chats if e.archived)
        if archived:
            parts.append(f"archived {archived}")
        self.chat_counts_label.configure(
            text=f"{len(self.all_chats)} loaded ({', '.join(parts)}) · "
                 f"{len(self.filtered_chats)} shown · {len(self.selected_chat_ids)} selected")

    def _build_queue_row(self, parent, index: int) -> Dict[str, Any]:
        """Creates one reusable row. Called exactly ROW_POOL_SIZE times, at startup."""
        frame = ctk.CTkFrame(parent)
        frame.grid(row=index, column=0, sticky="ew", padx=5, pady=2)
        frame.grid_remove()
        frame.grid_columnconfigure(1, weight=1)

        lbl_name = ctk.CTkLabel(frame, text="", width=240, anchor="w")
        lbl_name.grid(row=0, column=0, padx=(8, 4), pady=4)

        pbar = ctk.CTkProgressBar(frame, width=200)
        pbar.set(0.0)
        pbar.grid(row=0, column=1, padx=8, pady=4, sticky="ew")

        lbl_status = ctk.CTkLabel(frame, text="", width=170, anchor="w")
        lbl_status.grid(row=0, column=2, padx=4, pady=4)

        row: Dict[str, Any] = {
            "frame": frame, "name": lbl_name, "pbar": pbar,
            "status": lbl_status, "item_id": None, "shown": False,
        }

        btn_cancel = ctk.CTkButton(frame, text="Cancel", width=70, fg_color="red",
                                   command=lambda r=row: self._on_row_cancel(r))
        btn_cancel.grid(row=0, column=3, padx=(4, 8), pady=4)
        row["cancel"] = btn_cancel

        for widget in (frame, lbl_name, lbl_status):
            widget.bind("<MouseWheel>", self._on_queue_wheel)
            widget.bind("<Button-4>", self._on_queue_wheel)
            widget.bind("<Button-5>", self._on_queue_wheel)
        return row

    def _build_page_settings(self):
        self.page_settings.grid_columnconfigure(0, weight=1)

        box = ctk.CTkFrame(self.page_settings)
        box.pack(fill="both", expand=True, padx=10, pady=10)

        ctk.CTkLabel(box, text="⚙️ Application Settings",
                     font=("Arial", 16, "bold")).pack(anchor="w", padx=15, pady=(15, 10))

        f_dir = ctk.CTkFrame(box, fg_color="transparent")
        f_dir.pack(fill="x", padx=15, pady=8)
        ctk.CTkLabel(f_dir, text="Download Directory:", width=180, anchor="w").pack(side="left")
        self.entry_output_dir = ctk.CTkEntry(f_dir)
        self.entry_output_dir.pack(side="left", fill="x", expand=True, padx=5)
        self.entry_output_dir.insert(0, self.backend.config.get("output_dir", ""))
        ctk.CTkButton(f_dir, text="Browse...", width=80,
                      command=self.on_browse_output_dir).pack(side="right", padx=5)

        f_par = ctk.CTkFrame(box, fg_color="transparent")
        f_par.pack(fill="x", padx=15, pady=8)
        self.lbl_parallel = ctk.CTkLabel(
            f_par,
            text=f"Max Parallel File Downloads: {self.backend.config.get('max_parallel_files', 3)}",
            anchor="w"
        )
        self.lbl_parallel.pack(anchor="w")
        self.slider_parallel = ctk.CTkSlider(f_par, from_=1, to=10, number_of_steps=9,
                                             command=self.on_settings_slider_change)
        self.slider_parallel.set(self.backend.config.get("max_parallel_files", 3))
        self.slider_parallel.pack(fill="x", pady=4)

        f_dep = ctk.CTkFrame(box, fg_color="transparent")
        f_dep.pack(fill="x", padx=15, pady=8)
        self.lbl_depth = ctk.CTkLabel(f_dep, text="", anchor="w")
        self.lbl_depth.pack(anchor="w")
        # 0 means "unlimited"; the old 50..2000 range could never reach it.
        self.slider_depth = ctk.CTkSlider(f_dep, from_=0, to=2000, number_of_steps=40,
                                          command=self.on_settings_slider_change)
        self.slider_depth.set(self.backend.config.get("scan_depth", 500))
        self.slider_depth.pack(fill="x", pady=4)

        warn_frame = ctk.CTkFrame(box, fg_color=("gray85", "gray20"))
        warn_frame.pack(fill="x", padx=15, pady=15)
        ctk.CTkLabel(
            warn_frame,
            text="⚠️ High concurrency (4+ parallel downloads) may cause network lag or "
                 "temporary Telegram rate limits (FloodWait). Rate limits now pause and "
                 "auto-resume instead of failing the queue.",
            wraplength=600,
            justify="left"
        ).pack(padx=10, pady=10)

        f_parts = ctk.CTkFrame(box, fg_color="transparent")
        f_parts.pack(fill="x", padx=15, pady=8)
        self.lbl_parts = ctk.CTkLabel(f_parts, text="", anchor="w")
        self.lbl_parts.pack(side="left")
        ctk.CTkButton(f_parts, text="🧽 Clean up .part files", width=180,
                      command=self.on_clean_parts).pack(side="right")

        ctk.CTkButton(box, text="💾 Save Settings", fg_color="green",
                      command=self.on_save_settings).pack(anchor="e", padx=15, pady=15)

        self.on_settings_slider_change(None)

    def select_page(self, page_name: str):
        """Switches the active page."""
        self.page_groups.grid_forget()
        self.page_queue.grid_forget()
        self.page_settings.grid_forget()
        self.page_accounts.grid_forget()
        self.btn_nav_accounts.configure(fg_color=("gray75", "gray25") if page_name == "accounts" else "transparent")

        self.btn_nav_groups.configure(fg_color=("gray75", "gray25") if page_name == "groups" else "transparent")
        self.btn_nav_queue.configure(fg_color=("gray75", "gray25") if page_name == "queue" else "transparent")
        self.btn_nav_settings.configure(fg_color=("gray75", "gray25") if page_name == "settings" else "transparent")

        if page_name == "accounts":
            self.page_accounts.grid(row=0, column=1, sticky="nsew", padx=15, pady=15)
        elif page_name == "groups":
            self.page_groups.grid(row=0, column=1, sticky="nsew", padx=15, pady=15)
        elif page_name == "queue":
            self.page_queue.grid(row=0, column=1, sticky="nsew", padx=15, pady=15)
            self._sidebar_dirty = True
            self._queue_dirty = True
        elif page_name == "settings":
            self.page_settings.grid(row=0, column=1, sticky="nsew", padx=15, pady=15)
            self._refresh_parts_label()

    # ------------------------------------------------------- virtualized queue

    def _filtered_items(self) -> List[DownloadItem]:
        return self.backend.snapshot_queue(self.active_group_filter)

    def _mark_sidebar_dirty(self):
        self._sidebar_dirty = True

    def _on_queue_scroll(self, *args):
        """Tk scrollbar protocol handler driving the virtual window offset."""
        items_len = len(self._filtered_items())
        max_offset = max(0, items_len - ROW_POOL_SIZE)
        if not args:
            return
        if args[0] == "moveto":
            self.queue_offset = int(round(float(args[1]) * items_len))
        elif args[0] == "scroll":
            amount = int(args[1])
            step = ROW_POOL_SIZE if len(args) > 2 and args[2] == "pages" else 1
            self.queue_offset += amount * step
        self.queue_offset = max(0, min(self.queue_offset, max_offset))
        self._render_queue_window()

    def _on_queue_wheel(self, event):
        if getattr(event, "num", None) == 4:
            delta = 3
        elif getattr(event, "num", None) == 5:
            delta = -3
        else:
            raw = getattr(event, "delta", 0)
            delta = raw if abs(raw) < 30 else raw // 120
            delta *= 3
        if not delta:
            return
        items_len = len(self._filtered_items())
        max_offset = max(0, items_len - ROW_POOL_SIZE)
        self.queue_offset = max(0, min(self.queue_offset - delta, max_offset))
        self._render_queue_window()

    def _render_queue_window(self):
        """Repaints the visible window by rebinding pooled rows. Cost is O(pool)."""
        items = self._filtered_items()
        total = len(items)
        max_offset = max(0, total - ROW_POOL_SIZE)
        self.queue_offset = max(0, min(self.queue_offset, max_offset))

        self.visible_rows.clear()
        for i, row in enumerate(self.queue_rows):
            idx = self.queue_offset + i
            if idx < total:
                item = items[idx]
                self._bind_row(row, item)
                self.visible_rows[item.id] = row
                if not row["shown"]:
                    row["frame"].grid()
                    row["shown"] = True
            elif row["shown"]:
                row["frame"].grid_remove()
                row["shown"] = False
                row["item_id"] = None

        if total <= ROW_POOL_SIZE:
            self.queue_scrollbar.set(0.0, 1.0)
        else:
            first = self.queue_offset / total
            last = min(1.0, (self.queue_offset + ROW_POOL_SIZE) / total)
            self.queue_scrollbar.set(first, last)

    def _bind_row(self, row: Dict[str, Any], item: DownloadItem):
        row["item_id"] = item.id
        name = item.file_name if len(item.file_name) <= 38 else item.file_name[:35] + "..."
        if row["name"].cget("text") != name:
            row["name"].configure(text=name)
        row["pbar"].set(item.progress)
        row["status"].configure(text=self._format_item_status(item))
        row["cancel"].configure(
            state="disabled" if item.status in _TERMINAL_STATUSES else "normal"
        )

    def _on_row_cancel(self, row: Dict[str, Any]):
        item_id = row.get("item_id")
        if item_id:
            self.backend.cancel_download(item_id)

    def _format_item_status(self, item: DownloadItem) -> str:
        if item.status == "Downloading":
            return f"{int(item.progress * 100)}% ({item.speed / (1024 * 1024):.2f} MB/s)"
        if item.status == "Queued" and item.retries:
            return f"Queued (retry {item.retries})"
        return item.status

    def update_single_item_progress(self, item: DownloadItem):
        """Updates one visible row in place. Off-screen items cost nothing."""
        row = self.visible_rows.get(item.id)
        if row is not None and row.get("item_id") == item.id:
            row["pbar"].set(item.progress)
            row["status"].configure(text=self._format_item_status(item))

    def update_single_item_view(self, item: DownloadItem):
        row = self.visible_rows.get(item.id)
        if row is not None and row.get("item_id") == item.id:
            self._bind_row(row, item)

    def _update_summary(self):
        stats = self.backend.queue_stats()
        if not stats.total:
            self.queue_summary.configure(text="Queue is empty.")
            return
        parts = [
            f"{stats.total} files",
            f"{stats.done} done",
            f"{stats.active} active",
            f"{stats.queued} queued",
        ]
        if stats.failed:
            parts.append(f"{stats.failed} failed/cancelled")
        if stats.total_bytes:
            parts.append(human_size(stats.total_bytes))
        if stats.speed:
            parts.append(f"{stats.speed / (1024 * 1024):.1f} MB/s")
        if self._flood_notice:
            parts.append(self._flood_notice)
        self.queue_summary.configure(text="  ·  ".join(parts))

    def refresh_group_filter_sidebar(self):
        """Rebuilds the chat filter buttons from a snapshot taken under lock."""
        filter_str = self.search_queue_group_var.get().strip().lower() \
            if hasattr(self, "search_queue_group_var") else ""

        if None not in self.group_widgets:
            btn_all = ctk.CTkButton(self.group_filter_scroll, text="ALL Selected Groups",
                                    command=lambda: self.set_group_filter(None))
            btn_all.pack(fill="x", padx=5, pady=3)
            self.group_widgets[None] = btn_all

        for snap in self.backend.snapshot_states():
            text_str = f"{snap.chat_name} ({snap.total_completed}/{snap.total_discovered})"
            should_show = not filter_str or filter_str in snap.chat_name.lower()
            btn = self.group_widgets.get(snap.chat_id)

            if btn is None:
                btn = ctk.CTkButton(self.group_filter_scroll, text=text_str,
                                    command=lambda cid=snap.chat_id: self.set_group_filter(cid))
                self.group_widgets[snap.chat_id] = btn
                if should_show:
                    btn.pack(fill="x", padx=5, pady=3)
                continue

            if btn.cget("text") != text_str:
                btn.configure(text=text_str)
            if should_show:
                btn.pack(fill="x", padx=5, pady=3)
            else:
                btn.pack_forget()

    def set_group_filter(self, chat_id: Optional[int]):
        self.active_group_filter = chat_id
        self.queue_offset = 0
        if chat_id is None:
            self.queue_header.configure(text="Download Queue: ALL Groups")
        else:
            name = next((s.chat_name for s in self.backend.snapshot_states()
                         if s.chat_id == chat_id), f"Chat {chat_id}")
            self.queue_header.configure(text=f"Download Queue: {name}")
        self._render_queue_window()

    # ------------------------------------------------------------------ handlers

    def on_filter_chats(self, *_args):
        query = self.search_chat_var.get().strip().lower()
        kinds = {k for k, box in self.kind_filter_boxes.items() if box.get() == 1}
        show_archived = not self.show_archived_box or self.show_archived_box.get() == 1

        def keep(e: ChatEntry) -> bool:
            if e.archived and not show_archived:
                return False
            # A selected chat stays visible even if the filters would hide it,
            # so a narrowed filter can never silently drop it from the download.
            if kinds and e.kind not in kinds and e.id not in self.selected_chat_ids:
                return False
            return not query or query in e.name.lower()

        self.filtered_chats = [e for e in self.all_chats if keep(e)]
        self.chat_offset = 0
        self._render_chat_window()
        self._update_chat_counts()

    def on_select_all_shown(self):
        self.selected_chat_ids.update(e.id for e in self.filtered_chats)
        self._render_chat_window()
        self._update_chat_counts()
        self.set_status(f"Selected {len(self.selected_chat_ids)} chat(s).")

    def on_clear_chat_selection(self):
        self.selected_chat_ids.clear()
        self._render_chat_window()
        self._update_chat_counts()

    def _selected_dialogs(self) -> List[ChatEntry]:
        return [e for e in self.all_chats if e.id in self.selected_chat_ids]

    def on_scan_extensions(self):
        if self.page_accounts.busy or (self.backend.accounts and not self.backend.authorized):
            self.set_status("Finish connecting your account first.")
            return
        selected = self._selected_dialogs()
        if not selected:
            messagebox.showwarning("Selection Required", "Please select at least one chat in Step 1.")
            return
        logger.info("Scanning extensions for %d chats.", len(selected))
        self.set_status(f"Scanning {len(selected)} chat(s)...")
        self.backend.run_coro(self.backend.scan_extensions(selected))

    def on_start_downloads(self):
        if self.page_accounts.busy or (self.backend.accounts and not self.backend.authorized):
            self.set_status("Finish connecting your account first.")
            return
        selected_dialogs = self._selected_dialogs()
        if not selected_dialogs:
            messagebox.showwarning("Selection Required", "Please select at least one chat in Step 1.")
            return

        err = check_output_dir(self.backend.config.get("output_dir", ""))
        if err:
            messagebox.showerror("Download folder unusable", err)
            self.set_status(f"⚠️ {err}")
            return

        all_formats = False
        selected_exts: Set[str] = set()
        for ext, cb in self.ext_checkboxes.items():
            if cb.get() == 1:
                if ext == "*":
                    all_formats = True
                else:
                    selected_exts.add(ext)
        if not all_formats and not selected_exts:
            all_formats = True

        logger.info("Start Downloads: %d chats, exts=%s, all_formats=%s",
                    len(selected_dialogs), selected_exts, all_formats)
        self.backend.run_coro(
            self.backend.enqueue_selected_chats(selected_dialogs, selected_exts,
                                                all_formats=all_formats))
        self.select_page("queue")

    def on_clear_completed(self):
        removed = self.backend.clear_completed()
        self.queue_offset = 0
        self.set_status(f"Cleared {removed} finished item(s).")

    def on_retry_failed(self):
        if self.page_accounts.busy or (self.backend.accounts and not self.backend.authorized):
            self.set_status("Finish connecting your account first.")
            return
        count = self.backend.retry_failed()
        self.set_status(f"Requeued {count} item(s)." if count else "Nothing to retry.")

    def on_cancel_all(self):
        if messagebox.askyesno("Cancel all", "Cancel every queued and in-flight download?"):
            self.backend.cancel_all()
            self.set_status("Cancelled all pending downloads.")

    def on_open_output_dir(self):
        path = self.backend.config.get("output_dir", "")
        if not os.path.isdir(path):
            messagebox.showwarning("Not found", f"'{path}' does not exist yet.")
            return
        try:
            # subprocess.run with an argv list, never a shell string: output_dir is
            # user-supplied and a path containing quotes would otherwise be
            # interpreted as shell syntax.
            if sys.platform == "darwin":
                subprocess.run(["open", path], check=False)
            elif os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", path], check=False)
        except Exception as e:
            messagebox.showerror("Could not open folder", str(e))

    def on_browse_output_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.entry_output_dir.get() or BASE_DIR)
        if chosen:
            self.entry_output_dir.delete(0, "end")
            self.entry_output_dir.insert(0, chosen)

    def on_settings_slider_change(self, _val):
        par = int(self.slider_parallel.get())
        dep = int(self.slider_depth.get())
        self.lbl_parallel.configure(text=f"Max Parallel File Downloads: {par}")
        self.lbl_depth.configure(
            text=f"Chat Extension Scan Depth: {'Unlimited' if dep == 0 else f'{dep} Messages'}"
        )

    def on_save_settings(self):
        target = self.entry_output_dir.get().strip()
        err = check_output_dir(os.path.abspath(os.path.expanduser(target))) if target else "Choose a folder."
        if err:
            messagebox.showerror("Download folder unusable", err)
            return
        self.backend.update_config({
            "output_dir": target,
            "max_parallel_files": int(self.slider_parallel.get()),
            "scan_depth": int(self.slider_depth.get()),
        })
        self.entry_output_dir.delete(0, "end")
        self.entry_output_dir.insert(0, self.backend.config["output_dir"])
        self.set_status("Settings saved.")
        self._refresh_parts_label()

    def _refresh_parts_label(self):
        count, total = self.backend.sweep_orphan_parts()
        if count:
            self.lbl_parts.configure(
                text=f"{count} leftover .part file(s) from interrupted runs ({human_size(total)}).")
        else:
            self.lbl_parts.configure(text="No leftover .part files.")

    def on_clean_parts(self):
        count, total = self.backend.sweep_orphan_parts()
        if not count:
            messagebox.showinfo("Nothing to clean", "No leftover .part files were found.")
            return
        if messagebox.askyesno(
            "Delete partial downloads",
            f"Delete {count} incomplete .part file(s), freeing {human_size(total)}?\n\n"
            "These are fragments of interrupted downloads. The files will be "
            "downloaded again from the start when re-queued."
        ):
            removed, freed = self.backend.delete_orphan_parts()
            self.set_status(f"Removed {removed} .part file(s), freed {human_size(freed)}.")
            self._refresh_parts_label()

    def _offer_orphan_cleanup(self):
        count, total = self.backend.sweep_orphan_parts()
        if count:
            self.set_status(
                f"{count} leftover .part file(s) ({human_size(total)}) from interrupted runs "
                "— see Settings to clean up."
            )

    def on_close(self):
        """Cancels in-flight work and disconnects before the window goes away."""
        try:
            self.backend.shutdown()
        except Exception as e:
            logger.warning("Error during shutdown: %s", e)
        finally:
            self.destroy()


def load_credentials() -> Tuple[int, str]:
    """Reads Telegram API credentials. Raises ValueError with a user-facing message."""
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    api_id = os.getenv("TG_API_ID")
    api_hash = os.getenv("TG_API_HASH")
    if not api_id or not api_hash:
        raise ValueError(
            "Missing TG_API_ID or TG_API_HASH.\n\n"
            f"Create a .env file next to the app ({BASE_DIR}) containing:\n"
            "  TG_API_ID=your_id\n  TG_API_HASH=your_hash"
        )
    return validate_credentials(api_id, api_hash)


def load_account_store():
    """Load saved profiles, importing the legacy environment only on first use."""
    try:
        api_id, api_hash = load_credentials()
    except ValueError:
        api_id, api_hash = 0, ""
    session_name = os.getenv("TG_SESSION_NAME", "downloader_session")
    session_path = session_name if os.path.isabs(session_name) else os.path.join(BASE_DIR, session_name)
    legacy = (api_id, api_hash, session_path) if api_id else None
    return AccountStore(BASE_DIR, legacy=legacy)


def main():
    """Entry point for both `python interactive_downloader.py` and the tg-download script."""
    configure_logging()
    try:
        accounts = load_account_store()
    except ValueError as exc:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Cannot read saved accounts", str(exc))
        root.destroy()
        return 1
    profile = accounts.data["profiles"].get(accounts.data["active"])
    if profile:
        api_id, api_hash, session_path = profile["api_id"], profile["api_hash"], profile["session"]
    else:
        api_id, api_hash, session_path = 0, "", os.path.join(BASE_DIR, "downloader_session")
    backend = TelegramBackend(api_id, api_hash, session_path, accounts=accounts)
    app = App(backend)
    try:
        app.mainloop()
    finally:
        backend.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
