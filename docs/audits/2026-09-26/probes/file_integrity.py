"""Reproduce file integrity issues with fake transfers in disposable directories."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
import interactive_downloader as dl


def item(key, chat=1, size=4):
    return dl.DownloadItem(str(key), chat, "Same chat title", None, "same.mp4", key, ".mp4",
                           file_size=size, status="Downloading")


class Client:
    def __init__(self):
        self.calls = 0
        self.both_started = asyncio.Event()

    async def disconnect(self):
        pass

    async def download_media(self, message, file, progress_callback=None):
        self.calls += 1
        content = bytes([64 + self.calls]) * 4
        if self.calls == 2:
            self.both_started.set()
        await self.both_started.wait()
        Path(file).write_bytes(content)


def main():
    with tempfile.TemporaryDirectory(prefix="tgdl-integrity-audit-") as directory:
        dl.CONFIG_FILE = os.path.join(directory, "config.json")
        backend = dl.TelegramBackend(1, "fake", os.path.join(directory, "unused-session"))
        backend.config["output_dir"] = os.path.join(directory, "output")
        backend._schedule_downloads = lambda: None
        backend._check_and_trigger_next_batch = lambda _: None
        backend.client = Client()

        async def probe():
            first, second = item(1), item(2)
            await asyncio.gather(backend._download_worker(first), backend._download_worker(second))
            files = list(Path(backend.config["output_dir"]).rglob("*.mp4"))
            print("concurrent_collision", json.dumps({"statuses": [first.status, second.status],
                                                      "surviving_files": len(files)}))
            unrelated = item(3)
            other_chat = item(4, chat=2)
            await backend._download_worker(unrelated)
            await backend._download_worker(other_chat)
            print("false_duplicates", json.dumps({"unrelated_message": unrelated.status,
                                                  "other_same_title_chat": other_chat.status,
                                                  "total_transfer_calls": backend.client.calls}))

            truncated = item(5, size=4096)
            truncated.file_name = "truncated.mp4"
            async def short_transfer(*args, **kwargs):
                Path(kwargs["file"]).write_bytes(b"x")
            backend.client.download_media = short_transfer
            await backend._download_worker(truncated)
            print("truncated_result", json.dumps({"declared_bytes": 4096, "written_bytes": 1,
                                                  "status": truncated.status}))

            active = item(6)
            active.file_name = "active.mp4"
            started, release = asyncio.Event(), asyncio.Event()
            async def waiting_transfer(*args, **kwargs):
                with open(kwargs["file"], "wb") as stream:
                    stream.write(b"test")
                    stream.flush()
                    started.set()
                    await release.wait()
            backend.client.download_media = waiting_transfer
            task = asyncio.create_task(backend._download_worker(active))
            await started.wait()
            counted = backend.sweep_orphan_parts()
            deleted = backend.delete_orphan_parts()
            release.set()
            await task
            print("active_cleanup", json.dumps({"counted_as_orphan": counted[0], "deleted": deleted[0],
                                               "worker_status": active.status, "retries": active.retries}))

        try:
            backend.run_coro(probe()).result(10)
        finally:
            backend.shutdown()


if __name__ == "__main__":
    main()
