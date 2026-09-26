import asyncio, json, os, sys, tempfile
from types import SimpleNamespace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
import interactive_downloader as dl

TMP = tempfile.TemporaryDirectory(prefix='tgdl-scheduler-audit-')
dl.CONFIG_FILE = os.path.join(TMP.name, 'config.json')
def msg(i, ext='.mp4'):
    return SimpleNamespace(id=i, file=SimpleNamespace(ext=ext, name=f'{i}{ext}', size=4))
def chat(i):
    return dl.ChatEntry(i, f'Chat {i}', 'Group', False, i)
def make(name):
    b=dl.TelegramBackend(1, 'fake-hash', os.path.join(TMP.name, 'fake-session'))
    b.config={'output_dir':os.path.join(TMP.name,name),'max_parallel_files':3,'scan_depth':500}
    return b
class BaseClient:
    async def disconnect(self): pass
    async def download_media(self, message, file, progress_callback=None):
        await asyncio.sleep(0)
        with open(file,'wb') as f: f.write(b'test')
        return file

def test_cursor_loss():
    b=make('cursor')
    b._schedule_downloads=lambda:None
    b._check_and_trigger_next_batch=lambda _:None
    b._arm_fetch_retry=lambda _:None
    class Client(BaseClient):
        calls=[]
        async def iter_messages(self, entity, limit=None, offset_id=None):
            self.calls.append(offset_id)
            if len(self.calls)==1:
                yield msg(3)
                raise OSError('synthetic mid-page failure')
            for i in (3,2,1):
                if offset_id is None or i<offset_id: yield msg(i)
    b.client=Client(); b.all_formats_mode=True
    b.pagination_states[1]=dl.ChatPaginationState(1,'Chat 1',1)
    async def run():
        await b._fetch_next_chat_batch(1)
        before={'cursor':b.pagination_states[1].last_msg_id,'queue':[i.msg_id for i in b.download_queue]}
        await b._fetch_next_chat_batch(1)
        return {'after_failure':before,'retry_offsets':b.client.calls,'final_queue':[i.msg_id for i in b.download_queue]}
    try: print('cursor_loss',json.dumps(b.run_coro(run()).result(5)))
    finally: b.shutdown()

def test_global_filters():
    b=make('filter')
    b._spawn=lambda coro,owner=None:coro.close()
    b._schedule_downloads=lambda:None
    b._check_and_trigger_next_batch=lambda _:None
    class Client(BaseClient):
        async def iter_messages(self, entity, **kw):
            yield msg(2,'.mp4'); yield msg(1,'.jpg')
    b.client=Client()
    async def run():
        await b.enqueue_selected_chats([chat(1)],{'.mp4'})
        await b.enqueue_selected_chats([chat(2)],{'.jpg'})
        await b._fetch_next_chat_batch(1)
        return {'chat1_requested':['.mp4'],'chat2_requested':['.jpg'],'chat1_actual':[i.ext for i in b.download_queue]}
    try: print('global_filters',json.dumps(b.run_coro(run()).result(5)))
    finally:b.shutdown()

def test_reenqueue():
    b=make('reenqueue')
    b._schedule_downloads=lambda:None
    b._check_and_trigger_next_batch=lambda _:None
    class Client(BaseClient):
        def __init__(self):self.calls=0;self.ready=asyncio.Event();self.release=asyncio.Event()
        async def iter_messages(self, entity, **kw):
            self.calls+=1
            yield msg(2)
            if self.calls==2:self.ready.set()
            await self.release.wait()
            yield msg(1)
    async def run():
        b.client=Client()
        await b.enqueue_selected_chats([chat(1)],{'.mp4'})
        await asyncio.sleep(0)
        await b.enqueue_selected_chats([chat(1)],{'.mp4'})
        await asyncio.wait_for(b.client.ready.wait(),1)
        b.client.release.set()
        await asyncio.sleep(0.02)
        return {'iterator_calls':b.client.calls,'queue_ids':[i.id for i in b.download_queue],'index_ids':list(b.queue_index)}
    try: print('reenqueue',json.dumps(b.run_coro(run()).result(5)))
    finally:b.shutdown()

def test_cancel_fetch():
    b=make('cancel')
    class Client(BaseClient):
        def __init__(self):self.ready=asyncio.Event();self.release=asyncio.Event();self.downloads=0
        async def iter_messages(self, entity, **kw):
            yield msg(3)
            self.ready.set()
            await self.release.wait()
            yield msg(2); yield msg(1)
        async def download_media(self,*args,**kwargs):
            self.downloads+=1
            return await super().download_media(*args,**kwargs)
    async def run():
        b.client=Client()
        await b.enqueue_selected_chats([chat(1)],{'.mp4'})
        await b.client.ready.wait()
        b.cancel_all()
        at_cancel=len(b.download_queue)
        b.client.release.set()
        await asyncio.sleep(0.05)
        return {'queue_at_cancel':at_cancel,'download_calls_after_cancel':b.client.downloads,'final_statuses':[i.status for i in b.download_queue]}
    try:print('cancel_fetch',json.dumps(b.run_coro(run()).result(5)))
    finally:b.shutdown()

def test_flood_fetch():
    b=make('flood')
    b._schedule_downloads=lambda:None
    b._check_and_trigger_next_batch=lambda _:None
    b._arm_flood_timer=lambda:None
    delays=[]
    b._arm_fetch_retry=lambda _:delays.append(dl.FETCH_BACKOFF[0])
    class Client(BaseClient):
        calls=0
        async def iter_messages(self, entity, **kw):
            self.calls+=1
            raise dl.errors.FloodWaitError(request=None,capture=120)
            yield
    b.client=Client();b.all_formats_mode=True
    b.pagination_states[1]=dl.ChatPaginationState(1,'Chat 1',1)
    async def run():
        import time
        await b._fetch_next_chat_batch(1)
        remaining=b.flood_until-time.monotonic()
        await b._fetch_next_chat_batch(1)
        return {'flood_gate_remaining_seconds':round(remaining),'scheduled_retry_seconds':delays[0],'fetch_calls_while_gate_active':b.client.calls}
    try:print('flood_fetch',json.dumps(b.run_coro(run()).result(5)))
    finally:b.shutdown()

for test in (test_cursor_loss,test_global_filters,test_reenqueue,test_cancel_fetch,test_flood_fetch):test()
TMP.cleanup()
