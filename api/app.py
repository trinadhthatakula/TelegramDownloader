"""Loopback-only application API. The engine owns sessions, workers, and files."""
import asyncio
import hmac
import os
import queue
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine.service import DownloadService


class Scope(BaseModel):
    chat_id: int
    topic_id: int | None = None


class Selection(BaseModel):
    scopes: list[Scope] = Field(min_length=1, max_length=500)
    quantities: dict[str, int | None]
    order: str = 'newest'
    filters: dict = Field(default_factory=dict)
    skip_existing: bool = True
    output_dir: str | None = None


class Control(BaseModel):
    action: str
    chat_id: int | None = None
    topic_id: int | None = None


def directory_info(path, check=False):
    root = Path(path).expanduser().resolve()
    sensitive_targets = [
        Path.home() / '.ssh',
        Path.home() / '.gnupg',
    ]
    if sys.platform == 'win32' or os.name == 'nt':
        sensitive_targets.append(Path('C:/Windows/System32'))
        sys_root = os.environ.get('SystemRoot')
        if sys_root:
            sensitive_targets.append(Path(sys_root) / 'System32')
    else:
        sensitive_targets.extend([
            Path('/etc'),
            Path('/var/run'),
            Path('/proc'),
            Path('/sys'),
        ])
    for target in sensitive_targets:
        target_exp = target.expanduser()
        is_restricted = False
        try:
            if root.is_relative_to(target_exp):
                is_restricted = True
        except (ValueError, TypeError):
            pass
        if not is_restricted:
            try:
                if root.is_relative_to(target_exp.resolve()):
                    is_restricted = True
            except (ValueError, TypeError):
                pass
        if is_restricted:
            raise ValueError('Access to sensitive system directories is restricted.')
    if check:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=root) as stream:
            stream.write(b'write-check')
            stream.flush()
    if not root.is_dir():
        raise ValueError('Choose an existing directory')
    children = []
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.name.startswith('.') or not entry.is_dir(follow_symlinks=False):
                continue
            children.append({'name': entry.name, 'path': str(root / entry.name)})
            if len(children) >= 500:
                break
    return {'path': str(root), 'parent': str(root.parent), 'directories': sorted(children, key=lambda d: d['name'].casefold()),
            'free_bytes': shutil.disk_usage(root).free, 'writable': os.access(root, os.W_OK)}


def create_app(service: DownloadService, token=None, port=8765, dev_origin=None, shutdown=None):
    launch_token = token or secrets.token_urlsafe(32)
    cookie = secrets.token_urlsafe(32)
    origins = {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}
    if dev_origin:
        origins.add(dev_origin)
    events = deque(maxlen=128)
    revision = 0

    async def invoke(coro):
        return await asyncio.wrap_future(service.run_coro(coro))

    async def broadcast():
        nonlocal revision
        while True:
            changed = False
            # UI clients consume revisions, never Python objects or secrets.
            for _ in range(1000):
                try:
                    service.event_queue.get_nowait()
                    changed = True
                except queue.Empty:
                    break
            if changed:
                revision += 1
                events.append((revision, 'change'))
            await asyncio.sleep(0.25)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(broadcast())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await invoke(service.close_service())
        await asyncio.to_thread(service.shutdown)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.launch_token = launch_token

    @app.middleware('http')
    async def local_access(request, call_next):
        host = request.headers.get('host', '')
        expected_hosts = {urlsplit(origin).netloc for origin in origins}
        origin = request.headers.get('origin')
        if host not in expected_hosts or (origin and origin not in origins):
            return JSONResponse({'detail': 'This service accepts local app requests only'}, status_code=403)
        if request.url.path.startswith('/api/') and request.url.path != '/api/session':
            supplied = request.cookies.get('tgdl_session', '')
            if not hmac.compare_digest(supplied, cookie):
                return JSONResponse({'detail': 'Open the app using its launcher link'}, status_code=401)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/api/') else 'no-cache'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(ValueError)
    @app.exception_handler(TypeError)
    async def invalid(request, error):
        return JSONResponse({'detail': str(error)}, status_code=400)

    @app.exception_handler(OSError)
    async def disk_error(request, error):
        return JSONResponse({'detail': f'Filesystem operation failed: {error.strerror or type(error).__name__}'}, status_code=400)

    @app.post('/api/session')
    async def session(request: Request):
        supplied = request.headers.get('authorization', '').removeprefix('Bearer ')
        if not hmac.compare_digest(supplied, launch_token):
            raise HTTPException(401, 'Invalid launcher token')
        response = JSONResponse({'connected': True})
        response.set_cookie('tgdl_session', cookie, httponly=True, samesite='strict')
        return response

    @app.get('/api/state')
    async def state(chat_id: int | None = None, topic_id: int | None = None, offset: int = 0, limit: int = 50):
        return await invoke(service.state(chat_id, topic_id, max(0, offset), min(100, max(1, limit))))

    @app.get('/api/events')
    async def event_stream(request: Request):
        async def stream():
            try:
                last = int(request.headers.get('last-event-id', '0'))
            except ValueError:
                last = 0
            yield f'id: {revision}\nevent: change\ndata: {{"snapshot":true}}\n\n'
            last = revision
            heartbeat = 0
            while not await request.is_disconnected():
                if revision > last:
                    yield f'id: {revision}\nevent: change\ndata: {{"snapshot":true}}\n\n'
                    last = revision
                heartbeat += 1
                if heartbeat % 30 == 0:
                    yield ': heartbeat\n\n'
                await asyncio.sleep(0.5)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    async def operation(label, coro):
        async def start():
            return {'operation': service.start_operation(label, coro)}
        return await invoke(start())

    @app.post('/api/auth')
    async def auth(request: Request):
        data = await request.json()
        action = data.pop('action', '')
        if action not in ('connect','save','switch','begin_login','send_code','code','password','logout','add_account'):
            raise ValueError('Unknown account action')
        if action == 'add_account':
            base = service.accounts.data['profiles'].get(data.pop('base_profile', service.profile_id))
            if not base:
                raise ValueError('Save API details first')
            data.update(key=None, api_id=base['api_id'], api_hash=base['api_hash'])
            action = 'save'
        if action == 'save' and not data.get('api_hash') and data.get('key'):
            old = service.accounts.data['profiles'].get(data['key'])
            if old:
                data['api_hash'] = old['api_hash']
        async def run():
            await service.authenticate(action, **data)
            if service.authorized:
                await service.fetch_dialogs()
        return await operation('Account', run())

    @app.post('/api/chats/refresh')
    async def refresh():
        return await operation('Refresh chats', service.fetch_dialogs())

    @app.post('/api/scans')
    async def scan(request: Request):
        data = await request.json()
        ids = list(dict.fromkeys(int(id) for id in data.get('chat_ids', [])))
        if not ids or len(ids) > 500:
            raise ValueError('Select between 1 and 500 chats')
        return await operation('Scan history', service.scan(ids, bool(data.get('full'))))

    @app.post('/api/scans/cancel')
    async def cancel_scan():
        await invoke(service.cancel_scans())
        return {'cancelled': True}

    @app.get('/api/counters')
    async def counters(chat_id: int, topic_id: int | None = None):
        return await operation('Count media', service.get_counters(chat_id, topic_id))

    @app.post('/api/inventory')
    async def inventory(selection: Selection):
        async def get():
            return await service._disk_operation(service.catalog.summary, service.account_key,
                [s.model_dump() for s in selection.scopes], os.path.realpath(selection.output_dir or service.config['output_dir']), selection.filters)
        return await invoke(get())

    @app.post('/api/selection')
    async def save_selection(request: Request):
        value = await request.json()
        async def save():
            await service._disk_operation(service.catalog.preference, service.account_key, value)
        await invoke(save())
        return {'saved': True}

    @app.post('/api/jobs/preview')
    async def preview(selection: Selection):
        return await operation('Prepare selection', service.preview(selection.model_dump()))

    @app.post('/api/jobs/{id}/resume')
    async def resume(id: str):
        return await operation('Resume downloads', service.resume_job(id))

    @app.post('/api/control')
    async def control(command: Control):
        if command.action not in ('pause','resume','cancel','retry','clear'):
            raise ValueError('Unknown download action')
        return await operation('Update downloads', service.control_scope(command.action, command.chat_id, command.topic_id))

    @app.post('/api/items/{id}/cancel')
    async def cancel_item(id: str):
        await invoke(service._cancel_item(id))
        return {'cancelled': True}

    @app.post('/api/settings')
    async def settings(request: Request):
        data = await request.json()
        if not isinstance(data, dict):
            raise TypeError('Settings must be an object')
        await asyncio.to_thread(directory_info, data.get('output_dir') or service.config['output_dir'], True)
        error = await invoke(service.apply_config(data))
        if error:
            raise ValueError(error)
        return service.config

    @app.get('/api/directories')
    async def directory(path: str | None = None):
        return await asyncio.to_thread(directory_info, path or str(Path.home()))

    @app.post('/api/directories/check')
    async def check_directory(request: Request):
        data = await request.json()
        return await asyncio.to_thread(directory_info, data['path'], True)

    @app.get('/api/partials')
    async def partials():
        count, size = await asyncio.to_thread(service.sweep_orphan_parts, service.config['output_dir'])
        return {'count': count, 'bytes': size}

    @app.post('/api/partials/clean')
    async def clean(request: Request):
        data = await request.json()
        if data.get('confirm') is not True:
            raise ValueError('Confirm removal of unrecognized partial files')
        count, size = await asyncio.to_thread(service.delete_orphan_parts, service.config['output_dir'])
        return {'count': count, 'bytes': size}

    @app.post('/api/directories/open')
    async def open_directory():
        path = service.config['output_dir']
        if not os.path.isdir(path):
            raise ValueError('The download directory does not exist')
        def open_path():
            if sys.platform == 'win32':
                os.startfile(path)
            else:
                subprocess.run(['open' if sys.platform == 'darwin' else 'xdg-open', path], check=True, timeout=15)
        await asyncio.to_thread(open_path)
        return {'opened': True}

    @app.post('/api/issues/clear')
    async def clear_issues():
        service.issues.clear()
        return {'cleared': True}

    @app.post('/api/quit')
    async def quit_service():
        await invoke(service.close_service())
        if shutdown:
            shutdown()
        return {'stopped': True}

    bundle = Path(__file__).parent / 'static'
    if bundle.is_dir():
        app.mount('/assets', StaticFiles(directory=bundle / 'assets'), name='assets')
        @app.get('/')
        async def index():
            return FileResponse(bundle / 'index.html')
    else:
        @app.get('/')
        async def missing_bundle():
            return JSONResponse({'detail': 'Build the web UI first: cd web && pnpm install && pnpm build'}, status_code=503)
    return app
