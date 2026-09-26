"""Offline scheduler regressions; no Tk root, Telegram session, or real downloads."""
import asyncio
import os
import tempfile
import threading
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import patch

import interactive_downloader as dl
import transfer_scheduler as scheduler


def message(mid, ext=".mp4"):
    return SimpleNamespace(id=mid, file=SimpleNamespace(name=f"{mid}{ext}", ext=ext, size=4))


def chat(cid):
    return dl.ChatEntry(cid, f"Chat {cid}", "Group", False, cid)


class Client:
    async def disconnect(self):
        pass


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tgdl-scheduler-test-")
        self.config_patch = patch.object(dl, "CONFIG_FILE", os.path.join(self.tmp.name, "config.json"))
        self.config_patch.start()
        self.backend = dl.TelegramBackend(1, "test", os.path.join(self.tmp.name, "unused-session"))
        self.backend.config = {"output_dir": self.tmp.name, "max_parallel_files": 3, "scan_depth": 500}
        self.backend.client = Client()

        async def finish(item, generation=None):
            item.status = "Completed"
            item.downloaded_bytes = item.file_size
            self.backend.emit_event("item_updated", item)

        self.backend._download_worker = finish

    def tearDown(self):
        if not self.backend.loop.is_closed():
            self.backend.run_coro(self.backend._reset_scheduler()).result(5)
            self.backend.shutdown()
        self.config_patch.stop()
        self.tmp.cleanup()

    def run_async(self, coro):
        return self.backend.run_coro(coro).result(10)

    async def idle(self):
        await asyncio.wait_for(self.backend.wait_for_scheduler_idle(), 3)

    def test_failed_page_retries_without_skipping_buffered_messages(self):
        class Flaky(Client):
            def __init__(self):
                self.offsets = []

            async def iter_messages(self, entity, limit=None, offset_id=None):
                self.offsets.append(offset_id)
                if len(self.offsets) == 1:
                    yield message(3)
                    raise OSError("connection interrupted after a buffered message")
                for mid in (3, 2, 1):
                    if offset_id is None or mid < offset_id:
                        yield message(mid)

        async def run():
            self.backend.client = client = Flaky()
            await self.backend.enqueue_selected_chats([chat(1)], {".mp4"})
            await self.idle()
            self.assertEqual(client.offsets[:2], [None, None])
            self.assertEqual({item.msg_id for item in self.backend.download_queue}, {1, 2, 3})

        with patch.object(scheduler, "FETCH_BACKOFF", (0.01, 0.02)):
            self.run_async(run())

    def test_adding_group_preserves_each_jobs_format_filter(self):
        async def run():
            started, release = asyncio.Event(), asyncio.Event()

            class Mixed(Client):
                async def iter_messages(self, entity, **kwargs):
                    if entity == 1:
                        started.set()
                        await release.wait()
                    yield message(2, ".mp4")
                    yield message(1, ".jpg")

            self.backend.client = Mixed()
            await self.backend.enqueue_selected_chats([chat(1)], {".mp4"})
            await started.wait()
            await self.backend.enqueue_selected_chats([chat(2)], {".jpg"})
            release.set()
            await self.idle()
            self.assertEqual([(i.chat_id, i.ext) for i in self.backend.download_queue],
                             [(2, ".jpg"), (1, ".mp4")])

        self.run_async(run())

    def test_output_directory_is_captured_before_discovery_and_survives_resume(self):
        async def run():
            scanning, scan_release, transfer_started, transfer_release = (asyncio.Event() for _ in range(4))
            original = self.backend.config["output_dir"]
            updated = os.path.join(self.tmp.name, "new-downloads")
            observed = []

            class Delayed(Client):
                async def iter_messages(self, entity, **kwargs):
                    if entity == 1:
                        scanning.set()
                        await scan_release.wait()
                    yield message(1)

            async def transfer(item, generation=None):
                observed.append((item.chat_id, self.backend._resolve_paths(item)[0]))
                if item.chat_id == 1:
                    transfer_started.set()
                    await transfer_release.wait()
                item.status = "Completed"

            self.backend.client = Delayed()
            self.backend._download_worker = transfer
            await self.backend.enqueue_selected_chats([chat(1)], {".mp4"})
            await scanning.wait()
            self.assertIsNone(await self.backend.apply_config({"output_dir": updated}))
            await self.backend.enqueue_selected_chats([chat(2)], {".mp4"})
            scan_release.set()
            await transfer_started.wait()
            await self.backend.control_chat(1, "pause")
            self.assertIsNone(await self.backend.apply_config({"output_dir": os.path.join(updated, "later")}))
            await self.backend.control_chat(1, "resume")
            transfer_release.set()
            await self.idle()
            self.assertEqual([root for cid, root in observed if cid == 1], [original, original])
            self.assertEqual([root for cid, root in observed if cid == 2], [updated])
            self.assertEqual(self.backend.pagination_states[1].output_dir, original)
            self.assertEqual(self.backend.pagination_states[2].output_dir, updated)

        self.run_async(run())

    def test_repeated_start_does_not_overlap_or_rewind_discovery(self):
        async def run():
            started, release = asyncio.Event(), asyncio.Event()

            class Held(Client):
                calls = 0

                async def iter_messages(self, entity, **kwargs):
                    self.calls += 1
                    yield message(2)
                    started.set()
                    await release.wait()
                    yield message(1)

            self.backend.client = client = Held()
            await self.backend.enqueue_selected_chats([chat(1)], {".mp4"})
            await started.wait()
            await self.backend.enqueue_selected_chats([chat(1)], {".mp4"})
            release.set()
            await self.idle()
            self.assertEqual(client.calls, 1)
            self.assertEqual(len(self.backend.download_queue), 2)
            self.assertEqual(len(self.backend.queue_index), 2)

        self.run_async(run())

    def test_changed_formats_restart_only_matching_cancelled_rows(self):
        async def run():
            started, release = asyncio.Event(), asyncio.Event()

            class Mixed(Client):
                async def iter_messages(self, entity, **kwargs):
                    yield message(2, ".jpg")
                    yield message(1, ".mp4")

            async def transfer(item, generation=None):
                started.set()
                await release.wait()
                item.status = "Completed"
                self.backend.emit_event("item_updated", item)

            self.backend.client = Mixed()
            self.backend._download_worker = transfer
            await self.backend.enqueue_selected_chats([chat(1)], set(), all_formats=True)
            await started.wait()
            await self.backend.control_chat(1, "cancel")
            new_directory = os.path.join(self.tmp.name, "replacement-job")
            await self.backend.apply_config({"output_dir": new_directory})
            await self.backend.enqueue_selected_chats([chat(1)], {".mp4"})
            release.set()
            await self.idle()
            self.assertEqual(self.backend.queue_index["1_1"].status, "Completed")
            self.assertEqual(self.backend.queue_index["1_1"].output_dir, new_directory)
            self.assertEqual(self.backend.queue_index["1_2"].status, "Cancelled")
            self.assertEqual(len(self.backend.download_queue), 2)
            self.assertEqual(self.backend.pagination_states[1].chosen_exts, frozenset({".mp4"}))

        self.run_async(run())

    def test_start_after_completion_checks_only_newer_messages(self):
        async def run():
            class History(Client):
                newest = 3

                def __init__(self):
                    self.yielded = []

                async def iter_messages(self, entity, **kwargs):
                    for mid in range(self.newest, 0, -1):
                        self.yielded.append(mid)
                        yield message(mid)

            self.backend.client = client = History()
            await self.backend.enqueue_selected_chats([chat(1)], {".mp4"})
            await self.idle()
            client.yielded.clear()
            client.newest = 4
            await self.backend.enqueue_selected_chats([chat(1)], {".mp4"})
            await self.idle()
            self.assertEqual(client.yielded, [4, 3])
            self.assertEqual({item.msg_id for item in self.backend.download_queue}, {1, 2, 3, 4})

        self.run_async(run())

    def test_cancel_all_drains_discovery_before_returning(self):
        async def run():
            started, release = asyncio.Event(), asyncio.Event()

            class Held(Client):
                async def iter_messages(self, entity, **kwargs):
                    yield message(2)
                    started.set()
                    await release.wait()
                    yield message(1)

            self.backend.client = Held()
            await self.backend.enqueue_selected_chats([chat(1)], {".mp4"})
            await started.wait()
            await self.backend.control_all("cancel")
            release.set()
            await asyncio.sleep(0)
            self.assertEqual(self.backend.download_queue, [])
            self.assertEqual(self.backend._discovery_tasks, {})
            self.assertEqual(self.backend._discovery_reserved, {})

        self.run_async(run())

    def test_retry_failed_does_not_resume_a_cancelled_job(self):
        async def run():
            self.backend._ensure_scheduler()
            state = self.backend._make_chat_job(chat(1), frozenset({".mp4"}), False)
            state.cancelled = True
            self.backend.pagination_states[1] = state
            self.assertEqual(await self.backend.control_all("retry"), 0)
            self.assertTrue(state.cancelled)
            self.assertFalse(self.backend._discovery_tasks)

        self.run_async(run())

    def test_revoked_session_resets_without_cancelling_its_own_task(self):
        reset = threading.Event()

        class Revoked(Client):
            async def iter_messages(self, entity, **kwargs):
                raise dl.errors.AuthKeyUnregisteredError(request=None)
                yield  # pragma: no cover

        self.backend.client = Revoked()
        self.backend.auth_event = lambda *args, **kwargs: reset.set()
        self.run_async(self.backend.enqueue_selected_chats([chat(1)], {".mp4"}))
        self.assertTrue(reset.wait(2), "account reset deadlocked on its own discovery task")
        self.assertFalse(self.backend.pagination_states)
        self.assertFalse(self.backend._discovery_tasks)

    def test_revoked_session_inside_real_worker_resets_and_releases_storage(self):
        reset = threading.Event()

        class RevokedTransfer(Client):
            worker = None

            async def iter_messages(self, entity, **kwargs):
                yield message(1)

            async def download_media(self, message, **kwargs):
                self.worker = asyncio.current_task()
                raise dl.errors.AuthKeyUnregisteredError(request=None)

        self.backend.client = client = RevokedTransfer()
        self.backend._download_worker = dl.TelegramBackend._download_worker.__get__(self.backend)
        self.backend.auth_event = lambda *args, **kwargs: reset.set()
        self.run_async(self.backend.enqueue_selected_chats([chat(1)], {".mp4"}))
        self.assertTrue(reset.wait(2), "account reset deadlocked on its own worker task")

        async def verify():
            await asyncio.wait_for(asyncio.shield(client.worker), 2)
            self.assertFalse(client.worker.cancelled())
            self.assertFalse(self.backend.pagination_states)
            self.assertFalse(self.backend.download_queue)
            self.assertFalse(self.backend._active_workers)
            self.assertFalse(dl.get_storage(self.tmp.name).active)

        self.run_async(verify())

    def test_simultaneous_revoked_workers_coalesce_account_reset(self):
        reset = threading.Event()
        resets, loop_errors = [], []

        class RevokedTogether(Client):
            def __init__(self):
                self.ready = asyncio.Event()
                self.workers = []

            async def iter_messages(self, entity, **kwargs):
                yield message(2)
                yield message(1)

            async def download_media(self, message, **kwargs):
                self.workers.append(asyncio.current_task())
                if len(self.workers) == 2:
                    self.ready.set()
                await self.ready.wait()
                raise dl.errors.AuthKeyUnregisteredError(request=None)

        self.backend.client = client = RevokedTogether()
        self.backend._download_worker = dl.TelegramBackend._download_worker.__get__(self.backend)

        def auth_event(*args, **kwargs):
            resets.append(args)
            reset.set()

        self.backend.auth_event = auth_event
        self.backend.loop.call_soon_threadsafe(
            self.backend.loop.set_exception_handler, lambda loop, context: loop_errors.append(context))
        self.run_async(self.backend.enqueue_selected_chats([chat(1)], {".mp4"}))
        self.assertTrue(reset.wait(2), "simultaneous revoked responses deadlocked reset")

        async def verify():
            await asyncio.wait_for(asyncio.gather(*client.workers, return_exceptions=True), 2)
            self.assertEqual(len(client.workers), 2)
            self.assertEqual(len(resets), 1)
            self.assertEqual(self.backend._account_generation, 1)
            self.assertEqual(loop_errors, [])
            self.assertFalse(self.backend._active_workers)
            self.assertFalse(dl.get_storage(self.tmp.name).active)

        self.run_async(verify())

    def test_external_reset_waits_for_worker_owned_account_reset(self):
        reset = threading.Event()
        external_resets, loop_errors = [], []

        class One(Client):
            async def iter_messages(self, entity, **kwargs):
                yield message(1)

        async def revoked(item, generation=None):
            # Queue a shutdown-like external reset before the first reset's child
            # controllers run, which previously overwrote current-task exclusion.
            external_resets.append(asyncio.create_task(self.backend._reset_scheduler()))
            item.status = "Cancelled"
            await self.backend.session_invalid()

        self.backend.client = One()
        self.backend._download_worker = revoked
        self.backend.auth_event = lambda *args, **kwargs: reset.set()
        self.backend.loop.call_soon_threadsafe(
            self.backend.loop.set_exception_handler, lambda loop, context: loop_errors.append(context))
        self.run_async(self.backend.enqueue_selected_chats([chat(1)], {".mp4"}))
        self.assertTrue(reset.wait(2), "overlapping external reset cancelled its own worker dependency")

        async def verify():
            await asyncio.wait_for(asyncio.gather(*external_resets), 2)
            self.assertEqual(loop_errors, [])
            self.assertFalse(self.backend._scheduler_stopping)
            self.assertIsNone(self.backend._scheduler_excluded_task)
            self.assertFalse(self.backend._active_workers)

        self.run_async(verify())

    def test_hundred_groups_bound_discovery_and_pending_work(self):
        async def run():
            release, three_fetching, downloads = asyncio.Event(), asyncio.Event(), asyncio.Event()

            class Many(Client):
                active = peak = 0

                async def iter_messages(self, entity, limit=None, offset_id=None):
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    if self.active == scheduler.DISCOVERY_CONCURRENCY:
                        three_fetching.set()
                    try:
                        await release.wait()
                        for mid in range(100, 0, -1):
                            if offset_id is None or mid < offset_id:
                                yield message(mid)
                    finally:
                        self.active -= 1

            async def hold(item, generation=None):
                await downloads.wait()
                item.status = "Completed"

            self.backend.client = client = Many()
            self.backend._download_worker = hold
            self.backend.config["max_parallel_files"] = 1
            await self.backend.enqueue_selected_chats([chat(cid) for cid in range(100)], {".mp4"})
            await asyncio.wait_for(three_fetching.wait(), 1)
            self.assertEqual(client.peak, scheduler.DISCOVERY_CONCURRENCY)
            release.set()
            for _ in range(100):
                if self.backend._global_pending() == scheduler.GLOBAL_PENDING_LIMIT:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(self.backend._global_pending(), scheduler.GLOBAL_PENDING_LIMIT)
            self.assertLessEqual(client.peak, scheduler.DISCOVERY_CONCURRENCY)
            self.assertTrue(all(self.backend._pending_for_chat(cid) <= scheduler.PER_CHAT_PENDING_LIMIT
                                for cid in range(100)))
            await self.backend.control_all("cancel")
            self.assertFalse(self.backend._discovery_tasks)
            self.assertFalse(self.backend._active_workers)

        self.run_async(run())

    def test_pause_and_cancel_resume_admit_restored_rows_within_budgets(self):
        async def run():
            release = asyncio.Event()
            peak = 0

            class Three(Client):
                async def iter_messages(self, entity, offset_id=None, **kwargs):
                    for mid in (3, 2, 1):
                        if offset_id is None or mid < offset_id:
                            yield message(mid)

            async def transfer(item, generation=None):
                await release.wait()
                item.status = "Completed"
                self.backend.emit_event("item_updated", item)

            self.backend._ensure_scheduler()
            put_ready = self.backend._put_ready

            def admit(item):
                nonlocal peak
                put_ready(item)
                used = self.backend._global_pending() + sum(self.backend._discovery_reserved.values())
                peak = max(peak, used)
                self.assertLessEqual(used, 6)
                self.assertLessEqual(self.backend._admitted_for_chat(item.chat_id), 3)

            async def filled():
                for _ in range(100):
                    if self.backend._global_pending() == 6:
                        return
                    await asyncio.sleep(0)
                self.fail("discovery did not fill the expected admitted queue")

            self.backend._put_ready = admit
            self.backend.client = Three()
            self.backend._download_worker = transfer
            await self.backend.enqueue_selected_chats([chat(1), chat(2)], {".mp4"})
            await filled()
            await self.backend.control_chat(1, "pause")
            await self.backend.enqueue_selected_chats([chat(3)], {".mp4"})
            await filled()
            await self.backend.control_chat(1, "resume")
            self.assertEqual(self.backend._global_pending(), 6)
            self.assertEqual(len(self.backend._deferred_ids), 3)
            await self.backend.control_chat(2, "cancel")
            await self.backend.control_chat(2, "resume")
            self.assertEqual(self.backend._global_pending(), 6)
            self.assertEqual(len(self.backend._deferred_ids), 3)
            release.set()
            await self.idle()
            self.assertLessEqual(peak, 6)
            self.assertEqual(len(self.backend.download_queue), 9)
            self.assertTrue(all(item.status == "Completed" for item in self.backend.download_queue))
            self.assertFalse(self.backend._deferred_ids)

        with (patch.object(scheduler, "GLOBAL_PENDING_LIMIT", 6),
              patch.object(scheduler, "PER_CHAT_PENDING_LIMIT", 3),
              patch.object(scheduler, "REFILL_THRESHOLD", 2)):
            self.run_async(run())

    def test_multiple_batches_in_multiple_groups_drain_without_gaps(self):
        async def run():
            class Paged(Client):
                async def iter_messages(self, entity, limit=None, offset_id=None):
                    ids = [mid for mid in range(125, 0, -1) if offset_id is None or mid < offset_id]
                    for mid in ids[:limit]:
                        yield message(mid)

            self.backend.client = Paged()
            await self.backend.enqueue_selected_chats([chat(cid) for cid in range(5)], {".mp4"})
            await self.idle()
            self.assertEqual(len(self.backend.download_queue), 625)
            self.assertEqual(len(self.backend.queue_index), 625)
            self.assertTrue(all(item.status == "Completed" for item in self.backend.download_queue))
            self.assertTrue(all(not state.has_more for state in self.backend.pagination_states.values()))
            self.assertEqual(self.backend._global_pending(), 0)
            self.assertEqual(self.backend._discovery_reserved, {})

        self.run_async(run())

    def test_first_group_cannot_take_every_slot(self):
        async def run():
            started, release = asyncio.Event(), asyncio.Event()
            starts = []

            class Two(Client):
                async def iter_messages(self, entity, **kwargs):
                    for mid in (4, 3, 2, 1):
                        yield message(mid)

            async def hold(item, generation=None):
                starts.append(item.chat_id)
                if len(starts) == 3:
                    started.set()
                await release.wait()
                item.status = "Completed"

            self.backend.client = Two()
            self.backend._download_worker = hold
            await self.backend.enqueue_selected_chats([chat(1), chat(2)], {".mp4"})
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(set(starts), {1, 2})
            self.assertLessEqual(max(Counter(starts).values()), 2)
            await self.backend.control_all("cancel")

        self.run_async(run())

    def test_discovery_retry_honors_flood_deadline(self):
        async def run():
            failed = asyncio.Event()

            class Flooded(Client):
                calls = 0

                async def iter_messages(self, entity, **kwargs):
                    self.calls += 1
                    failed.set()
                    raise dl.errors.FloodWaitError(request=None, capture=10)
                    yield  # pragma: no cover

            self.backend.client = client = Flooded()
            await self.backend.enqueue_selected_chats([chat(1)], {".mp4"})
            await failed.wait()
            await asyncio.sleep(0)
            timer = self.backend._fetch_retry_handles[1]
            self.assertGreaterEqual(timer.when(), self.backend.flood_until - 0.01)
            self.backend._check_and_trigger_next_batch(1)
            await asyncio.sleep(0.02)
            self.assertEqual(client.calls, 1)
            await self.backend.control_all("cancel")
            self.assertFalse(self.backend._fetch_retry_handles)

        self.run_async(run())

    def test_pause_resume_retains_job_and_drains_old_workers(self):
        async def run():
            started, release = asyncio.Event(), asyncio.Event()
            generations = []

            class One(Client):
                async def iter_messages(self, entity, **kwargs):
                    yield message(1)

            async def hold(item, generation=None):
                generations.append(generation)
                started.set()
                await release.wait()
                item.status = "Completed"

            self.backend.client = One()
            self.backend._download_worker = hold
            await self.backend.enqueue_selected_chats([chat(1)], {".mp4"})
            await started.wait()
            cursor = self.backend.pagination_states[1].last_msg_id
            await self.backend.control_chat(1, "pause")
            self.assertEqual(self.backend.download_queue[0].status, "Paused")
            self.assertFalse(self.backend._active_workers)
            await self.backend.control_chat(1, "resume")
            release.set()
            await self.idle()
            self.assertEqual(self.backend.pagination_states[1].last_msg_id, cursor)
            self.assertEqual(self.backend.download_queue[0].status, "Completed")
            self.assertEqual(generations, [1, 2])

        self.run_async(run())

    def test_cancel_then_resume_waits_for_old_worker_cleanup(self):
        async def run():
            started, cleaning, cleaned, finish = (asyncio.Event() for _ in range(4))
            generations = []

            class One(Client):
                async def iter_messages(self, entity, **kwargs):
                    yield message(1)

            async def hold(item, generation=None):
                generations.append(generation)
                started.set()
                try:
                    await finish.wait()
                    item.status = "Completed"
                except asyncio.CancelledError:
                    cleaning.set()
                    await cleaned.wait()
                    item.status = "Cancelled"
                    raise

            self.backend.client = One()
            self.backend._download_worker = hold
            await self.backend.enqueue_selected_chats([chat(1)], {".mp4"})
            await started.wait()
            cancel = asyncio.create_task(self.backend.control_chat(1, "cancel"))
            await cleaning.wait()
            resume = asyncio.create_task(self.backend.control_chat(1, "resume"))
            await asyncio.sleep(0)
            self.assertEqual(generations, [1])
            cleaned.set()
            await cancel
            await resume
            finish.set()
            await self.idle()
            self.assertEqual(generations, [1, 2])
            self.assertEqual(self.backend.download_queue[0].status, "Completed")

        self.run_async(run())

    def test_cancel_all_waits_for_single_item_cancellation_already_draining(self):
        async def run():
            started, cleaning, cleaned = (asyncio.Event() for _ in range(3))

            class One(Client):
                async def iter_messages(self, entity, **kwargs):
                    yield message(1)

            async def transfer(item, generation=None):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleaning.set()
                    await cleaned.wait()
                    raise

            self.backend.client = One()
            self.backend._download_worker = transfer
            await self.backend.enqueue_selected_chats([chat(1)], {".mp4"})
            await started.wait()
            cancel_item = asyncio.create_task(self.backend._cancel_item("1_1"))
            await cleaning.wait()
            cancel_all = asyncio.create_task(self.backend.control_all("cancel"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(cancel_all.done())
            self.assertTrue(self.backend._active_workers)
            cleaned.set()
            await cancel_item
            await cancel_all
            self.assertFalse(self.backend._active_workers)

        self.run_async(run())

    def test_shutdown_drains_workers_and_cancels_scheduler_timers(self):
        async def run():
            started, flooded = asyncio.Event(), asyncio.Event()

            class Two(Client):
                async def iter_messages(self, entity, **kwargs):
                    if entity == 2:
                        flooded.set()
                        raise dl.errors.FloodWaitError(request=None, capture=10)
                    yield message(1)

            async def transfer(item, generation=None):
                started.set()
                await asyncio.Event().wait()

            self.backend.client = Two()
            self.backend._download_worker = transfer
            await self.backend.enqueue_selected_chats([chat(1)], {".mp4"})
            await started.wait()
            await self.backend.enqueue_selected_chats([chat(2)], {".mp4"})
            await flooded.wait()
            await asyncio.sleep(0)
            self.assertTrue(self.backend._active_workers)
            self.assertTrue(self.backend._fetch_retry_handles)
            self.assertIsNotNone(self.backend._flood_handle)

        self.run_async(run())
        self.backend.shutdown()
        self.assertTrue(self.backend.loop.is_closed())
        self.assertFalse(self.backend.thread.is_alive())
        self.assertFalse(self.backend._active_workers)
        self.assertFalse(self.backend._discovery_tasks)
        self.assertFalse(self.backend._fetch_retry_handles)
        self.assertFalse(self.backend._item_retry_handles)
        self.assertIsNone(self.backend._flood_handle)

    def test_cancel_before_worker_first_step_releases_slot(self):
        async def run():
            self.backend._ensure_scheduler()
            state = self.backend._make_chat_job(chat(1), frozenset({".mp4"}), False)
            self.backend.pagination_states[1] = state
            state.has_more = False
            item = self.backend._make_download_item(state, message(1), ".mp4")
            self.backend.download_queue.append(item)
            self.backend.queue_index[item.id] = item
            self.backend._queue_items_added([item])
            self.backend._put_ready(item)
            self.backend._schedule_on_loop()
            await self.backend._cancel_item(item.id)
            self.assertFalse(self.backend._active_workers)
            self.assertEqual(self.backend._active_counts[1], 0)
            self.assertEqual(item.status, "Cancelled")

        self.run_async(run())

    def test_cancel_supports_tasks_without_python311_cancelling_method(self):
        async def run():
            self.backend._ensure_scheduler()
            state = self.backend._make_chat_job(chat(1), frozenset({".mp4"}), False)
            state.has_more = False
            self.backend.pagination_states[1] = state
            item = self.backend._make_download_item(state, message(1), ".mp4")
            item.status = "Downloading"
            # Future provides the cancellation/await API used by Python 3.10
            # tasks, without the newer Task.cancelling() inspection method.
            legacy_task = self.backend.loop.create_future()
            self.assertFalse(hasattr(legacy_task, "cancelling"))
            item.task = legacy_task
            self.backend.download_queue.append(item)
            self.backend.queue_index[item.id] = item
            self.backend._queue_items_added([item])
            self.backend._active_workers[item.id] = (legacy_task, 1, item)
            self.backend._active_counts[1] = 1
            await self.backend.control_chat(1, "cancel")
            self.assertTrue(legacy_task.cancelled())
            self.assertEqual(item.status, "Cancelled")
            self.assertFalse(self.backend._active_workers)
            self.assertEqual(self.backend._active_counts[1], 0)

        self.run_async(run())

    def test_clear_completed_preserves_failed_and_cancelled_items(self):
        async def run():
            self.backend._ensure_scheduler()
            state = self.backend._make_chat_job(chat(1), frozenset(), True)
            state.has_more = False
            self.backend.pagination_states[1] = state
            for mid, status in enumerate(("Failed", "Cancelled", "Completed"), 1):
                item = self.backend._make_download_item(state, message(mid), ".mp4")
                item.status = status
                self.backend.download_queue.append(item)
                self.backend.queue_index[item.id] = item
                self.backend._queue_items_added([item])
            self.assertEqual(await self.backend.control_all("clear"), 1)
            self.assertEqual([item.status for item in self.backend.download_queue], ["Failed", "Cancelled"])

        self.run_async(run())


if __name__ == "__main__":
    unittest.main()
