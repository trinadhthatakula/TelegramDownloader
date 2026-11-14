import os
import subprocess
import sys
from typing import Tuple
from dotenv import load_dotenv
from telegram_auth import AccountStore, validate_credentials
import customtkinter as ctk
from tkinter import filedialog, messagebox, ttk
from engine import core as _core
from engine.core import *  # noqa: F403 - legacy public API
from engine.core import _CHAT_KIND_ORDER, _TERMINAL_STATUSES
from telegram_auth import AccountPage


class TelegramBackend(_core.TelegramBackend):
    def __init__(self, api_id, api_hash, session_path, accounts=None):
        super().__init__(api_id, api_hash, session_path, accounts, config_file=CONFIG_FILE)


def load_config():
    return _core.load_config(CONFIG_FILE)


def save_config(config_data):
    return _core.save_config(config_data, CONFIG_FILE)


def configure_logging():
    return _core.configure_logging(LOG_FILE)


# Maximum reusable rows. The actual visible count follows viewport size;
# allocating a pool must never imply that every row fits on screen.
ROW_POOL_SIZE = 40

# Same idea for the chat picker. An account with a few hundred dialogs used to
# build one CTkCheckBox each inside a single Tk callback, freezing the window
# for seconds while it appeared to be stuck loading.
CHAT_POOL_SIZE = 40

# Kinds offered as filter toggles, in display order.
CHAT_KINDS = ("Group", "Channel", "DM", "Bot")


class App(ctk.CTk):
    def __init__(self, backend: TelegramBackend):
        super().__init__()
        self.title("Telegram Multi-Chat Stream Downloader")
        self.geometry("1150x760")
        self.minsize(880, 600)
        ctk.set_appearance_mode("Dark")

        self.backend = backend

        # Virtualized chat picker state. Selection lives in a set of chat ids,
        # not in the widgets, so it survives row recycling and filtering.
        self.all_chats: List[ChatEntry] = []
        self.filtered_chats: List[ChatEntry] = []
        self.selected_chat_ids: Set[int] = set()
        self.chat_rows: List[Dict[str, Any]] = []
        self.chat_offset = 0
        self.chat_visible_count = 1
        self.queue_visible_count = 1
        self.chat_focus_index = 0
        self.queue_focus_index = 0
        self._resize_jobs = {}
        self._scan_request = None
        self._scan_busy = False
        self._scan_serial = 0
        self._errors = []
        self._background_callbacks = {}
        self._background_serial = 0
        self._starting_downloads = False
        self._saving_settings = False
        self.current_page = "groups"
        self.kind_filter_boxes: Dict[str, ctk.CTkCheckBox] = {}
        self.show_archived_box: Optional[ctk.CTkCheckBox] = None

        self.ext_checkboxes: Dict[str, ctk.CTkCheckBox] = {}
        self.group_widgets: Dict[Optional[int], ctk.CTkButton] = {}
        self.active_group_filter: Optional[int] = None  # None means "ALL"

        # Virtualized queue view state.
        self.queue_rows: List[Dict[str, Any]] = []
        self.visible_rows: Dict[str, Dict[str, Any]] = {}
        self.queue_offset = 0
        self._queue_dirty = True
        self._sidebar_dirty = True
        self._flood_notice = ""

        self._build_main_layout()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_queue()
        self._tick_refresh()
        self.page_accounts.busy = self.backend.accounts is not None
        self.page_accounts.update_buttons()
        self.backend.run_coro(self.backend.init_client())
        self.after(1200, self._offer_orphan_cleanup)

    # ------------------------------------------------------------------ plumbing

    def _poll_queue(self):
        """Keep each notification turn short enough for input/paint events to run."""
        if getattr(self, "_closing", False):
            return
        deadline = time.perf_counter() + 0.008
        try:
            while time.perf_counter() < deadline:
                try:
                    event_type, data = self.backend.event_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if event_type == "item_progress" and isinstance(data, DownloadItem):
                        if self.current_page == "queue":
                            self.update_single_item_progress(data)
                    else:
                        self._process_ui_update(event_type, data)
                except Exception as e:
                    logger.exception("Error handling UI event %r: %s", event_type, e)
        except Exception as e:
            logger.exception("Error in UI poll loop: %s", e)
        finally:
            self.after(16 if not self.backend.event_queue.empty() else 50, self._poll_queue)

    def _tick_refresh(self):
        """Coalesced repaint. Keeps per-event work off the hot path."""
        if getattr(self, "_closing", False):
            return
        try:
            if self.current_page != "queue":
                return
            if self._queue_dirty:
                self._queue_dirty = False
                self._render_queue_window()
            if self._sidebar_dirty:
                self._sidebar_dirty = False
                self.refresh_group_filter_sidebar()
            self._update_summary()
        except Exception as e:
            logger.exception("Error in refresh tick: %s", e)
        finally:
            self.after(400, self._tick_refresh)

    def _process_ui_update(self, event_type: str, data: Any):
        if getattr(self, "_closing", False):
            return
        if event_type == "login_state":
            self.page_accounts.handle(data)
            self.set_status(data["message"])
            if data["stage"] != "authorized":
                self.select_page("accounts")
        elif event_type == "login_finished":
            self.page_accounts.finished()
        elif event_type == "account_profile":
            self.page_accounts.show_profile(data)
        elif event_type == "account_reset":
            self.page_accounts.reset_profile()
            self._background_callbacks.clear()
            self._starting_downloads = False
            self._saving_settings = False
            self._scan_request = None
            self._scan_busy = False
            self._set_scan_busy(False)
            self.start_button.configure(state="normal")
            self.save_settings_button.configure(state="normal")
            self.all_chats.clear()
            self.filtered_chats.clear()
            self.selected_chat_ids.clear()
            self.active_group_filter = None
            self.chat_offset = self.queue_offset = 0
            self._flood_notice = ""
            self.search_chat_var.set("")
            self.search_queue_group_var.set("")
            self.selected_only_var.set(False)
            self.format_mode.set("all")
            self.dismiss_errors()
            for widget in self.group_widgets.values():
                widget.destroy()
            self.group_widgets.clear()
            self.queue_header.configure(text="Download Queue: All chats")
            self._update_chat_action_buttons()
            self.on_filter_chats()
            self.populate_scanned_extensions()
            self._queue_dirty = self._sidebar_dirty = True
        elif event_type == "auth_status":
            if data:
                self.set_status("Connected. Loading your chats...")
                self.backend.run_coro(self.backend.fetch_dialogs())
            else:
                self.select_page("accounts")
        elif event_type == "connect_failed":
            self.set_status(f"Could not reach Telegram: {data}")
            self.page_accounts.status.configure(text="Connection failed. Check your network and reconnect here.")
            self.select_page("accounts")
        elif event_type == "dialogs_progress":
            if isinstance(data, str):
                self.set_status(data)
            else:
                self.set_status(f"Loading your chats... {data} found so far.")
        elif event_type == "dialogs_loaded":
            # Only the pool is touched here, so this stays O(CHAT_POOL_SIZE)
            # however many dialogs the account has.
            self.all_chats = list(data or [])
            self.selected_chat_ids.intersection_update({e.id for e in self.all_chats})
            self.on_filter_chats()
            groups = sum(1 for e in self.all_chats if e.kind in ("Group", "Channel"))
            self.set_status(f"Loaded {len(self.all_chats)} chats "
                            f"({groups} groups/channels).")
        elif event_type in ("scan_started", "scan_progress", "scan_finished"):
            if isinstance(data, dict):
                if data.get("generation") != self._scan_request:
                    return
                self.scan_status.configure(text=data.get("message", ""))
                self.set_status(data.get("message", ""))
                if event_type == "scan_finished":
                    self._set_scan_busy(False)
                    if data.get("status") in ("complete", "completed", "success", "partial"):
                        self.populate_scanned_extensions(data.get("extensions", []))
                        if data.get("status") == "partial":
                            self.show_error(data.get("message", "Some chats could not be scanned. Scan again to retry."))
                    elif data.get("status") == "error":
                        self.show_error(data.get("message", "Scan failed. Try again."))
            elif event_type == "scan_progress":
                self.set_status(str(data))
        elif event_type == "extensions_scanned":
            self.populate_scanned_extensions(extra_exts=data)
            self.set_status(f"Scan complete: {len(data or [])} format(s) found.")
        elif event_type == "queue_updated":
            self._queue_dirty = True
            self._sidebar_dirty = True
        elif event_type == "item_updated":
            if self.current_page == "queue" and isinstance(data, DownloadItem):
                self.update_single_item_view(data)
            self._sidebar_dirty = True
            self._queue_dirty = True
        elif event_type == "flood_wait":
            self._flood_notice = f"⏳ Rate limited by Telegram — paused for {data}s"
            self.set_status(self._flood_notice)
        elif event_type == "flood_cleared":
            self._flood_notice = ""
            self.set_status("Rate limit expired. Resuming downloads.")
        elif event_type == "error":
            self.show_error(str(data))
        elif event_type == "background_result":
            request, result, error = data
            callbacks = self._background_callbacks.pop(request, None)
            if callbacks is None:
                return
            callback, finished = callbacks
            try:
                if error:
                    self.show_error(error)
                elif callback is not None:
                    callback(result)
            finally:
                if finished is not None:
                    finished()
        elif event_type == "action_finished":
            self.set_status(str(data))

    def set_status(self, text: str):
        self.status_label.configure(text=text[:160])

    def show_error(self, text: str):
        """Keep failures available after subsequent progress replaces the status."""
        self._errors.append(str(text))
        self._errors = self._errors[-100:]
        self.error_label.configure(text=f"{len(self._errors)} issue(s) — {str(text)[:100]}")
        self.error_bar.grid()
        self.set_status(str(text))

    def show_error_details(self):
        self._show_details("Download issues", "\n\n".join(self._errors))

    def dismiss_errors(self):
        self._errors.clear()
        self.error_bar.grid_remove()
        self._enable_keyboard_controls(self)

    def _show_details(self, title, text):
        window = ctk.CTkToplevel(self)
        window.title(title)
        window.geometry("620x360")
        window.transient(self)
        box = ctk.CTkTextbox(window, wrap="word")
        box.pack(fill="both", expand=True, padx=12, pady=12)
        box.insert("1.0", str(text))
        box.configure(state="disabled")
        ctk.CTkButton(window, text="Close", command=window.destroy).pack(pady=(0, 12))
        window.bind("<Escape>", lambda _event: window.destroy())
        window.after(50, window.focus_set)

    def _run_background(self, label, fn, on_success=None, on_finished=None):
        """Only the callable runs in a worker; all widget access stays on Tk."""
        if getattr(self, "_closing", False):
            return
        self._background_serial += 1
        request = self._background_serial
        self._background_callbacks[request] = (on_success, on_finished)
        self.set_status(label)

        async def run():
            try:
                result = await asyncio.to_thread(fn)
            except Exception as exc:
                self.backend.emit_event("background_result", (request, None, f"{label}: {exc}"))
            else:
                self.backend.emit_event("background_result", (request, result, None))
        self.backend.run_coro(run())

    # -------------------------------------------------------------------- layout

    def _build_main_layout(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar_frame = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(5, weight=1)

        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="Telegram Downloader",
                                       font=ctk.CTkFont(size=16, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=15, pady=(20, 10))

        self.btn_nav_groups = ctk.CTkButton(
            self.sidebar_frame, text="📁 Groups & Formats", anchor="w",
            command=lambda: self.select_page("groups")
        )
        self.btn_nav_groups.grid(row=1, column=0, padx=10, pady=5, sticky="ew")

        self.btn_nav_queue = ctk.CTkButton(
            self.sidebar_frame, text="⬇️ Download Queue", anchor="w",
            command=lambda: self.select_page("queue")
        )
        self.btn_nav_queue.grid(row=2, column=0, padx=10, pady=5, sticky="ew")

        self.btn_nav_settings = ctk.CTkButton(
            self.sidebar_frame, text="⚙️ Settings", anchor="w",
            command=lambda: self.select_page("settings")
        )
        self.btn_nav_settings.grid(row=3, column=0, padx=10, pady=5, sticky="ew")

        self.btn_nav_accounts = ctk.CTkButton(
            self.sidebar_frame, text="Accounts", anchor="w",
            command=lambda: self.select_page("accounts"))
        self.btn_nav_accounts.grid(row=4, column=0, padx=10, pady=5, sticky="ew")
        self.page_accounts = AccountPage(self, self)

        self.page_groups = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.page_queue = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.page_settings = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")

        self._build_page_groups()
        self._build_page_queue()
        self._build_page_settings()

        # Persistent status bar: backend failures used to be log-only, so an
        # outage looked identical to an idle window.
        self.status_label = ctk.CTkLabel(self, text="Starting up...", anchor="w",
                                         text_color="gray70")
        self.status_label.grid(row=2, column=0, columnspan=2, sticky="ew", padx=15, pady=(0, 8))
        self.error_bar = ctk.CTkFrame(self, fg_color=("#fff0df", "#493321"))
        self.error_bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=4)
        self.error_bar.grid_columnconfigure(0, weight=1)
        self.error_label = ctk.CTkLabel(self.error_bar, text="", anchor="w")
        self.error_label.grid(row=0, column=0, padx=8, sticky="ew")
        ctk.CTkButton(self.error_bar, text="View issues", width=90,
                      command=self.show_error_details).grid(row=0, column=1, padx=4, pady=4)
        ctk.CTkButton(self.error_bar, text="Dismiss", width=75,
                      command=self.dismiss_errors).grid(row=0, column=2, padx=4, pady=4)
        self.error_bar.grid_remove()

        self.select_page("groups")

    def _build_page_groups(self):
        self.page_groups.grid_columnconfigure(0, weight=1)
        self.page_groups.grid_columnconfigure(1, weight=1)
        self.page_groups.grid_rowconfigure(1, weight=1)

        left_box = ctk.CTkFrame(self.page_groups)
        left_box.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=5, pady=5)
        left_box.grid_rowconfigure(6, weight=1)
        left_box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(left_box, text="Step 1: Select Target Chats",
                     font=("Arial", 15, "bold")).grid(row=0, column=0, pady=(8, 4), padx=10, sticky="w")

        self.search_chat_var = ctk.StringVar()
        self.search_chat_var.trace_add("write", self.on_filter_chats)
        self.entry_chat_search = ctk.CTkEntry(left_box, placeholder_text="🔍 Search chats...",
                                              textvariable=self.search_chat_var)
        self.entry_chat_search.grid(row=1, column=0, padx=8, pady=(0, 5), sticky="ew")

        # Type filters. Every chat is loaded; these only narrow what is listed,
        # so a group can never be missing because of how it is classified.
        filter_row = ctk.CTkFrame(left_box, fg_color="transparent")
        filter_row.grid(row=2, column=0, padx=6, pady=(0, 2), sticky="ew")
        for kind in CHAT_KINDS:
            box = ctk.CTkCheckBox(filter_row, text=kind, width=70,
                                  command=self.on_filter_chats)
            if kind in ("Group", "Channel"):
                box.select()
            else:
                box.deselect()
            box.pack(side="left", padx=(2, 6))
            self.kind_filter_boxes[kind] = box

        self.show_archived_box = ctk.CTkCheckBox(left_box, text="Include archived",
                                                 command=self.on_filter_chats)
        self.show_archived_box.select()
        self.show_archived_box.grid(row=3, column=0, padx=10, pady=(0, 4), sticky="w")

        action_row = ctk.CTkFrame(left_box, fg_color="transparent")
        action_row.grid(row=4, column=0, padx=6, pady=(0, 4), sticky="ew")
        action_row.grid_columnconfigure(2, weight=1)
        ctk.CTkButton(action_row, text="Select all shown", width=120, height=26,
                      command=self.on_select_all_shown).grid(row=0, column=0, padx=2)
        ctk.CTkButton(action_row, text="Clear", width=60, height=26, fg_color="gray30",
                      command=self.on_clear_chat_selection).grid(row=0, column=1, padx=2)
        self.chat_counts_label = ctk.CTkLabel(action_row, text="No chats loaded yet.",
                                              width=1, height=50, anchor="w", text_color="gray70", wraplength=320)
        self.chat_counts_label.grid(row=1, column=0, columnspan=3, sticky="ew", padx=4, pady=4)
        self.selected_only_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(left_box, text="Show selected chats only", variable=self.selected_only_var,
                        command=self.on_filter_chats).grid(row=5, column=0, padx=10, pady=3, sticky="w")

        chat_body = ctk.CTkFrame(left_box, fg_color="transparent")
        self.chat_body = chat_body
        chat_body.grid(row=6, column=0, sticky="nsew", padx=8, pady=5)
        chat_body.grid_propagate(False)
        chat_body.grid_columnconfigure(0, weight=1)
        chat_body.grid_rowconfigure(CHAT_POOL_SIZE, weight=1)
        chat_body.bind("<Configure>", lambda _e: self._schedule_list_resize("chat"))

        for i in range(CHAT_POOL_SIZE):
            self.chat_rows.append(self._build_chat_row(chat_body, i))

        # CTkScrollbar.set() synchronously flushes every pending Tk paint. Native
        # scrollbars let the event loop paint after this short list callback.
        self.chat_scrollbar = ttk.Scrollbar(chat_body, orient="vertical", command=self._on_chat_scroll)
        self.chat_scrollbar.grid(row=0, column=1, rowspan=CHAT_POOL_SIZE + 1,
                                 sticky="ns", padx=(4, 0))
        self.chat_scrollbar.set(0.0, 1.0)

        for target in (chat_body, left_box):
            target.bind("<MouseWheel>", self._on_chat_wheel)
            target.bind("<Button-4>", self._on_chat_wheel)
            target.bind("<Button-5>", self._on_chat_wheel)

        right_box = ctk.CTkFrame(self.page_groups)
        right_box.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=5, pady=5)
        right_box.grid_rowconfigure(1, weight=1)
        right_box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(right_box, text="Step 2: Choose file formats",
                     font=("Arial", 15, "bold")).grid(row=0, column=0, pady=8, padx=10, sticky="w")
        self.format_mode = ctk.StringVar(value="all")
        format_modes = ctk.CTkFrame(right_box, fg_color="transparent")
        format_modes.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        ctk.CTkRadioButton(format_modes, text="All files", variable=self.format_mode, value="all",
                           command=self._update_format_mode).pack(anchor="w", pady=4)
        ctk.CTkRadioButton(format_modes, text="Selected formats", variable=self.format_mode, value="selected",
                           command=self._update_format_mode).pack(anchor="w", pady=4)
        right_box.grid_rowconfigure(1, weight=0)
        right_box.grid_rowconfigure(2, weight=1)
        self.ext_scroll = ctk.CTkScrollableFrame(right_box)
        self.ext_scroll.grid(row=2, column=0, sticky="nsew", padx=8, pady=5)

        self.lbl_ext_placeholder = ctk.CTkLabel(
            self.ext_scroll,
            text="All files will be downloaded.\n\n"
                 "To choose extensions, scan the selected chats first.",
            justify="center",
            text_color="gray70", wraplength=280,
        )
        self.lbl_ext_placeholder.pack(padx=20, pady=40)
        self.scan_status = ctk.CTkLabel(right_box, text="", anchor="w", wraplength=300)
        self.scan_status.grid(row=3, column=0, sticky="ew", padx=12, pady=4)

        btn_frame = ctk.CTkFrame(self.page_groups)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=10)

        self.scan_button = ctk.CTkButton(btn_frame, text="Scan selected chats", command=self.on_scan_extensions)
        self.scan_button.pack(side="left", padx=10, pady=8)
        self.cancel_scan_button = ctk.CTkButton(btn_frame, text="Cancel scan", width=100,
                                               state="disabled", command=self.on_cancel_scan)
        self.cancel_scan_button.pack(side="left", padx=4, pady=8)
        self.start_button = ctk.CTkButton(btn_frame, text="Add to downloads", fg_color="green",
                                         command=self.on_start_downloads)
        self.start_button.pack(side="right", padx=10, pady=8)

    def populate_scanned_extensions(self, extra_exts: Optional[List[str]] = None):
        """Populates Step 2 with the formats discovered by a scan."""
        had_choices = bool(self.ext_checkboxes)
        previous = {ext for ext, box in self.ext_checkboxes.items() if box.get()}
        for widget in self.ext_scroll.winfo_children():
            widget.destroy()
        self.ext_checkboxes.clear()

        scanned_list = sorted(set(extra_exts or []))
        if not scanned_list:
            ctk.CTkLabel(self.ext_scroll, text="Scan chats to discover available file extensions.",
                         wraplength=260, text_color="gray60").pack(padx=10, pady=10)
            return

        for ext in scanned_list:
            cb = ctk.CTkCheckBox(self.ext_scroll, text=ext)
            if not had_choices or ext in previous:
                cb.select()
            cb.pack(anchor="w", padx=5, pady=4)
            self._enable_keyboard_widget(cb)
            self.ext_checkboxes[ext] = cb
        self._update_format_mode()

    def _update_format_mode(self):
        selective = self.format_mode.get() == "selected"
        for cb in self.ext_checkboxes.values():
            cb.configure(state="normal" if selective else "disabled")
        if selective and not self.ext_checkboxes:
            self.scan_status.configure(text="Scan your selected chats, then choose at least one format.")

    def _build_page_queue(self):
        self.page_queue.grid_columnconfigure(0, weight=0, minsize=200)
        self.page_queue.grid_columnconfigure(1, weight=1)
        self.page_queue.grid_rowconfigure(0, weight=1)

        left_panel = ctk.CTkFrame(self.page_queue, width=200)
        left_panel.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        left_panel.grid_propagate(False)
        left_panel.pack_propagate(False)
        ctk.CTkLabel(left_panel, text="All downloads", font=("Arial", 14, "bold")).pack(pady=(8, 4))

        self.search_queue_group_var = ctk.StringVar()
        self.search_queue_group_var.trace_add("write", lambda *_: self._mark_sidebar_dirty())
        self.entry_queue_group_search = ctk.CTkEntry(left_panel, placeholder_text="🔍 Search filter...",
                                                     textvariable=self.search_queue_group_var)
        self.entry_queue_group_search.pack(fill="x", padx=10, pady=(0, 4))

        global_controls = ctk.CTkFrame(left_panel, fg_color="transparent")
        global_controls.pack(fill="x", padx=8, pady=3)
        ctk.CTkButton(global_controls, text="Pause all", width=80,
                      command=lambda: self._control_downloads("pause", all_chats=True)).pack(side="left", padx=2)
        ctk.CTkButton(global_controls, text="Resume all", width=80,
                      command=lambda: self._control_downloads("resume", all_chats=True)).pack(side="right", padx=2)
        ctk.CTkButton(left_panel, text="Clear completed", fg_color="gray30",
                      command=self.on_clear_completed).pack(fill="x", padx=10, pady=3)
        ctk.CTkButton(left_panel, text="Retry failed", fg_color="gray30",
                      command=self.on_retry_failed).pack(fill="x", padx=10, pady=3)
        ctk.CTkButton(left_panel, text="🛑 Cancel All", fg_color="gray30",
                      command=self.on_cancel_all).pack(fill="x", padx=10, pady=3)
        ctk.CTkButton(left_panel, text="📂 Open Folder", fg_color="gray30",
                      command=self.on_open_output_dir).pack(fill="x", padx=10, pady=3)

        self.group_filter_scroll = ctk.CTkScrollableFrame(left_panel)
        self.group_filter_scroll.pack(fill="both", expand=True, padx=5, pady=5)

        right_panel = ctk.CTkFrame(self.page_queue)
        right_panel.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)
        right_panel.grid_columnconfigure(0, weight=1)
        right_panel.grid_rowconfigure(3, weight=1)

        self.queue_header = ctk.CTkLabel(right_panel, text="Download Queue: ALL Groups",
                                         font=("Arial", 14, "bold"), anchor="w", wraplength=470)
        self.queue_header.grid(row=0, column=0, sticky="w", padx=10, pady=(8, 0))

        self.queue_summary = ctk.CTkLabel(right_panel, text="Queue is empty.",
                                          anchor="w", justify="left", wraplength=470, text_color="gray70")
        self.queue_summary.grid(row=1, column=0, sticky="ew", padx=10, pady=(2, 6))
        self.chat_actions = ctk.CTkFrame(right_panel, fg_color="transparent")
        self.chat_actions.grid(row=2, column=0, sticky="ew", padx=5, pady=(0, 4))
        self.chat_action_buttons = []
        for action, label in (("pause", "Pause chat"), ("resume", "Resume"),
                              ("cancel", "Cancel"), ("retry", "Retry failed")):
            button = ctk.CTkButton(self.chat_actions, text=label, width=75, state="disabled",
                                   command=lambda a=action: self._control_downloads(a))
            button.pack(side="left", padx=3, pady=2)
            self.chat_action_buttons.append(button)

        # A fixed pool of reusable rows replaces the old one-card-per-item
        # CTkScrollableFrame. Building 5 CTk widgets per file made every queued
        # batch a synchronous relayout of the entire list: ~54 ms at 50 rows but
        # ~313 ms at 1,200 and seconds at this project's ~8,900 files, which is
        # what made multi-group downloads unusable.
        body = ctk.CTkFrame(right_panel, fg_color="transparent")
        self.queue_body = body
        body.grid(row=3, column=0, sticky="nsew", padx=5, pady=(0, 5))
        body.grid_propagate(False)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(ROW_POOL_SIZE, weight=1)
        body.bind("<Configure>", lambda _e: self._schedule_list_resize("queue"))

        for i in range(ROW_POOL_SIZE):
            row = self._build_queue_row(body, i)
            self.queue_rows.append(row)

        self.queue_scrollbar = ttk.Scrollbar(body, orient="vertical", command=self._on_queue_scroll)
        self.queue_scrollbar.grid(row=0, column=1, rowspan=ROW_POOL_SIZE + 1,
                                  sticky="ns", padx=(4, 0))
        self.queue_scrollbar.set(0.0, 1.0)

        for target in (body, right_panel):
            target.bind("<MouseWheel>", self._on_queue_wheel)
            target.bind("<Button-4>", self._on_queue_wheel)
            target.bind("<Button-5>", self._on_queue_wheel)

    def _build_chat_row(self, parent, index: int) -> Dict[str, Any]:
        """Creates one reusable chat row. Called exactly CHAT_POOL_SIZE times."""
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=index, column=0, sticky="ew", padx=2)
        frame.grid_remove()
        frame.grid_columnconfigure(0, weight=1)

        row: Dict[str, Any] = {"frame": frame, "chat_id": None, "shown": False}
        cb = ctk.CTkCheckBox(frame, text="", width=1, command=lambda r=row: self._on_chat_toggle(r))
        cb.grid(row=0, column=0, sticky="ew", padx=(4, 4), pady=3)
        row["cb"] = cb

        kind = ctk.CTkLabel(frame, text="", width=64, anchor="e", text_color="gray60")
        kind.grid(row=0, column=1, sticky="e", padx=(4, 6))
        row["kind"] = kind
        info = ctk.CTkButton(frame, text="Info", width=42, height=24,
                             command=lambda r=row: self._show_chat_details(r))
        info.grid(row=0, column=2, padx=2)
        row["info"] = info

        for widget in (frame, cb, kind, info):
            self._bind_list_widget(widget, "chat")
        cb.bind("<FocusIn>", lambda _e, r=row: self._focus_row("chat", r))
        return row

    def _on_chat_toggle(self, row: Dict[str, Any]):
        chat_id = row.get("chat_id")
        if chat_id is None:
            return
        if row["cb"].get() == 1:
            self.selected_chat_ids.add(chat_id)
        else:
            self.selected_chat_ids.discard(chat_id)
        self._selection_changed()
        self._update_chat_counts()

    def _on_chat_scroll(self, *args):
        if not args:
            return
        total = len(self.filtered_chats)
        if args[0] == "moveto":
            self.chat_offset = int(round(float(args[1]) * total))
        elif args[0] == "scroll":
            step = self.chat_visible_count if len(args) > 2 and args[2] == "pages" else 1
            self.chat_offset += int(args[1]) * step
        self._render_chat_window()

    def _on_chat_wheel(self, event):
        if getattr(event, "num", None) == 4:
            delta = 3
        elif getattr(event, "num", None) == 5:
            delta = -3
        else:
            raw = getattr(event, "delta", 0)
            delta = (raw if abs(raw) < 30 else raw // 120) * 3
        if not delta:
            return
        self.chat_offset -= delta
        self._render_chat_window()

    def _render_chat_window(self):
        """Repaints the visible chat rows by rebinding the pool. Cost is O(pool)."""
        total = len(self.filtered_chats)
        max_offset = max(0, total - self.chat_visible_count)
        self.chat_offset = max(0, min(self.chat_offset, max_offset))

        for i, row in enumerate(self.chat_rows):
            idx = self.chat_offset + i
            if i < self.chat_visible_count and idx < total:
                entry = self.filtered_chats[idx]
                row["chat_id"] = entry.id
                label = entry.name if len(entry.name) <= 27 else entry.name[:24] + "..."
                if row["cb"].cget("text") != label:
                    row["cb"].configure(text=label)
                # select()/deselect() never fire the row command, so rebinding a
                # recycled row cannot corrupt the selection set.
                if entry.id in self.selected_chat_ids and not row["cb"].get():
                    row["cb"].select()
                elif entry.id not in self.selected_chat_ids and row["cb"].get():
                    row["cb"].deselect()
                tag = f"{entry.kind} 📦" if entry.archived else entry.kind
                if row["kind"].cget("text") != tag:
                    row["kind"].configure(text=tag)
                if not row["shown"]:
                    row["frame"].grid()
                    row["shown"] = True
            elif row["shown"]:
                row["frame"].grid_remove()
                row["shown"] = False
                row["chat_id"] = None

        if total <= self.chat_visible_count:
            self.chat_scrollbar.set(0.0, 1.0)
        else:
            self.chat_scrollbar.set(self.chat_offset / total,
                                    min(1.0, (self.chat_offset + self.chat_visible_count) / total))

    def _update_chat_counts(self):
        """Shows what was loaded, so 'are all my groups here' is answerable."""
        if not self.all_chats:
            self.chat_counts_label.configure(text="No chats loaded yet.")
            return
        counts: Dict[str, int] = {}
        for e in self.all_chats:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        parts = [f"{k} {counts[k]}" for k in CHAT_KINDS if counts.get(k)]
        archived = sum(1 for e in self.all_chats if e.archived)
        if archived:
            parts.append(f"archived {archived}")
        self.chat_counts_label.configure(
            text=f"{len(self.all_chats)} loaded ({', '.join(parts)}) · "
                 f"{len(self.filtered_chats)} shown · {len(self.selected_chat_ids)} selected")

    def _build_queue_row(self, parent, index: int) -> Dict[str, Any]:
        """Creates one reusable row. Called exactly ROW_POOL_SIZE times, at startup."""
        frame = ctk.CTkFrame(parent)
        frame.grid(row=index, column=0, sticky="ew", padx=5, pady=2)
        frame.grid_remove()
        frame.grid_columnconfigure(0, weight=1)

        lbl_name = ctk.CTkLabel(frame, text="", width=1, height=24, anchor="w")
        lbl_name.grid(row=0, column=0, columnspan=2, padx=(8, 4), pady=(2, 0), sticky="ew")
        source = ctk.CTkLabel(frame, text="", width=1, height=20, anchor="w", text_color="gray70")
        source.grid(row=1, column=0, columnspan=2, padx=8, sticky="ew")

        pbar = ctk.CTkProgressBar(frame, width=80)
        pbar.set(0.0)
        pbar.grid(row=2, column=0, padx=8, pady=(3, 6), sticky="ew")

        lbl_status = ctk.CTkLabel(frame, text="", width=135, height=20, anchor="e")
        lbl_status.grid(row=2, column=1, padx=4, pady=(0, 4))

        row: Dict[str, Any] = {
            "frame": frame, "name": lbl_name, "pbar": pbar,
            "status": lbl_status, "item_id": None, "shown": False,
            "source": source, "item": None,
        }

        btn_cancel = ctk.CTkButton(frame, text="Cancel", width=62, height=24, fg_color="red",
                                   command=lambda r=row: self._on_row_cancel(r))
        btn_cancel.grid(row=0, column=2, padx=(4, 8), pady=4)
        row["cancel"] = btn_cancel
        info = ctk.CTkButton(frame, text="Details", width=62, height=24,
                             command=lambda r=row: self._show_item_details(r))
        info.grid(row=1, column=2, rowspan=2, padx=(4, 8), pady=4)
        row["info"] = info

        for widget in (frame, lbl_name, lbl_status, source, pbar, btn_cancel, info):
            self._bind_list_widget(widget, "queue")
        info.bind("<FocusIn>", lambda _e, r=row: self._focus_row("queue", r))
        return row

    def _schedule_list_resize(self, kind):
        previous = self._resize_jobs.get(kind)
        if previous is not None:
            self.after_cancel(previous)
        self._resize_jobs[kind] = self.after(30, lambda: self._resize_list(kind))

    def _resize_list(self, kind):
        self._resize_jobs.pop(kind, None)
        body = self.chat_body if kind == "chat" else self.queue_body
        rows = self.chat_rows if kind == "chat" else self.queue_rows
        if not rows or body.winfo_height() <= 1:
            return
        # Request height includes font/widget scaling; external grid padding does not.
        frame = rows[0]["frame"]
        outside = int(frame._apply_widget_scaling(4) + 0.999) if kind == "queue" else 0
        row_height = max(1, frame.winfo_reqheight() + outside)
        capacity = min(len(rows), max(1, (body.winfo_height() - 2) // row_height))
        if kind == "chat":
            self.chat_visible_count = capacity
            self.chat_counts_label.configure(wraplength=max(180, body.winfo_width()))
            self._render_chat_window()
        else:
            self.queue_visible_count = capacity
            wrap = max(220, body.winfo_width() - 12)
            self.queue_summary.configure(wraplength=wrap)
            self.queue_header.configure(wraplength=wrap)
            self._render_queue_window()

    def _bind_list_widget(self, widget, kind):
        wheel = self._on_chat_wheel if kind == "chat" else self._on_queue_wheel
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(event, wheel)
        for key in ("Up", "Down", "Prior", "Next", "Home", "End"):
            widget.bind(f"<{key}>", lambda event, k=kind: self._on_list_key(k, event))
        if isinstance(widget, (ctk.CTkButton, ctk.CTkCheckBox)):
            widget._keyboard_enabled = True
            target = getattr(widget, "_text_label", None) or getattr(widget, "_canvas", widget)
            target.configure(takefocus=True)
            action = widget.invoke if isinstance(widget, ctk.CTkButton) else widget.toggle
            def activate(_event):
                action()
                return "break"
            widget.bind("<Return>", activate)
            widget.bind("<space>", activate)

    def _enable_keyboard_controls(self, parent):
        for widget in parent.winfo_children():
            self._enable_keyboard_widget(widget)
            self._enable_keyboard_controls(widget)

    def _enable_keyboard_widget(self, widget):
        if (isinstance(widget, (ctk.CTkButton, ctk.CTkCheckBox, ctk.CTkRadioButton))
                and not getattr(widget, "_keyboard_enabled", False)):
            target = getattr(widget, "_text_label", None) or getattr(widget, "_canvas", widget)
            target.configure(takefocus=True)
            action = widget.toggle if isinstance(widget, ctk.CTkCheckBox) else widget.invoke
            def activate(_event):
                action()
                return "break"
            widget.bind("<Return>", activate)
            widget.bind("<space>", activate)
            widget._keyboard_enabled = True

    def _focus_row(self, kind, row):
        rows = self.chat_rows if kind == "chat" else self.queue_rows
        offset = self.chat_offset if kind == "chat" else self.queue_offset
        setattr(self, f"{kind}_focus_index", offset + rows.index(row))

    def _on_list_key(self, kind, event):
        total = len(self.filtered_chats) if kind == "chat" else self._queue_window(0, 0)[0]
        if not total:
            return "break"
        count = self.chat_visible_count if kind == "chat" else self.queue_visible_count
        current = getattr(self, f"{kind}_focus_index")
        key = event.keysym
        target = {"Home": 0, "End": total - 1}.get(key, current + {
            "Up": -1, "Down": 1, "Prior": -count, "Next": count}.get(key, 0))
        target = max(0, min(total - 1, target))
        offset = getattr(self, f"{kind}_offset")
        offset = min(offset, target)
        if target >= offset + count:
            offset = target - count + 1
        setattr(self, f"{kind}_offset", offset)
        setattr(self, f"{kind}_focus_index", target)
        render = self._render_chat_window if kind == "chat" else self._render_queue_window
        render()
        rows = self.chat_rows if kind == "chat" else self.queue_rows
        rows[target - offset]["cb" if kind == "chat" else "info"].focus_set()
        return "break"

    def _show_chat_details(self, row):
        entry = next((e for e in self.all_chats if e.id == row.get("chat_id")), None)
        if entry is not None:
            self._show_details("Chat details", f"{entry.name}\n\n{entry.kind}\nChat ID: {entry.id}")

    def _show_item_details(self, row):
        item = row.get("item")
        if item is None:
            return
        details = (f"{item.file_name}\n\nChat: {item.chat_name}\n"
                   f"Size: {human_size(item.file_size or 0)}\nStatus: {self._format_item_status(item)}")
        error = getattr(item, "last_error", "")
        if error:
            details += f"\n\nIssue: {error}\nUse Retry failed after resolving the issue."
        self._show_details("File details", details)

    def _build_page_settings(self):
        self.page_settings.grid_columnconfigure(0, weight=1)

        box = ctk.CTkFrame(self.page_settings)
        box.pack(fill="both", expand=True, padx=10, pady=10)

        ctk.CTkLabel(box, text="⚙️ Application Settings",
                     font=("Arial", 16, "bold")).pack(anchor="w", padx=15, pady=(15, 10))

        f_dir = ctk.CTkFrame(box, fg_color="transparent")
        f_dir.pack(fill="x", padx=15, pady=8)
        ctk.CTkLabel(f_dir, text="Download Directory:", width=180, anchor="w").pack(side="left")
        self.entry_output_dir = ctk.CTkEntry(f_dir)
        self.entry_output_dir.pack(side="left", fill="x", expand=True, padx=5)
        self.entry_output_dir.insert(0, self.backend.config.get("output_dir", ""))
        ctk.CTkButton(f_dir, text="Browse...", width=80,
                      command=self.on_browse_output_dir).pack(side="right", padx=5)

        f_par = ctk.CTkFrame(box, fg_color="transparent")
        f_par.pack(fill="x", padx=15, pady=8)
        self.lbl_parallel = ctk.CTkLabel(
            f_par,
            text=f"Max Parallel File Downloads: {self.backend.config.get('max_parallel_files', 3)}",
            anchor="w"
        )
        self.lbl_parallel.pack(anchor="w")
        self.slider_parallel = ctk.CTkSlider(f_par, from_=1, to=10, number_of_steps=9,
                                             command=self.on_settings_slider_change)
        self.slider_parallel.set(self.backend.config.get("max_parallel_files", 3))
        self.slider_parallel.pack(fill="x", pady=4)

        f_dep = ctk.CTkFrame(box, fg_color="transparent")
        f_dep.pack(fill="x", padx=15, pady=8)
        self.lbl_depth = ctk.CTkLabel(f_dep, text="", anchor="w")
        self.lbl_depth.pack(anchor="w")
        # 0 means "unlimited"; the old 50..2000 range could never reach it.
        self.slider_depth = ctk.CTkSlider(f_dep, from_=0, to=2000, number_of_steps=40,
                                          command=self.on_settings_slider_change)
        self.slider_depth.set(self.backend.config.get("scan_depth", 500))
        self.slider_depth.pack(fill="x", pady=4)

        warn_frame = ctk.CTkFrame(box, fg_color=("gray85", "gray20"))
        warn_frame.pack(fill="x", padx=15, pady=15)
        ctk.CTkLabel(
            warn_frame,
            text="⚠️ High concurrency (4+ parallel downloads) may cause network lag or "
                 "temporary Telegram rate limits (FloodWait). Rate limits now pause and "
                 "auto-resume instead of failing the queue.",
            wraplength=600,
            justify="left"
        ).pack(padx=10, pady=10)

        f_parts = ctk.CTkFrame(box, fg_color="transparent")
        f_parts.pack(fill="x", padx=15, pady=8)
        self.lbl_parts = ctk.CTkLabel(f_parts, text="", anchor="w")
        self.lbl_parts.pack(side="left")
        ctk.CTkButton(f_parts, text="🧽 Clean up .part files", width=180,
                      command=self.on_clean_parts).pack(side="right")

        self.save_settings_button = ctk.CTkButton(box, text="Save settings", fg_color="green",
                                                 command=self.on_save_settings)
        self.save_settings_button.pack(anchor="e", padx=15, pady=15)

        self.on_settings_slider_change(None)

    def select_page(self, page_name: str):
        """Switches the active page."""
        self.current_page = page_name
        self.page_groups.grid_forget()
        self.page_queue.grid_forget()
        self.page_settings.grid_forget()
        self.page_accounts.grid_forget()
        self.btn_nav_accounts.configure(fg_color=("gray75", "gray25") if page_name == "accounts" else "transparent")

        self.btn_nav_groups.configure(fg_color=("gray75", "gray25") if page_name == "groups" else "transparent")
        self.btn_nav_queue.configure(fg_color=("gray75", "gray25") if page_name == "queue" else "transparent")
        self.btn_nav_settings.configure(fg_color=("gray75", "gray25") if page_name == "settings" else "transparent")

        if page_name == "accounts":
            self.page_accounts.grid(row=0, column=1, sticky="nsew", padx=15, pady=15)
        elif page_name == "groups":
            self.page_groups.grid(row=0, column=1, sticky="nsew", padx=15, pady=15)
            self._schedule_list_resize("chat")
        elif page_name == "queue":
            self.page_queue.grid(row=0, column=1, sticky="nsew", padx=15, pady=15)
            self._sidebar_dirty = True
            self._queue_dirty = True
            self._schedule_list_resize("queue")
        elif page_name == "settings":
            self.page_settings.grid(row=0, column=1, sticky="nsew", padx=15, pady=15)
            self._refresh_parts_label()

    # ------------------------------------------------------- virtualized queue

    def _filtered_items(self) -> List[DownloadItem]:
        return self.backend.snapshot_queue(self.active_group_filter)

    def _queue_window(self, offset, limit):
        return self.backend.snapshot_queue_window(self.active_group_filter, offset, limit)

    def _mark_sidebar_dirty(self):
        self._sidebar_dirty = True

    def _on_queue_scroll(self, *args):
        """Tk scrollbar protocol handler driving the virtual window offset."""
        items_len = self._queue_window(0, 0)[0]
        max_offset = max(0, items_len - self.queue_visible_count)
        if not args:
            return
        if args[0] == "moveto":
            self.queue_offset = int(round(float(args[1]) * items_len))
        elif args[0] == "scroll":
            amount = int(args[1])
            step = self.queue_visible_count if len(args) > 2 and args[2] == "pages" else 1
            self.queue_offset += amount * step
        self.queue_offset = max(0, min(self.queue_offset, max_offset))
        self._render_queue_window()

    def _on_queue_wheel(self, event):
        if getattr(event, "num", None) == 4:
            delta = 3
        elif getattr(event, "num", None) == 5:
            delta = -3
        else:
            raw = getattr(event, "delta", 0)
            delta = raw if abs(raw) < 30 else raw // 120
            delta *= 3
        if not delta:
            return
        items_len = self._queue_window(0, 0)[0]
        max_offset = max(0, items_len - self.queue_visible_count)
        self.queue_offset = max(0, min(self.queue_offset - delta, max_offset))
        self._render_queue_window()

    def _render_queue_window(self):
        """Repaints the visible window by rebinding pooled rows. Cost is O(pool)."""
        total, items = self._queue_window(self.queue_offset, self.queue_visible_count)
        max_offset = max(0, total - self.queue_visible_count)
        offset = max(0, min(self.queue_offset, max_offset))
        if offset != self.queue_offset:
            total, items = self._queue_window(offset, self.queue_visible_count)
        self.queue_offset = offset

        self.visible_rows.clear()
        for i, row in enumerate(self.queue_rows):
            if i < len(items) and i < self.queue_visible_count:
                item = items[i]
                self._bind_row(row, item)
                self.visible_rows[item.id] = row
                if not row["shown"]:
                    row["frame"].grid()
                    row["shown"] = True
            elif row["shown"]:
                row["frame"].grid_remove()
                row["shown"] = False
                row["item_id"] = None
                row["item"] = None

        if total <= self.queue_visible_count:
            self.queue_scrollbar.set(0.0, 1.0)
        else:
            first = self.queue_offset / total
            last = min(1.0, (self.queue_offset + self.queue_visible_count) / total)
            self.queue_scrollbar.set(first, last)

    def _bind_row(self, row: Dict[str, Any], item: DownloadItem):
        row["item_id"] = item.id
        row["item"] = item
        name = item.file_name if len(item.file_name) <= 38 else item.file_name[:35] + "..."
        if row["name"].cget("text") != name:
            row["name"].configure(text=name)
        source = item.chat_name if len(item.chat_name) <= 42 else item.chat_name[:39] + "..."
        row["source"].configure(text=source)
        row["pbar"].set(item.progress)
        row["status"].configure(text=self._format_item_status(item))
        row["cancel"].configure(
            state="disabled" if item.status in _TERMINAL_STATUSES else "normal"
        )

    def _on_row_cancel(self, row: Dict[str, Any]):
        item_id = row.get("item_id")
        if item_id:
            self.backend.cancel_download(item_id)

    def _format_item_status(self, item: DownloadItem) -> str:
        if item.status == "Downloading":
            return f"{int(item.progress * 100)}% ({item.speed / (1024 * 1024):.2f} MB/s)"
        if item.status == "Queued" and item.retries:
            return f"Queued (retry {item.retries})"
        return item.status

    def update_single_item_progress(self, item: DownloadItem):
        """Updates one visible row in place. Off-screen items cost nothing."""
        row = self.visible_rows.get(item.id)
        if row is not None and row.get("item_id") == item.id:
            row["pbar"].set(item.progress)
            row["status"].configure(text=self._format_item_status(item))

    def update_single_item_view(self, item: DownloadItem):
        row = self.visible_rows.get(item.id)
        if row is not None and row.get("item_id") == item.id:
            self._bind_row(row, item)

    def _update_summary(self):
        def describe(stats):
            if not stats.total:
                return "Queue is empty."
            parts = [f"{stats.total} files", f"{stats.done} done", f"{stats.active} active",
                     f"{stats.queued} queued"]
            for field, label in (("failed", "failed"), ("cancelled", "cancelled"), ("paused", "paused")):
                if getattr(stats, field, 0):
                    parts.append(f"{getattr(stats, field)} {label}")
            if stats.total_bytes:
                parts.append(human_size(stats.total_bytes))
            if stats.speed:
                parts.append(f"{stats.speed / (1024 * 1024):.1f} MB/s")
            return " · ".join(parts)
        text = "All chats: " + describe(self.backend.queue_stats())
        if self.active_group_filter is not None:
            text = "Selected chat: " + describe(self.backend.queue_stats(self.active_group_filter)) + "\n" + text
        if self._flood_notice:
            text += "\n" + self._flood_notice
        if self.queue_summary.cget("text") != text:
            self.queue_summary.configure(text=text)

    def refresh_group_filter_sidebar(self):
        """Rebuilds the chat filter buttons from a snapshot taken under lock."""
        filter_str = self.search_queue_group_var.get().strip().lower() \
            if hasattr(self, "search_queue_group_var") else ""

        snapshots = self.backend.snapshot_states()
        current_ids = {snapshot.chat_id for snapshot in snapshots}
        for chat_id in list(self.group_widgets):
            if chat_id is not None and chat_id not in current_ids:
                self.group_widgets.pop(chat_id).destroy()
        if self.active_group_filter is not None and self.active_group_filter not in current_ids:
            self.set_group_filter(None)

        if None not in self.group_widgets:
            btn_all = ctk.CTkButton(self.group_filter_scroll, text="All chats",
                                    command=lambda: self.set_group_filter(None))
            btn_all.pack(fill="x", padx=5, pady=3)
            self.group_widgets[None] = btn_all
            self._enable_keyboard_widget(btn_all)

        all_color = "gray35" if self.active_group_filter is None else "gray25"
        if self.group_widgets[None].cget("fg_color") != all_color:
            self.group_widgets[None].configure(fg_color=all_color)
        created = 0
        for snap in snapshots:
            name = snap.chat_name if len(snap.chat_name) <= 24 else snap.chat_name[:21] + "..."
            status = getattr(snap, "status", "")
            text_str = f"{name}\n{status} · {snap.total_completed}/{snap.total_discovered} found"
            should_show = not filter_str or filter_str in snap.chat_name.lower()
            btn = self.group_widgets.get(snap.chat_id)

            if btn is None:
                if not should_show:
                    continue
                # A large selection must not construct hundreds of Tk buttons
                # in one callback. Subsequent refresh turns finish the sidebar.
                if created >= 8:
                    self._sidebar_dirty = True
                    continue
                created += 1
                btn = ctk.CTkButton(self.group_filter_scroll, text=text_str,
                                    height=48,
                                    command=lambda cid=snap.chat_id: self.set_group_filter(cid))
                self.group_widgets[snap.chat_id] = btn
                self._enable_keyboard_widget(btn)
                if should_show:
                    btn.pack(fill="x", padx=5, pady=3)
                continue

            if btn.cget("text") != text_str:
                btn.configure(text=text_str)
            color = "gray35" if snap.chat_id == self.active_group_filter else "gray25"
            if btn.cget("fg_color") != color:
                btn.configure(fg_color=color)
            if should_show:
                if not btn.winfo_manager():
                    btn.pack(fill="x", padx=5, pady=3)
            else:
                btn.pack_forget()

    def set_group_filter(self, chat_id: Optional[int]):
        self.active_group_filter = chat_id
        self.queue_offset = 0
        if chat_id is None:
            self.queue_header.configure(text="Download Queue: All chats")
        else:
            name = next((s.chat_name for s in self.backend.snapshot_states()
                         if s.chat_id == chat_id), f"Chat {chat_id}")
            self.queue_header.configure(text=f"Download Queue: {name}")
        self._render_queue_window()
        self._update_summary()
        self._update_chat_action_buttons()
        self._sidebar_dirty = True

    def _update_chat_action_buttons(self):
        for button in self.chat_action_buttons:
            button.configure(state="normal" if self.active_group_filter is not None else "disabled")

    def _control_downloads(self, action, all_chats=False):
        if self.page_accounts.busy or (self.backend.accounts and not self.backend.authorized):
            self.set_status("Finish connecting your account first.")
            return
        chat_id = None if all_chats else self.active_group_filter
        if not all_chats and chat_id is None:
            return
        scope = "all chats" if all_chats else "the selected chat"
        if action == "cancel" and not messagebox.askyesno(
                "Cancel downloads", f"Cancel pending downloads from {scope}? You can resume them later."):
            return
        self.set_status(f"Applying {action} to {scope}...")
        async def run():
            try:
                count = await (self.backend.control_all(action) if all_chats
                               else self.backend.control_chat(chat_id, action))
                self.backend.emit_event("action_finished", f"{action.capitalize()} applied to {scope} ({count} items).")
            except Exception as exc:
                self.backend.emit_event("error", f"Could not {action} {scope}: {exc}")
        self.backend.run_coro(run())

    # ------------------------------------------------------------------ handlers

    def on_filter_chats(self, *_args):
        query = self.search_chat_var.get().strip().lower()
        kinds = {k for k, box in self.kind_filter_boxes.items() if box.get() == 1}
        show_archived = not self.show_archived_box or self.show_archived_box.get() == 1

        def keep(e: ChatEntry) -> bool:
            if self.selected_only_var.get() and e.id not in self.selected_chat_ids:
                return False
            if e.archived and not show_archived:
                return False
            if not self.selected_only_var.get() and e.kind not in kinds:
                return False
            return not query or query in e.name.lower()

        self.filtered_chats = [e for e in self.all_chats if keep(e)]
        self.chat_offset = 0
        self._render_chat_window()
        self._update_chat_counts()

    def on_select_all_shown(self):
        self.selected_chat_ids.update(e.id for e in self.filtered_chats)
        self._selection_changed()
        self._render_chat_window()
        self._update_chat_counts()
        self.set_status(f"Selected {len(self.selected_chat_ids)} chat(s).")

    def on_clear_chat_selection(self):
        self.selected_chat_ids.clear()
        self._selection_changed()
        self._render_chat_window()
        self._update_chat_counts()

    def _selected_dialogs(self) -> List[ChatEntry]:
        return [e for e in self.all_chats if e.id in self.selected_chat_ids]

    def _selection_changed(self):
        if self._scan_busy:
            self.on_cancel_scan()
        self._scan_request = None
        if self.ext_checkboxes:
            self.populate_scanned_extensions()
        self.scan_status.configure(text="Selection changed. Scan these chats to choose formats.")

    def _set_scan_busy(self, busy):
        self._scan_busy = busy
        self.scan_button.configure(state="disabled" if busy else "normal")
        self.cancel_scan_button.configure(state="normal" if busy else "disabled")

    def on_cancel_scan(self):
        self._scan_request = None
        self.backend.cancel_extension_scan()
        self._set_scan_busy(False)
        self.scan_status.configure(text="Scan cancelled. You can start a new scan.")

    def on_scan_extensions(self):
        if self._scan_busy:
            return
        if self.page_accounts.busy or (self.backend.accounts and not self.backend.authorized):
            self.set_status("Finish connecting your account first.")
            return
        selected = self._selected_dialogs()
        if not selected:
            messagebox.showwarning("Selection Required", "Please select at least one chat in Step 1.")
            return
        logger.info("Scanning extensions for %d chats.", len(selected))
        self.set_status(f"Scanning {len(selected)} chat(s)...")
        self.scan_status.configure(text=f"Scanning {len(selected)} chats...")
        self._set_scan_busy(True)
        self._scan_request = self.backend.begin_extension_scan(selected)

    def on_start_downloads(self):
        if self._starting_downloads:
            return
        if self.page_accounts.busy or (self.backend.accounts and not self.backend.authorized):
            self.set_status("Finish connecting your account first.")
            return
        selected_dialogs = self._selected_dialogs()
        if not selected_dialogs:
            messagebox.showwarning("Selection Required", "Please select at least one chat in Step 1.")
            return

        all_formats = self.format_mode.get() == "all"
        selected_exts = {ext for ext, cb in self.ext_checkboxes.items() if cb.get() == 1}
        if not all_formats and not selected_exts:
            self.scan_status.configure(text="Choose at least one format, or select All files.")
            self.set_status("No downloads added: choose at least one format.")
            return
        if self._scan_busy:
            self.on_cancel_scan()
        output_dir = self.backend.config.get("output_dir", "")
        generation = self.backend._account_generation
        self._starting_downloads = True
        self.start_button.configure(state="disabled")
        def finished():
            self._starting_downloads = False
            self.start_button.configure(state="normal")
        def ready(err):
            if generation != self.backend._account_generation:
                return
            if err:
                self.show_error(f"Download folder unusable: {err}. Choose a writable folder in Settings.")
                return
            self.backend.run_coro(self.backend.enqueue_selected_chats(
                selected_dialogs, selected_exts, all_formats=all_formats))
            self.set_status(f"Discovering files from {len(selected_dialogs)} chats...")
            self.select_page("queue")
        self._run_background("Checking download folder...", lambda: check_output_dir(output_dir), ready, finished)

    def on_clear_completed(self):
        self.queue_offset = 0
        self._control_downloads("clear", all_chats=True)

    def on_retry_failed(self):
        if self.page_accounts.busy or (self.backend.accounts and not self.backend.authorized):
            self.set_status("Finish connecting your account first.")
            return
        self._control_downloads("retry", all_chats=True)

    def on_cancel_all(self):
        self._control_downloads("cancel", all_chats=True)

    def on_open_output_dir(self):
        path = self.backend.config.get("output_dir", "")
        def open_folder():
            if not os.path.isdir(path):
                raise FileNotFoundError(f"'{path}' does not exist yet.")
            if sys.platform == "darwin":
                subprocess.run(["open", path], check=True, timeout=15)
            elif os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", path], check=True, timeout=15)
        self._run_background("Opening download folder...", open_folder)

    def on_browse_output_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.entry_output_dir.get() or BASE_DIR)
        if chosen:
            self.entry_output_dir.delete(0, "end")
            self.entry_output_dir.insert(0, chosen)

    def on_settings_slider_change(self, _val):
        par = int(self.slider_parallel.get())
        dep = int(self.slider_depth.get())
        self.lbl_parallel.configure(text=f"Max Parallel File Downloads: {par}")
        self.lbl_depth.configure(
            text=f"Chat Extension Scan Depth: {'Unlimited' if dep == 0 else f'{dep} Messages'}"
        )

    def on_save_settings(self):
        if self._saving_settings:
            return
        target = self.entry_output_dir.get().strip()
        if not target:
            self.show_error("Choose a download folder in Settings.")
            return
        config = {
            "output_dir": target,
            "max_parallel_files": int(self.slider_parallel.get()),
            "scan_depth": int(self.slider_depth.get()),
        }
        self._saving_settings = True
        self.save_settings_button.configure(state="disabled")
        def finished():
            self._saving_settings = False
            self.save_settings_button.configure(state="normal")
        def save():
            err = check_output_dir(os.path.abspath(os.path.expanduser(target)))
            if err:
                raise OSError(err)
            err = self.backend.update_config(config)
            if err:
                raise OSError(f"Could not save settings: {err}")
            return self.backend.config["output_dir"]
        def saved(directory):
            self.entry_output_dir.delete(0, "end")
            self.entry_output_dir.insert(0, directory)
            self.set_status("Settings saved.")
            self._refresh_parts_label()
        self._run_background("Saving settings...", save, saved, finished)

    def _refresh_parts_label(self):
        root = self.backend.config.get("output_dir", "")
        self.lbl_parts.configure(text="Checking incomplete files...")
        def checked(result):
            if self.backend.config.get("output_dir", "") != root:
                return
            count, total = result
            self.lbl_parts.configure(text=(f"{count} unowned .part file(s) ({human_size(total)})."
                                           if count else "No unowned .part files."))
        self._run_background("Checking incomplete files...", lambda: self.backend.sweep_orphan_parts(root), checked)

    def on_clean_parts(self):
        root = self.backend.config.get("output_dir", "")
        def inspected(result):
            count, total = result
            if not count:
                self.set_status("No unowned partial downloads to clean.")
                return
            if messagebox.askyesno("Delete partial downloads",
                    f"Delete {count} unowned partial file(s) from {root}, freeing {human_size(total)}?\n\n"
                    "Active downloads and files kept for resume are preserved."):
                def cleaned(result):
                    removed, freed = result
                    self._refresh_parts_label()
                    self.set_status(f"Removed {removed} .part file(s), freed {human_size(freed)}.")
                self._run_background("Cleaning partial files...", lambda: self.backend.delete_orphan_parts(root), cleaned)
        self._run_background("Checking incomplete files...", lambda: self.backend.sweep_orphan_parts(root), inspected)

    def _offer_orphan_cleanup(self):
        root = self.backend.config.get("output_dir", "")
        def checked(result):
            count, total = result
            if count and self.backend.config.get("output_dir", "") == root:
                self.set_status(f"{count} unowned .part file(s) ({human_size(total)}) — see Settings to clean up.")
        self._run_background("Checking incomplete files...", lambda: self.backend.sweep_orphan_parts(root), checked)

    def on_close(self):
        """Cancels in-flight work and disconnects before the window goes away."""
        if getattr(self, "_closing", False):
            return
        self._closing = True
        self.withdraw()
        stopped = threading.Event()
        def stop():
            try:
                self.backend.shutdown()
            finally:
                stopped.set()
        threading.Thread(target=stop, name="downloader-shutdown", daemon=True).start()
        def finish():
            if stopped.is_set():
                self.destroy()
            else:
                self.after(30, finish)
        finish()


def load_credentials() -> Tuple[int, str]:
    """Reads Telegram API credentials. Raises ValueError with a user-facing message."""
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    api_id = os.getenv("TG_API_ID")
    api_hash = os.getenv("TG_API_HASH")
    if not api_id or not api_hash:
        raise ValueError(
            "Missing TG_API_ID or TG_API_HASH.\n\n"
            f"Create a .env file next to the app ({BASE_DIR}) containing:\n"
            "  TG_API_ID=your_id\n  TG_API_HASH=your_hash"
        )
    return validate_credentials(api_id, api_hash)


def load_account_store():
    """Load saved profiles, importing the legacy environment only on first use."""
    try:
        api_id, api_hash = load_credentials()
    except ValueError:
        api_id, api_hash = 0, ""
    session_name = os.getenv("TG_SESSION_NAME", "downloader_session")
    session_path = session_name if os.path.isabs(session_name) else os.path.join(BASE_DIR, session_name)
    legacy = (api_id, api_hash, session_path) if api_id else None
    return AccountStore(BASE_DIR, legacy=legacy)


def main():
    """Entry point for both `python interactive_downloader.py` and the tg-download script."""
    configure_logging()
    try:
        accounts = load_account_store()
    except ValueError as exc:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Cannot read saved accounts", str(exc))
        root.destroy()
        return 1
    profile = accounts.data["profiles"].get(accounts.data["active"])
    if profile:
        api_id, api_hash, session_path = profile["api_id"], profile["api_hash"], profile["session"]
    else:
        api_id, api_hash, session_path = 0, "", os.path.join(BASE_DIR, "downloader_session")
    backend = TelegramBackend(api_id, api_hash, session_path, accounts=accounts)
    app = App(backend)
    try:
        app.mainloop()
    finally:
        backend.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
