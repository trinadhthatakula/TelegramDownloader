# Implementation follow-up: 2026-09-26 audit

This document describes the fixes implemented after the audit of commit `107f0d4`.
The [original audit](README.md), its probes, and recorded outputs remain unchanged
as evidence of the earlier behavior. They are historical findings, not a statement
that every reported defect remains present in the current working tree.

## Changes implemented

### Multi-chat scheduling and controls

`transfer_scheduler.py` owns per-chat jobs, queues, discovery tasks, retry timers,
and worker ownership on the backend event loop.

- Each job retains its own format selection. Adding another chat does not change
  the formats of existing jobs. Repeated Add actions do not rewind a live iterator
  or insert duplicate media rows.
- History cursors and accepted items are committed together. Failed or cancelled
  pages do not advance past media that has not entered the queue.
- History discovery is limited to three concurrent tasks, with reserved capacity
  included in budgets of 500 admitted/runnable transfers globally and 50 per chat.
  The configured 1–10 file-transfer slots are separate from discovery concurrency.
  Dispatch shares slots across ready chats and accounts for chats still discovering
  their first batch. Resumed historical rows wait for admission when capacity is
  full, so resuming paused jobs cannot bypass the budgets. Retained completed,
  paused, cancelled, and deferred history is not itself a runnable-work count.
- Each job captures its output directory when added. Delayed discovery, settings
  changes, retries, and pause/resume do not redirect that job's remaining files.
  Newly added jobs use the current configured directory.
- Discovery and transfer retries honor Telegram's FloodWait deadline as well as
  local backoff. A short retry timer cannot bypass a longer server wait.
- Pause, resume, cancel, and retry can target a chat or all jobs. Stop operations
  cancel discovery, timers, and workers and wait for termination before restarting.
  Worker/job generations prevent late results from overwriting a newer run.
- Retry failed affects failed work; Resume restores paused/cancelled work. Clear
  completed removes completed/skipped rows while retaining failed/cancelled items.
- Cancelling a job and choosing different formats restores matching previously
  cancelled files under the new job settings; duplicate IDs cannot silently hide
  that work. Concurrent revoked-session errors coalesce into one account reset.

### File identity, collisions, and interrupted downloads

`download_storage.py` adds an output-local SQLite manifest at
`.telegram-downloader.sqlite3`. Identity records include the account, chat,
message, media identity, and expected size; mutable Telegram file references are
not used as content identity.

- Destination reservations persist before transfers start. Distinct same-name
  media reserve separate files, and chats/accounts with identical sanitized titles
  receive distinct directories.
- Promotion creates the destination without replacing an existing file. An
  unexpected destination appearing during transfer receives a new suffix. A
  persisted promotion intent allows recovery after interruption; filesystems
  without hardlinks use exclusive file creation.
- Completed media is skipped only after its recorded size and SHA-256 digest
  match the file on disk. An unrelated equal-size file is never treated as the
  requested Telegram media.
- Known-size documents require the exact byte count before completion. Photos
  retain separate handling because Telegram can report estimated sizes.
- Manifest-owned document fragments survive pause, cancel, ordinary shutdown,
  account switching, and transient failures. Resume validates media metadata and
  uses supported aligned offsets. Changed media gets a separate partial path.
  A resumed stream that reaches normal EOF prematurely can fall back to a full
  download; network exceptions retain the partial for another resumable attempt.
- Cleanup protects active writers and valid manifest-owned fragments, including
  fragments retained across app restarts. It operates on the directory captured
  when the user requests cleanup.
- Preparation, hashing, promotion, and local writes run off the backend event
  loop. The Telethon-compatible sink handles both awaited document writes and
  photo helpers that write synchronously. Cancellation drains disk work before
  releasing file ownership, including repeated stop/shutdown requests.

#### Explicit legacy behavior

Existing files and folders are not automatically deleted, renamed, or claimed by
matching a filename and size. Where a prior unindexed file occupies a preferred
destination, a new transfer receives a suffix. The first run after upgrading may
therefore create duplicate content alongside older files; it preserves the older
file rather than assuming an identity that cannot be verified.

Unindexed legacy `.part` files remain on disk but are not appended to automatically.
They are eligible for the Settings cleanup action after confirmation. Recognized
resumable fragments are excluded from that cleanup. The manifest and its partial
files should remain with their output directory; deleting the manifest removes
the proof needed for safe skip/resume decisions.

The manifest persists file identity and progress, not the live queue or chat
selection. After reopening the app, the user must select chats and add them again;
verified completed media then skips and recognized documents resume.

### UI, responsiveness, and scan lifecycle

- `queue_state.py` maintains incremental global/per-chat totals and indexes.
  Normal summary and viewport requests no longer scan all retained history.
  Completed downloads release their heavy Telethon message objects.
- Repeated progress/view notifications are coalesced before Tk receives them.
  Control events are prioritized; the Tk event consumer has an elapsed-time budget
  and skips queue painting while that page is hidden.
- Chat/queue viewport capacity is calculated from the available height and row
  height. Resizing, scrolling over row children, and keyboard navigation can reach
  the end of the list.
- Both virtual lists use native `ttk.Scrollbar`. The previous custom scrollbar
  called `update_idletasks()` on every update, forcing pending paints to finish
  synchronously inside list callbacks.
- Chat filtering preserves the selection without forcing unrelated selected rows
  into every search. A selected-only view makes the retained selection reviewable.
- All files and Selected formats are mutually exclusive. Selected formats requires
  at least one extension. Format scans report progress, can be cancelled, and
  reject results from superseded scans or changed account/chat selections.
- File rows identify the source chat and expose details. Selected-chat and global
  totals/actions have separate scope. Persistent issue notices and a details view
  retain errors while unrelated progress continues.
- Folder checks, settings writes, and cleanup are background operations. Closing
  the app also delegates the shutdown work so Tk can remain responsive.
- Account changes clear old sidebar/filter state, pending UI callbacks, and busy
  controls. Results from an old account's folder checks cannot surface stale
  errors or leave the next account's Start button disabled.
- Authentication supports Enter-key submission, password visibility controls,
  focus changes between steps, and resend-code cooldown presentation.

## Validation recorded during implementation

Final checks on the integrated implementation:

| Command / suite | Recorded result |
| --- | --- |
| `python -m unittest tests_auth tests_scheduler tests_storage tests_queue_state tests_scan` | 67 passed: 17 authentication, 24 scheduler, 21 storage, 3 queue-state, 2 scan |
| `python tests_smoke.py` | 137 checks passed, 0 failed |
| `python tests_ui.py` | 63 checks passed |
| `python -m unittest tests_ui_regressions tests_auth_ui` | 10 real-Tk tests passed: 9 UI regressions and 1 Accounts integration, including the native-scrollbar change |
| Ruff on new modules/tests; F checks on existing modified Python files | Passed |
| `python -m pip wheel --no-deps .` | Built successfully; all five app modules and the CLI entry point verified in the wheel |
| `python bench_responsiveness.py` | All 14 checks passed, including bounded work, multi-group overlap, byte validation, and heartbeat targets |
| `python bench_ui.py after` | Completed for 50, 500, 1,200, 4,000, and 8,900 retained rows |

Run from the repository with the configured virtual environment:

```bash
.venv/bin/python -m unittest tests_auth tests_scheduler tests_storage tests_queue_state tests_scan
.venv/bin/python tests_smoke.py
.venv/bin/python tests_ui.py
.venv/bin/python -m unittest tests_ui_regressions tests_auth_ui
.venv/bin/python bench_responsiveness.py
.venv/bin/python bench_ui.py after
```

The new storage regressions include simultaneous same-name transfers, unindexed
equal-size files, restart verification, external destination races, no-hardlink
fallback, active/resumable cleanup protection, normal shutdown, repeated
cancellation, document/photo validation, resumed-stream EOF fallback, and the
installed Telethon library's actual sink helpers with a fake transport.

All test transfers used fake clients and temporary output directories. The passing
Tk tests establish the exercised interactions in the available desktop session;
they do not establish live Telegram throughput, behavior across all networks, or
the absence of every UI stall. No real Telegram account or private session was
used for these checks.

## Integrated responsiveness measurement

[Reproducible benchmark](../../../bench_responsiveness.py) and
[recorded JSON](validation-responsiveness.json), run on macOS 27.2 arm64 with Python
3.14.7. This uses real Tk, the production scheduler, and real temporary disk writes
with a fabricated Telegram transport. The 15-second scenario starts 24 chat jobs
with 80 available files per chat and 10,000 retained completed rows. During
transfers it repeatedly changes filters, scrolls, pauses, cancels, resumes, and
switches pages. All 14 acceptance checks passed.

| Measurement | Result |
| --- | ---: |
| Steady 20 ms heartbeat interval, p95 / maximum | 28.93 / 33.95 ms |
| Heartbeat lateness beyond the requested interval, p95 / maximum | 8.93 / 13.95 ms |
| Add-to-downloads callback | 0.39 ms |
| Combined group-filter and scroll callback, p95 / maximum | 4.79 / 5.80 ms |
| App construction / first queue layout | 578 / 147 ms |
| Maximum discovery tasks / transfer workers | 3 / 3 |
| Maximum admitted work including discovery reservations | 500 |
| Maximum admitted work per chat | 50 |
| Groups starting transfers / simultaneously transferring groups | 24 / 3 |
| Completed files checked byte for byte | 332 |
| Cancel-all drain | 5.75 ms |

Steady measurements begin after the first two seconds; app construction and first
layout are reported separately. Resumed work can include existing rows awaiting
admission: the peak of 546 queued/active UI rows included 46 deferred rows, while
admitted work plus reservations stayed at 500. The queue drained with no failed
transfers or persistent UI errors. This is an interaction/responsiveness check,
not a live Telegram bandwidth measurement.

A preliminary run exposed 79 ms p95 heartbeat intervals and roughly 85 ms
filter/scroll callbacks. Profiling identified `CTkScrollbar`'s synchronous idle
flush. Replacing the two virtual-list scrollbars addressed that measured path;
the final run above uses the source change to native scrollbars.
The original historical audit's headless timings remain separate evidence.

The separate static benchmark's 8,900-row batch repaint averaged 11.3 ms,
including Tk painting; scrolling to the end and painting took 28.64 ms. The
legacy UI suite also measured a 1.0 ms handler for loading 435 chats, followed by
141.3 ms of initial native painting. These include different work from the
callback-only measurements above and are reported separately.
