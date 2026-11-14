"""Local account profiles and non-interactive Telethon authentication.

API credentials are developer application credentials. Account authorization is
stored separately by Telethon, in one session per profile.
"""
import asyncio
import copy
import json
import os
import re
import tempfile
import uuid
import webbrowser
from io import BytesIO

from PIL import Image, ImageDraw, ImageOps
from telethon import TelegramClient, errors

API_SETUP_URL = "https://my.telegram.org/apps"


def validate_credentials(api_id, api_hash):
    try:
        number = int(str(api_id).strip())
        if number <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("API ID must be a positive whole number.") from None
    api_hash = str(api_hash or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise ValueError("API hash must contain the 32 hexadecimal characters from my.telegram.org.")
    return number, api_hash


def normalize_phone(phone):
    phone = re.sub(r"[\s()-]", "", phone)
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
        raise ValueError("Enter your country code and phone number, for example +1 4155552671.")
    return phone


def phone_with_country_code(country_code, number):
    country_code = country_code.strip()
    if not re.fullmatch(r"\+?[1-9][0-9]{0,2}", country_code):
        raise ValueError("Enter a country calling code, for example +1 or +91.")
    if not number.strip() or "+" in number:
        raise ValueError("Enter your phone number without the country code.")
    return normalize_phone("+" + country_code.lstrip("+") + number)


def profile_avatar(photo):
    """Decode the small Telegram photo into a circular avatar, kept only in memory."""
    try:
        with Image.open(BytesIO(photo)) as source:
            avatar = ImageOps.fit(source.convert("RGBA"), (192, 192))
        mask = Image.new("L", avatar.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 191, 191), fill=255)
        avatar.putalpha(mask)
        return avatar
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


class AccountStore:
    def __init__(self, directory, legacy=None):
        self.directory = directory
        self.path = os.path.join(directory, "accounts.json")
        self.data = {"active": None, "profiles": {}}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as stream:
                    data = json.load(stream)
                if not isinstance(data, dict) or not isinstance(data["profiles"], dict):
                    raise TypeError
                if data["active"] is not None and data["active"] not in data["profiles"]:
                    raise ValueError
                for key, profile in data["profiles"].items():
                    if not re.fullmatch(r"[0-9a-f]{32}|legacy", key):
                        raise ValueError
                    validate_credentials(profile["api_id"], profile["api_hash"])
                    if not isinstance(profile["label"], str):
                        raise TypeError
                    if not isinstance(profile["session"], str) or not os.path.isabs(profile["session"]):
                        raise ValueError
                self.data = data
            except (OSError, ValueError, KeyError, TypeError):
                raise ValueError("Cannot read accounts.json. Restore or repair it before starting the app.") from None
        elif legacy:
            api_id, api_hash, session = legacy
            self.data = {"active": "legacy", "profiles": {
                "legacy": {"label": "Existing .env account", "api_id": api_id,
                           "api_hash": api_hash, "session": session}}}

    def commit(self, data):
        """Atomic, owner-only storage; failed writes leave the old state intact."""
        os.makedirs(self.directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".accounts-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.data = data

    def save_profile(self, key, api_id, api_hash, label):
        api_id, api_hash = validate_credentials(api_id, api_hash)
        data = copy.deepcopy(self.data)
        if key is None:
            key = uuid.uuid4().hex
            sessions = os.path.join(self.directory, "sessions")
            os.makedirs(sessions, mode=0o700, exist_ok=True)
            data["profiles"][key] = {"session": os.path.join(sessions, key)}
        profile = data["profiles"][key]
        profile.update(api_id=api_id, api_hash=api_hash, label=label.strip() or "Telegram account")
        self.commit(data)
        return key

    def set_active(self, key):
        data = copy.deepcopy(self.data)
        data["active"] = key
        self.commit(data)


class AuthenticationMixin:
    """All methods execute on the backend loop; secrets never become UI events."""

    def configure_auth(self, store):
        self.accounts = store
        self.profile_id = store.data["active"] if store else None
        self.authorized = False
        self.auth_stage = "credentials"
        self._phone = None
        self._phone_code_hash = None
        self._auth_lock = asyncio.Lock()

    def auth_event(self, stage, message, **details):
        self.auth_stage = stage
        self.emit_event("login_state", {"stage": stage, "message": message, **details})

    async def _connect_account(self):
        if not self.api_id or not self.api_hash:
            self.auth_event("credentials", "Open Telegram API setup in your browser, then paste your API ID and hash below.")
            return
        if self.client is not None:
            await self.client.disconnect()
        self.client = TelegramClient(self.session_path, self.api_id, self.api_hash,
                                     flood_sleep_threshold=0, raise_last_call_error=True)
        session_file = getattr(self.client.session, "filename", None)
        if session_file and os.path.exists(session_file):
            os.chmod(session_file, 0o600)
        await self.client.connect()
        if await self.client.is_user_authorized():
            await self._signed_in()
        else:
            self.auth_event("phone", "Enter your phone number with country code to sign in.")
            self.emit_event("auth_status", False)

    async def _signed_in(self):
        user = await self.client.get_me()
        if user is None:
            raise ValueError("Telegram did not return an account. Reconnect and try again.")
        if self.accounts and self.profile_id:
            data = copy.deepcopy(self.accounts.data)
            profile = data["profiles"][self.profile_id]
            profile["user_id"] = user.id
            profile["label"] = " ".join(filter(None, [user.first_name, user.last_name])) or str(user.id)
            data["active"] = self.profile_id
            self.accounts.commit(data)
        self.authorized = True
        self.current_user_id = user.id
        self._phone = self._phone_code_hash = None
        name = user.first_name or str(user.id)
        self.auth_event("authorized", f"Signed in as {name} (ID {user.id}).")
        self.emit_event("auth_status", True)
        profile = {
            "profile_id": self.profile_id,
            "generation": self._account_generation,
            "name": " ".join(filter(None, [user.first_name, user.last_name])) or str(user.id),
            "detail": "@" + user.username if getattr(user, "username", None) else f"ID {user.id}",
            "photo": None,
        }
        self.emit_event("account_profile", profile)
        self._spawn(self._load_profile_photo(self.client, user, profile))

    async def _load_profile_photo(self, client, user, profile):
        try:
            photo = await asyncio.wait_for(
                client.download_profile_photo(user, file=bytes, download_big=False), timeout=15)
        except Exception:  # noqa: BLE001 - an optional photo must never interrupt sign-in.
            return
        if (photo and self.authorized and client is self.client
                and profile["generation"] == self._account_generation):
            self.emit_event("account_profile", {**profile, "photo": photo})

    async def session_invalid(self):
        await self._reset_account_work()
        self.auth_event("credentials", "Telegram revoked this session. Use Add account to create a fresh login session.")

    async def init_client(self):
        await self.authenticate("connect")

    async def _select_account(self, action, values):
        if action == "save":
            # Validate and save before disturbing a working session.
            key = self.accounts.save_profile(values.get("key"), values["api_id"],
                                             values["api_hash"], values.get("label", ""))
        else:
            key = values["key"]
        profile = self.accounts.data["profiles"][key]
        await self._reset_account_work()
        if self.client:
            await self.client.disconnect()
            self.client = None
        self.profile_id = key
        self.api_id, self.api_hash = profile["api_id"], profile["api_hash"]
        self.session_path = profile["session"]
        self._phone = self._phone_code_hash = None
        self.auth_stage = "credentials"
        await self._connect_account()

    async def _send_login_code(self, phone):
        if not self.client or self.authorized:
            raise ValueError("Connect an account before requesting a code.")
        phone = normalize_phone(phone)
        self.auth_stage = "phone"
        self._phone_code_hash = None
        sent = await self.client.send_code_request(phone)
        code_hash = getattr(sent, "phone_code_hash", None)
        if not code_hash:
            raise ValueError("Telegram could not provide a login code. Try signing in again.")
        self._phone, self._phone_code_hash = phone, code_hash
        self.auth_event("code", "Enter the code Telegram sent to your Telegram app or phone.",
                        retry_after=getattr(sent, "timeout", None) or 60)

    async def authenticate(self, action, **values):
        async with self._auth_lock:
            try:
                if action == "connect":
                    await self._connect_account()
                elif action in ("save", "switch"):
                    await self._select_account(action, values)
                elif action == "begin_login":
                    phone = normalize_phone(values["phone"])
                    await self._select_account("save", values)
                    if not self.authorized:
                        await self._send_login_code(phone)
                elif action == "send_code":
                    await self._send_login_code(values["phone"])
                elif action == "code":
                    if not self._phone_code_hash:
                        raise ValueError("Request a new login code first.")
                    code = values["code"].strip().replace(" ", "")
                    if not code:
                        raise ValueError("Enter the login code.")
                    await self.client.sign_in(phone=self._phone, code=code,
                                              phone_code_hash=self._phone_code_hash)
                    await self._signed_in()
                elif action == "password":
                    if self.auth_stage != "password":
                        raise ValueError("Submit your login code first.")
                    await self.client.sign_in(password=values["password"])
                    await self._signed_in()
                elif action == "logout":
                    if not self.client or not self.authorized:
                        raise ValueError("Connect the account you want to log out first.")
                    await self._reset_account_work()
                    if not await self.client.log_out():
                        raise ValueError("Telegram could not log out. Reconnect and try again.")
                    self.client = None
                    self._phone = self._phone_code_hash = None
                    if self.accounts:
                        self.accounts.set_active(None)
                    self.auth_event("credentials", "Logged out. This downloader session has been revoked. Saved API details can be reused.")
                else:
                    raise ValueError("Unknown login action.")
            except errors.SessionPasswordNeededError:
                self.auth_event("password", "Enter your Telegram two-step verification password.")
            except errors.PhoneCodeExpiredError:
                self._phone_code_hash = None
                self.auth_event("phone", "That login code expired. Request a new code.")
            except errors.PhoneCodeInvalidError:
                self.auth_event("code", "That login code is incorrect. Try again.")
            except errors.PasswordHashInvalidError:
                self.auth_event("password", "Incorrect two-step verification password. Try again.")
            except errors.PhoneNumberInvalidError:
                self.auth_event("phone", "Telegram did not recognize that phone number. Check the number and country code.")
            except errors.PhoneNumberBannedError:
                self.auth_event("phone", "Telegram has banned this phone number. Use another account or contact Telegram support.")
            except errors.ApiIdInvalidError:
                self.auth_event("credentials", "Telegram rejected the API ID/hash. Copy them again from my.telegram.org/apps.")
            except errors.FloodWaitError as exc:
                self.auth_event(self.auth_stage, f"Telegram rate limit: wait {exc.seconds} seconds before trying again.",
                                retry_after=exc.seconds)
            except (errors.UnauthorizedError, errors.AuthKeyError):
                self.authorized = False
                self.auth_event("credentials", "This session is no longer valid. Use Add account to create a fresh login session.")
            except (OSError, asyncio.TimeoutError):
                self.auth_event(self.auth_stage, "Could not connect or save account details. Check your network and local file permissions, then retry.")
            except ValueError as exc:
                self.auth_event(self.auth_stage, str(exc))
            except Exception as exc:  # noqa: BLE001 - RPC failures must reach the GUI without secrets.
                # Do not expose RPC request objects, phone numbers, codes or passwords.
                self.auth_event(self.auth_stage, f"Login failed ({type(exc).__name__}). Check your details and try again.")
            finally:
                self.emit_event("login_finished")


def __getattr__(name):
    if name == "AccountPage":
        from telegram_auth_ui import AccountPage
        return AccountPage
    raise AttributeError(name)
