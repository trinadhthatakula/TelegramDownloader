"""Durable inventories and frozen job selections. Disk methods run off the event loop."""
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager


class Catalog:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        if os.path.islink(self.path):
            raise ValueError('Catalog must not be a symbolic link')
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS chats (
                    account TEXT, id INTEGER, name TEXT, kind TEXT, archived INTEGER, forum INTEGER,
                    PRIMARY KEY(account,id));
                CREATE TABLE IF NOT EXISTS topics (
                    account TEXT, chat INTEGER, id INTEGER, title TEXT, PRIMARY KEY(account,chat,id));
                CREATE TABLE IF NOT EXISTS scans (
                    account TEXT, chat INTEGER, cursor INTEGER DEFAULT 0, upper INTEGER DEFAULT 0,
                    boundary INTEGER DEFAULT 0, scanned INTEGER DEFAULT 0, status TEXT,
                    error TEXT DEFAULT '', updated REAL, PRIMARY KEY(account,chat));
                CREATE TABLE IF NOT EXISTS media (
                    account TEXT, chat INTEGER, msg INTEGER, identity TEXT, metadata TEXT,
                    category TEXT, topic INTEGER, name TEXT, ext TEXT, size INTEGER, date TEXT,
                    verified INTEGER DEFAULT 0, verified_root TEXT DEFAULT '',
                    PRIMARY KEY(account,chat,msg));
                CREATE INDEX IF NOT EXISTS media_scope ON media(account,chat,topic,category,msg);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, account TEXT, request_id TEXT, spec TEXT, output_dir TEXT,
                    created REAL, UNIQUE(account,request_id));
                CREATE TABLE IF NOT EXISTS job_items (
                    job TEXT, chat INTEGER, msg INTEGER, identity TEXT, record TEXT, status TEXT,
                    downloaded INTEGER DEFAULT 0, error TEXT DEFAULT '', path TEXT DEFAULT '',
                    PRIMARY KEY(job,chat,msg));
                CREATE TABLE IF NOT EXISTS preferences (account TEXT PRIMARY KEY, selection TEXT);
                PRAGMA user_version=1;
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def save_chats(self, account, chats):
        with self.connect() as db:
            db.executemany('INSERT OR REPLACE INTO chats VALUES (?,?,?,?,?,?)',
                [(account, c['id'], c['name'], c['kind'], c['archived'], c['forum']) for c in chats])

    def chats(self, account):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM chats WHERE account=? ORDER BY name', (account,))]

    def save_topics(self, account, chat, topics):
        with self.connect() as db:
            db.executemany('INSERT OR REPLACE INTO topics VALUES (?,?,?,?)',
                           [(account, chat, id, title) for id, title in topics.items()])

    def topics(self, account, chat):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,title FROM topics WHERE account=? AND chat=? ORDER BY title',
                                               (account, chat))]

    def begin_scan(self, account, chat, full=False):
        with self.connect() as db:
            row = db.execute('SELECT * FROM scans WHERE account=? AND chat=?', (account, chat)).fetchone()
            if full:
                db.execute('DELETE FROM media WHERE account=? AND chat=?', (account, chat))
            if row and not full and row['status'] != 'complete':
                db.execute("UPDATE scans SET status='scanning',error='' WHERE account=? AND chat=?", (account, chat))
            else:
                boundary = row['upper'] if row and not full else 0
                db.execute('INSERT OR REPLACE INTO scans VALUES (?,?,?,?,?,?,?,?,?)',
                           (account, chat, 0, boundary, boundary, 0, 'scanning', '', time.time()))
            return dict(db.execute('SELECT * FROM scans WHERE account=? AND chat=?', (account, chat)).fetchone())

    def commit_page(self, account, chat, rows, cursor, upper, scanned, complete=False):
        with self.connect() as db:
            for r in rows:
                db.execute('''INSERT INTO media
                    (account,chat,msg,identity,metadata,category,topic,name,ext,size,date)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account,chat,msg) DO UPDATE SET
                    identity=excluded.identity,metadata=excluded.metadata,category=excluded.category,
                    topic=excluded.topic,name=excluded.name,ext=excluded.ext,size=excluded.size,date=excluded.date,
                    verified=CASE WHEN identity=excluded.identity THEN verified ELSE 0 END''',
                    (account, chat, r['msg'], r['identity'], r['metadata'], r['category'], r['topic'],
                     r['name'], r['ext'], r['size'], r['date']))
            db.execute('UPDATE scans SET cursor=?,upper=?,scanned=scanned+?,status=?,error=?,updated=? '
                       'WHERE account=? AND chat=?',
                       (cursor, upper, scanned, 'complete' if complete else 'scanning', '', time.time(), account, chat))

    def scan_state(self, account, chat):
        with self.connect() as db:
            row = db.execute('SELECT * FROM scans WHERE account=? AND chat=?', (account, chat)).fetchone()
            return dict(row) if row else None

    def stop_scan(self, account, chat, status, error=''):
        with self.connect() as db:
            db.execute('UPDATE scans SET status=?,error=?,updated=? WHERE account=? AND chat=?',
                       (status, error, time.time(), account, chat))

    @staticmethod
    def scope_where(account, scopes, filters=None):
        filters = filters or {}
        if not scopes:
            raise ValueError('Select at least one chat or topic')
        pieces, args = [], [account]
        for scope in scopes:
            if scope.get('topic_id') is None:
                pieces.append('(chat=?)')
                args.append(scope['chat_id'])
            else:
                pieces.append('(chat=? AND topic=?)')
                args.extend([scope['chat_id'], scope['topic_id']])
        where = 'account=? AND (' + ' OR '.join(pieces) + ')'
        for field, op, key in [('date','>=','after'), ('date','<=','before'), ('size','>=','min_size'), ('size','<=','max_size')]:
            if filters.get(key) is not None and filters[key] != '':
                where += f' AND {field}{op}?'
                args.append(filters[key])
        if filters.get('extensions'):
            exts = [str(e).lower() for e in filters['extensions']]
            where += ' AND ext IN (' + ','.join('?' for _ in exts) + ')'
            args.extend(exts)
        return where, args

    def summary(self, account, scopes, root='', filters=None):
        where, args = self.scope_where(account, scopes, filters)
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT category,COUNT(*) AS found, '
                'SUM(CASE WHEN verified=1 AND verified_root=? THEN 1 ELSE 0 END) AS verified, '
                'COALESCE(SUM(size),0) AS bytes,SUM(CASE WHEN size IS NULL THEN 1 ELSE 0 END) AS unknown '
                'FROM media WHERE ' + where + ' GROUP BY category', [root, *args])]

    def rows(self, account, scopes, filters=None, order='newest', offset=0, limit=200):
        where, args = self.scope_where(account, scopes, filters)
        direction = 'ASC' if order == 'oldest' else 'DESC'
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM media WHERE ' + where +
                f' ORDER BY date {direction},msg {direction},chat ASC LIMIT ? OFFSET ?', [*args, limit, offset])]

    def mark_verified(self, account, rows, root):
        with self.connect() as db:
            db.executemany('UPDATE media SET verified=?,verified_root=? WHERE account=? AND chat=? AND msg=? AND identity=?',
                [(ok, root, account, r['chat'], r['msg'], r['identity']) for r, ok in rows])

    def create_job(self, account, request_id, spec, root, rows):
        with self.connect() as db:
            old = db.execute('SELECT id FROM jobs WHERE account=? AND request_id=?', (account, request_id)).fetchone()
            if old:
                return old[0]
            id = uuid.uuid4().hex
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',
                       (id, account, request_id, json.dumps(spec), root, time.time()))
            db.executemany("INSERT INTO job_items(job,chat,msg,identity,record,status) VALUES (?,?,?,?,?,'Paused')",
                           [(id, r['chat'], r['msg'], r['identity'], json.dumps(r)) for r in rows])
            return id

    def jobs(self, account):
        with self.connect() as db:
            jobs = [dict(r) for r in db.execute('SELECT * FROM jobs WHERE account=? ORDER BY created DESC', (account,))]
            for job in jobs:
                job['counts'] = {r[0]: r[1] for r in db.execute('SELECT status,COUNT(*) FROM job_items WHERE job=? GROUP BY status', (job['id'],))}
                job['spec'] = json.loads(job['spec'])
            return jobs

    def job(self, account, id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE account=? AND id=?', (account, id)).fetchone()
            if row is None:
                raise ValueError('Job is not available to this account')
            return dict(row)

    def job_rows(self, account, id, offset=0, limit=500):
        self.job(account, id)
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM job_items WHERE job=? ORDER BY rowid LIMIT ? OFFSET ?', (id, limit, offset))]

    def update_items(self, rows):
        with self.connect() as db:
            db.executemany('UPDATE job_items SET status=?,downloaded=?,error=?,path=? WHERE job=? AND chat=? AND msg=?', rows)

    def preference(self, account, selection=None):
        with self.connect() as db:
            if selection is not None:
                db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?)', (account, json.dumps(selection)))
            row = db.execute('SELECT selection FROM preferences WHERE account=?', (account,)).fetchone()
            return json.loads(row[0]) if row else {}
