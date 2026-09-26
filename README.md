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
this run's queue and chat selections. Completed files and recognized resumable
fragments remain on disk. Reconnect the same account and select its chats again
to continue those downloads.

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
   Bot) and *Include archived* narrow the list without clearing your selection.
   Use *Show selected chats only* to review it; clear the search and enable
   archived chats when reviewing the entire selection. *Select all shown* ticks
   the current list.
   Choose **All files**, or choose **Selected formats**, scan the selected chats,
   and tick at least one extension. Scans show progress and can be cancelled;
   changing the chat selection invalidates the previous scan. Click
   **Add to downloads** to create jobs for all selected chats.
2. **Download Queue** — each row shows its source chat, progress, and file details.
   Select a chat in the sidebar for its totals and **Pause chat**, **Resume**,
   **Cancel**, and **Retry failed** actions. The sidebar also has explicit controls
   for all downloads. Errors remain available in **View issues**; a later progress
   update does not erase them. Lists adapt their visible rows to the window size
   and support keyboard navigation.
3. **Settings** — download folder, parallel downloads (1–10), scan depth
   (0 = unlimited, for format scanning), and cleanup for unrecognized `.part`
   files. Folder checks, settings writes, and cleanup run in the background.

Multiple chat jobs keep their own format selections. Repeated **Add to downloads**
does not create overlapping history scans or duplicate queue entries. Cancel a
running job before adding it with different formats. Discovery uses at most three
concurrent history requests. At most 50 transfers per chat and 500 globally are
admitted to run; additional resumed work waits for capacity. File transfers use
the separate parallel-download setting and share slots across chats. Completed
history remains visible until **Clear completed** removes it.

Each job keeps the output directory selected when it was added, including through
retries, pause, and resume. Changing Settings affects newly added jobs without
moving an existing job's remaining files into a different directory.

**Pause** stops discovery and transfers while retaining progress. **Cancel** also
stops the selected work; **Resume** can restore paused or cancelled items.
**Retry failed** retries failed transfers and failed discovery, and
**Clear completed** removes only completed/skipped rows, preserving failed and
cancelled work. Stopping a job waits for its old workers before a resume can start.

Telegram rate limits pause discovery and new transfer dispatch until the server's
deadline. Retries also use backoff. Jobs resume automatically unless you have
paused or cancelled them; exhausted retries remain visible for manual recovery.

### Existing files, safe names, and resume

Downloads normally land in `<output_dir>/<chat name>/<file name>`. Chats with the
same sanitized title receive distinct directories, and colliding filenames receive
suffixes such as `name (2).ext`. Concurrent transfers cannot overwrite each other's
destinations.

Each output directory contains a local `.telegram-downloader.sqlite3` manifest.
It records the account, chat, message, and media identity associated with each
destination and partial file. A previous download is skipped only when its recorded
size and content digest still match. Documents with a known size must finish at
that exact size; Telegram photos allow their reported size to be an estimate.

**Files downloaded before this manifest existed are preserved.** Their names and
sizes alone cannot establish which Telegram message they came from, so the first
new run may create suffixed copies instead of skipping them. Existing folders are
reused where they can be associated safely; nothing is automatically renamed,
overwritten, or deleted to migrate old downloads.

Recognized document fragments survive pause, cancel, normal close, account
switching, and transient failures. A later attempt resumes from a supported aligned
offset. Changed media identities never reuse another file's fragment. If a resume
stream ends early without a network error, the app can retry with a full transfer.
Unindexed legacy `.part` files are preserved but are not trusted for automatic
resume; Settings cleanup can remove these unrecognized fragments after confirmation.
Cleanup excludes active transfers and valid manifest-owned partials.

The queue and selected chats are still kept in memory. After reopening the app,
select the chats and **Add to downloads** again to skip verified completed media
and resume recognized fragments. Keep the manifest with its output directory;
removing it loses that identity information and makes existing files unindexed.

`downloader.log` rotates at 5 MB (3 backups).

## Tests

No Telegram account or network is needed: suites use fake clients and temporary
output directories. UI suites require a working desktop/Tk session.

```bash
.venv/bin/python -m unittest tests_auth tests_scheduler tests_storage tests_queue_state tests_scan
.venv/bin/python tests_smoke.py
.venv/bin/python tests_ui.py
.venv/bin/python -m unittest tests_ui_regressions tests_auth_ui
.venv/bin/python bench_responsiveness.py
.venv/bin/python bench_ui.py after
```

The focused suites cover authentication, bounded multi-chat scheduling, stop/resume
races, safe file promotion, partial-file recovery, incremental queue snapshots,
coalesced updates, and cancelled/superseded scans. See the
[implementation and validation record](docs/audits/2026-09-26/FIXES.md) for recorded
results and the distinction between fake-client checks and live Telegram behavior.
The responsiveness benchmark exercises real Tk, scheduling, and disk writes with
24 generated chat jobs and 10,000 retained rows; it saves a JSON report under
`docs/audits/2026-09-26/`. The static repaint comparison is available separately
through `bench_ui.py both`.

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
