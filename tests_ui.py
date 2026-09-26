"""Drives the real Tk UI headlessly: builds it, feeds events, clicks things.

Run:  .venv/bin/python tests_ui.py
"""
import os
import sys
import time
import asyncio
import tempfile
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

TMP = tempfile.mkdtemp(prefix="tgdl-uitest-")
import interactive_downloader as dl  # noqa: E402

dl.CONFIG_FILE = os.path.join(TMP, "config.json")
dl.LOG_FILE = os.path.join(TMP, "downloader.log")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


class FakeEntity:
    def __init__(self, did, name, bot=False):
        self.id, self.title, self.bot = did, name, bot


def chat(did, name, kind="Group", archived=False):
    """Builds the ChatEntry the backend now hands the UI."""
    return dl.ChatEntry(id=did, name=name, kind=kind, archived=archived,
                        entity=FakeEntity(did, name))


def items(n, chats=7):
    out = []
    for i in range(n):
        cid = 1000 + (i % chats)
        out.append(dl.DownloadItem(
            id=f"{cid}_{i}", chat_id=cid, chat_name=f"Chat {cid}", message=None,
            file_name=f"VID_{i:06d}.mp4", msg_id=i, ext=".mp4",
            file_size=5 * 1024 * 1024,
            status="Downloading" if i % 11 == 0 else "Queued",
            progress=(i % 100) / 100.0, downloaded_bytes=i * 512, speed=1.5e6,
        ))
    return out


def main():
    dl.configure_logging()
    backend = dl.TelegramBackend(1, "hash", os.path.join(TMP, "sess"))
    backend.config = {"output_dir": os.path.join(TMP, "out"), "max_parallel_files": 3, "scan_depth": 0}
    backend.init_client = lambda: asyncio.sleep(0)
    # This suite fabricates progress states without real worker tasks. Scheduler
    # integration is exercised separately; UI controls must never dispatch these
    # dummy Message objects to a client.
    backend._schedule_downloads = lambda: None
    backend._pump_discovery = lambda: None

    app = dl.App(backend)
    pump = lambda: (app.update_idletasks(), app.update())

    try:
        print("\n[1] window builds")
        pump()
        check("app constructed", app.winfo_exists() == 1)
        check("row pool preallocated", len(app.queue_rows) == dl.ROW_POOL_SIZE)
        check("no rows shown while queue is empty", not any(r["shown"] for r in app.queue_rows))
        check("summary says empty", "empty" in app.queue_summary.cget("text").lower())

        print("\n[2] pages switch")
        for page in ("queue", "settings", "groups", "queue"):
            app.select_page(page)
            pump()
        check("page switching raises nothing", True)

        print("\n[3] dialogs arrive")
        app.select_page("groups")
        for _ in range(3):
            pump(); time.sleep(0.04)
        dialogs = [chat(1000 + i, f"Chat {1000 + i}") for i in range(7)]
        backend.emit_event("dialogs_loaded", dialogs)
        for _ in range(6):
            pump(); time.sleep(0.06)
        check("7 chats loaded", len(app.all_chats) == 7, str(len(app.all_chats)))
        check("all 7 are listed", len(app.filtered_chats) == 7, str(len(app.filtered_chats)))
        shown = sum(1 for r in app.chat_rows if r["shown"])
        check("7 pooled rows visible", shown == 7, str(shown))
        check("pool stayed bounded", len(app.chat_rows) == dl.CHAT_POOL_SIZE,
              str(len(app.chat_rows)))

        app.search_chat_var.set("Chat 1003")
        pump()
        check("chat search filters the list", len(app.filtered_chats) == 1,
              str(len(app.filtered_chats)))
        check("only one row painted", sum(1 for r in app.chat_rows if r["shown"]) == 1)
        app.search_chat_var.set("")
        pump()

        print("\n[3b] a few hundred dialogs stay responsive and selectable")
        many = ([chat(2000 + i, f"Group {i:03d}") for i in range(260)]
                + [chat(3000 + i, f"Channel {i:03d}", kind="Channel") for i in range(40)]
                + [chat(4000 + i, f"Person {i:03d}", kind="DM") for i in range(120)]
                + [chat(5000 + i, f"Archived {i:03d}", archived=True) for i in range(15)])
        # Timed directly: the old code built one CTkCheckBox per dialog inside
        # this single call, so the whole window froze for its duration.
        t0 = time.perf_counter()
        app._process_ui_update("dialogs_loaded", many)
        callback_time = time.perf_counter() - t0
        app.update_idletasks()
        t_build = time.perf_counter() - t0
        pump()
        check("all 435 dialogs kept", len(app.all_chats) == 435, str(len(app.all_chats)))
        check("widget count still bounded", len(app.chat_rows) == dl.CHAT_POOL_SIZE)

        t0 = time.perf_counter()
        app._render_chat_window()
        app._update_chat_counts()
        app.update_idletasks()
        repaint_ms = (time.perf_counter() - t0) * 1000
        check(f"repaint is O(pool) -- {repaint_ms:.1f} ms", repaint_ms < 60,
              f"{repaint_ms:.1f} ms")
        check(f"435-dialog callback remains bounded -- {callback_time * 1000:.1f} ms",
              callback_time < 0.05, f"{callback_time * 1000:.1f} ms")
        # First native layout is distinct from subsequent repaints. Responsiveness
        # under continuing load is measured by the heartbeat regression suite.
        check(f"435-dialog first layout -- {t_build * 1000:.1f} ms",
              t_build < 0.25, f"{t_build * 1000:.1f} ms")

        # DMs are filtered out by default; groups/channels/archived are not.
        check("DMs hidden by default", len(app.filtered_chats) == 315,
              str(len(app.filtered_chats)))
        app.kind_filter_boxes["DM"].select()
        app.on_filter_chats()
        pump()
        check("enabling DMs reveals them", len(app.filtered_chats) == 435,
              str(len(app.filtered_chats)))
        app.show_archived_box.deselect()
        app.on_filter_chats()
        pump()
        check("archived can be hidden", len(app.filtered_chats) == 420,
              str(len(app.filtered_chats)))
        app.show_archived_box.select()
        app.on_filter_chats()
        pump()

        print("\n[3c] selection survives scrolling and filtering")
        app.on_select_all_shown()
        pump()
        check("select all shown selects every dialog",
              len(app.selected_chat_ids) == 435, str(len(app.selected_chat_ids)))
        check("_selected_dialogs agrees", len(app._selected_dialogs()) == 435,
              str(len(app._selected_dialogs())))
        app.on_clear_chat_selection()
        pump()

        # Click the first visible row, scroll far away, come back.
        first_id = app.filtered_chats[0].id
        app.chat_rows[0]["cb"].toggle()
        pump()
        check("clicking a row selects that chat", app.selected_chat_ids == {first_id},
              str(app.selected_chat_ids))
        app._on_chat_scroll("moveto", "0.5")
        pump()
        check("scrolled rows do not leak selection", app.selected_chat_ids == {first_id},
              str(app.selected_chat_ids))
        check("recycled row shows unselected state",
              app.chat_rows[0]["cb"].get() == 0)
        app._on_chat_scroll("moveto", "0.0")
        pump()
        check("returning restores the checked state",
              app.chat_rows[0]["cb"].get() == 1)
        app._on_chat_wheel(type("E", (), {"delta": -120, "num": None})())
        pump()
        check("wheel scrolling keeps selection", app.selected_chat_ids == {first_id},
              str(app.selected_chat_ids))

        # A selected chat must stay reachable even when filters would hide it.
        app.search_chat_var.set("zzzz-no-match")
        pump()
        check("selected chat survives a non-matching filter",
              first_id in {e.id for e in app._selected_dialogs()})
        app.search_chat_var.set("")
        app.on_clear_chat_selection()
        pump()

        print("\n[3d] hostile dialog data does not truncate the list")
        backend.emit_event("dialogs_loaded", [
            chat(9001, ""), chat(9002, "x" * 400), chat(9003, "emoji 🔑 name"),
            chat(9004, "Bot", kind="Bot"),
        ])
        for _ in range(4):
            pump(); time.sleep(0.05)
        check("all 4 odd dialogs kept", len(app.all_chats) == 4, str(len(app.all_chats)))
        backend.emit_event("dialogs_loaded", dialogs)
        for _ in range(4):
            pump(); time.sleep(0.05)

        print("\n[4] a large queue renders")
        app.select_page("queue")
        pool = items(8900)
        with backend.queue_lock:
            backend.download_queue = pool
            backend.queue_index = {i.id: i for i in pool}
        with backend.states_lock:
            for i in range(7):
                backend.pagination_states[1000 + i] = dl.ChatPaginationState(
                    chat_id=1000 + i, chat_name=f"Chat {1000 + i}",
                    total_discovered=1272, total_completed=100)
        backend.emit_event("queue_updated", None)
        for _ in range(14):
            pump(); time.sleep(0.05)

        # Time the repaint itself, not the pump loop's sleeps: this is the work
        # that used to block the Tk thread every time a batch was queued.
        samples = []
        for _ in range(5):
            t0 = time.perf_counter()
            app._render_queue_window()
            app.refresh_group_filter_sidebar()
            app._update_summary()
            app.update_idletasks()
            samples.append((time.perf_counter() - t0) * 1000)
        paint = min(samples)
        check("8900-item queue repaints in well under a frame", paint < 50,
              f"{paint:.1f} ms per repaint")
        print(f"        (repaint at 8900 queued items: {paint:.1f} ms)")
        check("exactly the viewport is visible", len(app.visible_rows) == app.queue_visible_count,
              str(len(app.visible_rows)))
        check("widget count still bounded",
              len(app.queue_rows) == dl.ROW_POOL_SIZE, str(len(app.queue_rows)))
        check("summary reflects 8900 files", "8900 files" in app.queue_summary.cget("text"),
              app.queue_summary.cget("text"))
        check("sidebar built 7 chats + ALL", len(app.group_widgets) == 8, str(len(app.group_widgets)))

        print("\n[5] scrolling")
        first_before = app.queue_rows[0]["name"].cget("text")
        app._on_queue_scroll("moveto", "1.0")
        pump()
        check("scroll to end changes the window",
              app.queue_rows[0]["name"].cget("text") != first_before)
        check("offset clamped at the end", app.queue_offset == 8900 - app.queue_visible_count,
              str(app.queue_offset))
        app._on_queue_scroll("moveto", "-5")
        pump()
        check("negative fraction clamps to 0", app.queue_offset == 0, str(app.queue_offset))
        app._on_queue_scroll("scroll", "3", "pages")
        pump()
        check("page scroll advances by the viewport size",
              app.queue_offset == 3 * app.queue_visible_count, str(app.queue_offset))

        class Wheel:
            def __init__(self, delta=0, num=None):
                self.delta, self.num = delta, num
        app._on_queue_wheel(Wheel(delta=-120))
        pump()
        check("mac/windows wheel scrolls down", app.queue_offset > 3 * app.queue_visible_count,
              str(app.queue_offset))
        app._on_queue_wheel(Wheel(num=4))
        pump()
        check("x11 button-4 scrolls up", app.queue_offset < 3 * app.queue_visible_count + 3,
              str(app.queue_offset))

        print("\n[6] chat filter")
        app.set_group_filter(1003)
        pump()
        check("header shows the chat", "Chat 1003" in app.queue_header.cget("text"),
              app.queue_header.cget("text"))
        shown = [app.backend.queue_index[r["item_id"]] for r in app.queue_rows if r["shown"]]
        check("only that chat's rows are bound", all(i.chat_id == 1003 for i in shown),
              str({i.chat_id for i in shown}))
        check("offset reset on filter change", app.queue_offset == 0)
        app.search_queue_group_var.set("Chat 1005")
        for _ in range(4):
            pump(); time.sleep(0.15)
        mapped = [cid for cid, b in app.group_widgets.items()
                  if cid is not None and b.winfo_ismapped()]
        check("sidebar search filters chats", mapped == [1005], str(mapped))
        app.search_queue_group_var.set("")
        for _ in range(4):
            pump(); time.sleep(0.15)
        app.set_group_filter(None)
        pump()
        check("back to all chats", "All chats" in app.queue_header.cget("text"))

        print("\n[7] per-row cancel button")
        target = app.queue_rows[0]
        target_id = target["item_id"]
        target["cancel"].invoke()
        for _ in range(6):
            pump(); time.sleep(0.06)
        check("clicking Cancel cancels that item",
              backend.queue_index[target_id].status == "Cancelled",
              backend.queue_index[target_id].status)
        check("cancel button disabled for terminal rows",
              app.visible_rows[target_id]["cancel"].cget("state") == "disabled")

        print("\n[8] progress updates only touch visible rows")
        live = next(i for i in pool[:app.queue_visible_count] if i.status != "Cancelled")
        live.status = "Downloading"
        live.progress = 0.77
        live.speed = 3 * 1024 * 1024
        app.update_single_item_progress(live)
        pump()
        check("visible row shows new progress",
              "77%" in app.visible_rows[live.id]["status"].cget("text"),
              app.visible_rows[live.id]["status"].cget("text"))
        offscreen = pool[5000]
        offscreen.progress = 0.5
        app.update_single_item_progress(offscreen)
        check("offscreen update is a no-op", offscreen.id not in app.visible_rows)

        print("\n[9] backend events")
        backend.emit_event("flood_wait", 42)
        pump(); time.sleep(0.1); pump()
        check("flood wait surfaces in the status bar", "42s" in app.status_label.cget("text"),
              app.status_label.cget("text"))
        backend.emit_event("error", "disk full")
        pump(); time.sleep(0.1); pump()
        check("errors surface in the status bar", "disk full" in app.status_label.cget("text"),
              app.status_label.cget("text"))
        backend.emit_event("connect_failed", None)  # deliberately malformed payload
        backend.emit_event("scan_progress", "Scanning 1/3")
        pump(); time.sleep(0.15); pump()
        check("a bad event does not stop later events",
              "Scanning 1/3" in app.status_label.cget("text"), app.status_label.cget("text"))

        print("\n[10] queue actions from the UI")
        app.on_retry_failed()
        for _ in range(5):
            pump(); time.sleep(0.05)
        check("retry_failed preserves intentionally cancelled row",
              backend.queue_index[target_id].status == "Cancelled",
              backend.queue_index[target_id].status)
        n_before = len(backend.download_queue)
        with backend.queue_lock:
            for i in backend.download_queue[:100]:
                i.status = "Completed"
        app.on_clear_completed()
        for _ in range(4):
            pump(); time.sleep(0.1)
        check("clear finished shrank the queue", len(backend.download_queue) == n_before - 100,
              str(len(backend.download_queue)))
        check("view survived the shrink", len(app.visible_rows) == app.queue_visible_count)

        print("\n[11] settings page")
        app.select_page("settings")
        pump()
        app.slider_parallel.set(7)
        app.slider_depth.set(0)
        app.on_settings_slider_change(None)
        pump()
        check("slider label updates", "7" in app.lbl_parallel.cget("text"), app.lbl_parallel.cget("text"))
        check("depth 0 shows Unlimited", "Unlimited" in app.lbl_depth.cget("text"),
              app.lbl_depth.cget("text"))
        new_out = os.path.join(TMP, "chosen")
        app.entry_output_dir.delete(0, "end")
        app.entry_output_dir.insert(0, new_out)
        app.on_save_settings()
        for _ in range(10):
            pump(); time.sleep(0.05)
        check("settings persisted", backend.config["max_parallel_files"] == 7
              and backend.config["output_dir"] == new_out, str(backend.config))
        check("config.json written", os.path.exists(dl.CONFIG_FILE))
        check("orphan .part label present", "part" in app.lbl_parts.cget("text").lower(),
              app.lbl_parts.cget("text"))

        print("\n[12] empty queue after everything is cleared")
        with backend.queue_lock:
            backend.download_queue = []
            backend.queue_index = {}
        app.select_page("queue")
        app._render_queue_window()
        app._update_summary()
        pump()
        check("all rows hidden again", not any(r["shown"] for r in app.queue_rows))
        check("summary back to empty", "empty" in app.queue_summary.cget("text").lower())

        print("\n[13] close")
        app.on_close()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            try:
                if not app.winfo_exists():
                    break
                pump()
            except Exception:
                break
            time.sleep(0.02)
        try:
            destroyed = app.winfo_exists() == 0
        except Exception:
            destroyed = True  # Tk raises once the interpreter is gone
        check("window destroyed", destroyed)
        check("loop thread stopped", not backend.thread.is_alive())

    except Exception:
        FAIL.append("unhandled")
        traceback.print_exc()
    finally:
        try:
            app.on_close()
        except Exception:
            pass
        backend.shutdown()

    print(f"\n{'=' * 60}\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
