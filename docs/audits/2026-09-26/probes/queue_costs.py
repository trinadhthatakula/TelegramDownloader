import asyncio
import json
import logging
import os
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
import interactive_downloader as dl
logging.disable(logging.CRITICAL)

def median_ms(fn, times=7):
    samples=[]
    for _ in range(times):
        start=time.perf_counter()
        fn()
        samples.append((time.perf_counter()-start)*1000)
    return round(statistics.median(samples),3)

with tempfile.TemporaryDirectory() as directory:
    dl.CONFIG_FILE=os.path.join(directory,'config.json')
    b=dl.TelegramBackend(1,'not-real',os.path.join(directory,'unused'))
    b.config['max_parallel_files']=3
    result=[]
    try:
        for n in (1000,10000,100000,300000):
            b.download_queue=[dl.DownloadItem(str(i),i%10,'Synthetic',None,'file.bin',i,'.bin',
                file_size=1024,status='Completed' if i<n-1003 else ('Downloading' if i>=n-3 else 'Queued'))
                for i in range(n)]
            b.pagination_states={0:dl.ChatPaginationState(0,'Synthetic',entity=None)}
            r={'retained_items':n,
               'ui_summary_scan_ms':median_ms(b.queue_stats),
               'ui_group_filter_scan_ms':median_ms(lambda:b.snapshot_queue(0)),
               'ui_all_list_copy_ms':median_ms(b.snapshot_queue),
               'scheduler_when_all_slots_busy_ms':median_ms(b._schedule_downloads),
               'refill_count_scan_ms':median_ms(lambda:b._check_and_trigger_next_batch(0))}
            result.append(r)
        print(json.dumps({'probe':'Headless CPU only; no Tk, network or disk transfer time included. 10 chats, 3 active files.',
                          'measurements':result},indent=2))
    finally:
        b.shutdown()
