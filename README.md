# Telegram Downloader

A desktop app (customtkinter + Telethon) for bulk-downloading media from Telegram
chats and channels. Pick chats, optionally filter by file type, and it streams
files into per-chat folders while showing live per-file progress.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Create a `.env` next to `interactive_downloader.py`:

```
TG_API_ID=your_id          # from https://my.telegram.org/apps
TG_API_HASH=your_hash
OUTPUT_ROOT=./telegram_downloads   # optional; default download folder
```

`.env` and the `*.session` files are credentials. They are listed in
`.gitignore` — keep them out of version control and out of backups you share.

Sign in once from a terminal so Telethon can write its session file:

```bash
.venv/bin/python -c "from telethon import TelegramClient; import os; \
  TelegramClient('downloader_session', int(os.environ['TG_API_ID']), \
  os.environ['TG_API_HASH']).start()"
```

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

No Telegram account or network needed — both suites use fakes.

```bash
.venv/bin/python tests_smoke.py   # backend: paths, config, download worker, retries, resume
.venv/bin/python tests_ui.py      # builds the real Tk window and drives it
.venv/bin/python bench_ui.py      # queue-view repaint cost vs. queue size
```

## Checking your chat list

To confirm the app is listing everything your account has, close the app and run:

```bash
.venv/bin/python diagnose_dialogs.py
```

It pages both folders read-only, downloads nothing, and prints per-type counts
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
