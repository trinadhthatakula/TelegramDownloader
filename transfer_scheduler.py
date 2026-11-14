"""Bounded, fair transfer jobs. All mutations run on the backend event loop."""
import asyncio
import logging
import math
import threading
import time
from collections import defaultdict, deque

from telethon import errors

logger = logging.getLogger(__name__)
DISCOVERY_CONCURRENCY = 3
GLOBAL_PENDING_LIMIT = 500
PER_CHAT_PENDING_LIMIT = 50
REFILL_THRESHOLD = 15
SCAN_WINDOW = 200
FETCH_BACKOFF = (2, 5, 15, 45, 120)


class TransferSchedulerMixin:
    async def session_invalid(self):
        """Coalesce concurrent revoked-session responses on the backend loop."""
        if self._account_resetting or self._shutting_down:
            return
        await super().session_invalid()

    def _ensure_scheduler(self):
        if getattr(self, "_scheduler_ready", False):
            return
        self._scheduler_ready = True
        self._ready_by_chat = defaultdict(deque)
        self._queued_ids = set()
        self._queued_counts = defaultdict(int)
        self._deferred_by_chat = defaultdict(deque)
        self._deferred_ids = set()
        self._deferred_counts = defaultdict(int)
        self._last_admitted_chat = None
        self._active_counts = defaultdict(int)
        self._active_workers = {}
        self._discovery_tasks = {}
        self._discovery_reserved = {}
        self._discovery_waiting = deque()
        self._discovery_waiting_ids = set()
        self._fetch_retry_handles = {}
        self._item_retry_handles = {}
        self._chat_control_locks = {}
        self._flood_handle = None
        self._scheduler_stopping = False
        self._scheduler_reset_complete = None
        for item in self.download_queue:
            self._put_ready(item)

    def _track_scheduler_task(self, coro):
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task

    def _put_ready(self, item):
        if (item.status != "Queued" or item.id in self._queued_ids
                or item.id in self._deferred_ids or item.id in self._active_workers
                or item.id in self._item_retry_handles):
            return
        if (self._global_pending() + sum(self._discovery_reserved.values()) >= GLOBAL_PENDING_LIMIT
                or self._admitted_for_chat(item.chat_id) >= PER_CHAT_PENDING_LIMIT):
            self._deferred_by_chat[item.chat_id].append(item.id)
            self._deferred_ids.add(item.id)
            self._deferred_counts[item.chat_id] += 1
            return
        self._ready_by_chat[item.chat_id].append(item.id)
        self._queued_ids.add(item.id)
        self._queued_counts[item.chat_id] += 1

    def _remove_ready(self, item):
        if item.id in self._deferred_ids:
            self._deferred_ids.remove(item.id)
            self._deferred_counts[item.chat_id] -= 1
        if item.id in self._queued_ids:
            self._queued_ids.remove(item.id)
            self._queued_counts[item.chat_id] -= 1

    def _admitted_for_chat(self, chat_id):
        return (self._queued_counts[chat_id] + self._active_counts[chat_id]
                + sum(1 for cid, _handle in self._item_retry_handles.values() if cid == chat_id))

    def _pending_for_chat(self, chat_id):
        return self._admitted_for_chat(chat_id) + self._deferred_counts[chat_id]

    def _global_pending(self):
        """Admitted transfers; restored history may wait in the deferred ID queue."""
        return len(self._queued_ids) + len(self._active_workers) + len(self._item_retry_handles)

    def _admit_deferred(self):
        candidates = [cid for cid, count in self._deferred_counts.items()
                      if count and self._job_enabled(self.pagination_states.get(cid))]
        while candidates and self._global_pending() + sum(self._discovery_reserved.values()) < GLOBAL_PENDING_LIMIT:
            if self._last_admitted_chat in candidates:
                index = (candidates.index(self._last_admitted_chat) + 1) % len(candidates)
                candidates = candidates[index:] + candidates[:index]
            chat_id = candidates[0]
            if self._admitted_for_chat(chat_id) >= PER_CHAT_PENDING_LIMIT:
                candidates.pop(0)
                continue
            bucket = self._deferred_by_chat[chat_id]
            item = None
            while bucket:
                item_id = bucket.popleft()
                if item_id not in self._deferred_ids:
                    continue
                self._deferred_ids.remove(item_id)
                self._deferred_counts[chat_id] -= 1
                candidate = self.queue_index.get(item_id)
                if candidate is not None and candidate.status == "Queued":
                    item = candidate
                    break
            if item is not None:
                self._put_ready(item)
                self._last_admitted_chat = chat_id
            if not self._deferred_counts[chat_id]:
                candidates.remove(chat_id)

    def _job_enabled(self, state):
        return state is not None and not state.paused and not state.cancelled

    def _chat_items(self, chat_id):
        """Use the UI's existing per-chat index for occasional job controls."""
        window = getattr(self, "snapshot_queue_window", None)
        if window is not None:
            return window(chat_id, 0, len(self.download_queue))[1]
        return [item for item in self.download_queue if item.chat_id == chat_id]

    async def enqueue_selected_chats(self, selected_dialogs, chosen_exts, all_formats=False):
        self._ensure_scheduler()
        if self.accounts and not self.authorized:
            return
        formats = frozenset(str(ext).lower() for ext in chosen_exts if ext != "*")
        use_all = all_formats or "*" in chosen_exts or not formats
        if use_all:
            formats = frozenset()
        # Retained for older integrations; discovery always reads its job's copy.
        self.chosen_exts = set(formats)
        self.all_formats_mode = use_all
        for dialog in selected_dialogs:
            state = self.pagination_states.get(dialog.id)
            if state is not None:
                if (state.chosen_exts, state.all_formats) != (formats, use_all):
                    if state.cancelled or (not state.has_more and not self._pending_for_chat(dialog.id)):
                        await self.control_chat(dialog.id, "cancel")
                        with self.states_lock:
                            state = self._make_chat_job(dialog, formats, use_all)
                            self.pagination_states[dialog.id] = state
                        # The old job's cancelled rows still own their message IDs.
                        # Restore the requested subset instead of letting dedupe
                        # silently skip every previously discovered matching file.
                        for item in self._chat_items(dialog.id):
                            if (item.status == "Cancelled" or item.status.startswith("Failed")) and (
                                    use_all or item.ext in formats):
                                item.status = "Queued"
                                item.output_dir = state.output_dir
                                item._pause_requested = False
                                item.retries = 0
                                item.speed = item.progress = 0.0
                                item.downloaded_bytes = 0
                                item.last_error = ""
                                self._put_ready(item)
                                self.emit_event("item_updated", item)
                                state.total_discovered += 1
                        self._request_discovery(dialog.id)
                    else:
                        self.report_error(
                            f"'{state.chat_name}' already has a job with different formats. "
                            "Cancel that job before starting a different selection."
                        )
                    continue
                # Repeated Start clicks never rewind or overlap a live iterator.
                if state.paused or state.cancelled:
                    await self.control_chat(dialog.id, "resume")
                elif not state.has_more and not self._pending_for_chat(dialog.id) and not state.last_error:
                    previous = state
                    state = self._make_chat_job(dialog, formats, use_all)
                    state.stop_msg_id = previous.newest_msg_id
                    state.newest_msg_id = previous.newest_msg_id
                    state.total_discovered = previous.total_discovered
                    state.total_completed = previous.total_completed
                    with self.states_lock:
                        self.pagination_states[dialog.id] = state
                self._request_discovery(dialog.id)
                continue
            with self.states_lock:
                state = self._make_chat_job(dialog, formats, use_all)
                self.pagination_states[dialog.id] = state
            self._request_discovery(dialog.id)
        self.emit_event("queue_updated", None)
        self._pump_discovery()
        self._schedule_downloads()

    def _request_discovery(self, chat_id):
        state = self.pagination_states.get(chat_id)
        if (not self._job_enabled(state) or not state.has_more or state.retry_armed
                or chat_id in self._discovery_tasks or chat_id in self._discovery_waiting_ids
                or self._pending_for_chat(chat_id) >= REFILL_THRESHOLD):
            return
        self._discovery_waiting.append(chat_id)
        self._discovery_waiting_ids.add(chat_id)

    def _pump_discovery(self):
        if self._scheduler_stopping or self._shutting_down or self._account_resetting:
            return
        self._admit_deferred()
        if time.monotonic() < self.flood_until:
            self._arm_flood_timer()
            return
        while self._discovery_waiting and len(self._discovery_tasks) < DISCOVERY_CONCURRENCY:
            available = GLOBAL_PENDING_LIMIT - self._global_pending() - sum(self._discovery_reserved.values())
            if available <= 0:
                return
            chat_id = self._discovery_waiting.popleft()
            self._discovery_waiting_ids.discard(chat_id)
            state = self.pagination_states.get(chat_id)
            if (not self._job_enabled(state) or not state.has_more or state.retry_armed
                    or chat_id in self._discovery_tasks):
                continue
            pending = self._pending_for_chat(chat_id)
            if pending >= REFILL_THRESHOLD:
                continue
            budget = min(PER_CHAT_PENDING_LIMIT - pending, available)
            if budget <= 0:
                continue
            state.fetching_in_progress = True
            self._discovery_reserved[chat_id] = budget
            task = self._track_scheduler_task(
                self._fetch_next_chat_batch(chat_id, state.generation, budget))
            self._discovery_tasks[chat_id] = task

    async def _fetch_next_chat_batch(self, chat_id, generation=None, budget=None):
        self._ensure_scheduler()
        state = self.pagination_states.get(chat_id)
        if generation is None:
            # Public compatibility entry point still goes through the bounded pump.
            self._request_discovery(chat_id)
            self._pump_discovery()
            task = self._discovery_tasks.get(chat_id)
            if task is not None and task is not asyncio.current_task():
                await asyncio.shield(task)
            return
        if not self._job_enabled(state) or state.generation != generation:
            return
        account_generation = self._account_generation
        new_items = []
        scanned = 0
        cursor = state.last_msg_id
        newest = state.newest_msg_id
        reached_previous_scan = False
        failed = False
        try:
            kwargs = {"limit": SCAN_WINDOW}
            if cursor > 0:
                kwargs["offset_id"] = cursor
            entity = state.entity if state.entity is not None else chat_id
            iterator = self.client.iter_messages(entity, **kwargs)
            try:
                async for msg in iterator:
                    if state.stop_msg_id and msg.id <= state.stop_msg_id:
                        reached_previous_scan = True
                        break
                    cursor = msg.id
                    newest = max(newest, msg.id)
                    scanned += 1
                    if not msg.file:
                        continue
                    ext = (msg.file.ext or "").lower()
                    if not (state.all_formats or ext in state.chosen_exts):
                        continue
                    item_id = f"{chat_id}_{msg.id}"
                    if item_id in self.queue_index:
                        continue
                    new_items.append(self._make_download_item(state, msg, ext))
                    if len(new_items) >= budget:
                        break
            finally:
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    await close()
            resolve_topics = getattr(self, "_resolve_item_topics", None)
            if resolve_topics and new_items:
                await resolve_topics(chat_id, entity, new_items)
            if (account_generation != self._account_generation or state.generation != generation
                    or not self._job_enabled(state)):
                return
            # Commit the cursor and accepted rows together. A failed iterator leaves
            # the previous cursor intact, so none of its buffered files are lost.
            accepted = []
            with self.queue_lock:
                # This batch is now committed without yielding; its reserved credit
                # becomes runnable rows, rather than being counted twice.
                self._discovery_reserved.pop(chat_id, None)
                for item in new_items:
                    if item.id not in self.queue_index:
                        self.download_queue.append(item)
                        self.queue_index[item.id] = item
                        self._put_ready(item)
                        accepted.append(item)
                hook = getattr(self, "_queue_items_added", None)
                if accepted and hook:
                    hook(accepted)
                state.last_msg_id = cursor
                state.newest_msg_id = newest
                state.total_discovered += len(accepted)
            state.has_more = not (reached_previous_scan or
                                  (scanned < SCAN_WINDOW and len(new_items) < budget))
            state.consecutive_errors = 0
            state.last_error = ""
            state.discovery_started = True
            if accepted:
                self.emit_event("queue_updated", None)
        except asyncio.CancelledError:
            raise
        except (errors.UnauthorizedError, errors.AuthKeyError):
            await self.session_invalid()
        except errors.FloodError as exc:
            failed = True
            state.last_error = "Telegram rate limit"
            self._enter_flood_wait(getattr(exc, "seconds", 0) or 1)
        except Exception as exc:
            logger.exception("Could not fetch the next batch for chat %s", chat_id)
            failed = True
            state.last_error = str(exc)
        finally:
            current = asyncio.current_task()
            if self._discovery_tasks.get(chat_id) is current:
                self._discovery_tasks.pop(chat_id, None)
                self._discovery_reserved.pop(chat_id, None)
            if state.generation == generation:
                state.fetching_in_progress = False
                if failed and self._job_enabled(state):
                    self._arm_fetch_retry(chat_id)
                elif self._job_enabled(state):
                    self._request_discovery(chat_id)
            self.emit_event("queue_updated", None)
            self._pump_discovery()
            self._schedule_downloads()

    def _arm_fetch_retry(self, chat_id):
        state = self.pagination_states.get(chat_id)
        if not self._job_enabled(state) or state.retry_armed or self._scheduler_stopping:
            return
        state.consecutive_errors += 1
        if state.consecutive_errors > len(FETCH_BACKOFF):
            state.has_more = False
            self.report_error(f"Stopped scanning '{state.chat_name}': {state.last_error}")
            return
        delay = max(FETCH_BACKOFF[state.consecutive_errors - 1], self.flood_until - time.monotonic())
        state.retry_armed = True
        generation = state.generation
        account_generation = self._account_generation

        def retry():
            self._fetch_retry_handles.pop(chat_id, None)
            if (generation != state.generation or account_generation != self._account_generation
                    or not self._job_enabled(state)):
                return
            state.retry_armed = False
            self._request_discovery(chat_id)
            self._pump_discovery()

        self._fetch_retry_handles[chat_id] = self.loop.call_later(delay, retry)

    def _check_and_trigger_next_batch(self, chat_id):
        def check():
            self._ensure_scheduler()
            self._request_discovery(chat_id)
            self._pump_discovery()
        self._call_on_loop(check)

    def _enter_flood_wait(self, seconds):
        self._ensure_scheduler()
        seconds = max(1, int(seconds or 1))
        self.flood_until = max(self.flood_until, time.monotonic() + seconds)
        self.emit_event("flood_wait", seconds)
        self._arm_flood_timer()

    def _arm_flood_timer(self):
        def arm():
            self._ensure_scheduler()
            if self._flood_handle is not None or self._scheduler_stopping:
                return
            self._flood_timer_armed = True
            generation = self._account_generation

            def clear():
                self._flood_handle = None
                self._flood_timer_armed = False
                if generation != self._account_generation or self._scheduler_stopping:
                    return
                if time.monotonic() < self.flood_until:
                    self._arm_flood_timer()
                    return
                self.emit_event("flood_cleared", None)
                self._pump_discovery()
                self._schedule_downloads()

            self._flood_handle = self.loop.call_later(max(0.01, self.flood_until - time.monotonic()), clear)
        self._call_on_loop(arm)

    def _schedule_downloads(self):
        self._call_on_loop(self._schedule_on_loop)

    def _schedule_on_loop(self):
        self._ensure_scheduler()
        if self._scheduler_stopping or self._shutting_down or self._account_resetting:
            return
        self._pump_discovery()
        if time.monotonic() < self.flood_until:
            self._arm_flood_timer()
            return
        slots = self.config.get("max_parallel_files", 3) - len(self._active_workers)
        if slots <= 0:
            return
        ready_chats = [cid for cid, count in self._queued_counts.items()
                       if count and self._job_enabled(self.pagination_states.get(cid))]
        # Reserve fair shares while another selected group is discovering its first
        # batch. A fast first iterator must not occupy every long-lived transfer slot.
        contenders = set(ready_chats) | set(self._discovery_tasks) | self._discovery_waiting_ids
        contenders.update(cid for cid, count in self._active_counts.items() if count)
        per_chat = max(1, math.ceil(self.config.get("max_parallel_files", 3) / max(1, len(contenders))))
        while slots > 0 and ready_chats:
            if self.last_dispatched_chat_id in ready_chats:
                offset = (ready_chats.index(self.last_dispatched_chat_id) + 1) % len(ready_chats)
                ready_chats = ready_chats[offset:] + ready_chats[:offset]
            selected = next((cid for cid in ready_chats if self._active_counts[cid] < per_chat), None)
            if selected is None:
                # Preserve a share for first discovery, then lend unused capacity
                # once all contenders have supplied (or exhausted) their queues.
                discovering = set(self._discovery_tasks) | self._discovery_waiting_ids
                if any(not self._active_counts[cid] and not self._queued_counts[cid]
                       for cid in discovering):
                    break
                selected = min(ready_chats, key=lambda cid: self._active_counts[cid])
            item = None
            bucket = self._ready_by_chat[selected]
            while bucket:
                item_id = bucket.popleft()
                candidate = self.queue_index.get(item_id)
                if candidate is not None and item_id in self._queued_ids:
                    self._remove_ready(candidate)
                    if candidate.status == "Queued":
                        item = candidate
                        break
            if item is None:
                ready_chats.remove(selected)
                continue
            state = self.pagination_states[selected]
            item.worker_generation = getattr(item, "worker_generation", 0) + 1
            item._pause_requested = False
            item.status = "Downloading"
            generation = item.worker_generation
            task = self._track_scheduler_task(self._run_scheduled_worker(item, generation, state.generation))
            item.task = task
            self._active_workers[item.id] = (task, generation, item)
            self._active_counts[selected] += 1
            self.last_dispatched_chat_id = selected
            slots -= 1
            self.emit_event("item_updated", item)
            if not self._queued_counts[selected]:
                ready_chats.remove(selected)

    async def _run_scheduled_worker(self, item, generation, job_generation):
        try:
            await self._download_worker(item, generation)
        finally:
            owner = self._active_workers.get(item.id)
            if owner is not None and owner[0] is asyncio.current_task():
                self._active_workers.pop(item.id, None)
                self._active_counts[item.chat_id] -= 1
                if item.task is asyncio.current_task():
                    item.task = None
                state = self.pagination_states.get(item.chat_id)
                if (not self._shutting_down and not self._scheduler_stopping
                        and self._job_enabled(state) and state.generation == job_generation
                        and item.status == "Queued"):
                    delay = max(min(30, 2 ** max(0, item.retries - 1)), self.flood_until - time.monotonic())

                    def retry():
                        self._item_retry_handles.pop(item.id, None)
                        if (self._job_enabled(state) and state.generation == job_generation
                                and generation == item.worker_generation and item.status == "Queued"):
                            self._put_ready(item)
                            self._schedule_downloads()

                    self._item_retry_handles[item.id] = (item.chat_id, self.loop.call_later(delay, retry))
                self._request_discovery(item.chat_id)
                self._pump_discovery()
                self._schedule_downloads()

    def _submit_job_control(self, coro):
        if threading.current_thread() is self.thread:
            return self._track_scheduler_task(coro)
        return self.run_coro(coro)

    async def _stop_chat_work(self, state, pause=False):
        state.generation += 1
        state.paused = pause
        state.cancelled = not pause
        state.retry_armed = False
        self._discovery_waiting_ids.discard(state.chat_id)
        self._discovery_waiting = deque(cid for cid in self._discovery_waiting if cid != state.chat_id)
        timer = self._fetch_retry_handles.pop(state.chat_id, None)
        if timer:
            timer.cancel()
        tasks = []
        cancel_tasks = []
        fetch = self._discovery_tasks.get(state.chat_id)
        excluded = getattr(self, "_scheduler_excluded_task", None)
        if fetch is not None and fetch is not asyncio.current_task() and fetch is not excluded:
            tasks.append(fetch)
            cancel_tasks.append(fetch)
        changed = 0
        for item in self._chat_items(state.chat_id):
            if item.status not in ("Queued", "Downloading", "Paused"):
                continue
            changed += 1
            self._remove_ready(item)
            retry = self._item_retry_handles.pop(item.id, None)
            if retry:
                retry[1].cancel()
            item._pause_requested = pause
            item.status = "Paused" if pause else "Cancelled"
            self.emit_event("item_updated", item)
        # Already-cancelled workers and successful workers releasing disk ownership
        # still need draining, even though their status is no longer Downloading.
        for task, _generation, item in list(self._active_workers.values()):
            if item.chat_id == state.chat_id and task is not asyncio.current_task() and task is not excluded:
                tasks.append(task)
                if item.status in ("Cancelled", "Paused"):
                    cancel_tasks.append(task)
        for task in cancel_tasks:
            # Task.cancelling() is available from Python 3.11; 3.10 still
            # supports cancellation and our ownership drain below.
            if not getattr(task, "cancelling", lambda: 0)():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its first coroutine step never runs finally.
        if fetch is not None and fetch.done() and self._discovery_tasks.get(state.chat_id) is fetch:
            self._discovery_tasks.pop(state.chat_id, None)
            self._discovery_reserved.pop(state.chat_id, None)
        for item_id, (task, _generation, item) in list(self._active_workers.items()):
            if item.chat_id == state.chat_id and task.done():
                self._active_workers.pop(item_id, None)
                self._active_counts[state.chat_id] -= 1
                if item.task is task:
                    item.task = None
        state.fetching_in_progress = False
        return changed

    async def control_chat(self, chat_id, action):
        self._ensure_scheduler()
        lock = self._chat_control_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            state = self.pagination_states.get(chat_id)
            if state is None:
                return 0
            count = 0
            if action in ("pause", "cancel"):
                count = await self._stop_chat_work(state, pause=action == "pause")
            elif action in ("resume", "retry"):
                statuses = ("Paused", "Cancelled") if action == "resume" else ()
                items = self._chat_items(chat_id)
                if action == "retry" and not state.consecutive_errors and not any(
                        item.status.startswith("Failed") for item in items):
                    return 0
                # A previous stop may still be draining when this action is queued.
                for task, _generation, item in list(self._active_workers.values()):
                    if item.chat_id == chat_id and (item.status in statuses or item.status.startswith("Failed")):
                        await asyncio.gather(task, return_exceptions=True)
                state.paused = state.cancelled = False
                if state.consecutive_errors:
                    state.consecutive_errors = 0
                    state.last_error = ""
                    state.has_more = True
                for item in items:
                    if item.status in statuses or (action == "retry" and item.status.startswith("Failed")):
                        item.status = "Queued"
                        item._pause_requested = False
                        item.retries = 0
                        item.speed = 0.0
                        self._put_ready(item)
                        self.emit_event("item_updated", item)
                        count += 1
                self._request_discovery(chat_id)
            elif action == "clear":
                count = self._clear_completed_items(chat_id)
            else:
                raise ValueError(f"Unknown job action: {action}")
            self.emit_event("queue_updated", None)
            self._pump_discovery()
            self._schedule_downloads()
            return count

    async def control_all(self, action):
        self._ensure_scheduler()
        if action not in ("pause", "resume", "cancel", "retry", "clear"):
            raise ValueError(f"Unknown job action: {action}")
        if action == "clear":
            count = self._clear_completed_items()
            self.emit_event("queue_updated", None)
            return count
        # Mark every job first so draining one worker cannot start another group.
        if action in ("pause", "cancel"):
            for state in self.pagination_states.values():
                state.paused = action == "pause"
                state.cancelled = action == "cancel"
        counts = await asyncio.gather(*(self.control_chat(cid, action)
                                       for cid in list(self.pagination_states)))
        if action == "cancel" and self._flood_handle is not None:
            self._flood_handle.cancel()
            self._flood_handle = None
            self._flood_timer_armed = False
        return sum(counts)

    def _clear_completed_items(self, chat_id=None):
        with self.queue_lock:
            items = self.download_queue if chat_id is None else self._chat_items(chat_id)
            removed = [item for item in items if item.id not in self._active_workers
                       and item.status in ("Completed", "Skipped (Exists)")]
            removed_ids = {item.id for item in removed}
            if not removed_ids:
                return 0
            self.download_queue = [item for item in self.download_queue if item.id not in removed_ids]
            for item in removed:
                self.queue_index.pop(item.id, None)
                self._remove_ready(item)
            hook = getattr(self, "_queue_items_removed", None)
            if hook:
                hook(removed)
            return len(removed)

    async def _cancel_item(self, item_id):
        self._ensure_scheduler()
        item = self.queue_index.get(item_id)
        if item is None:
            return 0
        lock = self._chat_control_locks.setdefault(item.chat_id, asyncio.Lock())
        async with lock:
            return await self._cancel_item_locked(item_id)

    async def _cancel_item_locked(self, item_id):
        item = self.queue_index.get(item_id)
        if item is None or item.status not in ("Queued", "Downloading", "Paused"):
            return 0
        self._remove_ready(item)
        retry = self._item_retry_handles.pop(item.id, None)
        if retry:
            retry[1].cancel()
        item._pause_requested = False
        item.status = "Cancelled"
        owner = self._active_workers.get(item.id)
        if owner is not None:
            owner[0].cancel()
            await asyncio.gather(owner[0], return_exceptions=True)
            if self._active_workers.get(item.id) is owner:
                self._active_workers.pop(item.id, None)
                self._active_counts[item.chat_id] -= 1
                if item.task is owner[0]:
                    item.task = None
        self.emit_event("item_updated", item)
        self._request_discovery(item.chat_id)
        self._schedule_downloads()
        return 1

    def cancel_download(self, item_id):
        return self._submit_job_control(self._cancel_item(item_id))

    def cancel_all(self, exclude_task=None):
        return self._submit_job_control(self.control_all("cancel"))

    def retry_failed(self):
        return self._submit_job_control(self.control_all("retry"))

    def clear_completed(self):
        return self._submit_job_control(self.control_all("clear"))

    async def _reset_scheduler(self):
        self._ensure_scheduler()
        current = asyncio.current_task()
        while self._scheduler_stopping:
            # A worker already being drained must finish, never await the reset
            # that is itself waiting for that worker. External shutdown/reset
            # callers can wait and perform their own final cleanup pass.
            if (current is getattr(self, "_scheduler_excluded_task", None)
                    or current in self._discovery_tasks.values()
                    or any(task is current for task, _generation, _item in self._active_workers.values())):
                return
            completion = self._scheduler_reset_complete
            if completion is not None:
                await asyncio.shield(completion)
        completion = self.loop.create_future()
        self._scheduler_reset_complete = completion
        self._scheduler_stopping = True
        self._scheduler_excluded_task = current
        try:
            await self.control_all("cancel")
            for handle in self._fetch_retry_handles.values():
                handle.cancel()
            for _chat_id, handle in self._item_retry_handles.values():
                handle.cancel()
            if self._flood_handle is not None:
                self._flood_handle.cancel()
            self._fetch_retry_handles.clear()
            self._item_retry_handles.clear()
            self._discovery_tasks.clear()
            self._discovery_reserved.clear()
            self._discovery_waiting.clear()
            self._discovery_waiting_ids.clear()
            self._ready_by_chat.clear()
            self._queued_ids.clear()
            self._queued_counts.clear()
            self._deferred_by_chat.clear()
            self._deferred_ids.clear()
            self._deferred_counts.clear()
            self._active_counts.clear()
            self._active_workers.clear()
            self._chat_control_locks.clear()
            self._flood_handle = None
            self._flood_timer_armed = False
        finally:
            self._scheduler_excluded_task = None
            self._scheduler_stopping = False
            self._scheduler_reset_complete = None
            if not completion.done():
                completion.set_result(None)

    async def wait_for_scheduler_idle(self):
        self._ensure_scheduler()
        while (self._discovery_tasks or self._active_workers or self._discovery_waiting_ids
               or self._queued_ids or self._deferred_ids or self._fetch_retry_handles or self._item_retry_handles):
            await asyncio.sleep(0.01)
