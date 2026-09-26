"""Authentication lifecycle tests with fake Telegram clients; no real accounts."""
import asyncio
import copy
import os
import stat
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from PIL import Image
from telethon import errors

import interactive_downloader as dl
from telegram_auth import AccountStore, phone_with_country_code, profile_avatar, validate_credentials

HASH = "a" * 32


class FakeClient:
    instances: ClassVar[list] = []
    authorized_paths: ClassVar[set] = set()

    def __init__(self, path, api_id, api_hash, **kwargs):
        self.path = path
        self.session = SimpleNamespace(filename=None)
        self.connected = False
        self.authorized = path in self.authorized_paths
        self.password_needed = False
        self.logged_out = False
        self.calls = []
        self.photo = None
        self.instances.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return SimpleNamespace(id=123, first_name="Test", last_name="Account")

    async def download_profile_photo(self, user, *, file, download_big):
        return self.photo

    async def send_code_request(self, phone):
        self.calls.append(("send", phone))
        return SimpleNamespace(phone_code_hash="temporary-hash")

    async def sign_in(self, **values):
        self.calls.append(("sign_in", values))
        if values.get("code") == "expired":
            raise errors.PhoneCodeExpiredError(None)
        if values.get("code") == "wrong":
            raise errors.PhoneCodeInvalidError(None)
        if self.password_needed and "password" not in values:
            raise errors.SessionPasswordNeededError(None)
        if values.get("password") == "wrong":
            raise errors.PasswordHashInvalidError(None)
        self.authorized = True

    async def log_out(self):
        self.logged_out = True
        self.authorized = False
        self.connected = False
        return True


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AccountStore(self.temp.name)
        self.config_patch = patch.object(dl, "CONFIG_FILE", os.path.join(self.temp.name, "config.json"))
        self.client_patch = patch("telegram_auth.TelegramClient", FakeClient)
        self.config_patch.start()
        self.client_patch.start()
        FakeClient.instances = []
        FakeClient.authorized_paths = set()
        self.backend = dl.TelegramBackend(0, "", os.path.join(self.temp.name, "legacy"), self.store)

    def tearDown(self):
        self.backend.shutdown()
        self.client_patch.stop()
        self.config_patch.stop()
        self.temp.cleanup()

    def auth(self, action, **values):
        self.backend.run_coro(self.backend.authenticate(action, **values)).result(5)

    def add(self):
        self.auth("save", key=None, api_id="123", api_hash=HASH)
        return self.backend.profile_id

    def drain_photo(self):
        async def drain():
            tasks = [task for task in self.backend._pending_tasks if task is not asyncio.current_task()]
            if tasks:
                await asyncio.gather(*tasks)
        self.backend.run_coro(drain()).result(5)

    def test_profile_photo_loads_after_login_and_decodes_as_circle(self):
        self.add()
        buffer = BytesIO()
        Image.new("RGB", (32, 16), "blue").save(buffer, format="PNG")
        photo = buffer.getvalue()
        self.backend.client.photo = photo
        self.auth("send_code", phone="+14155552671")
        self.auth("code", code="12345")
        self.drain_photo()
        profiles = [data for event, data in self.backend.event_queue.queue if event == "account_profile"]
        self.assertIsNone(profiles[0]["photo"])
        self.assertEqual(profiles[-1]["photo"], photo)
        self.assertEqual(profiles[-1]["name"], "Test Account")
        self.assertEqual(profiles[-1]["profile_id"], self.backend.profile_id)
        avatar = profile_avatar(photo)
        self.assertEqual(avatar.size, (192, 192))
        self.assertEqual(avatar.getpixel((0, 0))[3], 0)
        self.assertEqual(avatar.getpixel((96, 96)), (0, 0, 255, 255))
        self.assertIsNone(profile_avatar(b"not an image"))

    def test_missing_or_failed_profile_photo_does_not_fail_login(self):
        self.add()
        self.auth("send_code", phone="+14155552671")
        self.auth("code", code="12345")
        self.drain_photo()
        self.assertTrue(self.backend.authorized)
        profile = next(data for event, data in self.backend.event_queue.queue if event == "account_profile")
        self.assertIsNone(profile["photo"])
        async def unavailable(*args, **kwargs):
            raise OSError("offline")
        self.backend.client.download_profile_photo = unavailable
        self.backend.run_coro(self.backend._load_profile_photo(self.backend.client, None, profile)).result(5)
        self.assertTrue(self.backend.authorized)
        self.assertEqual(self.backend.auth_stage, "authorized")

    def test_switch_cancels_pending_photo_and_discards_old_profile(self):
        self.add()
        started = asyncio.Event()
        cancelled = []
        async def slow_photo(*args, **kwargs):
            started.set()
            try:
                await asyncio.sleep(100)
            finally:
                cancelled.append(True)
        self.backend.client.download_profile_photo = slow_photo
        self.auth("send_code", phone="+14155552671")
        self.auth("code", code="12345")
        self.backend.run_coro(started.wait()).result(5)
        self.add()
        self.assertEqual(cancelled, [True])
        self.assertFalse(any(event == "account_profile" for event, _ in self.backend.event_queue.queue))

    def test_missing_credentials_opens_setup_without_client(self):
        self.backend.run_coro(self.backend.init_client()).result(5)
        self.assertEqual(self.backend.auth_stage, "credentials")
        self.assertEqual(FakeClient.instances, [])

    def test_validation_does_not_disrupt_current_client(self):
        self.add()
        client = self.backend.client
        self.auth("save", key=None, api_id="bad", api_hash=HASH)
        self.assertIs(self.backend.client, client)
        self.assertTrue(client.connected)
        for api_id, api_hash in [(0, HASH), (-1, HASH), ("1.5", HASH), (123, "bad")]:
            with self.assertRaises(ValueError):
                validate_credentials(api_id, api_hash)

    def test_country_code_and_phone_validation(self):
        self.assertEqual(phone_with_country_code("+1", "(415) 555-2671"), "+14155552671")
        self.assertEqual(phone_with_country_code("91", "98765 43210"), "+919876543210")
        for country, number in [("", "4155552671"), ("+", "4155552671"),
                                ("+0", "4155552671"), ("+1234", "123456789"),
                                ("+1", "+14155552671"), ("+1", ""), ("+1", "abc")]:
            with self.subTest(country=country, number=number), self.assertRaises(ValueError):
                phone_with_country_code(country, number)

    def test_phone_first_login_creates_session_and_skips_optional_password(self):
        self.auth("begin_login", key=None, api_id=123, api_hash=HASH, phone="+14155552671")
        self.assertEqual(self.backend.auth_stage, "code")
        self.assertEqual(self.backend.client.calls, [("send", "+14155552671")])
        self.auth("code", code="12345")
        self.assertTrue(self.backend.authorized)
        self.assertEqual(self.backend.auth_stage, "authorized")
        self.assertFalse(any(t == "login_state" and d["stage"] == "password"
                             for t, d in self.backend.event_queue.queue))

    def test_invalid_phone_does_not_interrupt_current_account(self):
        self.add()
        client = self.backend.client
        self.auth("begin_login", key=None, api_id=123, api_hash=HASH, phone="invalid")
        self.assertIs(self.backend.client, client)
        self.assertTrue(client.connected)
        self.assertEqual(len(self.store.data["profiles"]), 1)

    def test_code_and_password_lifecycle_and_secret_storage(self):
        key = self.add()
        self.assertEqual(self.backend.auth_stage, "phone")
        self.auth("send_code", phone="+14155552671")
        self.auth("code", code="wrong")
        self.assertEqual(self.backend.auth_stage, "code")
        self.backend.client.password_needed = True
        self.auth("code", code="12345")
        self.assertEqual(self.backend.auth_stage, "password")
        self.auth("password", password="wrong")
        self.assertFalse(self.backend.authorized)
        self.auth("password", password="unique-test-secret")
        self.assertTrue(self.backend.authorized)
        self.assertEqual(AccountStore(self.temp.name).data["active"], key)
        saved = Path(self.store.path).read_text()
        for secret in ("unique-test-secret", "temporary-hash", "+14155552671", "12345"):
            self.assertNotIn(secret, saved)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.path).st_mode), 0o600)
        self.assertIsNone(self.backend._phone_code_hash)

    def test_expired_code_and_flood_wait_recovery(self):
        self.add()
        self.auth("send_code", phone="+14155552671")
        self.auth("code", code="expired")
        self.assertEqual(self.backend.auth_stage, "phone")
        self.assertIsNone(self.backend._phone_code_hash)
        async def limited(phone):
            raise errors.FloodWaitError(None, capture=42)
        self.backend.client.send_code_request = limited
        self.auth("send_code", phone="+14155552671")
        events = list(self.backend.event_queue.queue)
        self.assertTrue(any(t == "login_state" and "42" in d["message"] for t, d in events))

    def test_switch_drains_old_tasks_and_state_before_connecting(self):
        first = self.add()
        old_client = self.backend.client
        drained = []
        async def old_work():
            try:
                await asyncio.sleep(100)
            finally:
                drained.append(self.backend.client is old_client)
                self.backend.emit_event("dialogs_loaded", ["stale"])
        future = self.backend.run_coro(old_work())
        self.backend.run_coro(asyncio.sleep(0)).result(5)
        self.backend.download_queue.append(dl.DownloadItem("1", 1, "Old", None, "x", 1, ".txt"))
        second = self.add()
        self.assertNotEqual(first, second)
        self.assertNotEqual(self.store.data["profiles"][first]["session"], self.backend.session_path)
        self.assertEqual(drained, [True])
        self.assertTrue(future.done())
        self.assertFalse(old_client.connected)
        self.assertEqual(self.backend.download_queue, [])
        self.assertEqual(self.backend.pagination_states, {})
        self.assertFalse(any(t == "dialogs_loaded" for t, _ in self.backend.event_queue.queue))
        FakeClient.authorized_paths.add(self.store.data["profiles"][first]["session"])
        self.auth("switch", key=first)
        self.assertTrue(self.backend.authorized)
        self.assertEqual(self.store.data["active"], first)

    def test_logout_revokes_only_current_session_and_does_not_restore_env(self):
        first = self.add()
        self.auth("send_code", phone="+14155552671")
        self.auth("code", code="good")
        other = self.store.save_profile(None, 456, HASH, "Other")
        client = self.backend.client
        self.auth("logout")
        self.assertTrue(client.logged_out)
        self.assertIsNone(self.backend.client)
        self.assertFalse(self.backend.authorized)
        reloaded = AccountStore(self.temp.name, legacy=(999, HASH, "/unused"))
        self.assertIsNone(reloaded.data["active"])
        self.assertEqual(set(reloaded.data["profiles"]), {first, other})

    def test_legacy_keeps_existing_session_path(self):
        path = os.path.join(self.temp.name, "original_session")
        store = AccountStore(self.temp.name, legacy=(123, HASH, path))
        self.assertEqual(store.data["profiles"]["legacy"]["session"], path)
        store.set_active("legacy")
        self.assertEqual(AccountStore(self.temp.name).data, store.data)

    def test_failed_write_preserves_previous_credentials(self):
        key = self.add()
        before = copy.deepcopy(self.store.data)
        with patch("telegram_auth.os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.store.save_profile(key, 999, "b" * 32, "Changed")
        self.assertEqual(self.store.data, before)
        self.assertEqual(AccountStore(self.temp.name).data, before)
        self.assertFalse(list(Path(self.temp.name).glob(".accounts-*")))

    def test_revoked_session_during_download_clears_work_and_requests_login(self):
        self.add()
        self.backend.authorized = True
        self.backend.config["output_dir"] = os.path.join(self.temp.name, "downloads")
        item = dl.DownloadItem("1", 1, "Old", None, "x.txt", 1, ".txt", status="Downloading")
        self.backend.download_queue.append(item)
        async def revoked(*args):
            raise errors.SessionRevokedError(None)
        self.backend._transfer = revoked
        async def worker():
            item.task = asyncio.current_task()
            await self.backend._download_worker(item)
        self.backend.run_coro(worker()).result(5)
        self.assertFalse(self.backend.authorized)
        self.assertEqual(self.backend.download_queue, [])
        self.assertEqual(self.backend.auth_stage, "credentials")
        self.assertEqual(item.status, "Cancelled")
        self.assertTrue(any(t == "account_reset" for t, _ in self.backend.event_queue.queue))

    def test_missing_or_invalid_environment_still_loads_account_setup(self):
        with patch.object(dl, "BASE_DIR", self.temp.name), patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(dl.load_account_store().data["active"])
            os.environ["TG_API_ID"] = "bad"
            os.environ["TG_API_HASH"] = HASH
            self.assertIsNone(dl.load_account_store().data["active"])

    def test_corrupt_store_is_not_overwritten(self):
        Path(self.store.path).write_text("broken")
        with self.assertRaises(ValueError):
            AccountStore(self.temp.name)
        self.assertEqual(Path(self.store.path).read_text(), "broken")


if __name__ == "__main__":
    unittest.main()
