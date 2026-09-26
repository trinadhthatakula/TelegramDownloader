# Telegram Downloader

A desktop app (customtkinter + Telethon) for bulk-downloading media from Telegram
chats and channels. Pick chats, optionally filter by file type, and it streams
files into per-chat folders while showing live per-file progress.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Launch the app and open **Accounts**. No `.env` file or terminal login is required:

1. Click **Open Telegram API setup in browser** to open
   [my.telegram.org/apps](https://my.telegram.org/apps). Sign in there, then open
   **API development tools** and create an application if needed.
2. Copy the **API ID** and **API hash** into the app and click
   **Save API details & continue**.
3. **Phone number:** enter the country code and phone number in separate fields,
   then click **Send login code**.
4. **Login code:** enter the code delivered by Telegram and click **Continue**.
   You can resend the code or go back to change your phone number.
5. **Two-step verification:** if enabled on your account, enter your password and
   click **Log in**. Otherwise this screen is skipped.
6. The **Signed in** screen shows your name and Telegram profile photo; click
   **Continue to chats**. Accounts without a photo show initials instead. Photos
   load in the background and are kept only in memory.

Only the current sign-in step is visible. API setup is separate and can be
reopened with **API setup** when credentials need updating.

The browser sign-in retrieves your API application credentials; it does not sign
this downloader into your Telegram account. API IDs/hashes normally do not expire.
Login codes do expire, and account sessions can be revoked or become unauthorized.
The app reports these separately and lets you retry from **Accounts**.

### Accounts and credential updates

- **Switch accounts:** choose **Add account** to start at the phone-number screen
  using your existing API details, then sign in with the other phone number.
  Each profile gets a separate session. Choose a saved account and click
  **Connect saved account** to switch back without signing in again while
  that session remains valid.
- **Replace API credentials:** select the saved account, open **API setup**, paste its new API ID/hash,
  and click **Save API details & continue**.
- **Revoked session:** use **Add account** for a fresh session and sign in again.
- **Log out current account:** revokes only this downloader's current session.
  Saved API details remain available for a later login. Other saved sessions remain signed in.

Connecting, switching accounts, and logging out stop pending downloads and clear
this run's queue and chat selections. Completed files remain on disk.

Existing `.env` credentials and `TG_SESSION_NAME` are imported on first use,
keeping the original session path. Once profiles have been saved, `accounts.json`
takes precedence; logging out does not silently reactivate the `.env` account.
An optional `.env` can still contain:

```dotenv
TG_API_ID=your_id
TG_API_HASH=your_hash
TG_SESSION_NAME=downloader_session
OUTPUT_ROOT=./telegram_downloads
```

API details are stored in `accounts.json`; sessions live in `sessions/` (or the
legacy session path). These are sensitive local files, **not encrypted**. New
session directories use owner-only access, and account/session files use owner-only
permissions on POSIX systems. Login codes and two-step passwords are never saved.
The credential files are ignored by Git; keep them out of shared backups.

## Run

```bash
.venv/bin/python interactive_downloader.py
# or, after `pip install -e .`
.venv/bin/tg-download
```

## Using it

1. **Groups & Formats** — tick the chats you want. Every chat, channel and group
   is loaded, including archived ones; the type toggles (Group / Channel / DM /
   Bot) and *Include archived* only narrow what is *listed*, and a chat you have
   already ticked stays visible even when a filter would hide it. The counter
   shows `loaded (by type) · shown · selected` so you can confirm nothing is
   missing. *Select all shown* ticks everything currently listed.
   *Scan Selected Chats* walks recent history and lists the file types it found;
   leave *ALL Formats* ticked to take everything. Then *Start Downloads*.
2. **Download Queue** — live progress, per-file Cancel, and a per-chat filter
   sidebar showing `completed/discovered`. Files are fetched in batches and the
   queue refills itself as it drains, so very large chats stream rather than
   being enumerated up front.
3. **Settings** — download folder, parallel downloads (1–10), scan depth
   (0 = unlimited), and cleanup for leftover `.part` files.

Downloads land in `<output_dir>/<chat name>/<file name>`. A file already on disk
with the expected size is skipped, so re-running is cheap and interrupted runs
resume. A different file that happens to share a name is saved as
`name (2).ext` rather than overwriting.

Telegram rate limits (FloodWait) pause the whole queue and resume automatically;
they no longer fail the affected files. Failed items can be requeued with
*Retry Failed*.

`downloader.log` rotates at 5 MB (3 backups).

## Tests

No Telegram account or network needed — all suites use fakes. UI suites require a desktop session.

```bash
.venv/bin/python tests_auth.py    # credential storage and authentication lifecycle
.venv/bin/python tests_auth_ui.py # real Accounts UI with fake Telegram responses
.venv/bin/python tests_smoke.py   # backend: paths, config, download worker, retries, resume
.venv/bin/python tests_ui.py      # builds the real Tk window and drives it
.venv/bin/python bench_ui.py      # queue-view repaint cost vs. queue size
```

## Checking your chat list

To confirm the app is listing everything your account has, close the app and run:

```bash
.venv/bin/python diagnose_dialogs.py
```

It uses the active saved account (or the legacy environment session before migration),
pages both folders read-only, downloads nothing, and prints per-type counts
plus every group and channel by name. Close the GUI first — the two share one
SQLite session file.

## macOS note

If Python ever segfaults under Tk, macOS shows a "reopen windows?" alert at the
*next* launch, and Tk blocks during init waiting for that click — so the app
appears to hang on startup with no window. This repo assumes it is suppressed:

```bash
defaults write org.python.python ApplePersistenceIgnoreState -bool YES
# undo with: defaults delete org.python.python ApplePersistenceIgnoreState
```
