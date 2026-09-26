"""Drive the Accounts page with fake Telegram responses, without real credentials."""
import os
import tempfile
import time
import unittest
from io import BytesIO
from unittest.mock import patch

from PIL import Image

import interactive_downloader as dl
from telegram_auth import API_SETUP_URL, AccountStore
from tests_auth import HASH, FakeClient


class AccountUITests(unittest.TestCase):
    def test_login_switch_and_logout(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(dl, "CONFIG_FILE", os.path.join(directory, "config.json")), \
                patch("telegram_auth.TelegramClient", FakeClient):
            FakeClient.instances = []
            FakeClient.authorized_paths = set()
            store = AccountStore(directory)
            backend = dl.TelegramBackend(0, "", os.path.join(directory, "unused"), store)
            async def dialogs():
                backend.emit_event("dialogs_loaded", [])
            backend.fetch_dialogs = dialogs
            app = dl.App(backend)
            page = app.page_accounts

            def pump_until(predicate):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    app.update()
                    if predicate():
                        return
                    time.sleep(0.02)
                self.fail("UI did not reach expected state")

            try:
                pump_until(lambda: not page.busy)
                self.assertTrue(page.winfo_ismapped())
                self.assertEqual(page.stage, "credentials")
                self.assertEqual(page.send_button.cget("state"), "disabled")
                with patch("telegram_auth.webbrowser.open", return_value=True) as browser:
                    page.open_browser()
                    browser.assert_called_once_with(API_SETUP_URL)

                page.api_id.insert(0, "bad")
                page.save()
                self.assertIn("positive whole number", page.status.cget("text"))
                self.assertEqual(FakeClient.instances[-1:] if FakeClient.instances else [], [])
                page.api_id.delete(0, "end")
                page.api_id.insert(0, "123")
                page.api_hash.insert(0, HASH)
                page.save()
                self.assertEqual(page.picker.cget("state"), "disabled")
                pump_until(lambda: not page.busy)
                first = backend.profile_id
                self.assertEqual(page.stage, "phone")
                self.assertEqual([name for name, frame in page.sections.items() if frame.winfo_manager()], ["phone"])
                self.assertEqual(page.send_button.cget("state"), "normal")
                page.country_code.insert(0, "+1")
                page.phone.insert(0, "4155552671")
                page.send_button.invoke()
                pump_until(lambda: not page.busy)
                self.assertEqual(page.stage, "code")
                self.assertEqual([name for name, frame in page.sections.items() if frame.winfo_manager()], ["code"])
                page.code.insert(0, "wrong")
                page.code_button.invoke()
                pump_until(lambda: not page.busy)
                self.assertEqual(page.screen, "code")
                self.assertIn("incorrect", page.status.cget("text"))
                page.change_phone()
                self.assertEqual(page.screen, "phone")
                page.send_button.invoke()
                pump_until(lambda: not page.busy)
                self.assertEqual(page.screen, "code")
                self.assertEqual(page.phone.get(), "4155552671")
                self.assertEqual(page.country_code.get(), "+1")
                photo = BytesIO()
                Image.new("RGB", (32, 32), "blue").save(photo, format="PNG")
                backend.client.photo = photo.getvalue()
                backend.client.password_needed = True
                page.code.insert(0, "12345")
                page.code_button.invoke()
                self.assertEqual(page.code.get(), "")
                pump_until(lambda: not page.busy)
                self.assertEqual(page.stage, "password")
                self.assertEqual([name for name, frame in page.sections.items() if frame.winfo_manager()], ["password"])
                page.password.insert(0, "test-password")
                page.password_button.invoke()
                self.assertEqual(page.password.get(), "")
                pump_until(lambda: not page.busy)
                self.assertEqual(page.stage, "authorized")
                self.assertEqual([name for name, frame in page.sections.items() if frame.winfo_manager()], ["authorized"])
                self.assertTrue(backend.authorized)
                self.assertIn("Test Account", page.picker.get())
                pump_until(lambda: page.avatar_image is not None)
                self.assertEqual(page.account_name.cget("text"), "Test Account")
                self.assertEqual(page.phone.get(), "")

                app.all_chats = [dl.ChatEntry(id=1, name="Old", kind="Group", archived=False, entity=None)]
                app.selected_chat_ids = {1}
                app.active_group_filter = 1
                backend.download_queue.append(dl.DownloadItem("1", 1, "Old", None, "x", 1, ".txt"))
                page.choose("Add account")
                self.assertEqual(page.api_id.get(), "123")
                self.assertEqual(page.screen, "phone")
                self.assertEqual(page.send_button.cget("state"), "normal")
                page.country_code.insert(0, "+1")
                page.phone.insert(0, "4155552672")
                page.send_button.invoke()
                pump_until(lambda: not page.busy)
                self.assertNotEqual(backend.profile_id, first)
                self.assertIsNone(page.avatar_image)
                self.assertEqual(page.screen, "code")
                page.code.insert(0, "12345")
                page.code_button.invoke()
                pump_until(lambda: not page.busy)
                self.assertEqual(page.screen, "authorized")
                self.assertEqual(app.all_chats, [])
                self.assertEqual(app.selected_chat_ids, set())
                self.assertIsNone(app.active_group_filter)
                self.assertEqual(backend.download_queue, [])

                FakeClient.authorized_paths.add(store.data["profiles"][first]["session"])
                label = next(label for label, key in page.choices.items() if key == first)
                page.choose(label)
                page.switch()
                pump_until(lambda: not page.busy)
                self.assertTrue(backend.authorized)
                current_client = backend.client
                page.logout_button.invoke()
                pump_until(lambda: not page.busy)
                self.assertTrue(current_client.logged_out)
                self.assertFalse(backend.authorized)
                self.assertIsNone(page.avatar_image)
                self.assertEqual(page.account_name.cget("text"), "Telegram account")
                self.assertIsNone(AccountStore(directory).data["active"])
                self.assertEqual(page.stage, "credentials")
            finally:
                app.on_close()


if __name__ == "__main__":
    unittest.main()
