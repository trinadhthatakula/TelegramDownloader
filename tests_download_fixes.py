"""Regressions from the download review, using temporary files and fake transport."""
import asyncio
import datetime
import errno
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon import TelegramClient, errors, functions, types
from telethon.client.downloads import _CdnRedirect
from telethon.sessions import MemorySession

import download_storage as storage
import interactive_downloader as dl
from tests_scheduler import SchedulerTests, Client, chat, message
from tests_storage import WorkerStorageTests, StorageTests, WritingClient, make_item


class DownloadFixTests(unittest.TestCase):
    def test_uneven_chats_fill_available_slots(self):
        fixture = SchedulerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        async def run():
            class Uneven(Client):
                async def iter_messages(self, entity, **kwargs):
                    for mid in range(12 if entity == 1 else 1, 0, -1):
                        yield message(mid)
            async def hold(item, generation=None):
                await asyncio.Future()
            backend = fixture.backend
            backend.client, backend._download_worker = Uneven(), hold
            backend.config['max_parallel_files'] = 10
            await backend.enqueue_selected_chats([chat(1), chat(2), chat(3)], {'.mp4'})
            for _ in range(30):
                await asyncio.sleep(0)
            self.assertEqual(len(backend._active_workers), 10)
            self.assertEqual(set(backend._active_counts), {1, 2, 3})
            await backend.control_all('cancel')
        fixture.run_async(run())

    def test_hash_does_not_block_another_reservation(self):
        fixture = StorageTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        first = fixture.reserve(make_item())
        Path(first.temp_path).write_bytes(b'data')
        entered, release = threading.Event(), threading.Event()
        original = storage._digest
        def hold(path):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test hash was not released')
            return original(path)
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(storage, '_digest', hold):
            completing = pool.submit(fixture.storage.finalize, first)
            try:
                self.assertTrue(entered.wait(1))
                other = pool.submit(fixture.reserve, make_item(2)).result(1)
                self.assertNotEqual(first.save_path, other.save_path)
            finally:
                release.set()
            completing.result(2)

    def worker_fixture(self):
        fixture = WorkerStorageTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def test_cdn_resume_restarts_through_normal_downloader(self):
        fixture = self.worker_fixture()
        class Redirect(WritingClient):
            async def iter_download(self, *args, **kwargs):
                raise _CdnRedirect()
                yield
        fixture.backend.client = client = Redirect(b'x' * (2 * dl.RESUME_ALIGN))
        item = make_item(size=len(client.data))
        path = Path(fixture.temp.name) / 'resume.part'
        path.write_bytes(b'y' * dl.RESUME_ALIGN)
        fixture.backend.run_coro(fixture.backend._transfer(item, str(path), '')).result(5)
        self.assertEqual(client.calls, 1)
        self.assertEqual(path.read_bytes(), client.data)

    def test_cancel_returns_real_telethon_borrowed_sender(self):
        fixture = self.worker_fixture()
        async def run():
            session = MemorySession()
            session.set_dc(1, '127.0.0.1', 443)
            client = TelegramClient(session, 1, 'fake')
            document = types.Document(id=1, access_hash=1, file_reference=b'r',
                date=datetime.datetime.now(datetime.timezone.utc), mime_type='video/mp4',
                size=3 * dl.RESUME_ALIGN, dc_id=2, attributes=[])
            item = make_item(size=document.size)
            item.message = types.Message(id=1, peer_id=types.PeerChannel(123),
                                        media=types.MessageMediaDocument(document=document))
            entered, returned = asyncio.Event(), []
            sender = object()
            client._borrow_exported_sender = AsyncMock(return_value=sender)
            async def give_back(value):
                returned.append(value)
            async def request(*args, **kwargs):
                entered.set()
                await asyncio.Future()
            client._return_exported_sender, client._call = give_back, request
            fixture.backend.client = client
            path = Path(fixture.temp.name) / 'resume.part'
            path.write_bytes(b'x' * dl.RESUME_ALIGN)
            task = asyncio.create_task(fixture.backend._download_resumed(item, str(path),
                                        dl.RESUME_ALIGN, lambda *args: None))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(returned, [sender])
        fixture.backend.run_coro(run()).result(5)

    def test_rpc_retry_exhaustion_preserves_error_and_requeues(self):
        fixture = self.worker_fixture()
        async def exhaust():
            client = TelegramClient(MemorySession(), 1, 'fake', request_retries=0,
                                    raise_last_call_error=True)
            class Sender:
                def send(self, request, ordered=False):
                    future = asyncio.get_running_loop().create_future()
                    future.set_exception(errors.ServerError(request, 'temporary server error'))
                    return future
            with patch('telethon.client.users.asyncio.sleep', new=AsyncMock()):
                try:
                    await client._call(Sender(), functions.help.GetConfigRequest())
                except errors.ServerError as exc:
                    return exc
            self.fail('server error was lost')
        error = fixture.backend.run_coro(exhaust()).result(3)
        class Flaky(WritingClient):
            async def download_media(self, *args, **kwargs):
                raise error
        fixture.backend.client = Flaky()
        item = make_item()
        fixture.run_worker(item)
        self.assertEqual((item.status, item.retries), ('Queued', 1))

    def test_disk_full_does_not_blindly_retry(self):
        fixture = self.worker_fixture()
        class Full(WritingClient):
            async def download_media(self, *args, **kwargs):
                raise OSError(errno.ENOSPC, 'No space left on device')
        fixture.backend.client = Full()
        item = make_item()
        fixture.run_worker(item)
        self.assertEqual((item.status, item.retries), ('Failed', 0))


if __name__ == '__main__':
    unittest.main()
