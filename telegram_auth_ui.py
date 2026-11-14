"""Tk adapter for the authentication service."""
import webbrowser
import customtkinter as ctk
from PIL import Image
from telegram_auth import (
    API_SETUP_URL,
    phone_with_country_code,
    profile_avatar,
    validate_credentials,
)


class AccountPage(ctk.CTkScrollableFrame):
    """One visible sign-in step at a time, with API setup kept separate."""

    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.backend = app.backend
        self.selected_key = self.backend.profile_id
        self.busy = False
        self.stage = "credentials"
        self.screen = "credentials"
        self.buttons = []
        self.fields = []
        self.sections = {}
        self._last_action = None
        self._resend_until = 0.0
        self._login_retry_until = 0.0
        self._cooldown_job = None
        ctk.CTkLabel(self, text="Telegram accounts", font=("Arial", 22, "bold")).pack(anchor="w", pady=(12, 8))
        self.picker = ctk.CTkOptionMenu(self, values=["Add account"], command=self.choose)
        self.picker.pack(fill="x", pady=6)
        management = ctk.CTkFrame(self, fg_color="transparent")
        management.pack(fill="x", pady=4)
        self.connect_button = self.button(management, "Connect saved account", self.switch)
        self.button(management, "API setup", lambda: self.show_step("credentials"))
        self.step_label = ctk.CTkLabel(self, text="", font=("Arial", 18, "bold"))
        self.step_label.pack(anchor="w", pady=(18, 6))
        self.status = ctk.CTkLabel(self, text="Waiting for connection...", justify="left", wraplength=660)
        self.status.pack(anchor="w", pady=(0, 12))

        setup = self.section("credentials")
        self.note(setup, "Open my.telegram.org, sign in and choose API development tools. "
                  "Copy the API ID and hash here. You only need this setup once.")
        self.button(setup, "Open Telegram API setup in browser", self.open_browser)
        self.api_id = self.entry(setup, "API ID")
        self.api_hash = self.entry(setup, "API hash", secret=True)
        self.button(setup, "Save API details & continue", self.save)

        phone = self.section("phone")
        self.note(phone, "Enter your country calling code and phone number to receive a Telegram login code.")
        self.country_code = self.entry(phone, "Country code (for example +91)")
        self.phone = self.entry(phone, "Phone number (without country code)")
        self.send_button = self.button(phone, "Send login code", self.send_code)

        code = self.section("code")
        self.note(code, "Check your Telegram app or phone for the login code.")
        self.code = self.entry(code, "Login code")
        self.code_button = self.button(code, "Continue", self.verify_code)
        self.resend_button = self.button(code, "Resend code", self.send_code)
        self.button(code, "Change phone number", self.change_phone)

        password = self.section("password")
        self.note(password, "Your account uses two-step verification. Enter your Telegram password to finish signing in.")
        self.password = self.entry(password, "Two-step verification password", secret=True)
        self.show_password = ctk.CTkCheckBox(password, text="Show password",
                                            command=self.toggle_password_visibility)
        self.show_password.pack(anchor="w", padx=12, pady=(0, 8))
        self.fields.append(self.show_password)
        self.password_button = self.button(password, "Log in", self.verify_password)
        self.button(password, "Use another phone number", self.change_phone)

        success = self.section("authorized")
        identity = ctk.CTkFrame(success, fg_color="transparent")
        identity.pack(fill="x", padx=12, pady=16)
        self.avatar_image = None
        # Keep a transparent image for initials: CTkLabel does not reliably clear
        # the underlying Tk image when configured with image=None.
        self.empty_avatar_image = ctk.CTkImage(
            light_image=Image.new("RGBA", (96, 96), (0, 0, 0, 0)), size=(96, 96))
        self.avatar_label = ctk.CTkLabel(identity, text="?", width=96, height=96,
                                       image=self.empty_avatar_image, compound="center",
                                       corner_radius=48, fg_color=("gray80", "gray25"),
                                       font=("Arial", 28, "bold"))
        self.avatar_label.pack(side="left", padx=(0, 16))
        details = ctk.CTkFrame(identity, fg_color="transparent")
        details.pack(side="left", fill="x", expand=True)
        self.account_name = ctk.CTkLabel(details, text="Telegram account", anchor="w",
                                       font=("Arial", 20, "bold"), wraplength=440)
        self.account_name.pack(fill="x")
        self.account_detail = ctk.CTkLabel(details, text="", anchor="w", text_color="gray70")
        self.account_detail.pack(fill="x")
        self.note(success, "You are signed in and ready to download.")
        self.button(success, "Continue to chats", lambda: self.app.select_page("groups"))
        self.logout_button = self.button(success, "Log out current account", lambda: self.start("logout"))
        self.note(self, "Connecting or switching stops the queue and clears chat selections. "
                  "Downloaded files stay on disk. Login codes and passwords are never saved.")
        self.refresh()
        self.show_step("credentials")
        for entry, action in ((self.api_id, self.save), (self.api_hash, self.save),
                               (self.country_code, self.phone.focus_set), (self.phone, self.send_code),
                               (self.code, self.verify_code), (self.password, self.verify_password)):
            entry.bind("<Return>", lambda _event, fn=action: self.submit(fn))

    def submit(self, action):
        if not self.busy:
            action()
        return "break"

    def toggle_password_visibility(self):
        self.password.configure(show="" if self.show_password.get() else "*")

    def reset_profile(self):
        self.avatar_label.configure(image=self.empty_avatar_image, text="?")
        self.avatar_image = None
        self.account_name.configure(text="Telegram account")
        self.account_detail.configure(text="")

    def show_profile(self, profile):
        # Ignore late results belonging to a previous session, including a reconnect
        # of the same saved account.
        if (not self.backend.authorized or profile["profile_id"] != self.backend.profile_id
                or profile["generation"] != self.backend._account_generation):
            return
        self.account_name.configure(text=profile["name"])
        self.account_detail.configure(text=profile["detail"])
        initials = "".join(word[0] for word in profile["name"].split()[:2]).upper() or "?"
        avatar = profile_avatar(profile["photo"]) if profile.get("photo") else None
        if avatar is None:
            self.avatar_label.configure(image=self.empty_avatar_image, text=initials)
            self.avatar_image = None
        else:
            self.avatar_image = ctk.CTkImage(light_image=avatar, dark_image=avatar, size=(96, 96))
            self.avatar_label.configure(image=self.avatar_image, text="")

    def section(self, name):
        frame = ctk.CTkFrame(self)
        self.sections[name] = frame
        return frame

    @staticmethod
    def note(parent, text):
        ctk.CTkLabel(parent, text=text, justify="left", wraplength=660,
                     text_color="gray70").pack(anchor="w", padx=12, pady=12)

    def entry(self, parent, label, secret=False):
        ctk.CTkLabel(parent, text=label, anchor="w").pack(fill="x", padx=12, pady=(8, 0))
        widget = ctk.CTkEntry(parent, show="*" if secret else "")
        widget.pack(fill="x", padx=12, pady=(0, 8))
        self.fields.append(widget)
        return widget

    def button(self, parent, text, command):
        widget = ctk.CTkButton(parent, text=text, command=command)
        widget.pack(anchor="w", padx=12, pady=6)
        self.buttons.append(widget)
        widget._keyboard_enabled = True
        target = getattr(widget, "_text_label", None) or widget._canvas
        target.configure(takefocus=True)
        widget.bind("<Return>", lambda _event: self.submit(widget.invoke))
        widget.bind("<space>", lambda _event: self.submit(widget.invoke))
        return widget

    def show_step(self, step):
        self.screen = step
        for frame in self.sections.values():
            frame.pack_forget()
        self.sections[step].pack(fill="x", pady=8)
        if step != "password":
            self.show_password.deselect()
            self.password.configure(show="*")
        self.step_label.configure(text={
            "credentials": "API setup",
            "phone": "1. Phone number",
            "code": "2. Login code",
            "password": "3. Two-step verification",
            "authorized": "Signed in",
        }[step])
        self.update_buttons()
        if not self.busy:
            self.focus_step()
        self._parent_canvas.yview_moveto(0)

    def focus_step(self):
        entry = {"credentials": self.api_id, "phone": self.country_code,
                 "code": self.code, "password": self.password}.get(self.screen)
        if entry is not None:
            entry.focus_set()

    def refresh(self):
        store = self.backend.accounts
        profiles = store.data["profiles"] if store else {}
        self.choices = {"Add account": None}
        self.choices.update({f"{p['label']} · {key[:8]}": key for key, p in profiles.items()})
        self.picker.configure(values=list(self.choices))
        selected = next((label for label, key in self.choices.items() if key == self.selected_key), "Add account")
        self.picker.set(selected)

    def choose(self, label):
        if self.busy:
            return
        self.selected_key = self.choices[label]
        self._resend_until = 0.0
        self.picker.set(label)
        profiles = self.backend.accounts.data["profiles"] if self.backend.accounts else {}
        profile = (profiles.get(self.selected_key) or profiles.get(self.backend.profile_id)
                   or next(iter(profiles.values()), {}))
        for entry, value in ((self.api_id, profile.get("api_id", self.backend.api_id or "")),
                             (self.api_hash, profile.get("api_hash", self.backend.api_hash or ""))):
            entry.delete(0, "end")
            entry.insert(0, str(value))
        self.clear_secrets()
        self.status.configure(text="Connect this saved account to continue. For a different account, choose Add account."
                              if self.selected_key else "Sign in to another Telegram account.")
        current = self.selected_key is not None and self.selected_key == self.backend.profile_id
        if current and self.backend.authorized:
            self.show_step("authorized")
        elif self.api_id.get() and self.api_hash.get():
            self.show_step("phone")
        else:
            self.show_step("credentials")

    def clear_secrets(self):
        for entry in (self.country_code, self.phone, self.code, self.password):
            entry.delete(0, "end")
        self.show_password.deselect()
        self.password.configure(show="*")

    def update_buttons(self):
        state = "disabled" if self.busy else "normal"
        for widget in [*self.buttons, *self.fields, self.picker]:
            widget.configure(state=state)
        if not self.busy:
            self.connect_button.configure(state="normal" if self.selected_key else "disabled")
            for button, enabled in ((self.send_button, self.screen == "phone" and (self.selected_key is None or
                                        (self.selected_key == self.backend.profile_id and not self.backend.authorized))),
                                    (self.resend_button, self.screen == "code"),
                                    (self.code_button, self.screen == "code" and self.stage == "code"),
                                    (self.password_button, self.screen == "password" and self.stage == "password"),
                                    (self.logout_button, self.backend.authorized)):
                button.configure(state="normal" if enabled else "disabled")
        self._update_cooldown()

    def _update_cooldown(self):
        # Monotonic time is provided by the Tk-owned app module's clock as well;
        # import locally to keep authentication protocol code independent.
        import time
        now = time.monotonic()
        resend = max(0, int(self._resend_until - now + 0.999))
        retry = max(0, int(self._login_retry_until - now + 0.999))
        remaining = max(resend, retry)
        self.resend_button.configure(text=f"Resend in {remaining}s" if remaining else "Resend code")
        if remaining:
            self.resend_button.configure(state="disabled")
        elif not self.busy:
            self.resend_button.configure(state="normal" if self.screen == "code" else "disabled")
        if retry:
            self.send_button.configure(state="disabled", text=f"Try again in {retry}s")
            self.code_button.configure(state="disabled")
            self.password_button.configure(state="disabled")
        else:
            self.send_button.configure(text="Send login code")
        if self._cooldown_job is not None:
            self.after_cancel(self._cooldown_job)
            self._cooldown_job = None
        if remaining:
            self._cooldown_job = self.after(1000, self._cooldown_tick)

    def _cooldown_tick(self):
        self._cooldown_job = None
        self.update_buttons()

    def open_browser(self):
        try:
            if webbrowser.open(API_SETUP_URL):
                return
        except webbrowser.Error:
            pass
        self.status.configure(text=f"Open {API_SETUP_URL} in your browser.")

    def start(self, action, **values):
        import time
        if self.busy or (action in ("send_code", "begin_login", "code", "password")
                         and time.monotonic() < self._login_retry_until):
            return
        self._last_action = action
        self.busy = True
        self.status.configure(text="Contacting Telegram...")
        self.update_buttons()
        self.backend.run_coro(self.backend.authenticate(action, **values))

    def save(self):
        try:
            api_id, api_hash = validate_credentials(self.api_id.get(), self.api_hash.get())
        except ValueError as exc:
            self.status.configure(text=str(exc))
            return
        self.start("save", key=self.selected_key, api_id=api_id, api_hash=api_hash)

    def switch(self):
        if self.selected_key is not None:
            self.start("switch", key=self.selected_key)

    def send_code(self):
        import time
        if self.screen == "code" and time.monotonic() < self._resend_until:
            return
        if self.selected_key is not None and self.selected_key != self.backend.profile_id:
            self.status.configure(text="Connect the selected saved account first.")
            return
        try:
            phone = phone_with_country_code(self.country_code.get(), self.phone.get())
            if (self.selected_key is not None and self.selected_key == self.backend.profile_id
                    and self.backend.client is not None and not self.backend.authorized):
                self.start("send_code", phone=phone)
            else:
                api_id, api_hash = validate_credentials(self.api_id.get(), self.api_hash.get())
                self.start("begin_login", key=self.selected_key, api_id=api_id, api_hash=api_hash, phone=phone)
        except ValueError as exc:
            self.status.configure(text=str(exc))

    def change_phone(self):
        self.code.delete(0, "end")
        self.password.delete(0, "end")
        self.status.configure(text="Update your phone number and request a new login code.")
        self.show_step("phone")

    def verify_code(self):
        code = self.code.get()
        self.code.delete(0, "end")
        self.start("code", code=code)

    def verify_password(self):
        password = self.password.get()
        self.password.delete(0, "end")
        self.start("password", password=password)

    def handle(self, data):
        import time
        self.stage = data["stage"]
        retry_after = data.get("retry_after")
        if self.stage == "code" and self._last_action in ("send_code", "begin_login"):
            self._resend_until = time.monotonic() + max(1, int(retry_after or 60))
        if retry_after and "rate limit" in data["message"].lower():
            self._login_retry_until = time.monotonic() + max(1, int(retry_after))
        self.status.configure(text=data["message"])
        self.selected_key = self.backend.profile_id
        # Widgets stay disabled until the action finishes; clear secrets afterwards.
        self.show_step(self.stage)

    def finished(self):
        self.busy = False
        self.update_buttons()
        if self.stage == "authorized":
            self.clear_secrets()
        self.refresh()
        # Keep API setup available for the current profile without exposing it at each step.
        profiles = self.backend.accounts.data["profiles"] if self.backend.accounts else {}
        profile = (profiles.get(self.selected_key) or profiles.get(self.backend.profile_id)
                   or next(iter(profiles.values()), {}))
        for entry, value in ((self.api_id, profile.get("api_id", self.backend.api_id or "")),
                             (self.api_hash, profile.get("api_hash", self.backend.api_hash or ""))):
            entry.delete(0, "end")
            entry.insert(0, str(value))
        self.focus_step()
