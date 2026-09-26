"""Durable media identities and collision-safe files for the downloader.

Old, unindexed files are preserved, never assumed to belong to a Telegram message.
All public disk operations are synchronous and must run in a worker thread.
"""
import asyncio
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass


class IncompleteDownloadError(IOError):
    """The transfer ended without the complete document bytes."""


@dataclass(frozen=True)
class MediaIdentity:
    key: str
    expected_size: int | None
    exact_size: bool
    metadata: str


@dataclass(frozen=True)
class Reservation:
    key: str
    save_path: str
    temp_path: str
    expected_size: int | None
    exact_size: bool
    complete: bool = False
    written: int = 0


def media_identity(account, item):
    """Use immutable Telegram media IDs, never expiring file references."""
    message = item.message
    document = getattr(message, "document", None)
    photo = getattr(message, "photo", None)
    media = getattr(message, "media", None)
    document = document or getattr(media, "document", None)
    photo = photo or getattr(media, "photo", None)
    source = document or photo
    kind = "photo" if photo is not None and document is None else "document"
    metadata = json.dumps({
        "account": str(account), "chat": item.chat_id, "message": item.msg_id,
        "kind": kind, "media": getattr(source, "id", None),
        "size": item.file_size,
        # The fallback is useful for test clients and unusual media without an ID.
        "edited": str(getattr(message, "edit_date", None)) if source is None else None,
    }, sort_keys=True, separators=(",", ":"))
    return MediaIdentity(hashlib.sha256(metadata.encode()).hexdigest(), item.file_size,
                         kind != "photo" and item.file_size is not None, metadata)


def _digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DownloadStorage:
    def __init__(self, root):
        self.root = os.path.realpath(root)
        self.lock = threading.RLock()
        self.active = set()
        os.makedirs(self.root, exist_ok=True)
        self.database = os.path.join(self.root, ".telegram-downloader.sqlite3")
        if os.path.islink(self.database):
            raise ValueError("The download manifest must not be a symbolic link.")
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS chats (
                    account TEXT NOT NULL, chat_id INTEGER NOT NULL,
                    directory TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(account, chat_id)
                );
                CREATE TABLE IF NOT EXISTS media (
                    identity TEXT PRIMARY KEY, metadata TEXT NOT NULL,
                    destination TEXT NOT NULL UNIQUE, partial TEXT NOT NULL UNIQUE,
                    expected_size INTEGER, exact_size INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'partial', size INTEGER, digest TEXT
                );
            """)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _path(self, relative):
        path = os.path.join(self.root, relative)
        if os.path.isabs(relative) or os.path.commonpath([self.root, os.path.realpath(path)]) != self.root:
            raise ValueError("Refusing to write outside the download folder.")
        if os.path.islink(path):
            raise ValueError("Download paths must not be symbolic links.")
        return path

    def _free_destination(self, db, relative, identity=None):
        stem, extension = os.path.splitext(relative)
        candidate, number = relative, 1
        while (os.path.lexists(self._path(candidate)) or db.execute(
                "SELECT 1 FROM media WHERE destination=? AND identity != ?",
                (candidate, identity or "")).fetchone()):
            number += 1
            candidate = f"{stem} ({number}){extension}"
        return candidate

    def _chat_directory(self, db, account, chat_id, preferred):
        row = db.execute("SELECT directory FROM chats WHERE account=? AND chat_id=?",
                         (account, chat_id)).fetchone()
        if row:
            return row["directory"]
        candidate, number = preferred, 1
        while (db.execute("SELECT 1 FROM chats WHERE directory=?", (candidate,)).fetchone()
               or (os.path.lexists(self._path(candidate)) and not os.path.isdir(self._path(candidate)))):
            number += 1
            candidate = f"{preferred} [{chat_id}]" if number == 2 else f"{preferred} [{chat_id}-{number}]"
        db.execute("INSERT INTO chats VALUES (?, ?, ?)", (account, chat_id, candidate))
        return candidate

    def prepare(self, account, chat_id, preferred_directory, filename, identity):
        if any(not part or part in (".", "..") or "/" in part or "\\" in part
               for part in (preferred_directory, filename)):
            raise ValueError("Download names must be single path components.")
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            directory = self._chat_directory(db, str(account), chat_id, preferred_directory)
            os.makedirs(self._path(directory), exist_ok=True)
            row = db.execute("SELECT * FROM media WHERE identity=?", (identity.key,)).fetchone()
            if row is None:
                destination = self._free_destination(db, os.path.join(directory, filename))
                partial = os.path.join(directory, f".tgdl-{identity.key}.part")
                if os.path.lexists(self._path(partial)):
                    partial = os.path.join(directory, f".tgdl-{identity.key}-{uuid.uuid4().hex}.part")
                db.execute("""INSERT INTO media
                    (identity,metadata,destination,partial,expected_size,exact_size)
                    VALUES (?,?,?,?,?,?)""", (identity.key, identity.metadata, destination, partial,
                                               identity.expected_size, int(identity.exact_size)))
                row = db.execute("SELECT * FROM media WHERE identity=?", (identity.key,)).fetchone()
            elif row["metadata"] != identity.metadata:
                raise ValueError("The download manifest contains conflicting media metadata.")
            final, partial = self._path(row["destination"]), self._path(row["partial"])
            complete = (row["state"] in ("ready", "complete") and row["digest"]
                        and os.path.isfile(final) and os.path.getsize(final) == row["size"]
                        and _digest(final) == row["digest"])
            if complete:
                db.execute("UPDATE media SET state='complete' WHERE identity=?", (identity.key,))
                if os.path.isfile(partial) and partial not in self.active:
                    os.unlink(partial)
                return Reservation(identity.key, final, partial, identity.expected_size,
                                   identity.exact_size, True, row["size"])
            db.execute("UPDATE media SET state='partial',digest=NULL,size=NULL WHERE identity=?",
                       (identity.key,))
            if os.path.lexists(final):
                destination = self._free_destination(db, row["destination"], identity.key)
                db.execute("UPDATE media SET destination=?,state='partial',digest=NULL,size=NULL WHERE identity=?",
                           (destination, identity.key))
                final = self._path(destination)
            if partial in self.active:
                raise RuntimeError("This media is already being downloaded.")
            if os.path.exists(partial) and (not os.path.isfile(partial) or
                    (identity.exact_size and os.path.getsize(partial) > identity.expected_size)):
                # Retain inconsistent data for explicit cleanup; never append to it.
                partial_relative = os.path.join(directory, f".tgdl-{identity.key}-{uuid.uuid4().hex}.part")
                db.execute("UPDATE media SET partial=?,state='partial' WHERE identity=?",
                           (partial_relative, identity.key))
                partial = self._path(partial_relative)
            db.commit()
            self.active.add(partial)
            return Reservation(identity.key, final, partial, identity.expected_size, identity.exact_size)

    def release(self, reservation):
        with self.lock:
            self.active.discard(reservation.temp_path)

    @staticmethod
    def partial_bytes(reservation):
        try:
            return os.path.getsize(reservation.temp_path)
        except OSError:
            return 0

    def finalize(self, reservation):
        """Validate, persist promotion intent, and atomically create without overwrite."""
        with self.lock, self._connect() as db:
            partial = self._path(os.path.relpath(reservation.temp_path, self.root))
            size = os.path.getsize(partial)
            if not size:
                raise IncompleteDownloadError("download produced an empty file")
            if reservation.exact_size and size != reservation.expected_size:
                raise IncompleteDownloadError(f"download wrote {size} of {reservation.expected_size} bytes")
            with open(partial, "rb") as stream:
                os.fsync(stream.fileno())
            digest = _digest(partial)
            # Commit intent first: a crash immediately after linking is recoverable.
            db.execute("UPDATE media SET state='ready',size=?,digest=? WHERE identity=?",
                       (size, digest, reservation.key))
            db.commit()
            while True:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT destination FROM media WHERE identity=?", (reservation.key,)).fetchone()
                destination = self._path(row["destination"])
                try:
                    self._promote_exclusive(partial, destination)
                except FileExistsError:
                    relative = self._free_destination(db, row["destination"], reservation.key)
                    db.execute("UPDATE media SET destination=? WHERE identity=?", (relative, reservation.key))
                    db.commit()
                    continue
                db.execute("UPDATE media SET state='complete' WHERE identity=?", (reservation.key,))
                db.commit()
                os.unlink(partial)
                return destination, size

    @staticmethod
    def _promote_exclusive(partial, destination):
        try:
            os.link(partial, destination)
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
            # FAT/network filesystems may not support links. Exclusive create
            # still guarantees no clobber, and the manifest verifies recovery.
            with open(destination, "xb") as target:
                try:
                    with open(partial, "rb") as source:
                        shutil.copyfileobj(source, target, 1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                except BaseException:
                    os.unlink(destination)
                    raise

    def orphan_parts(self, delete=False):
        """Exclude both active writers and manifest-owned resumable fragments."""
        count = total = 0
        with self.lock, self._connect() as db:
            protected = set(self.active)
            for row in db.execute("SELECT partial,expected_size,exact_size,state FROM media"):
                path = self._path(row["partial"])
                if (os.path.isfile(path) and os.path.getsize(path) > 0 and row["state"] != "complete" and
                        (not row["exact_size"] or os.path.getsize(path) <= row["expected_size"])):
                    protected.add(path)
            for directory, names, files in os.walk(self.root, followlinks=False):
                names[:] = [name for name in names if not os.path.islink(os.path.join(directory, name))]
                for name in files:
                    path = os.path.join(directory, name)
                    if not name.endswith(".part") or path in protected or os.path.islink(path):
                        continue
                    try:
                        size = os.path.getsize(path)
                        if delete:
                            os.unlink(path)
                        count += 1
                        total += size
                    except OSError:
                        continue
        return count, total


_storages = {}
_storages_lock = threading.Lock()


def get_storage(root):
    root = os.path.realpath(root)
    with _storages_lock:
        storage = _storages.get(root)
        if storage is None:
            storage = _storages[root] = DownloadStorage(root)
        return storage


class _PendingWrite:
    def __init__(self, task):
        self.task = task

    def __await__(self):
        return asyncio.shield(self.task).__await__()


async def _drain_task(task):
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class AsyncDiskSink:
    """Telethon-compatible sink, including photo helpers that do not await write.

    Each write schedules its disk work and returns an awaitable. finish() drains
    writes even for the synchronous cached-photo path before flushing/closing.
    """
    def __init__(self, path, stream):
        self.name = path
        self.stream = stream
        self.position = 0
        self.pending = None

    @classmethod
    async def open(cls, path, offset=None):
        def open_stream():
            stream = open(path, "r+b" if offset is not None else "wb")  # noqa: SIM115 - finish owns closure
            if offset is not None:
                stream.truncate(offset)
                stream.seek(offset)
            return stream
        task = asyncio.create_task(asyncio.to_thread(open_stream))
        try:
            stream = await asyncio.shield(task)
        except asyncio.CancelledError:
            stream = await _drain_task(task)
            await asyncio.to_thread(stream.close)
            raise
        sink = cls(path, stream)
        sink.position = offset or 0
        return sink

    def __fspath__(self):
        return self.name

    def tell(self):
        return self.position

    def write(self, data):
        previous = self.pending
        self.position += len(data)

        async def write_chunk():
            if previous is not None:
                await previous
            return await asyncio.to_thread(self.stream.write, data)

        self.pending = asyncio.create_task(write_chunk())
        return _PendingWrite(self.pending)

    def flush(self):
        # Telethon calls this synchronously. finish owns the real disk flush.
        pass

    async def finish(self):
        async def finish_writes():
            try:
                if self.pending is not None:
                    await self.pending
                await asyncio.to_thread(self.stream.flush)
            finally:
                await asyncio.to_thread(self.stream.close)

        task = asyncio.create_task(finish_writes())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await _drain_task(task)
            raise
