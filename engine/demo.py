"""Explicit offline demo transport. Never opens a real Telegram connection."""
import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from telethon import types, errors
from engine.service import DownloadService

DEMO_NAMES = {1001: 'Animals', 1002: 'Design inspiration', 1003: 'Weekend adventures', 1004: 'Saved recipes', 1005: 'Photography club', 1006: 'Music discoveries', 1007: 'Project archive'}


class DemoClient:
    def __init__(self):
        self.authorized = True

    async def disconnect(self):
        pass

    async def get_me(self):
        return SimpleNamespace(id=42, first_name='Alex', last_name='Morgan', username='offline_demo')

    async def download_profile_photo(self, *args, **kwargs):
        return None

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash='demo', timeout=5)

    async def sign_in(self, **values):
        if values.get('code') == '00000':
            raise errors.SessionPasswordNeededError(None)
        self.authorized = True

    async def log_out(self):
        self.authorized = False
        return True

    async def iter_dialogs(self, archived=False, **kwargs):
        for id, name in DEMO_NAMES.items():
            if archived != (id == 1007):
                continue
            yield SimpleNamespace(id=id, name=name, is_group=True, is_channel=True, is_user=False,
                                  archived=archived, entity=SimpleNamespace(forum=id == 1001, id=id))

    def message(self, chat, mid):
        ext = '.jpg' if mid <= 200 else '.mp4' if mid <= 249 else '.pdf'
        name = f"{('woodland' if chat == 1001 else 'collection')}_{mid:04d}{ext}"
        mime = 'image/jpeg' if ext == '.jpg' else 'video/mp4' if ext == '.mp4' else 'application/pdf'
        doc = types.Document(id=chat * 10000 + mid, access_hash=1, file_reference=b'demo',
                             date=datetime(2026, 1, 1, tzinfo=timezone.utc), mime_type=mime, size=512*1024,
                             dc_id=1, attributes=[types.DocumentAttributeFilename(name)])
        return types.Message(id=mid, peer_id=types.PeerChannel(chat),
            date=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=mid),
            media=types.MessageMediaDocument(document=doc),
            reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=10 if mid % 2 else 20)
                     if chat == 1001 else None)

    async def iter_messages(self, entity, limit=200, offset_id=0, **kwargs):
        id = getattr(entity, 'id', entity)
        high = min(261, offset_id - 1 if offset_id else 261)
        for mid in range(high, max(0, high-limit), -1):
            if mid % 30 == 0:
                await asyncio.sleep(0.02)
            yield self.message(id, mid)

    async def get_messages(self, entity, ids):
        return self.message(getattr(entity, 'id', entity), ids)

    async def __call__(self, request):
        if hasattr(request, 'topics'):
            return SimpleNamespace(topics=[SimpleNamespace(id=id, title={10:'Dogs',20:'Cats',1:'General'}.get(id, f'Topic {id}')) for id in request.topics])
        if hasattr(request, 'filters'):
            return [SimpleNamespace(filter=f, count=200 if 'Photos' in type(f).__name__ else 49, inexact=False) for f in request.filters]
        raise ValueError('Not available in the offline demo')

    async def download_media(self, message, file, progress_callback=None):
        total = message.file.size
        for offset in range(0, total, 32768):
            await asyncio.sleep(0.06)
            await file.write(bytes([message.id % 256]) * min(32768, total-offset))
            if progress_callback:
                progress_callback(min(offset+32768, total), total)

    async def iter_download(self, message, offset, request_size):
        while offset < message.file.size:
            await asyncio.sleep(0.06)
            data = bytes([message.id % 256]) * min(request_size, message.file.size-offset)
            offset += len(data)
            yield data


class DemoService(DownloadService):
    async def _connect_account(self):
        self.client = DemoClient()
        if self.profile_id == 'd' * 32:
            await self._signed_in()
        else:
            self.auth_event('phone', 'Offline demo: enter any phone number, then any code. Code 00000 opens two-step verification.')
            self.client.authorized = False
