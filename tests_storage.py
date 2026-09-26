"""Offline storage regressions. Temporary files only; no Telegram sessions."""
import asyncio
import errno
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telethon import TelegramClient, types

import interactive_downloader as dl
from download_storage import AsyncDiskSink, DownloadStorage, media_identity


def make_item(mid=1, *, name="same.mp4", size=4, chat=123, title="Chat", media_id=None, photo=False):
    source = SimpleNamespace(id=media_id if media_id is not None else mid)
    message = SimpleNamespace(id=mid, document=None if photo else source,
                              photo=source if photo else None)
    return dl.DownloadItem(f"{chat}_{mid}", chat, title, message, name, mid,
                           ".jpg" if photo else ".mp4", size, status="Downloading")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tgdl-storage-test-")
        self.storage = DownloadStorage(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def reserve(self, item, account="account"):
        return self.storage.prepare(account, item.chat_id, item.chat_name, item.file_name,
                                    media_identity(account, item))

    def test_parallel_same_name_reservations_never_overwrite(self):
        items = [make_item(mid) for mid in (1, 2, 3)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            reservations = list(pool.map(self.reserve, items))
            for number, reservation in enumerate(reservations):
                Path(reservation.temp_path).write_bytes(bytes([number]) * 4)
            paths = [result[0] for result in pool.map(self.storage.finalize, reservations)]
        self.assertEqual(len(set(paths)), 3)
        self.assertEqual({Path(path).read_bytes() for path in paths}, {bytes([n]) * 4 for n in range(3)})

    def test_unindexed_equal_size_file_is_preserved_without_false_skip(self):
        directory = Path(self.temp.name) / "Chat"
        directory.mkdir()
        original = directory / "same.mp4"
        original.write_bytes(b"old!")
        reservation = self.reserve(make_item())
        self.assertFalse(reservation.complete)
        self.assertNotEqual(reservation.save_path, str(original))
        Path(reservation.temp_path).write_bytes(b"new!")
        self.storage.finalize(reservation)
        self.assertEqual(original.read_bytes(), b"old!")

    def test_same_titles_and_other_accounts_have_distinct_directories(self):
        first = self.reserve(make_item(chat=1))
        second = self.reserve(make_item(chat=2))
        third = self.reserve(make_item(chat=1), account="other")
        self.assertEqual(len({str(Path(r.save_path).parent) for r in (first, second, third)}), 3)

    def test_manifest_survives_restart_and_detects_same_size_local_changes(self):
        item = make_item()
        reservation = self.reserve(item)
        Path(reservation.temp_path).write_bytes(b"good")
        path, _ = self.storage.finalize(reservation)
        self.storage.release(reservation)
        self.storage = DownloadStorage(self.temp.name)
        self.assertTrue(self.reserve(item).complete)
        Path(path).write_bytes(b"edit")
        next_reservation = self.reserve(item)
        self.assertFalse(next_reservation.complete)
        self.assertNotEqual(path, next_reservation.save_path)
        self.assertEqual(Path(path).read_bytes(), b"edit")

    def test_changed_telegram_media_never_reuses_old_partial(self):
        first = self.reserve(make_item(media_id=10))
        Path(first.temp_path).write_bytes(b"old")
        self.storage.release(first)
        updated = self.reserve(make_item(media_id=20))
        self.assertNotEqual(first.temp_path, updated.temp_path)
        self.assertFalse(Path(updated.temp_path).exists())

    def test_missing_completed_file_can_restart_with_protected_partial(self):
        item = make_item(size=8)
        original = self.reserve(item)
        Path(original.temp_path).write_bytes(b"complete")
        final, _ = self.storage.finalize(original)
        self.storage.release(original)
        Path(final).unlink()
        retry = self.reserve(item)
        Path(retry.temp_path).write_bytes(b"part")
        self.storage.release(retry)
        self.assertEqual(self.storage.orphan_parts(delete=True), (0, 0))
        self.assertEqual(Path(retry.temp_path).read_bytes(), b"part")

    def test_external_destination_creation_during_transfer_cannot_be_clobbered(self):
        reservation = self.reserve(make_item())
        Path(reservation.save_path).write_bytes(b"user")
        Path(reservation.temp_path).write_bytes(b"data")
        final, _ = self.storage.finalize(reservation)
        self.assertNotEqual(final, reservation.save_path)
        self.assertEqual(Path(reservation.save_path).read_bytes(), b"user")
        self.assertEqual(Path(final).read_bytes(), b"data")

    def test_no_hardlink_filesystem_uses_exclusive_creation(self):
        reservation = self.reserve(make_item())
        Path(reservation.temp_path).write_bytes(b"data")
        with patch("download_storage.os.link", side_effect=OSError(errno.EPERM, "no links")):
            final, _ = self.storage.finalize(reservation)
        self.assertEqual(Path(final).read_bytes(), b"data")

    def test_cleanup_protects_active_and_resumable_files(self):
        reservation = self.reserve(make_item(size=1024))
        Path(reservation.temp_path).write_bytes(b"part")
        orphan = Path(self.temp.name) / "unknown.part"
        orphan.write_bytes(b"orphan")
        self.assertEqual(self.storage.orphan_parts(), (1, 6))
        self.assertEqual(self.storage.orphan_parts(delete=True), (1, 6))
        self.assertTrue(Path(reservation.temp_path).exists())
        self.storage.release(reservation)
        self.storage = DownloadStorage(self.temp.name)
        self.assertEqual(self.storage.orphan_parts(delete=True), (0, 0))
        self.assertTrue(Path(reservation.temp_path).exists())

    def test_rejects_directory_components_and_external_symlinks(self):
        with self.assertRaises(ValueError):
            self.reserve(make_item(name="../escape.mp4"))
        with tempfile.TemporaryDirectory() as outside:
            (Path(self.temp.name) / "Chat").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                self.reserve(make_item())


class WritingClient:
    def __init__(self, data=b"data"):
        self.data = data
        self.calls = 0

    async def download_media(self, message, file, progress_callback=None):
        self.calls += 1
        await file.write(self.data)
        return file

    async def disconnect(self):
        pass


class WorkerStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tgdl-worker-storage-")
        self.config_patch = patch.object(dl, "CONFIG_FILE", os.path.join(self.temp.name, "config.json"))
        self.config_patch.start()
        self.backend = dl.TelegramBackend(1, "fake", os.path.join(self.temp.name, "session"))
        self.backend.config["output_dir"] = os.path.join(self.temp.name, "out")
        self.backend.client = WritingClient()

    def tearDown(self):
        self.backend.shutdown()
        self.config_patch.stop()
        self.temp.cleanup()

    def run_worker(self, item):
        self.backend.run_coro(self.backend._download_worker(item)).result(10)

    def test_concurrent_transfers_with_same_name_keep_both_files(self):
        class BarrierClient(WritingClient):
            def __init__(self):
                super().__init__()
                self.ready = asyncio.Event()

            async def download_media(self, message, file, progress_callback=None):
                self.calls += 1
                if self.calls == 2:
                    self.ready.set()
                await self.ready.wait()
                await file.write(bytes([message.id]) * 4)

        self.backend.client = BarrierClient()
        items = [make_item(1), make_item(2)]

        async def run():
            await asyncio.gather(*(self.backend._download_worker(item) for item in items))

        self.backend.run_coro(run()).result(10)
        self.assertEqual([item.status for item in items], ["Completed", "Completed"])
        self.assertNotEqual(items[0].output_path, items[1].output_path)
        self.assertEqual({Path(item.output_path).read_bytes() for item in items}, {b"\x01" * 4, b"\x02" * 4})
        self.assertTrue(all(item.message is None for item in items))

    def test_document_size_is_strict_but_actual_photo_size_can_differ(self):
        self.backend.client = WritingClient(b"x")
        document = make_item(size=4096)
        self.run_worker(document)
        self.assertEqual(document.status, "Queued")
        self.assertFalse(Path(document.output_path).exists())
        photo = make_item(2, name="photo.jpg", size=4096, photo=True)
        self.run_worker(photo)
        self.assertEqual(photo.status, "Completed")
        self.assertEqual(Path(photo.output_path).read_bytes(), b"x")

    def test_same_telegram_identity_skips_verified_download(self):
        first = make_item()
        self.run_worker(first)
        repeated = make_item()
        self.run_worker(repeated)
        self.assertEqual(repeated.status, "Skipped (Exists)")
        self.assertEqual(repeated.output_path, first.output_path)
        self.assertEqual(self.backend.client.calls, 1)
        self.assertIsNone(repeated.message)

    def test_cancel_preserves_partial_and_next_worker_resumes(self):
        total = 3 * dl.RESUME_ALIGN

        class ResumableClient(WritingClient):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.offset = None

            async def download_media(self, message, file, progress_callback=None):
                self.calls += 1
                await file.write(b"a" * (2 * dl.RESUME_ALIGN))
                self.started.set()
                await asyncio.Future()

            async def iter_download(self, message, offset, request_size):
                self.offset = offset
                yield b"b" * (total - offset)

        client = self.backend.client = ResumableClient()
        item = make_item(size=total)

        async def stop():
            task = asyncio.create_task(self.backend._download_worker(item))
            await client.started.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.backend.run_coro(stop()).result(10)
        self.assertEqual(item.status, "Cancelled")
        parts = list(Path(self.backend.config["output_dir"]).rglob("*.part"))
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].stat().st_size, 2 * dl.RESUME_ALIGN)
        self.assertEqual(self.backend.delete_orphan_parts(), (0, 0))
        item.status = "Downloading"
        self.run_worker(item)
        self.assertEqual(item.status, "Completed")
        self.assertEqual(client.offset, 2 * dl.RESUME_ALIGN)
        self.assertEqual(client.calls, 1)
        self.assertEqual(Path(item.output_path).stat().st_size, total)

    def test_transient_failure_keeps_partial_for_retry(self):
        class FailingClient(WritingClient):
            async def download_media(self, message, file, progress_callback=None):
                await file.write(b"part")
                raise OSError("temporary network failure")

        self.backend.client = FailingClient()
        item = make_item(size=4096)
        self.run_worker(item)
        self.assertEqual(item.status, "Queued")
        self.assertEqual(item.retries, 1)
        parts = list(Path(self.backend.config["output_dir"]).rglob("*.part"))
        self.assertEqual(parts[0].read_bytes(), b"part")

    def test_normal_premature_resume_eof_falls_back_to_complete_download(self):
        total = 3 * dl.RESUME_ALIGN
        item = make_item(size=total)
        account = f"session:{os.path.abspath(self.backend.session_path)}"
        storage = DownloadStorage(self.backend.config["output_dir"])
        reservation = storage.prepare(account, item.chat_id, item.chat_name, item.file_name,
                                      media_identity(account, item))
        Path(reservation.temp_path).write_bytes(b"a" * (2 * dl.RESUME_ALIGN))
        storage.release(reservation)

        class ShortResumeClient(WritingClient):
            async def iter_download(self, message, offset, request_size):
                yield b"x" * 4096

        client = self.backend.client = ShortResumeClient(b"b" * total)
        self.run_worker(item)
        self.assertEqual(item.status, "Completed")
        self.assertEqual(client.calls, 1)
        self.assertEqual(Path(item.output_path).read_bytes(), b"b" * total)

    def test_normal_shutdown_retains_resumable_bytes(self):
        started = threading.Event()

        class SlowClient(WritingClient):
            async def download_media(self, message, file, progress_callback=None):
                await file.write(b"part")
                started.set()
                await asyncio.Future()

        self.backend.client = SlowClient()
        item = make_item(size=4096)
        self.backend.run_coro(self.backend._download_worker(item))
        self.assertTrue(started.wait(5))
        self.backend.shutdown()
        self.assertEqual(item.status, "Cancelled")
        parts = list(Path(self.backend.config["output_dir"]).rglob("*.part"))
        self.assertEqual(parts[0].read_bytes(), b"part")

    def test_repeated_cancellation_during_disk_cleanup_releases_ownership(self):
        ready = asyncio.Event()
        checking_partial = threading.Event()
        finish_check = threading.Event()
        original = DownloadStorage.partial_bytes

        class SlowClient(WritingClient):
            async def download_media(self, message, file, progress_callback=None):
                await file.write(b"part")
                ready.set()
                await asyncio.Future()

        def delayed_check(reservation):
            checking_partial.set()
            if not finish_check.wait(5):
                raise TimeoutError("test did not release the partial check")
            return original(reservation)

        self.backend.client = SlowClient()
        item = make_item(size=4096)

        async def stop_twice():
            task = asyncio.create_task(self.backend._download_worker(item))
            await ready.wait()
            task.cancel()
            self.assertTrue(await asyncio.to_thread(checking_partial.wait, 5))
            task.cancel()
            await asyncio.sleep(0)
            finish_check.set()
            await asyncio.gather(task, return_exceptions=True)

        with patch.object(DownloadStorage, "partial_bytes", side_effect=delayed_check):
            self.backend.run_coro(stop_twice()).result(10)
        self.backend.client = WritingClient(b"x" * 4096)
        item.status = "Downloading"
        self.run_worker(item)
        self.assertEqual(item.status, "Completed")

    def test_stale_worker_generation_cannot_mutate_current_item(self):
        item = make_item()
        item.worker_generation = 2
        self.backend.run_coro(self.backend._download_worker(item, 1)).result(5)
        self.assertEqual(item.status, "Downloading")
        self.assertEqual(self.backend.client.calls, 0)

    def test_sink_supports_nonawaiting_photo_writes_off_event_loop(self):
        path = os.path.join(self.temp.name, "photo.bin")
        thread_ids = []
        event_thread = None

        class RecordingStream:
            def __init__(self, stream):
                self.stream = stream

            def write(self, data):
                thread_ids.append(threading.get_ident())
                return self.stream.write(data)

            def flush(self):
                thread_ids.append(threading.get_ident())
                self.stream.flush()

            def close(self):
                self.stream.close()

        async def use_sink():
            nonlocal event_thread
            event_thread = threading.get_ident()
            sink = await AsyncDiskSink.open(path)
            sink.stream = RecordingStream(sink.stream)
            sink.write(b"photo")  # Telethon's cached-photo helper does not await.
            await sink.finish()

        self.backend.run_coro(use_sink()).result(5)
        self.assertEqual(Path(path).read_bytes(), b"photo")
        self.assertTrue(thread_ids)
        self.assertNotIn(event_thread, thread_ids)

    def test_sink_is_compatible_with_installed_telethon_transfer_helpers(self):
        path = os.path.join(self.temp.name, "telethon.bin")
        progress = []

        class FakeTransport:
            async def _iter_download(self, location, **kwargs):
                yield b"first"
                yield b"second"

        async def use_helpers():
            sink = await AsyncDiskSink.open(path)
            await TelegramClient._download_file(
                FakeTransport(), None, sink, file_size=11,
                progress_callback=lambda current, total: progress.append((current, total)))
            await sink.finish()
            self.assertEqual(Path(path).read_bytes(), b"firstsecond")
            photo = await AsyncDiskSink.open(path)
            TelegramClient._download_cached_photo_size(
                None, types.PhotoCachedSize(type="x", w=1, h=1, bytes=b"cached"), photo)
            await photo.finish()

        self.backend.run_coro(use_helpers()).result(5)
        self.assertEqual(progress, [(5, 11), (11, 11)])
        self.assertEqual(Path(path).read_bytes(), b"cached")


if __name__ == "__main__":
    unittest.main()
