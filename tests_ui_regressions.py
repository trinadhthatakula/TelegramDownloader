"""Real Tk regressions using only temporary files and fabricated Telegram data."""
import asyncio
import os
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from tkinter import TclError
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import customtkinter as ctk

import interactive_downloader as dl


class UIRegressionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(dl, "CONFIG_FILE", os.path.join(self.directory, "config.json")))
        self.stack.enter_context(patch.object(dl.App, "_offer_orphan_cleanup", return_value=None))
        self.backend = dl.TelegramBackend(1, "fake", os.path.join(self.directory, "unused"))
        self.backend.config["output_dir"] = os.path.join(self.directory, "downloads")
        self.backend.init_client = lambda: asyncio.sleep(0)
        self.app = dl.App(self.backend)
        self.pump()

    def pump(self, duration=0.15):
        end = time.monotonic() + duration
        while time.monotonic() < end:
            self.app.update_idletasks()
            self.app.update()
            time.sleep(0.005)

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.pump(0.02)
        self.assertTrue(predicate())

    def tearDown(self):
        ctk.set_widget_scaling(1.0)
        self.app.on_close()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            try:
                if not self.app.winfo_exists():
                    break
                self.app.update()
            except TclError:
                break
            time.sleep(0.01)
        self.backend.shutdown()
        self.stack.close()

    def seed(self, count=30):
        items = [dl.DownloadItem(str(i), i % 3, f"Group {i % 3}", None,
                                 f"File {i}.mp4", i, ".mp4", file_size=1024)
                 for i in range(count)]
        with self.backend.queue_lock:
            self.backend.download_queue = items
            self.backend.queue_index = {item.id: item for item in items}
        with self.backend.states_lock:
            self.backend.pagination_states = {
                i: dl.ChatPaginationState(i, f"Group {i}") for i in range(3)}
        self.app.all_chats = [dl.ChatEntry(i, f"Chat {i}", "Group", False) for i in range(count)]
        self.app.on_filter_chats()
        self.backend.emit_event("queue_updated")
        return items

    def assert_rows_fit(self, kind):
        rows = self.app.chat_rows if kind == "chat" else self.app.queue_rows
        body = self.app.chat_body if kind == "chat" else self.app.queue_body
        bottom = body.winfo_rooty() + body.winfo_height()
        shown = [row for row in rows if row["shown"]]
        self.assertTrue(shown)
        for row in shown:
            frame = row["frame"]
            self.assertLessEqual(frame.winfo_rooty() + frame.winfo_height(), bottom + 1)
            self.assertGreaterEqual(frame.winfo_rooty(), body.winfo_rooty())

    def test_last_rows_reachable_after_resize_and_scaling(self):
        self.seed()
        for width, height, scale in ((1024, 768, 1), (1150, 760, 1), (1150, 760, 1.25)):
            ctk.set_widget_scaling(scale)
            self.app.geometry(f"{width}x{height}")
            for page, kind in (("groups", "chat"), ("queue", "queue")):
                self.app.select_page(page)
                self.pump(0.3)
                scroll = self.app._on_chat_scroll if kind == "chat" else self.app._on_queue_scroll
                scroll("moveto", "1.0")
                self.pump()
                self.assert_rows_fit(kind)
                rows = self.app.chat_rows if kind == "chat" else self.app.queue_rows
                shown = [row for row in rows if row["shown"]]
                self.assertEqual(shown[-1]["chat_id" if kind == "chat" else "item_id"], 29 if kind == "chat" else "29")
                self.assertLess(len(shown), 30)

    def test_keyboard_end_reaches_final_chat_and_space_selects_it(self):
        self.seed(45)
        self.app.select_page("groups")
        self.pump()
        self.app._on_list_key("chat", SimpleNamespace(keysym="End"))
        self.pump()
        row = [row for row in self.app.chat_rows if row["shown"]][-1]
        self.assertEqual(row["chat_id"], 44)
        row["cb"].toggle()
        self.assertIn(44, self.app.selected_chat_ids)
        # The widget under the pointer has a binding, not only its parent frame.
        self.assertTrue(row["cb"]._text_label.bind("<MouseWheel>"))

    def test_explicit_formats_never_turn_empty_selection_into_all(self):
        self.seed()
        self.app.selected_chat_ids = {0}
        self.app.populate_scanned_extensions([".mp4", ".pdf"])
        self.app.format_mode.set("selected")
        self.app._update_format_mode()
        for checkbox in self.app.ext_checkboxes.values():
            checkbox.deselect()
        self.app.populate_scanned_extensions([".mp4", ".pdf", ".jpg"])
        self.assertTrue(all(checkbox.get() == 0 for checkbox in self.app.ext_checkboxes.values()))
        with patch.object(self.backend, "enqueue_selected_chats", new_callable=AsyncMock) as enqueue:
            self.app.on_start_downloads()
            self.pump()
            enqueue.assert_not_awaited()
            self.app.ext_checkboxes[".pdf"].select()
            self.app.on_start_downloads()
            self.wait_for(lambda: enqueue.await_count == 1)
            self.assertEqual(enqueue.await_args.args[1], {".pdf"})
            self.assertFalse(enqueue.await_args.kwargs["all_formats"])

    def test_account_reset_removes_old_sidebar_and_header(self):
        self.seed()
        self.app.select_page("queue")
        self.app.refresh_group_filter_sidebar()
        self.app.set_group_filter(1)
        self.app.search_queue_group_var.set("Group")
        with self.backend.states_lock:
            self.backend.pagination_states.clear()
        self.app._process_ui_update("account_reset", None)
        self.assertFalse(self.app.group_widgets)
        self.assertEqual(self.app.search_queue_group_var.get(), "")
        self.assertEqual(self.app.queue_header.cget("text"), "Download Queue: All chats")
        self.assertIsNone(self.app.active_group_filter)

    def test_account_reset_during_folder_check_allows_start_again(self):
        self.seed()
        self.app.selected_chat_ids = {0}
        entered, release = threading.Event(), threading.Event()

        def slow_check(_path):
            entered.set()
            release.wait(3)
            return "Old account folder error"

        try:
            with patch.object(dl, "check_output_dir", side_effect=slow_check):
                self.app.on_start_downloads()
                self.wait_for(entered.is_set)
                self.app._process_ui_update("account_reset", None)
                self.assertFalse(self.app._starting_downloads)
                self.assertEqual(self.app.start_button.cget("state"), "normal")
                release.set()
                self.pump(0.2)
            self.assertFalse(self.app._errors)
            self.seed()
            self.app.selected_chat_ids = {0}
            with patch.object(self.backend, "enqueue_selected_chats", new_callable=AsyncMock) as enqueue:
                self.app.on_start_downloads()
                self.wait_for(lambda: enqueue.await_count == 1)
        finally:
            release.set()

    def test_cancelled_scan_cannot_replace_new_results(self):
        self.seed()
        self.app.selected_chat_ids = {0}
        with patch.object(self.backend, "begin_extension_scan", return_value=7) as begin, \
                patch.object(self.backend, "cancel_extension_scan") as cancel:
            self.app.on_scan_extensions()
            self.app.on_scan_extensions()
            begin.assert_called_once()
            self.app.on_cancel_scan()
            cancel.assert_called_once()
            self.app._process_ui_update("scan_finished", {
                "generation": 7, "status": "completed", "extensions": [".old"], "message": "Old result"})
            self.assertNotIn(".old", self.app.ext_checkboxes)
            self.assertFalse(self.app._scan_busy)

    def test_filtered_totals_source_labels_and_explicit_action_scope(self):
        self.seed()
        self.app.select_page("queue")
        self.app.refresh_group_filter_sidebar()
        self.app.set_group_filter(1)
        self.pump()
        text = self.app.queue_summary.cget("text")
        self.assertIn("Selected chat: 10 files", text)
        self.assertIn("All chats: 30 files", text)
        self.assertTrue(all(row["source"].cget("text") == "Group 1"
                            for row in self.app.queue_rows if row["shown"]))
        with patch.object(self.backend, "control_chat", new_callable=AsyncMock, return_value=1) as control:
            self.app._control_downloads("pause")
            self.wait_for(lambda: control.await_count == 1)
            control.assert_awaited_once_with(1, "pause")

    def test_filesystem_work_leaves_tk_heartbeat_running(self):
        self.seed()
        self.app.selected_chat_ids = {0}
        ticks = []
        active = [True]
        def heartbeat():
            ticks.append(time.monotonic())
            if active[0]:
                self.app.after(10, heartbeat)
        def slow_check(_path):
            time.sleep(0.25)
            return "temporary test error"
        heartbeat()
        with patch.object(dl, "check_output_dir", side_effect=slow_check):
            started = time.monotonic()
            self.app.on_start_downloads()
            self.assertLess(time.monotonic() - started, 0.1)
            self.pump(0.4)
        active[0] = False
        self.assertGreater(len(ticks), 10)
        self.assertTrue(self.app._errors)
        self.app.set_status("Unrelated progress")
        self.assertTrue(self.app.error_bar.winfo_manager())

    def test_login_enter_password_reveal_and_resend_countdown(self):
        self.app.select_page("accounts")
        page = self.app.page_accounts
        page.api_id.insert(0, "123")
        page.api_hash.insert(0, "a" * 32)
        page.show_step("phone")
        page.country_code.insert(0, "+1")
        page.phone.insert(0, "4155552671")
        page.phone.focus_force()
        self.pump()
        with patch.object(self.backend, "authenticate", new_callable=AsyncMock) as authenticate:
            page.phone._entry.event_generate("<Return>")
            self.wait_for(lambda: authenticate.await_count == 1)
            self.assertEqual(authenticate.await_args.args[0], "begin_login")
        page.busy = False
        page.show_step("password")
        page.password.insert(0, "temporary-test-password")
        page.show_password.select()
        page.toggle_password_visibility()
        self.assertEqual(page.password.cget("show"), "")
        page.clear_secrets()
        self.assertEqual(page.password.cget("show"), "*")
        self.assertEqual(page.password.get(), "")
        page._last_action = "send_code"
        page.handle({"stage": "code", "message": "Code sent", "retry_after": 30})
        self.assertEqual(page.resend_button.cget("state"), "disabled")
        self.assertIn("Resend in", page.resend_button.cget("text"))
        page._resend_until = 0
        page._cooldown_tick()
        self.assertEqual(page.resend_button.cget("state"), "normal")


if __name__ == "__main__":
    unittest.main()
