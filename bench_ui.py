"""Measures queue-view repaint cost: virtualized rows vs. one-widget-set-per-file.

Run:  .venv/bin/python bench_ui.py
"""
import os
import sys
import time
import asyncio
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

TMP = tempfile.mkdtemp(prefix="tgdl-bench-")
import interactive_downloader as dl  # noqa: E402

dl.CONFIG_FILE = os.path.join(TMP, "config.json")
dl.LOG_FILE = os.path.join(TMP, "downloader.log")

import customtkinter as ctk  # noqa: E402

SIZES = [50, 500, 1200, 4000, 8900]
# Building the "before" list is itself the bottleneck being measured, so the
# comparison stops where it stops being feasible to construct at all.
OLD_SIZES = [50, 200, 400, 800, 1200]
BATCH = dl.TARGET_BATCH_SIZE


def say(*a):
    print(*a, flush=True)


def make_items(n, chats=7):
    out = []
    for i in range(n):
        cid = 1000 + (i % chats)
        out.append(dl.DownloadItem(
            id=f"{cid}_{i}", chat_id=cid, chat_name=f"Chat {cid}",
            message=None, file_name=f"VID_2024042{i % 10}_1113{i:05d}.mp4",
            msg_id=i, ext=".mp4", file_size=7 * 1024 * 1024,
            status="Downloading" if i % 13 == 0 else "Queued",
            progress=(i % 100) / 100.0, downloaded_bytes=i * 1024, speed=1.2e6,
        ))
    return out


def bench_new():
    """Times one 'a batch was just queued' repaint at each queue size."""
    backend = dl.TelegramBackend(1, "hash", os.path.join(TMP, "sess"))
    backend.config = {"output_dir": os.path.join(TMP, "out"), "max_parallel_files": 3, "scan_depth": 0}
    backend.init_client = lambda: asyncio.sleep(0)  # no network

    app = dl.App(backend)
    app.update_idletasks()
    app.update()

    results = []
    for n in SIZES:
        items = make_items(n)
        with backend.queue_lock:
            backend.download_queue = items
            backend.queue_index = {i.id: i for i in items}

        app.queue_offset = 0
        app._render_queue_window()
        app.update_idletasks()

        # A batch arrives: queue grows by BATCH and the view repaints.
        samples = []
        for k in range(5):
            more = make_items(BATCH)
            for j, it in enumerate(more):
                it.id = f"{it.chat_id}_extra{k}_{j}"
            with backend.queue_lock:
                backend.download_queue.extend(more)
                for it in more:
                    backend.queue_index[it.id] = it
            t0 = time.perf_counter()
            app._render_queue_window()
            app._update_summary()
            app.update_idletasks()
            samples.append((time.perf_counter() - t0) * 1000)
        repaint = sum(samples) / len(samples)

        # Progress ticks for the visible rows (the 10 Hz hot path).
        visible = list(app.visible_rows.values())[:12]
        t0 = time.perf_counter()
        for r in visible:
            item = backend.queue_index.get(r["item_id"])
            if item:
                app.update_single_item_progress(item)
        app.update_idletasks()
        prog = (time.perf_counter() - t0) * 1000

        # Scrolling to the far end of the queue.
        t0 = time.perf_counter()
        app._on_queue_scroll("moveto", "1.0")
        app.update_idletasks()
        scroll = (time.perf_counter() - t0) * 1000

        widgets = len(app.queue_rows) * 5
        results.append((n, repaint, prog, scroll, widgets))
        say(f"  n={n:>5}  repaint={repaint:7.1f} ms   12 progress ticks={prog:6.2f} ms"
            f"   scroll-to-end={scroll:6.2f} ms   row widgets={widgets}")

    app.destroy()
    backend.shutdown()
    return results


def _make_old_row(frame, i):
    """One queue row the way refresh_queue_view used to build them."""
    row = ctk.CTkFrame(frame)
    row.pack(fill="x", padx=5, pady=2)
    ctk.CTkLabel(row, text=f"VID_{i:08d}.mp4", width=240, anchor="w").pack(side="left", padx=5)
    pb = ctk.CTkProgressBar(row, width=200)
    pb.set(0.5)
    pb.pack(side="left", padx=10)
    ctk.CTkLabel(row, text="42% (1.20 MB/s)", width=150).pack(side="left")
    ctk.CTkButton(row, text="Cancel", width=70, fg_color="red").pack(side="right", padx=5)


def bench_old(budget_s=300.0):
    """Reproduces the previous approach: 5 CTk widgets per item, packed into a scroll frame."""
    root = ctk.CTk()
    root.geometry("1150x760")
    frame = ctk.CTkScrollableFrame(root)
    frame.pack(fill="both", expand=True)
    root.update_idletasks()

    # Warm-up: the first CTk widget of each class pays one-off font/image setup.
    warm = ctk.CTkFrame(root)
    _make_old_row(warm, 0)
    root.update_idletasks()
    warm.destroy()
    root.update_idletasks()

    results = []
    built = 0
    started = time.perf_counter()
    for n in OLD_SIZES:
        while built < n:
            _make_old_row(frame, built)
            built += 1
            if time.perf_counter() - started > budget_s:
                say(f"  (stopped building at {built} rows - {budget_s:.0f}s budget exhausted)")
                root.destroy()
                return results
        root.update_idletasks()

        t0 = time.perf_counter()
        for _ in range(BATCH):
            _make_old_row(frame, built)
            built += 1
        root.update_idletasks()
        repaint = (time.perf_counter() - t0) * 1000
        results.append((n, repaint, built * 5))
        say(f"  n={n:>5}  one batch of {BATCH} = {repaint:8.1f} ms   live widgets={built * 5}")

    root.destroy()
    return results


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"

    old = new = None
    if mode in ("both", "before"):
        say("BEFORE - one widget set per queued file (previous implementation)")
        old = bench_old()
    if mode in ("both", "after"):
        say(f"\nAFTER - virtualized pool of {dl.ROW_POOL_SIZE} reusable rows "
            "(current implementation)")
        new = bench_new()

    if old and new:
        say("\n" + "=" * 74)
        say(f"{'queue size':>11} | {'before (ms)':>12} | {'after (ms)':>11} | "
            f"{'speedup':>8} | widgets before/after")
        say("-" * 74)
        by_n = {n: r for n, r, _p, _s, _w in new}
        widgets_after = dl.ROW_POOL_SIZE * 5
        for n, o, ow in old:
            r = by_n[n]
            say(f"{n:>11} | {o:>12.1f} | {r:>11.1f} | {o / r:>7.0f}x | {ow:>6} / {widgets_after}")
