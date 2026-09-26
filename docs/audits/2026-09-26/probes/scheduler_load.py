import asyncio, json, os, sys, tempfile
from types import SimpleNamespace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
import interactive_downloader as dl
TMP=tempfile.TemporaryDirectory(prefix='tgdl-scheduler-load-')
dl.CONFIG_FILE=os.path.join(TMP.name,'config.json')
def msg(i):return SimpleNamespace(id=i,file=SimpleNamespace(ext='.bin',name=f'{i}.bin',size=4))
def chat(i):return dl.ChatEntry(i,f'Chat {i}','Group',False,i)
def make(name,parallel):
    b=dl.TelegramBackend(1,'fake',os.path.join(TMP.name,'session'))
    b.config={'output_dir':os.path.join(TMP.name,name),'max_parallel_files':parallel,'scan_depth':500}
    return b
class Client:
    async def disconnect(self):pass

def fanout():
    b=make('fanout',1)
    b._schedule_downloads=lambda:None
    class C(Client):
        def __init__(self):self.active=0;self.peak=0;self.ready=asyncio.Event();self.release=asyncio.Event()
        async def iter_messages(self,entity,**kw):
            self.active+=1;self.peak=max(self.peak,self.active)
            if self.active==100:self.ready.set()
            try:
                await self.release.wait()
                yield msg(1)
            finally:self.active-=1
    async def run():
        b.client=C()
        await b.enqueue_selected_chats([chat(i) for i in range(100)],set(),True)
        await asyncio.wait_for(b.client.ready.wait(),1)
        result={'selected_groups':100,'max_parallel_files':1,'simultaneous_history_iterators':b.client.peak}
        b.client.release.set()
        await asyncio.sleep(0)
        return result
    try:print('fanout',json.dumps(b.run_coro(run()).result(5)))
    finally:b.shutdown()

def fairness():
    b=make('fairness',3)
    class C(Client):
        def __init__(self):self.started=[];self.ready=asyncio.Event();self.release=asyncio.Event()
        async def iter_messages(self,entity,**kw):
            for i in range(4,0,-1):yield msg(i)
        async def download_media(self,message,file,progress_callback=None):
            self.started.append(file)
            if len(self.started)==3:self.ready.set()
            await self.release.wait()
            with open(file,'wb') as f:f.write(b'test')
    async def run():
        b.client=C()
        await b.enqueue_selected_chats([chat(1),chat(2)],set(),True)
        await asyncio.wait_for(b.client.ready.wait(),1)
        return {'active_chat_ids':[i.chat_id for i in b.download_queue if i.status=='Downloading'],'second_group_queued':sum(i.chat_id==2 and i.status=='Queued' for i in b.download_queue),'first_group_blocked_downloads':len(b.client.started)}
    try:print('fairness',json.dumps(b.run_coro(run()).result(5)))
    finally:b.shutdown()
fanout();fairness();TMP.cleanup()
