"""Headless checks for interactive_downloader.py. No network, no Telegram account.

Run:  .venv/bin/python tests_smoke.py
"""
import os
import sys
import time
import shutil
import asyncio
import tempfile
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

TMP_ROOT = tempfile.mkdtemp(prefix="tgdl-test-")

import interactive_downloader as dl  # noqa: E402

# Keep the real config.json / log untouched.
dl.CONFIG_FILE = os.path.join(TMP_ROOT, "config.json")
dl.LOG_FILE = os.path.join(TMP_ROOT, "downloader.log")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


# --------------------------------------------------------------- pure helpers

def test_helpers():
    print("\n[1] sanitization / containment")
    root = os.path.join(TMP_ROOT, "out")
    os.makedirs(root, exist_ok=True)

    # The exact string seen in the real downloader.log: an uploader-supplied
    # absolute name made os.path.join throw away the output directory.
    hostile = "/app/VID_20240423_111309_573.mp4"
    safe = dl.sanitize_file_name(hostile, 1, ".mp4")
    check("absolute name loses its leading path", safe == "VID_20240423_111309_573.mp4", safe)
    check("absolute name stays inside root",
          dl.is_within(root, os.path.join(root, safe)))
    check("raw name escapes root (regression being fixed)",
          not dl.is_within(root, os.path.join(root, hostile)))

    for raw, expect_inside in [
        ("../../etc/passwd", True),
        ("..", True),
        ("....//....//x.mp4", True),
        ("C:\\Windows\\evil.exe", True),
        ("a/b/c/d.mp4", True),
        ("\x00zero.mp4", True),
        ("", True),
        ("   ", True),
        ("." * 300 + ".mp4", True),
    ]:
        s = dl.sanitize_file_name(raw, 42, ".mp4")
        ok = ("/" not in s and "\\" not in s and s not in ("", ".", "..")
              and dl.is_within(root, os.path.join(root, s))
              and len(s) <= dl.MAX_FILENAME_LEN)
        check(f"sanitize({raw!r:>22}) -> {s!r}", ok and expect_inside, s)

    check("empty name falls back to msg id", dl.sanitize_file_name(None, 7, ".jpg") == "msg_7.jpg")
    check("chat dir falls back to chat id", dl.sanitize_chat_dir("///", -100) == "chat_-100")
    check("dot-only chat name falls back", dl.sanitize_chat_dir("..", -1) == "chat_-1")

    # Backwards compatibility: ~8,900 files / ~62 GB are already on disk under the
    # original naming. Any change here silently re-downloads them.
    print("\n[1b] existing downloads keep their names")
    for raw in ["IMG_4330.MP4", "msg_15040.mp4", "video_10%4030-06-2024_02-50-50.mp4",
                "By_@Desipremier_On_Telegram2023-11-24-15-50-40.mp4",
                "@Mega_Officials ( Telegram ) 07 .mp4",
                "看着闺蜜吃男友鸡巴.mp4",
                "Desi_Indian_Girl_Dancing_Naked_ලංකාවේ_නංගිගේ.mp4",
                "Sherlyn Chopra XXX - I Only Eat Extra Spicy • Pornkeen.net.mp4",
                "Join { @Mega129 } Telegram ChanneL (20).MP4"]:
        check(f"unchanged: {raw[:44]}", dl.sanitize_file_name(raw, 1, ".mp4") == raw,
              dl.sanitize_file_name(raw, 1, ".mp4"))
    for title, folder in [("RAVEN GROUP 🔑", "RAVEN GROUP"),
                          ("Mom Collection", "Mom Collection"),
                          ("INDIAN MOM VIP", "INDIAN MOM VIP"),
                          ("Sis inc 💦", "Sis inc"),
                          ("Spy incest", "Spy incest")]:
        check(f"chat folder {title!r} -> {folder!r}", dl.sanitize_chat_dir(title, 1) == folder,
              dl.sanitize_chat_dir(title, 1))

    real = os.path.join(BASE, "telegram_downloads")
    if os.path.isdir(real):
        names = [f for _dp, _dn, fs in os.walk(real) for f in fs if not f.startswith(".")]
        renamed = [f for f in names if dl.sanitize_file_name(f, 1, os.path.splitext(f)[1]) != f]
        dirs = [d for d in os.listdir(real) if os.path.isdir(os.path.join(real, d))]
        misdir = [d for d in dirs if dl.sanitize_chat_dir(d, 1) != d]
        check(f"none of {len(names)} downloaded files would be renamed",
              not renamed, f"{len(renamed)} e.g. {renamed[:3]}")
        check(f"none of {len(dirs)} chat folders would move", not misdir, str(misdir))

    print("\n[2] config validation")
    c = dl.validate_config({})
    check("defaults are complete", set(c) == {"output_dir", "max_parallel_files", "scan_depth"})
    check("parallel clamped high", dl.validate_config({"max_parallel_files": 999})["max_parallel_files"] == 10)
    check("parallel clamped low", dl.validate_config({"max_parallel_files": 0})["max_parallel_files"] == 1)
    check("garbage parallel falls back", dl.validate_config({"max_parallel_files": "x"})["max_parallel_files"] == 3)
    check("negative depth falls back", dl.validate_config({"scan_depth": -5})["scan_depth"] == 500)
    check("zero depth kept (unlimited)", dl.validate_config({"scan_depth": 0})["scan_depth"] == 0)
    check("output_dir made absolute", os.path.isabs(dl.validate_config({"output_dir": "rel/dir"})["output_dir"]))
    check("output_dir tilde expanded", "~" not in dl.validate_config({"output_dir": "~/x"})["output_dir"])
    check("null config survives", dl.validate_config({"output_dir": None})["output_dir"] is not None)

    print("\n[3] atomic config write")
    cfg = {"output_dir": TMP_ROOT, "max_parallel_files": 4, "scan_depth": 100}
    check("save returns no error", dl.save_config(cfg) is None)
    check("no .tmp left behind", not os.path.exists(dl.CONFIG_FILE + ".tmp"))
    check("round-trips", dl.load_config()["max_parallel_files"] == 4)

    print("\n[4] output dir validation")
    check("writable dir accepted", dl.check_output_dir(os.path.join(TMP_ROOT, "newdir")) is None)
    blocker = os.path.join(TMP_ROOT, "is-a-file")
    open(blocker, "w").close()
    check("file-instead-of-dir rejected", dl.check_output_dir(blocker) is not None)
    ro = os.path.join(TMP_ROOT, "readonly")
    os.makedirs(ro, exist_ok=True)
    os.chmod(ro, 0o500)
    check("read-only dir rejected", dl.check_output_dir(os.path.join(ro, "sub")) is not None)
    os.chmod(ro, 0o700)

    check("human_size", dl.human_size(0) == "0 B" and dl.human_size(1536) == "1.5 KB"
          and dl.human_size(62 * 1024 ** 3) == "62.0 GB")


# ------------------------------------------------------------------ fake client

class FakeFile:
    def __init__(self, name, size, ext):
        self.name, self.size, self.ext = name, size, ext


class FakeMessage:
    def __init__(self, mid, name, size=4096, ext=".mp4"):
        self.id = mid
        self.file = FakeFile(name, size, ext) if name is not None else None


class FakeEntity:
    def __init__(self, did, name, bot=False, megagroup=False):
        self.id, self.title, self.bot, self.megagroup = did, name, bot, megagroup


def FakeDialog(did, name):
    """The backend is handed ChatEntry objects by the UI, not raw Dialogs."""
    return dl.ChatEntry(id=did, name=name, kind="Group", archived=False,
                        entity=FakeEntity(did, name))


class RawDialog:
    """Shaped like telethon's custom.Dialog, for ChatEntry.from_dialog."""

    def __init__(self, did, name, kind="Group", archived=False, bot=False):
        self.id, self.name, self.title = did, name, name
        self.archived = archived
        self.is_group = kind == "Group"
        self.is_channel = kind in ("Channel", "Group")
        self.is_user = kind in ("DM", "Bot")
        self.entity = FakeEntity(did, name, bot=bot)


class FakeClient:
    """Minimal stand-in for TelegramClient covering the paths the app uses."""

    def __init__(self, chats, fail_first_n_floods=0, download_error=None):
        self.chats = chats  # {chat_id: [FakeMessage]}
        self.floods_left = fail_first_n_floods
        self.download_error = download_error
        self.downloads = 0

    def _messages_for(self, entity):
        cid = entity.id if hasattr(entity, "id") else entity
        return self.chats.get(cid, [])

    async def iter_messages(self, entity, limit=None, offset_id=None):
        msgs = sorted(self._messages_for(entity), key=lambda m: -m.id)
        if offset_id:
            msgs = [m for m in msgs if m.id < offset_id]
        for m in msgs[: (limit or len(msgs))]:
            await asyncio.sleep(0)
            yield m

    async def get_messages(self, entity, ids=None):
        for m in self._messages_for(entity):
            if m.id == ids:
                return m
        return None

    async def download_media(self, message, file=None, progress_callback=None):
        if self.floods_left > 0:
            self.floods_left -= 1
            raise dl.errors.FloodWaitError(request=None, capture=1)
        if self.download_error:
            raise self.download_error
        self.downloads += 1
        size = message.file.size
        with open(file, "wb") as fh:
            fh.write(b"\xab" * size)
        if progress_callback:
            progress_callback(size, size)
        return file


def make_backend(out_dir, parallel=3):
    b = dl.TelegramBackend(1, "hash", os.path.join(TMP_ROOT, "sess"))
    b.config = {"output_dir": out_dir, "max_parallel_files": parallel, "scan_depth": 0}
    return b


def drain(backend, timeout=15.0, settle=0.4):
    """Waits until nothing is queued/downloading and no events for `settle` seconds."""
    deadline = time.monotonic() + timeout
    last_change = time.monotonic()
    prev = None
    while time.monotonic() < deadline:
        with backend.queue_lock:
            sig = tuple(sorted((i.id, i.status) for i in backend.download_queue))
            busy = any(i.status in ("Queued", "Downloading") for i in backend.download_queue)
        with backend.states_lock:
            fetching = any(s.fetching_in_progress for s in backend.pagination_states.values())
        if sig != prev:
            prev, last_change = sig, time.monotonic()
        if not busy and not fetching and time.monotonic() - last_change > settle:
            return True
        time.sleep(0.05)
    return False


def statuses(backend):
    with backend.queue_lock:
        return {i.id: i.status for i in backend.download_queue}


# ----------------------------------------------------------------- integration

def test_happy_path():
    print("\n[5] multi-chat download end to end")
    out = os.path.join(TMP_ROOT, "happy")
    b = make_backend(out, parallel=3)
    try:
        chats = {
            111: [FakeMessage(i, f"a_{i}.mp4") for i in range(1, 26)],
            222: [FakeMessage(i, f"b_{i}.mp4") for i in range(1, 26)],
            333: [FakeMessage(i, None) for i in range(1, 6)],  # no media at all
        }
        b.client = FakeClient(chats)
        dialogs = [FakeDialog(111, "Chat One"), FakeDialog(222, "Chat Two"), FakeDialog(333, "Empty")]
        b.run_coro(b.enqueue_selected_chats(dialogs, set(), all_formats=True)).result(10)
        finished = drain(b, timeout=30)
        st = statuses(b)

        check("drained without stalling", finished, f"{len(st)} items")
        check("all 50 files queued", len(st) == 50, str(len(st)))
        check("every item terminal", all(v in ("Completed", "Skipped (Exists)") for v in st.values()),
              str(sorted(set(st.values()))))
        check("nothing stuck Downloading", "Downloading" not in st.values())
        check("both chats made progress",
              len(os.listdir(os.path.join(out, "Chat One"))) == 25
              and len(os.listdir(os.path.join(out, "Chat Two"))) == 25)
        check("no .part files remain",
              not any(f.endswith(".part") for _, _, fs in os.walk(out) for f in fs))
        check("empty chat created no dir", not os.path.exists(os.path.join(out, "Empty")))
        check("both chats got download slots (fair round-robin)",
              all(s.total_completed == 25 for s in b.snapshot_states() if s.chat_id in (111, 222)))
        check("queue_index matches queue", len(b.queue_index) == len(b.download_queue))

        stats = b.queue_stats()
        check("stats consistent", stats.total == 50 and stats.done == 50 and stats.active == 0)

        # Re-enqueue the same chats: everything is already on disk.
        b.run_coro(b.enqueue_selected_chats(dialogs, set(), all_formats=True)).result(10)
        drain(b, timeout=30)
        st2 = statuses(b)
        check("re-enqueue does not duplicate rows", len(st2) == 50, str(len(st2)))
        check("existing files skipped, not re-downloaded", b.client.downloads == 50, str(b.client.downloads))
    finally:
        b.shutdown()


def test_path_traversal():
    print("\n[6] hostile filenames stay inside the download folder")
    out = os.path.join(TMP_ROOT, "hostile")
    b = make_backend(out, parallel=2)
    try:
        chats = {
            777: [
                FakeMessage(1, "/app/VID_20240423_111309_573.mp4"),
                FakeMessage(2, "../../../../../../tmp/tgdl-escape.mp4"),
                FakeMessage(3, "/etc/passwd"),
                FakeMessage(4, "..\\..\\win.mp4"),
                FakeMessage(5, None),
            ]
        }
        b.client = FakeClient(chats)
        b.run_coro(b.enqueue_selected_chats(
            [FakeDialog(777, "../../escape-chat")], set(), all_formats=True)).result(10)
        drain(b)

        written = [os.path.join(dp, f) for dp, _, fs in os.walk(out) for f in fs]
        check("4 media files written", len(written) == 4, str(written))
        check("every file inside output_dir", all(dl.is_within(out, p) for p in written))
        check("nothing escaped to /tmp", not os.path.exists("/tmp/tgdl-escape.mp4"))
        check("chat dir did not escape",
              os.path.isdir(os.path.join(out, "_.._escape-chat"))
              or all(dl.is_within(out, p) for p in written))
        check("all items terminal",
              all(v in ("Completed", "Skipped (Exists)") for v in statuses(b).values()),
              str(statuses(b)))
    finally:
        b.shutdown()


def test_unwritable_dir_frees_slots():
    print("\n[7] a broken output dir fails items instead of eating slots")
    ro = os.path.join(TMP_ROOT, "ro-parent")
    os.makedirs(ro, exist_ok=True)
    out = os.path.join(ro, "nope")
    os.makedirs(out, exist_ok=True)
    os.chmod(out, 0o500)  # exists but no new subdirectories allowed
    b = make_backend(out, parallel=2)
    try:
        b.client = FakeClient({888: [FakeMessage(i, f"x_{i}.mp4") for i in range(1, 9)]})
        b.run_coro(b.enqueue_selected_chats([FakeDialog(888, "Locked")], set(), all_formats=True)).result(10)
        finished = drain(b, timeout=25)
        st = statuses(b)
        check("did not deadlock on burnt slots", finished, str(st))
        check("no item left Downloading", "Downloading" not in st.values(), str(st))
        check("all items marked Failed", all(v.startswith("Failed") for v in st.values()), str(set(st.values())))
        check("retries were bounded",
              all(i.retries <= dl.MAX_ITEM_RETRIES for i in b.download_queue))
    finally:
        os.chmod(out, 0o700)
        b.shutdown()


def test_flood_requeue():
    print("\n[8] FloodWait pauses and auto-resumes")
    out = os.path.join(TMP_ROOT, "flood")
    b = make_backend(out, parallel=2)
    try:
        b.client = FakeClient({999: [FakeMessage(i, f"f_{i}.mp4") for i in range(1, 7)]},
                              fail_first_n_floods=3)
        b.run_coro(b.enqueue_selected_chats([FakeDialog(999, "Flooded")], set(), all_formats=True)).result(10)
        finished = drain(b, timeout=40)
        st = statuses(b)
        check("recovered after rate limit", finished, str(st))
        check("all 6 eventually completed",
              sum(1 for v in st.values() if v == "Completed") == 6, str(st))
        check("no permanent 'Rate Limited' dead end", "Rate Limited" not in st.values())
        check("flood event was emitted", b.flood_until > 0)
    finally:
        b.shutdown()


def test_collision_and_skip():
    print("\n[9] name collisions never delete an existing file")
    out = os.path.join(TMP_ROOT, "collide")
    chat_dir = os.path.join(out, "Dupes")
    os.makedirs(chat_dir, exist_ok=True)

    same = os.path.join(chat_dir, "same.mp4")
    with open(same, "wb") as fh:
        fh.write(b"\xab" * 4096)  # identical size -> should be skipped
    other = os.path.join(chat_dir, "other.mp4")
    with open(other, "wb") as fh:
        fh.write(b"PRECIOUS")  # different size -> must not be destroyed

    b = make_backend(out, parallel=1)
    try:
        b.client = FakeClient({555: [FakeMessage(1, "same.mp4"), FakeMessage(2, "other.mp4")]})
        b.run_coro(b.enqueue_selected_chats([FakeDialog(555, "Dupes")], set(), all_formats=True)).result(10)
        drain(b)
        st = statuses(b)
        check("identical file skipped", st.get("555_1") == "Skipped (Exists)", str(st))
        check("colliding file completed", st.get("555_2") == "Completed", str(st))
        check("pre-existing file untouched", open(other, "rb").read() == b"PRECIOUS")
        check("collision saved as 'other (2).mp4'", os.path.exists(os.path.join(chat_dir, "other (2).mp4")),
              str(sorted(os.listdir(chat_dir))))
        check("only one download happened", b.client.downloads == 1, str(b.client.downloads))
    finally:
        b.shutdown()


def test_fetch_backoff():
    print("\n[10] a permanently failing chat backs off instead of spinning")
    out = os.path.join(TMP_ROOT, "backoff")

    class BrokenClient(FakeClient):
        def __init__(self):
            super().__init__({})
            self.calls = 0

        async def iter_messages(self, entity, limit=None, offset_id=None):
            self.calls += 1
            raise RuntimeError("no access")
            yield  # pragma: no cover

    b = make_backend(out, parallel=2)
    try:
        client = BrokenClient()
        b.client = client
        b.run_coro(b.enqueue_selected_chats([FakeDialog(444, "Broken")], set(), all_formats=True)).result(10)
        time.sleep(3.0)
        check("retry count bounded by backoff schedule", client.calls <= 3, f"{client.calls} calls in 3s")
        check("at least one retry attempted", client.calls >= 2, str(client.calls))
    finally:
        b.shutdown()


def test_queue_operations():
    print("\n[11] queue actions")
    out = os.path.join(TMP_ROOT, "ops")
    b = make_backend(out, parallel=1)
    try:
        b.client = FakeClient({666: [FakeMessage(i, f"o_{i}.mp4") for i in range(1, 5)]},
                              download_error=OSError("disk on fire"))
        b.run_coro(b.enqueue_selected_chats([FakeDialog(666, "Ops")], set(), all_formats=True)).result(10)
        drain(b, timeout=30)
        st = statuses(b)
        check("failures are terminal", all(v == "Failed" for v in st.values()), str(st))

        b.client.download_error = None
        n = b.retry_failed()
        check("retry_failed requeued everything", n == 4, str(n))
        drain(b, timeout=30)
        check("all complete after retry",
              all(v == "Completed" for v in statuses(b).values()), str(statuses(b)))

        removed = b.clear_completed()
        check("clear_completed drops finished rows", removed == 4 and not b.download_queue, str(removed))
        check("queue_index cleared with it", not b.queue_index)

        # Orphaned .part sweep.
        part_dir = os.path.join(out, "Ops")
        with open(os.path.join(part_dir, ".1_2.part"), "wb") as fh:
            fh.write(b"x" * 100)
        cnt, size = b.sweep_orphan_parts()
        check("orphan .part detected", cnt == 1 and size == 100, f"{cnt}/{size}")
        cnt, size = b.delete_orphan_parts()
        check("orphan .part removed", cnt == 1 and not os.path.exists(os.path.join(part_dir, ".1_2.part")))
    finally:
        b.shutdown()


def test_cancel():
    print("\n[12] cancel")
    out = os.path.join(TMP_ROOT, "cancel")

    class SlowClient(FakeClient):
        async def download_media(self, message, file=None, progress_callback=None):
            open(file, "wb").close()
            await asyncio.sleep(30)

    b = make_backend(out, parallel=2)
    try:
        b.client = SlowClient({321: [FakeMessage(i, f"c_{i}.mp4") for i in range(1, 7)]})
        b.run_coro(b.enqueue_selected_chats([FakeDialog(321, "Slow")], set(), all_formats=True)).result(10)
        time.sleep(1.5)
        with b.queue_lock:
            active = [i for i in b.download_queue if i.status == "Downloading"]
        check("downloads actually started", len(active) == 2, str(len(active)))
        b.cancel_all()
        time.sleep(1.5)
        st = statuses(b)
        check("everything cancelled", all(v == "Cancelled" for v in st.values()), str(set(st.values())))
        check("cancelled .part files cleaned up",
              not any(f.endswith(".part") for _, _, fs in os.walk(out) for f in fs))
    finally:
        b.shutdown()


def test_resume():
    print("\n[13] resume from a partial .part")
    out = os.path.join(TMP_ROOT, "resume")
    chat_dir = os.path.join(out, "Resume")
    os.makedirs(chat_dir, exist_ok=True)

    total = 3 * dl.RESUME_ALIGN
    already = 2 * dl.RESUME_ALIGN
    with open(os.path.join(chat_dir, ".314_1.part"), "wb") as fh:
        fh.write(b"\xab" * already)

    class ChunkClient(FakeClient):
        def __init__(self, chats):
            super().__init__(chats)
            self.requested_offset = None
            self.bytes_sent = 0

        def iter_download(self, message, offset=0, request_size=None):
            self.requested_offset = offset
            remaining = message.file.size - offset
            client = self

            async def gen():
                sent = 0
                while sent < remaining:
                    n = min(request_size, remaining - sent)
                    sent += n
                    client.bytes_sent += n
                    await asyncio.sleep(0)
                    yield b"\xab" * n
            return gen()

    b = make_backend(out, parallel=1)
    try:
        client = ChunkClient({314: [FakeMessage(1, "big.mp4", size=total)]})
        b.client = client
        b.run_coro(b.enqueue_selected_chats([FakeDialog(314, "Resume")], set(), all_formats=True)).result(10)
        drain(b, timeout=30)
        st = statuses(b)
        check("resumed download completed", st.get("314_1") == "Completed", str(st))
        check("resumed from the aligned offset", client.requested_offset == already, str(client.requested_offset))
        check("only the missing bytes were fetched", client.bytes_sent == total - already, str(client.bytes_sent))
        final = os.path.join(chat_dir, "big.mp4")
        check("final file is complete", os.path.exists(final) and os.path.getsize(final) == total)
        check("no download_media fallback used", client.downloads == 0, str(client.downloads))
    finally:
        b.shutdown()


def test_resume_falls_back():
    print("\n[14] a broken resume restarts instead of failing")
    out = os.path.join(TMP_ROOT, "resume-bad")
    chat_dir = os.path.join(out, "Broken")
    os.makedirs(chat_dir, exist_ok=True)
    total = 3 * dl.RESUME_ALIGN
    with open(os.path.join(chat_dir, ".271_1.part"), "wb") as fh:
        fh.write(b"\xab" * (2 * dl.RESUME_ALIGN))

    class TruncatingClient(FakeClient):
        def iter_download(self, message, offset=0, request_size=None):
            async def gen():
                await asyncio.sleep(0)
                # Stops well short of the end: a truncated resume.
                yield b"\xab" * 4096
            return gen()

    b = make_backend(out, parallel=1)
    try:
        client = TruncatingClient({271: [FakeMessage(1, "big.mp4", size=total)]})
        b.client = client
        b.run_coro(b.enqueue_selected_chats([FakeDialog(271, "Broken")], set(), all_formats=True)).result(10)
        drain(b, timeout=30)
        final = os.path.join(chat_dir, "big.mp4")
        check("truncated resume did not fail the item", statuses(b).get("271_1") == "Completed",
              str(statuses(b)))
        check("fell back to a full download", client.downloads == 1, str(client.downloads))
        check("final file is the full size",
              os.path.exists(final) and os.path.getsize(final) == total,
              str(os.path.getsize(final)) if os.path.exists(final) else "missing")
    finally:
        b.shutdown()


def test_size_mismatch_tolerated():
    print("\n[15] an inexact reported size does not fail the download")
    out = os.path.join(TMP_ROOT, "mismatch")

    class ShortClient(FakeClient):
        async def download_media(self, message, file=None, progress_callback=None):
            self.downloads += 1
            with open(file, "wb") as fh:
                fh.write(b"\xab" * (message.file.size - 17))  # photos report estimates
            return file

    b = make_backend(out, parallel=1)
    try:
        b.client = ShortClient({272: [FakeMessage(1, "photo.jpg", size=4096, ext=".jpg")]})
        b.run_coro(b.enqueue_selected_chats([FakeDialog(272, "Photos")], set(), all_formats=True)).result(10)
        drain(b, timeout=20)
        check("completed despite the size mismatch", statuses(b).get("272_1") == "Completed",
              str(statuses(b)))
        check("file kept", os.path.exists(os.path.join(out, "Photos", "photo.jpg")))
    finally:
        b.shutdown()


def test_empty_download_fails():
    print("\n[16] a zero-byte result is treated as a failure")
    out = os.path.join(TMP_ROOT, "empty")

    class EmptyClient(FakeClient):
        async def download_media(self, message, file=None, progress_callback=None):
            self.downloads += 1
            open(file, "wb").close()
            return file

    b = make_backend(out, parallel=1)
    try:
        b.client = EmptyClient({273: [FakeMessage(1, "nothing.mp4")]})
        b.run_coro(b.enqueue_selected_chats([FakeDialog(273, "Empty")], set(), all_formats=True)).result(10)
        drain(b, timeout=30)
        check("marked Failed, not Completed", statuses(b).get("273_1") == "Failed", str(statuses(b)))
        check("no empty file left behind",
              not os.path.exists(os.path.join(out, "Empty", "nothing.mp4")))
        check("retried up to the cap", b.client.downloads == dl.MAX_ITEM_RETRIES + 1,
              str(b.client.downloads))
    finally:
        b.shutdown()


def test_chat_entry_classification():
    print("\n[17] dialogs are classified, never dropped")
    cases = [
        (RawDialog(1, "Supergroup", "Group"), "Group", False),
        (RawDialog(2, "Broadcast", "Channel"), "Channel", False),
        (RawDialog(3, "Alice", "DM"), "DM", False),
        (RawDialog(4, "Helper", "Bot", bot=True), "Bot", False),
        (RawDialog(5, "Stashed", "Group", archived=True), "Group", True),
    ]
    for raw, kind, archived in cases:
        e = dl.ChatEntry.from_dialog(raw)
        check(f"{raw.name} -> {kind}", e.kind == kind, e.kind)
        check(f"{raw.name} archived={archived}", e.archived is archived, str(e.archived))

    # An unclassifiable dialog must still become a selectable entry.
    class Odd:
        id = 99
        name = None
        title = None
    odd = dl.ChatEntry.from_dialog(Odd())
    check("nameless dialog still gets an entry", odd.id == 99 and odd.name == "Chat 99",
          odd.name)
    check("unknown type defaults to Group", odd.kind == "Group", odd.kind)


class DialogClient:
    """Serves dialogs per folder, optionally flooding the first N attempts."""

    def __init__(self, main_list, archived_list, floods=0, fail_archive=False):
        self.main_list, self.archived_list = main_list, archived_list
        self.floods, self.fail_archive = floods, fail_archive
        self.calls = []

    def iter_dialogs(self, limit=None, archived=False):
        self.calls.append(archived)
        source = self.archived_list if archived else self.main_list
        floods, fail_archive = self.floods, self.fail_archive

        async def gen():
            if not archived and floods:
                self.floods -= 1
                raise dl.errors.FloodWaitError(request=None, capture=1)
            if archived and fail_archive:
                raise RuntimeError("archive unavailable")
            for d in source:
                await asyncio.sleep(0)
                yield d

        return gen()


def test_dialog_enumeration():
    print("\n[18] every chat, channel and group is enumerated")
    out = os.path.join(TMP_ROOT, "dialogs")
    os.makedirs(out, exist_ok=True)

    def run(client):
        b = make_backend(out)
        b.client = client
        try:
            b.run_coro(b.fetch_dialogs()).result(20)
            loaded, errs = None, []
            while not b.event_queue.empty():
                kind, data = b.event_queue.get_nowait()
                if kind == "dialogs_loaded":
                    loaded = data
                elif kind == "error":
                    errs.append(data)
            return loaded, errs
        finally:
            b.shutdown()

    main_list = [RawDialog(i, f"Group {i:03d}", "Group") for i in range(250)]
    main_list += [RawDialog(500 + i, f"Person {i}", "DM") for i in range(40)]
    arch = [RawDialog(900 + i, f"Stashed {i}", "Group", archived=True) for i in range(12)]

    client = DialogClient(main_list, arch)
    loaded, errs = run(client)
    check("both folders were walked", client.calls == [False, True], str(client.calls))
    check("all 302 dialogs returned", len(loaded) == 302, str(len(loaded)))
    check("archived groups included",
          sum(1 for e in loaded if e.archived) == 12,
          str(sum(1 for e in loaded if e.archived)))
    check("groups sort ahead of DMs", loaded[0].kind == "Group" and loaded[-1].kind == "DM",
          f"{loaded[0].kind}..{loaded[-1].kind}")
    check("no error reported on the happy path", not errs, str(errs))

    # A dialog present in both folders must not appear twice.
    dupe = DialogClient(main_list, main_list[:5] + arch)
    loaded, _ = run(dupe)
    check("overlapping folders are deduped by id", len(loaded) == 302, str(len(loaded)))

    # A rate limit mid-enumeration must not empty the list.
    flooded = DialogClient(main_list, arch, floods=1)
    loaded, errs = run(flooded)
    check("retries through a FloodWait", len(loaded) == 302, str(len(loaded)))
    check("flood retry did not surface as an error", not errs, str(errs))

    # A folder that fails outright must still yield the other folder's chats.
    broken = DialogClient(main_list, arch, fail_archive=True)
    loaded, errs = run(broken)
    check("partial results survive a failed folder", len(loaded) == 290, str(len(loaded)))
    check("the failure is reported to the user", len(errs) == 1, str(errs))


def main():
    print(f"temp root: {TMP_ROOT}")
    dl.configure_logging()
    for fn in (test_helpers, test_happy_path, test_path_traversal,
               test_unwritable_dir_frees_slots, test_flood_requeue,
               test_collision_and_skip, test_fetch_backoff,
               test_queue_operations, test_cancel, test_resume,
               test_resume_falls_back, test_size_mismatch_tolerated,
               test_empty_download_fails, test_chat_entry_classification,
               test_dialog_enumeration):
        try:
            fn()
        except Exception:
            FAIL.append(fn.__name__)
            print(f"  ERROR in {fn.__name__}:")
            traceback.print_exc()

    print(f"\n{'=' * 60}\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
