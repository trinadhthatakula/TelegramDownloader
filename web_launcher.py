"""Start the local web app. Use --demo to exercise it without Telegram or credentials."""
import argparse
import os
import sys
import tempfile
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
import uvicorn

from api.app import create_app
from engine.core import BASE_DIR, configure_logging
from engine.service import DownloadService
from telegram_auth import AccountStore, validate_credentials


def account_store(directory):
    load_dotenv(Path(directory) / '.env')
    legacy = None
    try:
        id, hash = validate_credentials(os.getenv('TG_API_ID'), os.getenv('TG_API_HASH'))
        session = Path(os.getenv('TG_SESSION_NAME', 'downloader_session')).expanduser()
        if not session.is_absolute():
            session = Path(directory) / session
        legacy = id, hash, str(session)
    except ValueError:
        pass
    return AccountStore(str(directory), legacy=legacy)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', default=BASE_DIR)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--dev', action='store_true', help='Allow the local Vite frontend on port 5173')
    parser.add_argument('--demo', action='store_true', help='Generated chats and downloads in an isolated temporary directory')
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    directory = Path(tempfile.mkdtemp(prefix='tgdl-demo-')) if args.demo else Path(args.data_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    lock = open(directory / '.web-service.lock', 'a+')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0); lock.write(' '); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('The downloader is already running for this data directory.', file=sys.stderr)
        lock.close()
        return 1
    configure_logging(str(directory / 'downloader.log'))
    try:
        accounts = account_store(directory)
        cls = DownloadService
        if args.demo:
            from engine.demo import DemoService
            cls = DemoService
            accounts.data = {'active': 'd' * 32, 'profiles': {'d' * 32: {
                'api_id': 42, 'api_hash': 'a' * 32, 'label': 'Offline demo', 'session': str(directory / 'demo-session')}}}
            accounts.commit(accounts.data)
        profile = accounts.data['profiles'].get(accounts.data['active']) or {}
        service = cls(profile.get('api_id', 0), profile.get('api_hash', ''),
                      profile.get('session', str(directory / 'downloader_session')), accounts,
                      str(directory / 'catalog.sqlite3'), str(directory / 'config.json'))
        if args.demo:
            service.config['output_dir'] = str(directory / 'downloads')
            service.run_coro(service.init_client()).result(10)
            service.run_coro(service.fetch_dialogs()).result(10)
        server = None
        def shutdown():
            if server:
                server.should_exit = True
        app = create_app(service, port=args.port, dev_origin='http://127.0.0.1:5173' if args.dev else None, shutdown=shutdown)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, log_level='warning', access_log=False))
        url = f'http://127.0.0.1:{5173 if args.dev else args.port}/#token={app.state.launch_token}'
        print(f'Open the downloader: {url}', flush=True)
        if args.demo:
            print(f'OFFLINE DEMO — generated files only. Data directory: {directory}', flush=True)
        if not args.no_browser:
            import threading
            threading.Timer(1, lambda: webbrowser.open(url)).start()
        server.run()
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        lock.close()


if __name__ == '__main__':
    sys.exit(main())
