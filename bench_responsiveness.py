"""Benchmark real Tk + scheduler + storage with generated traffic, never Telegram.

Run: .venv/bin/python bench_responsiveness.py
The only output retained is the JSON report; all downloaded bytes use a temporary directory.
"""
import asyncio
import json
import platform
import statistics
import tempfile
import time
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import interactive_downloader as dl
import transfer_scheduler as scheduler

GROUPS = 24
FILES_PER_GROUP = 80
RETAINED_HISTORY = 10_000
FILE_BYTES = 128 * 1024
HEARTBEAT_MS = 20
DURATION_SECONDS = 15
REPORT = Path(__file__).parent / "docs/audits/2026-09-26/validation-responsiveness.json"


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    return {"samples": len(ordered), "p50_ms": round(statistics.median(ordered), 3),
            "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * .95))], 3),
            "max_ms": round(ordered[-1], 3)}


class GeneratedClient:
    """Telethon-shaped transport; the production worker still performs every disk operation."""
    def __init__(self):
        self.active_groups = Counter()
        self.started_groups = set()
        self.peak_group_overlap = 0
        self.transfer_starts = 0
        self.transfer_completions = 0
        self.transfer_cancellations = 0
        self.discovery_active = 0
        self.discovery_peak = 0

    async def disconnect(self):
        pass

    async def iter_messages(self, entity, limit=None, offset_id=None, **_kwargs):
        self.discovery_active += 1
        self.discovery_peak = max(self.discovery_peak, self.discovery_active)
        try:
            await asyncio.sleep(.02)
            sent = 0
            for index in range(FILES_PER_GROUP, 0, -1):
                mid = 100_000 + index
                if offset_id is not None and mid >= offset_id:
                    continue
                if limit is not None and sent >= limit:
                    break
                await asyncio.sleep(.001)
                sent += 1
                yield SimpleNamespace(
                    id=mid, chat_id=entity, document=SimpleNamespace(id=entity * 1_000_000 + mid),
                    photo=None, file=SimpleNamespace(name=f"generated_{mid}.bin", ext=".bin", size=FILE_BYTES))
        finally:
            self.discovery_active -= 1

    async def download_media(self, message, file, progress_callback=None):
        group = message.chat_id
        self.active_groups[group] += 1
        self.started_groups.add(group)
        self.transfer_starts += 1
        self.peak_group_overlap = max(self.peak_group_overlap, len(self.active_groups))
        written = 0
        try:
            while written < FILE_BYTES:
                await asyncio.sleep(.015)
                data = bytes([group % 251]) * min(16 * 1024, FILE_BYTES - written)
                await file.write(data)
                written += len(data)
                if progress_callback:
                    progress_callback(written, FILE_BYTES)
            self.transfer_completions += 1
            return file
        except asyncio.CancelledError:
            self.transfer_cancellations += 1
            raise
        finally:
            self.active_groups[group] -= 1
            if not self.active_groups[group]:
                del self.active_groups[group]


def main():
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(),
              "python": platform.python_version(), "platform": platform.platform(),
              "scenario": {"groups": GROUPS, "files_per_group": FILES_PER_GROUP,
                           "retained_completed_rows": RETAINED_HISTORY, "bytes_per_file": FILE_BYTES,
                           "parallel_files": 3, "heartbeat_interval_ms": HEARTBEAT_MS,
                           "workload_seconds": DURATION_SECONDS, "uses_real_telegram": False,
                           "uses_real_tk_scheduler_and_storage": True},
              "capacity_semantics": "pending_files and pending_plus_reserved count admitted transfer slots; "
                                    "queued_and_active_rows also includes restored rows awaiting admission."}
    with ExitStack() as stack:
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="tgdl-responsive-")))
        stack.enter_context(patch.object(dl, "CONFIG_FILE", str(directory / "config.json")))
        stack.enter_context(patch.object(dl, "LOG_FILE", str(directory / "benchmark.log")))
        stack.enter_context(patch.object(dl.messagebox, "askyesno", return_value=True))
        backend = dl.TelegramBackend(1, "generated-not-a-credential", str(directory / "unused"))
        backend.config.update(output_dir=str(directory / "downloads"), max_parallel_files=3, scan_depth=500)
        client = GeneratedClient()
        backend.client = client
        backend.init_client = lambda: asyncio.sleep(0)
        chats = [dl.ChatEntry(1000 + i, f"Generated group {i + 1:02}", "Group", False, 1000 + i)
                 for i in range(GROUPS)]
        history = [dl.DownloadItem(f"{1000 + i % GROUPS}_{i + 1}", 1000 + i % GROUPS,
                                  f"Generated group {i % GROUPS + 1:02}", None, f"history_{i}.bin", i + 1,
                                  ".bin", file_size=FILE_BYTES, status="Completed", progress=1)
                   for i in range(RETAINED_HISTORY)]
        with backend.queue_lock:
            backend.download_queue.extend(history)
            backend.queue_index.update({item.id: item for item in history})
            backend._queue_items_added(history)
        started = time.perf_counter()
        app = dl.App(backend)
        report["initial_layout"] = {"construct_app_ms": round((time.perf_counter() - started) * 1000, 3)}
        app.all_chats = chats
        app.selected_chat_ids = {chat.id for chat in chats}
        app.on_filter_chats()
        started = time.perf_counter()
        app.select_page("queue")
        app._render_queue_window()
        app.update_idletasks()
        app._resize_list("queue")
        app.update_idletasks()
        report["initial_layout"]["first_queue_layout_ms"] = round((time.perf_counter() - started) * 1000, 3)
        report["initial_layout"]["visible_queue_rows"] = app.queue_visible_count

        beats = []
        actions = []
        operations = Counter()
        method_samples = {}
        for owner, name in ((app, "_render_queue_window"), (app, "_bind_row"),
                            (app, "set_group_filter"), (app, "_on_queue_scroll"),
                            (app, "_update_summary"), (app, "refresh_group_filter_sidebar"),
                            (backend, "snapshot_queue_window")):
            original = getattr(owner, name)
            values = method_samples.setdefault(name, [])
            def timed(*args, method=original, samples=values, **kwargs):
                start = time.perf_counter()
                try:
                    return method(*args, **kwargs)
                finally:
                    samples.append((time.perf_counter() - start) * 1000)
            setattr(owner, name, timed)
        phase = ["warmup"]
        running = [True]
        previous_beat = [time.perf_counter()]
        peaks = Counter()
        capacity_exceedances = []
        watchdog = [False]
        epoch = time.perf_counter()

        def heartbeat():
            now = time.perf_counter()
            beats.append((phase[0], (now - previous_beat[0]) * 1000))
            previous_beat[0] = now
            if running[0]:
                app.after(HEARTBEAT_MS, heartbeat)

        async def observe():
            while running[0]:
                if getattr(backend, "_scheduler_ready", False):
                    pending = backend._global_pending()
                    reserved = sum(backend._discovery_reserved.values())
                    peaks["pending_files"] = max(peaks["pending_files"], pending)
                    peaks["pending_plus_reserved"] = max(peaks["pending_plus_reserved"], pending + reserved)
                    peaks["per_chat_pending"] = max(peaks["per_chat_pending"], max(
                        (backend._pending_for_chat(cid) for cid in backend.pagination_states), default=0))
                    peaks["discovery_tasks"] = max(peaks["discovery_tasks"], len(backend._discovery_tasks))
                    peaks["active_workers"] = max(peaks["active_workers"], len(backend._active_workers))
                    totals = backend.queue_stats()
                    peaks["queued_and_active_rows"] = max(peaks["queued_and_active_rows"],
                                                           totals.queued + totals.active)
                    peaks["deferred_resume_rows"] = max(peaks["deferred_resume_rows"],
                                                        len(getattr(backend, "_deferred_ids", ())))
                    if pending + reserved > scheduler.GLOBAL_PENDING_LIMIT and len(capacity_exceedances) < 8:
                        capacity_exceedances.append({"at_seconds": round(time.perf_counter() - epoch, 3),
                                                     "pending": pending, "reserved": reserved})
                peaks["ui_event_backlog"] = max(peaks["ui_event_backlog"], backend.event_queue.qsize())
                await asyncio.sleep(.01)

        def action(name, fn):
            if not running[0]:
                return
            start = time.perf_counter()
            fn()
            actions.append({"action": name, "at_seconds": round(start - epoch, 3),
                            "callback_ms": round((time.perf_counter() - start) * 1000, 3)})
            operations[name] += 1

        def scroll():
            if running[0] and phase[0] != "drain":
                if app.current_page == "queue":
                    number = operations["scroll"]
                    group = None if number % 4 == 0 else chats[number % GROUPS].id
                    def move():
                        app.set_group_filter(group)
                        app._on_queue_scroll("moveto", str((0, 1, .5)[number % 3]))
                    action("scroll", move)
                app.after(200, scroll)

        def scoped_control(index, operation):
            app.set_group_filter(chats[index].id)
            app._control_downloads(operation)

        original_update = app._process_ui_update
        def process(event, data):
            if event == "benchmark_finished":
                running[0] = False
                report["final_queue"] = data["stats"]
                report["drain_ms"] = data["drain_ms"]
                report["persistent_ui_errors"] = list(app._errors)
                app.on_close()
            else:
                original_update(event, data)
        app._process_ui_update = process

        async def drain():
            start = time.perf_counter()
            await backend.control_all("cancel")
            await backend.wait_for_scheduler_idle()
            backend.emit_event("benchmark_finished", {"stats": asdict(backend.queue_stats()),
                                "drain_ms": round((time.perf_counter() - start) * 1000, 3)})

        def finish():
            phase[0] = "drain"
            backend.run_coro(drain())

        def timeout():
            watchdog[0] = True
            running[0] = False
            app.on_close()

        app.after(HEARTBEAT_MS, heartbeat)
        backend.run_coro(observe())
        app.after(250, lambda: action("enqueue", app.on_start_downloads))
        app.after(2000, lambda: phase.__setitem__(0, "steady"))
        app.after(1000, scroll)
        app.after(3500, lambda: action("pause_chat", lambda: scoped_control(0, "pause")))
        app.after(4500, lambda: action("resume_chat", lambda: scoped_control(0, "resume")))
        app.after(6500, lambda: action("cancel_chat", lambda: scoped_control(1, "cancel")))
        app.after(7500, lambda: action("resume_chat", lambda: scoped_control(1, "resume")))
        app.after(9500, lambda: action("show_groups", lambda: app.select_page("groups")))
        app.after(10000, lambda: action("show_queue", lambda: app.select_page("queue")))
        app.after(DURATION_SECONDS * 1000, finish)
        app.after((DURATION_SECONDS + 15) * 1000, timeout)
        try:
            app.mainloop()
        finally:
            running[0] = False
            backend.shutdown()

        downloaded = [path.resolve() for path in (directory / "downloads").rglob("*.bin")]
        completed_paths = {Path(item.output_path).resolve(): item.chat_id for item in backend.snapshot_queue()
                           if item.status in ("Completed", "Skipped (Exists)")
                           and getattr(item, "output_path", None)}
        payloads_correct = (set(downloaded) == set(completed_paths) and all(
            path.read_bytes() == bytes([chat_id % 251]) * FILE_BYTES
            for path, chat_id in completed_paths.items()))
        report["heartbeat"] = {name: distribution([gap for stage, gap in beats if stage == name])
                               for name in ("warmup", "steady", "drain")}
        report["heartbeat"]["steady_lateness"] = distribution(
            [max(0, gap - HEARTBEAT_MS) for stage, gap in beats if stage == "steady"])
        report["action_callback_costs"] = {name: distribution([a["callback_ms"] for a in actions
                                                              if a["action"] == name]) for name in operations}
        report["method_callback_costs"] = {name: distribution(values) for name, values in method_samples.items()}
        report["peaks"] = dict(peaks)
        report["capacity_exceedances"] = capacity_exceedances
        report["traffic"] = {"groups_started": len(client.started_groups),
                             "simultaneous_groups": client.peak_group_overlap,
                             "transfer_starts": client.transfer_starts,
                             "transport_completions": client.transfer_completions,
                             "transport_cancellations": client.transfer_cancellations,
                             "discovery_overlap": client.discovery_peak,
                             "final_files_on_disk": len(downloaded),
                             "every_final_file_has_expected_size": all(p.stat().st_size == FILE_BYTES for p in downloaded),
                             "every_final_file_has_expected_payload": payloads_correct,
                             "payload_files_checked": len(completed_paths),
                             "payload_validation": "Full byte comparison after the measured Tk phase."}
        report["watchdog_triggered"] = watchdog[0]
        final = report.get("final_queue", {})
        steady = report["heartbeat"]["steady"]
        report["checks"] = {
            "no_watchdog_timeout": not watchdog[0],
            "discovery_bounded": peaks["discovery_tasks"] <= scheduler.DISCOVERY_CONCURRENCY,
            "pending_bounded": peaks["pending_plus_reserved"] <= scheduler.GLOBAL_PENDING_LIMIT,
            "per_chat_pending_bounded": peaks["per_chat_pending"] <= scheduler.PER_CHAT_PENDING_LIMIT,
            "workers_bounded": peaks["active_workers"] <= 3,
            "multiple_groups_transfer_concurrently": client.peak_group_overlap >= 3,
            "every_group_started": len(client.started_groups) == GROUPS,
            "cancellation_drained_workers": final.get("active") == 0 and final.get("queued") == 0,
            "no_download_failures": final.get("failed") == 0,
            "files_match_completed_count": len(downloaded) == final.get("done", -1) - RETAINED_HISTORY,
            "complete_files_have_correct_sizes": report["traffic"]["every_final_file_has_expected_size"],
            "complete_files_have_correct_payloads": payloads_correct,
            "steady_heartbeat_p95_under_50ms": steady["p95_ms"] is not None and steady["p95_ms"] < 50,
            "steady_heartbeat_max_under_200ms": steady["max_ms"] is not None and steady["max_ms"] < 200,
        }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if all(report["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
