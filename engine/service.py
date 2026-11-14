"""Application service shared by the local API and the durable worker lifecycle."""
import asyncio
import base64
import json
import os
import shutil
import time
import uuid
from dataclasses import asdict

from telethon import errors, functions, types

from download_storage import MediaIdentity, get_storage, media_identity
from engine.catalog import Catalog
from engine.core import ChatPaginationState, DownloadItem, TelegramBackend
from engine.media import CATEGORIES, category, is_forum


class DownloadService(TelegramBackend):
    def __init__(self, api_id, api_hash, session_path, accounts, catalog_path, config_file=None):
        self.catalog = Catalog(catalog_path)
        self.dialogs = {}
        self.topic_titles = {}
        self.login_state = {'stage': 'credentials', 'message': 'Connect a Telegram account to get started.'}
        self.profile = None
        self.issues = []
        self.operations = {}
        self.scan_tasks = {}
        self.quick_counts = {}
        self._dirty_items = {}
        self._flush_handle = None
        self._catalog_gate = asyncio.Semaphore(3)
        self._plan_lock = asyncio.Lock()
        self._job_lock = asyncio.Lock()
        self.previews = {}
        super().__init__(api_id, api_hash, session_path, accounts, config_file)

    @property
    def account_key(self):
        if not self.authorized or not getattr(self, 'current_user_id', None):
            raise ValueError('Connect a Telegram account first')
        return f'user:{self.current_user_id}'

    def emit_event(self, event_type, data=None):
        if event_type == 'dialogs_loaded':
            self.dialogs = {d.id: d for d in data}
        elif event_type == 'login_state':
            self.login_state = dict(data)
        elif event_type == 'account_profile':
            self.profile = {**data, 'photo': 'data:image/jpeg;base64,' + base64.b64encode(data['photo']).decode()
                            if data.get('photo') else None}
        elif event_type == 'error':
            self.issues.append({'id': uuid.uuid4().hex, 'message': str(data), 'time': time.time()})
            self.issues = self.issues[-100:]
        if event_type == 'item_updated' and isinstance(data, DownloadItem) and data.job_id:
            self._dirty_items[(data.job_id, data.chat_id, data.msg_id)] = (
                data.status, data.downloaded_bytes, getattr(data, 'last_error', ''),
                getattr(data, 'output_path', ''), data.job_id, data.chat_id, data.msg_id)
            if self._flush_handle is None and not self._shutting_down:
                self._flush_handle = self.loop.call_later(0.3, lambda: self._spawn(self.flush_jobs()))
        super().emit_event(event_type, data)

    async def flush_jobs(self):
        if self._flush_handle:
            self._flush_handle.cancel()
        self._flush_handle = None
        rows = list(self._dirty_items.values())
        self._dirty_items.clear()
        if rows:
            try:
                await self._disk_operation(self.catalog.update_items, rows)
            except BaseException:
                for row in rows:
                    self._dirty_items.setdefault((row[4], row[5], row[6]), row)
                raise

    async def _reset_account_work(self):
        tasks = list(self.scan_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super()._reset_account_work()
        await self.flush_jobs()
        self.current_user_id = None
        self.dialogs.clear()
        self.topic_titles.clear()
        self.quick_counts.clear()
        self.previews.clear()
        self.profile = None
        self.operations.clear()

    async def fetch_dialogs(self):
        await super().fetch_dialogs()
        if self.authorized:
            await self._disk_operation(self.catalog.save_chats, self.account_key, [
                {'id': d.id, 'name': d.name, 'kind': d.kind, 'archived': d.archived,
                 'forum': is_forum(d.entity)} for d in self.dialogs.values()])

    def start_operation(self, label, coroutine):
        id = uuid.uuid4().hex
        op = {'id': id, 'label': label, 'status': 'running', 'message': '', 'result': None}
        self.operations[id] = op
        # Keep recent operation state bounded, retaining active work.
        for old in list(self.operations)[:-50]:
            if self.operations[old]['status'] != 'running':
                del self.operations[old]
        async def run():
            try:
                op['result'] = await coroutine
                op['status'] = 'complete'
            except asyncio.CancelledError:
                op['status'] = 'cancelled'
                raise
            except Exception as exc:  # noqa: BLE001
                op['status'], op['message'] = 'failed', str(exc)
                self.report_error(f'{label}: {exc}')
            finally:
                self.emit_event('operation', op)
        self._spawn(run())
        return id

    def _dialog(self, chat_id):
        dialog = self.dialogs.get(chat_id)
        if dialog is None:
            raise ValueError('Refresh chats and select a chat available to this account')
        return dialog

    async def get_counters(self, chat_id, topic_id=None):
        dialog = self._dialog(chat_id)
        filters = [types.InputMessagesFilterPhotos(), types.InputMessagesFilterVideo(),
                   types.InputMessagesFilterDocument(), types.InputMessagesFilterMusic(),
                   types.InputMessagesFilterVoice(), types.InputMessagesFilterGif()]
        while time.monotonic() < self.flood_until:
            await asyncio.sleep(min(1, self.flood_until - time.monotonic()))
        try:
            result = await self.client(functions.messages.GetSearchCountersRequest(
                peer=dialog.entity, filters=filters, top_msg_id=topic_id))
            counts = [{'filter': type(r.filter).__name__, 'count': r.count,
                       'approximate': bool(getattr(r, 'inexact', False))} for r in result]
            self.quick_counts[f'{chat_id}:{topic_id}'] = counts
            return counts
        except errors.FloodError as exc:
            self._enter_flood_wait(getattr(exc, 'seconds', 1))
            return []

    async def scan(self, chat_ids, full=False):
        account = self.account_key
        async def one(chat_id):
            existing = self.scan_tasks.get(chat_id)
            if existing:
                return await asyncio.shield(existing)
            task = asyncio.create_task(self._scan_chat(account, self._dialog(chat_id), full))
            self.scan_tasks[chat_id] = task
            try:
                return await task
            finally:
                if self.scan_tasks.get(chat_id) is task:
                    del self.scan_tasks[chat_id]
        return await asyncio.gather(*(one(id) for id in dict.fromkeys(chat_ids)))

    async def cancel_scans(self):
        tasks = list(self.scan_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _scan_chat(self, account, dialog, full):
        async with self._catalog_gate:
            state = await self._disk_operation(self.catalog.begin_scan, account, dialog.id, full)
            self.topic_titles.pop(dialog.id, None)
            attempts = 0
            try:
                while True:
                    while time.monotonic() < self.flood_until:
                        await asyncio.sleep(min(1, self.flood_until - time.monotonic()))
                    if getattr(self, '_active_workers', {}):
                        await asyncio.sleep(0.1)
                    rows, items, scanned = [], [], 0
                    cursor, upper = state['cursor'], state['upper']
                    reached = False
                    try:
                        iterator = self.client.iter_messages(dialog.entity, limit=200, offset_id=cursor or 0)
                        async for message in iterator:
                            if state['boundary'] and message.id <= state['boundary']:
                                reached = True
                                break
                            scanned += 1
                            cursor = message.id
                            upper = max(upper, message.id)
                            if not message.file:
                                continue
                            job_state = ChatPaginationState(dialog.id, dialog.name, entity=dialog.entity)
                            item = self._make_download_item(job_state, message, (message.file.ext or '').lower())
                            identity = media_identity(account, item)
                            items.append(item)
                            date = getattr(message, 'date', None)
                            rows.append({'msg': message.id, 'identity': identity.key, 'metadata': identity.metadata,
                                'category': category(message), 'topic': item.topic_id,
                                'name': item.file_name, 'ext': item.ext, 'size': item.file_size,
                                'date': date.isoformat() if date else ''})
                        await self._resolve_item_topics(dialog.id, dialog.entity, items)
                        complete = reached or scanned < 200
                        await self._disk_operation(self.catalog.commit_page, account, dialog.id, rows,
                                                   cursor, upper, scanned, complete)
                        await self._disk_operation(self.catalog.save_topics, account, dialog.id,
                                                   self.topic_titles.get(dialog.id, {}))
                        state.update(cursor=cursor, upper=upper)
                        attempts = 0
                        self.emit_event('inventory', {'chat_id': dialog.id, 'complete': complete})
                        if complete:
                            return {'chat_id': dialog.id, 'status': 'complete'}
                        await asyncio.sleep(0)
                    except errors.FloodError as exc:
                        self._enter_flood_wait(getattr(exc, 'seconds', 1))
                    except (errors.UnauthorizedError, errors.AuthKeyError):
                        # Let the operation fail; account reset cannot await itself.
                        raise ValueError('Telegram session expired. Reconnect the account.') from None
                    except (OSError, errors.RPCError):
                        attempts += 1
                        if attempts > 5:
                            raise
                        await asyncio.sleep(min(30, 2 ** attempts))
                    finally:
                        close = getattr(locals().get('iterator'), 'aclose', None)
                        if close:
                            await close()
            except asyncio.CancelledError:
                await self._disk_operation(self.catalog.stop_scan, account, dialog.id, 'paused')
                raise
            except Exception as exc:
                await self._disk_operation(self.catalog.stop_scan, account, dialog.id, 'failed', str(exc))
                raise

    @staticmethod
    def record_identity(row):
        meta = json.loads(row['metadata'])
        return MediaIdentity(row['identity'], row['size'], meta['kind'] != 'photo' and row['size'] is not None,
                             row['metadata'])

    async def preview(self, spec):
        async with self._plan_lock:
            account = self.account_key
            generation = self._account_generation
            scopes = spec['scopes']
            for scope in scopes:
                state = await self._disk_operation(self.catalog.scan_state, account, scope['chat_id'])
                if not state or state['status'] != 'complete':
                    raise ValueError('Finish scanning the selected chats before choosing an exact quantity')
            root = os.path.realpath(spec.get('output_dir') or self.config['output_dir'])
            store = await self._disk_operation(get_storage, root)
            limits = spec.get('quantities', {})
            if not limits or any(c not in CATEGORIES or (n is not None and (not isinstance(n, int) or n < 0))
                                 for c, n in limits.items()):
                raise ValueError('Choose valid media categories and non-negative quantities')
            selected, counts, offset, total_bytes, unknown = [], {}, 0, 0, 0
            topic_cache = {}
            chat_names = {}
            busy = {item.expected_identity for item in self.download_queue
                    if item.status in ('Queued', 'Downloading', 'Paused') and item.expected_identity}
            while True:
                batch = await self._disk_operation(self.catalog.rows, account, scopes, spec.get('filters'),
                                                   spec.get('order', 'newest'), offset, 200)
                if not batch:
                    break
                verification = []
                for row in batch:
                    if row['category'] not in limits:
                        continue
                    ok = await self._disk_operation(store.verified, self.record_identity(row))
                    verification.append((row, ok))
                    cat = row['category']
                    limit = limits[cat]
                    if (ok and spec.get('skip_existing', True)) or row['identity'] in busy:
                        continue
                    if limit is not None and counts.get(cat, 0) >= limit:
                        continue
                    if row['chat'] not in chat_names:
                        chat_names[row['chat']] = self._dialog(row['chat']).name
                    row['chat_name'] = chat_names[row['chat']]
                    if row['chat'] not in topic_cache:
                        topics = await self._disk_operation(self.catalog.topics, account, row['chat'])
                        topic_cache[row['chat']] = {t['id']: t['title'] for t in topics}
                    row['topic_name'] = topic_cache[row['chat']].get(row['topic'])
                    selected.append(row)
                    counts[cat] = counts.get(cat, 0) + 1
                    total_bytes += row['size'] or 0
                    unknown += int(row['size'] is None)
                await self._disk_operation(self.catalog.mark_verified, account, verification, root)
                offset += len(batch)
                if generation != self._account_generation:
                    raise asyncio.CancelledError()
            req_id = spec.get('request_id') or spec.get('id') or uuid.uuid4().hex
            # Store potentially large frozen selections on disk, not in browser state.
            job = await self._disk_operation(self.catalog.create_job, account, req_id, spec, root, selected)
            self.previews[job] = {'account': account, 'generation': generation}
            return {'id': job, 'counts': counts, 'total': len(selected), 'bytes': total_bytes,
                    'free_bytes': shutil.disk_usage(root).free, 'unknown_sizes': unknown, 'output_dir': root,
                    'sample': [{'chat': r['chat_name'], 'topic': r['topic_name'], 'file': r['name']}
                               for r in selected[:8]]}

    async def resume_job(self, job_id):
        async with self._job_lock:
            account = self.account_key
            generation = self._account_generation
            job = await self._disk_operation(self.catalog.job, account, job_id)
            if not os.path.isdir(job['output_dir']):
                raise ValueError('The original download directory is missing. Reconnect its drive before resuming.')
            store = await self._disk_operation(get_storage, job['output_dir'])

            # Preflight check across all job rows to ensure no conflicts with items belonging to other jobs
            all_job_rows = await self._disk_operation(self.catalog.job_rows, account, job_id, 0, 1000000)
            for saved in all_job_rows:
                r = json.loads(saved['record'])
                item_id = f"{r['chat']}_{r['msg']}"
                existing = self.queue_index.get(item_id)
                if existing and existing.job_id != job_id:
                    raise ValueError(f"Media '{r['name']}' is already queued by another job ({existing.job_id})")

            self._ensure_scheduler()
            offset = admitted = 0
            while True:
                batch = await self._disk_operation(self.catalog.job_rows, account, job_id, offset, 200)
                if not batch:
                    break
                added = []
                for saved in batch:
                    r = json.loads(saved['record'])
                    if saved['status'] in ('Completed', 'Skipped (Exists)'):
                        ok = await self._disk_operation(store.verified, self.record_identity(r))
                        if ok:
                            continue
                    id = f"{r['chat']}_{r['msg']}"
                    existing = self.queue_index.get(id)
                    if existing:
                        if existing.status in ('Paused', 'Cancelled') or existing.status.startswith('Failed'):
                            existing.status = 'Queued'
                            existing.retries = 0
                            state = self.pagination_states[existing.chat_id]
                            state.paused = state.cancelled = False
                            self._put_ready(existing)
                            self.emit_event('item_updated', existing)
                        continue
                    dialog = self._dialog(r['chat'])
                    state = self.pagination_states.get(r['chat'])
                    if state is None:
                        state = ChatPaginationState(r['chat'], dialog.name, entity=dialog.entity,
                                                    has_more=False, output_dir=job['output_dir'])
                        self.pagination_states[r['chat']] = state
                    state.paused = state.cancelled = False
                    item = DownloadItem(id, r['chat'], dialog.name, None, r['name'], r['msg'], r['ext'], r['size'],
                        output_dir=job['output_dir'], topic_id=r['topic'], topic_name=r['topic_name'],
                        expected_identity=r['identity'], job_id=job_id)
                    self.download_queue.append(item)
                    self.queue_index[id] = item
                    self._put_ready(item)
                    added.append(item)
                    admitted += 1
                if added:
                    self._queue_items_added(added)
                    for item in added:
                        self.emit_event('item_updated', item)
                offset += len(batch)
                if generation != self._account_generation:
                    raise asyncio.CancelledError()
            await self.flush_jobs()
            self._schedule_downloads()
            return {'queued': admitted}

    async def control_scope(self, action, chat_id=None, topic_id=None):
        if topic_id is None:
            result = await (self.control_all(action) if chat_id is None else self.control_chat(chat_id, action))
        else:
            self._ensure_scheduler()
            state = self.pagination_states.get(chat_id)
            if state is None:
                return 0
            lock = self._chat_control_locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                items = [i for i in self._chat_items(chat_id) if i.topic_id == topic_id]
                result = 0
                for item in items:
                    if action in ('pause', 'cancel'):
                        if item.status in ('Queued', 'Downloading', 'Paused'):
                            await self._cancel_item_locked(item.id)
                            if action == 'pause':
                                item.status = 'Paused'
                                self.emit_event('item_updated', item)
                            result += 1
                    elif action in ('resume', 'retry'):
                        eligible = item.status in ('Paused', 'Cancelled') if action == 'resume' else item.status.startswith('Failed')
                        if eligible:
                            state.paused = state.cancelled = False
                            item.status, item.retries = 'Queued', 0
                            self._put_ready(item)
                            self.emit_event('item_updated', item)
                            result += 1
                    elif action == 'clear' and item.status in ('Completed', 'Skipped (Exists)') and item.id not in self._active_workers:
                        self.download_queue.remove(item)
                        self.queue_index.pop(item.id, None)
                        result += 1
                if action == 'clear':
                    self._queue_items_removed([])
                self._schedule_downloads()
        await self.flush_jobs()
        return result

    async def state(self, chat_id=None, topic_id=None, offset=0, limit=50):
        account = self.account_key if self.authorized else None
        if topic_id is None:
            total, items = self.snapshot_queue_window(chat_id, offset, limit)
        else:
            scoped = [i for i in self._chat_items(chat_id) if i.topic_id == topic_id]
            total, items = len(scoped), scoped[offset:offset + limit]
        profiles = [] if not self.accounts else [
            {'id': id, 'label': p['label'], 'api_id': p['api_id']}
            for id, p in self.accounts.data['profiles'].items()]
        chats = await self._disk_operation(self.catalog.chats, account) if account else []
        for c in chats:
            c['scan'] = await self._disk_operation(self.catalog.scan_state, account, c['id'])
            c['topics'] = await self._disk_operation(self.catalog.topics, account, c['id'])
        return {'authorized': self.authorized, 'account': account, 'profile_id': self.profile_id,
            'login': self.login_state, 'profile': self.profile, 'profiles': profiles, 'chats': chats,
            'config': self.config, 'issues': self.issues, 'operations': list(self.operations.values()),
            'flood_seconds': max(0, int(self.flood_until - time.monotonic())),
            'stats': asdict(self.queue_stats(chat_id)), 'queue_total': total,
            'queue': [{k: getattr(i, k, None) for k in ('id','chat_id','chat_name','file_name','file_size',
                'status','progress','downloaded_bytes','speed','last_error','output_path','topic_id','topic_name','job_id')}
                      for i in items],
            'jobs': await self._disk_operation(self.catalog.jobs, account) if account else [],
            'selection': await self._disk_operation(self.catalog.preference, account) if account else {}}

    async def close_service(self):
        await self.cancel_scans()
        await self._reset_scheduler()
        await self.flush_jobs()
