"""Catalog, selection, topics, and restart checks without a Telegram account."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from telethon import types
from engine.demo import DemoClient, DemoService
from engine.media import category, topic_id
from telegram_auth import AccountStore


class CatalogServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = AccountStore(self.tmp.name)
        self.store.data = {'active': 'd'*32, 'profiles': {'d'*32: {'label':'Demo','api_id':42,'api_hash':'a'*32,'session':str(self.root/'session')}}}
        self.backend = self.make_service()
        self.run_async(self.backend.init_client())
        self.run_async(self.backend.fetch_dialogs())

    def make_service(self):
        service = DemoService(42, 'a'*32, str(self.root/'session'), self.store,
                              str(self.root/'catalog.sqlite3'), str(self.root/'config.json'))
        service.config['output_dir'] = str(self.root/'out')
        return service

    def tearDown(self):
        self.run_async(self.backend.close_service())
        self.backend.shutdown()
        self.tmp.cleanup()

    def run_async(self, coro):
        return self.backend.run_coro(coro).result(15)

    def spec(self, **extra):
        return {'scopes':[{'chat_id':1001}], 'quantities':{'images':2,'videos':1}, **extra}

    def test_more_than_2000_messages_complete_and_incremental_no_duplicates(self):
        class History(DemoClient):
            async def iter_messages(self, entity, limit=200, offset_id=0, **kwargs):
                high = min(2507, offset_id-1 if offset_id else 2507)
                for mid in range(high, max(0, high-limit), -1):
                    yield self.message(getattr(entity, 'id', entity), mid)
        self.backend.client = History()
        self.run_async(self.backend.scan([1001]))
        state = self.backend.catalog.scan_state('user:42', 1001)
        self.assertEqual((state['scanned'], state['status']), (2507, 'complete'))
        first = self.backend.catalog.summary('user:42', [{'chat_id':1001}])
        self.assertEqual(sum(r['found'] for r in first), 2507)
        self.run_async(self.backend.scan([1001]))
        self.assertEqual(self.backend.catalog.summary('user:42', [{'chat_id':1001}]), first)

    def test_quantities_parent_topic_union_and_topic_directories(self):
        self.run_async(self.backend.scan([1001]))
        preview = self.run_async(self.backend.preview(self.spec(scopes=[{'chat_id':1001}, {'chat_id':1001, 'topic_id':10}])))
        self.assertEqual(preview['counts'], {'images':2,'videos':1})
        self.run_async(self.backend.resume_job(preview['id']))
        self.run_async(self.backend.wait_for_scheduler_idle())
        self.run_async(self.backend.flush_jobs())
        files = [p for p in (self.root/'out').rglob('*') if p.suffix in ('.jpg','.mp4')]
        self.assertEqual(len(files), 3)
        self.assertEqual({p.parent.name for p in files}, {'Cats','Dogs'})
        self.assertEqual({i.status for i in self.backend.download_queue}, {'Completed'})

    def test_resume_after_restart_preserves_frozen_identity_and_account(self):
        self.run_async(self.backend.scan([1001]))
        preview = self.run_async(self.backend.preview(self.spec(quantities={'images':1})))
        self.backend.shutdown()
        self.backend = self.make_service()
        self.run_async(self.backend.init_client())
        self.run_async(self.backend.fetch_dialogs())
        self.run_async(self.backend.resume_job(preview['id']))
        self.run_async(self.backend.wait_for_scheduler_idle())
        self.assertEqual(self.backend.download_queue[0].status, 'Completed')
        with self.assertRaises(ValueError):
            self.backend.catalog.job('user:999', preview['id'])

    def test_existing_verification_excludes_completed_from_quantity(self):
        self.run_async(self.backend.scan([1001]))
        first = self.run_async(self.backend.preview(self.spec(quantities={'images':1})))
        self.run_async(self.backend.resume_job(first['id']))
        self.run_async(self.backend.wait_for_scheduler_idle())
        second = self.run_async(self.backend.preview(self.spec(quantities={'images':1})))
        r1 = self.backend.catalog.job_rows('user:42', first['id'])[0]
        r2 = self.backend.catalog.job_rows('user:42', second['id'])[0]
        self.assertNotEqual(r1['msg'], r2['msg'])

    def test_cancelled_scan_checkpoints_and_resumes(self):
        gate = asyncio.Event()
        class Held(DemoClient):
            async def iter_messages(self, entity, limit=200, offset_id=0, **kwargs):
                if offset_id:
                    gate.set()
                    await asyncio.Future()
                async for msg in super().iter_messages(entity, limit, offset_id):
                    yield msg
        self.backend.client = Held()
        async def run():
            task = asyncio.create_task(self.backend.scan([1001]))
            await asyncio.wait_for(gate.wait(), 3)
            await self.backend.cancel_scans()
            await asyncio.gather(task, return_exceptions=True)
        self.run_async(run())
        state = self.backend.catalog.scan_state('user:42', 1001)
        self.assertEqual(state['status'], 'paused')
        self.assertEqual(state['scanned'], 200)
        self.backend.client = DemoClient()
        self.run_async(self.backend.scan([1001]))
        self.assertEqual(self.backend.catalog.scan_state('user:42', 1001)['scanned'], 261)

    def test_incomplete_scan_cannot_claim_final_quantities(self):
        with self.assertRaises(ValueError):
            self.run_async(self.backend.preview(self.spec()))

    def test_topic_rename_does_not_move_reserved_files(self):
        from download_storage import DownloadStorage, media_identity
        from tests_storage import make_item
        store = DownloadStorage(self.root/'files')
        a = make_item()
        r1 = store.prepare('account',123,'Animals','one.mp4',media_identity('account',a),10,'Dogs')
        store.release(r1)
        b = make_item(2)
        r2 = store.prepare('account',123,'Animals','two.mp4',media_identity('account',b),10,'Puppies')
        self.assertEqual(Path(r1.save_path).parent, Path(r2.save_path).parent)
        c = make_item(3)
        r3 = store.prepare('account',123,'Animals','three.mp4',media_identity('account',c),20,'dogs')
        self.assertNotEqual(Path(r1.save_path).parent, Path(r3.save_path).parent)

    def test_classification_and_ordinary_replies(self):
        msg = DemoClient().message(1001, 1)
        self.assertEqual(category(msg), 'images')
        self.assertEqual(topic_id(msg),10)
        msg.reply_to = types.MessageReplyHeader(reply_to_msg_id=500)
        self.assertIsNone(topic_id(msg))
        self.assertEqual(topic_id(msg, True), 1)
        msg.reply_to = types.MessageReplyHeader(forum_topic=True,reply_to_msg_id=999,reply_to_top_id=10)
        self.assertEqual(topic_id(msg),10)

    def test_resume_job_reconciles_missing_completed_file(self):
        self.run_async(self.backend.scan([1001]))
        preview = self.run_async(self.backend.preview(self.spec(quantities={'images':1})))
        job_id = preview['id']
        self.run_async(self.backend.resume_job(job_id))
        self.run_async(self.backend.wait_for_scheduler_idle())
        self.run_async(self.backend.flush_jobs())
        self.assertEqual(self.backend.download_queue[0].status, 'Completed')

        # Find the downloaded file on disk and remove it
        files = [p for p in (self.root/'out').rglob('*.jpg')]
        self.assertEqual(len(files), 1)
        files[0].unlink()

        # Clear active queue in memory to simulate a fresh run/restart
        self.backend.download_queue.clear()
        self.backend.queue_index.clear()

        # Resume the job again: manifest reconciliation notices file is gone and requeues it!
        self.run_async(self.backend.resume_job(job_id))
        self.assertEqual(len(self.backend.download_queue), 1)
        self.assertIn(self.backend.download_queue[0].status, ('Queued', 'Downloading', 'Completed'))
        self.run_async(self.backend.wait_for_scheduler_idle())
        self.assertEqual(self.backend.download_queue[0].status, 'Completed')
        self.assertTrue(files[0].exists())

    def test_resume_job_conflict_preflight(self):
        self.run_async(self.backend.scan([1001]))
        p1 = self.run_async(self.backend.preview(self.spec(quantities={'images':1})))
        # Create second preview for same item
        p2 = self.run_async(self.backend.preview(self.spec(quantities={'images':1}, skip_existing=False)))
        self.run_async(self.backend.resume_job(p1['id']))
        # Attempting to resume p2 which conflicts with active items in p1 must be rejected before mutating queue
        initial_queue_len = len(self.backend.download_queue)
        with self.assertRaises(ValueError):
            self.run_async(self.backend.resume_job(p2['id']))
        self.assertEqual(len(self.backend.download_queue), initial_queue_len)


if __name__ == '__main__':
    unittest.main()
