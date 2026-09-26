# Telegram Downloader: performance, multi-group, and UI/UX audit

Audited on 2026-09-26 against commit `107f0d4`.

The audit used three specialist agents for UI/UX, scheduling, and reliability, plus
independent source review and offline probes. Production code was not changed.
All probes use fake Telegram clients and disposable output directories. No real
Telegram account, session, credential file, or private downloader log was accessed.

## Assessment

Multi-group support exists, but its job isolation, cancellation, discovery
concurrency, and initial slot allocation are incomplete. The existing two-group
happy-path test passes; that does not establish smooth concurrent downloads.

The reported UI freezes have several plausible contributors in the current code:
queue-wide scans and shared locks on the Tk thread, unrestricted discovery fanout,
synchronous filesystem work, and repeated UI work as batches arrive. Fixed row
pools also have a viewport sizing defect that can make a working list appear
stuck. A live Tk freeze was **not reproduced** in this environment: the GUI
benchmark exits with code 134 while creating Tk. Distinguish these confirmed code
paths and synthetic measurements from a measured root cause on the user's desktop.

Several data-integrity bugs deserve attention before throughput tuning. Fake
transfers reproduced overwritten files, false duplicate skips, and lost messages
following a failed history page.

## Highest-priority findings

Priority P1 means address before relying on multi-group downloads; P2 means address
in the subsequent reliability/responsiveness pass.

### 1. P1: Concurrent downloads can overwrite each other's output

**Trigger:** two different messages resolve to the same chat directory and filename.
Workers check whether the destination exists before awaiting the transfer. Both can
choose the same free path; both later call `os.replace`.

**Reproduction:** two concurrent four-byte fake transfers both reported `Completed`,
but only one `same.mp4` survived. An unrelated equal-size message and a second chat
with the same title were subsequently `Skipped (Exists)` without transferring.

**Code:** `interactive_downloader.py:111` (title-only directories), `:930` (preflight
existence/size check), `:944` (alternative name selection), `:966` (promotion).

**Fix:** reserve final paths before dispatch, use promotion that cannot overwrite
another item's destination, and persist a mapping from account/chat/message/media
identity to output paths. Filename and byte count are insufficient identity.
Preserve existing directory compatibility through an explicit migration map.

Evidence: [probe](probes/file_integrity.py), [output](file-integrity-results.txt).

### 2. P1: A failed history page can silently lose messages

**Trigger:** Telegram iteration yields some messages, then fails before a batch is
committed. `last_msg_id` advances on every yielded message, while new items are
appended only after iteration succeeds.

**Reproduction:** message 3 was yielded, then an injected error left cursor 3 and an
empty queue. Retrying from offset 3 returned messages 2 and 1; message 3 was lost.

**Code:** `interactive_downloader.py:681–682`, `:697–725`, `:739–746`.

**Fix:** stage cursor changes and commit cursor + items together, or commit each
accepted item before advancing the recoverable cursor. Retrying a page must be
idempotent and must not skip uncommitted items.

Evidence: `cursor_loss` in [scheduler results](scheduler-results.txt).

### 3. P1: Adding or restarting groups mutates jobs already in progress

**Triggers:** enqueue A with `.mp4`, then enqueue B with `.jpg`; or click Start again
while a selected group's history iterator is running.

**Reproductions:** A subsequently queued `.jpg`, despite requesting `.mp4`. Repeated
Start produced two iterators for one chat and queue IDs `1_2, 1_1, 1_2`, while the
index contained only two IDs.

**Code:** `interactive_downloader.py:631–632` stores filters globally; `:643–648`
resets pagination and clears the fetching flag even if an iterator is active;
`:692–722` checks duplicates before the eventual append.

**Fix:** create an explicit job per chat with immutable format/depth options and a
run ID. Make Add to queue idempotent for existing jobs; offer Restart separately.
Allow only one discovery task per job and perform duplicate insertion atomically.

Evidence: `global_filters` and `reenqueue` in [scheduler results](scheduler-results.txt).

### 4. P1: Cancel All does not stop discovery; rapid Retry races with cancellation

**Reproduction:** Cancel All was invoked while a history iterator was paused and the
queue was empty. Releasing the iterator started and completed three downloads after
cancellation. Separately, cancelling a transfer and immediately retrying it returned
one requeued item, but its old worker later overwrote the retry state back to
`Cancelled` and cleared its task reference.

**Code:** `interactive_downloader.py:1147–1162` cancels download tasks but not history
tasks; `:718–728` can still append and dispatch; `:1164–1179` permits retry before
worker termination; `:973–976` and `:1027` mutate the shared item in the old worker.

**Fix:** model job states explicitly, including Cancelling. Stop discovery,
backoff timers, and workers as one operation; wait for termination before a retry.
Require a matching run/worker ID before an old task may update an item.

Evidence: `cancel_fetch` in [scheduler results](scheduler-results.txt). Rapid retry
was also verified in a separate inline fake-client probe by the reliability agent.

### 5. P1: Fixed 40-row lists do not account for viewport height

Both chat and queue views put up to 40 rows in ordinary frames and clamp scrolling
to `total - 40`. Row heights require roughly 1,200 px for chats and 1,600 px for the
queue, while the initial window is 760 px tall. With 30 items the code disables
scrolling entirely; with longer lists the final rows can remain below the viewport
at the maximum permitted offset.

**Code:** `interactive_downloader.py:1271`, `:1530–1540`, `:1663–1677`, `:1719–1754`,
`:1772–1796`, `:1901–1959`.

**Fix:** derive visible capacity from actual viewport height and row height, add a
small overscan buffer, and use visible capacity for scrollbar fractions and limits.
Recalculate on resize and font scaling. Test that the final chat and final Cancel
button can actually be reached. This is confirmed by layout/scroll calculations;
exact on-screen clipping remains visually unverified in this audit.

### 6. P1: Discovery is unbounded and initial worker allocation can favor one group

**Reproductions:** selecting 100 groups with `max_parallel_files=1` opened 100
simultaneous history iterators. With two groups and three download slots, all three
active downloads initially belonged to group 1 while group 2's four files waited.
The scheduler's round-robin logic applies only to groups ready at dispatch time;
it does not reserve opportunities for discovery still in progress.

**Code:** `interactive_downloader.py:650–652`, `:840–888`.

**Fix:** bound history concurrency separately from file-transfer concurrency,
maintain bounded per-chat queues and a global pending-work budget, and use an
explicit active-chat scheduler. Establish fair initial dispatch for multiple ready
chats and bounded waiting time as additional chats become ready. A configurable
per-chat cap can prevent long downloads from occupying every slot.

Evidence: [load probe](probes/scheduler_load.py), [output](scheduler-load-results.txt).

### 7. P1: Discovery retries ignore Telegram's FloodWait deadline

**Reproduction:** an injected 120-second FloodWait set the download deadline to 120
seconds, but the discovery backoff selected two seconds; invoking another fetch
made another history request while that deadline was still active.

**Code:** `interactive_downloader.py:734–738`, `:750–781`, `:654` versus the download
scheduler's gate at `:845–847`.

**Fix:** every applicable discovery request and retry must honor Telegram's
server-provided wait. Schedule retries no earlier than the maximum of server wait
and local backoff, rather than merely pausing file dispatch.

Evidence: `flood_fetch` in [scheduler results](scheduler-results.txt). The probe
manually retries while the gate is active; it does not wait two real minutes.

### 8. P1: Format controls can contradict the user's selection

“All Formats” and every discovered extension begin selected. Deselecting `.mp4`
while All remains selected still downloads videos. Clearing every checkbox also
silently falls back to All.

**Code:** `interactive_downloader.py:1581–1597`, `:2121–2130`, `:631–632`.

**Fix:** offer mutually exclusive All files / Selected formats modes. Require at
least one selected extension in selective mode, and show the effective selection
in the job summary before enqueueing.

## Responsiveness and reliability improvements

### 9. P2: Queue work scales with all retained history, not just visible/active items

Completed items and their Telethon message objects remain in memory until explicitly
cleared. Summary calculation scans the entire queue under `queue_lock` every 400 ms,
even with another page open. Filtering copies/scans the queue during painting and
scrolling; scheduling and per-chat refill also rescan it. A 40-widget pool bounds
widget count, not total work or lock hold time. Non-progress events also enter an
unbounded queue; processing is limited to 200 events per tick, with no elapsed-time
budget. Progress throttling is already present, so this is not simply a missing
progress throttle.

**Code:** `interactive_downloader.py:321`, `:474–497`, `:793–801`, `:850–862`,
`:1055–1072`, `:1309–1355`, `:1895–1935`, `:1996–2046`.

Headless median timings from seven samples on this machine (10 synthetic chats,
three active files, mostly completed history; no Tk painting or network included):

| Retained items | UI summary scan | Filter by chat | Busy-slot scheduler scan | Refill count scan |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 0.065 ms | 0.008 ms | 0.007 ms | 0.010 ms |
| 10,000 | 1.046 ms | 0.088 ms | 0.062 ms | 0.105 ms |
| 100,000 | 10.548 ms | 1.348 ms | 0.683 ms | 1.428 ms |
| 300,000 | 33.278 ms | 5.263 ms | 2.203 ms | 5.653 ms |

These show scaling cost; they do not independently explain a seconds-long freeze
at a smaller queue size. Actual message objects, widget work, filesystem waits,
and lock contention are additional costs.

**Fix:** maintain incremental global/per-chat counters and per-chat deques; keep
completed history separately from active queues; expose immutable, small view
snapshots or viewport slices. Serialize state mutation on the backend loop. Coalesce
redundant updates before enqueueing, prioritize control/error events, and give Tk
work a small time budget. Skip hidden-page paints and update changed sidebar rows.

Evidence: [CPU probe](probes/queue_costs.py), [measurements](queue-costs.json).

### 10. P2: Filesystem work still blocks the UI; cleanup can delete active files

Start checks/creates the output directory synchronously. Settings save calls fsync;
startup/settings/cleanup recursively walk the download directory on the Tk thread.
Slow disks or network-mounted destinations therefore block interaction. Cleanup
calls every `.part` file an orphan, including files still being written.

**Reproduction:** cleanup counted and deleted a live four-byte temporary file; its
worker then requeued with an error.

**Code:** `interactive_downloader.py:191–219`, `:1193–1227`, `:1305`, `:1889–1891`,
`:2115`, `:2188–2233`.

**Fix:** run filesystem checks/scans in background workers, track active temporary
paths, and exclude them atomically from cleanup. Display operation progress and
allow cancellation. Moving a destructive scan off the UI thread alone is insufficient.

Evidence: `active_cleanup` in [file integrity results](file-integrity-results.txt).

### 11. P2: Interruption discards resumable work; validation is too permissive

Normal Close cancels workers, whose cancellation handlers delete their partials.
FloodWait and ordinary transfer errors also discard partials. Current resume support
mostly helps pre-existing fragments or abnormal process exits. The reliability
agent confirmed deletion of an in-flight partial on normal shutdown.

Additionally, a fake transfer returning one byte for a declared 4,096-byte MP4 was
marked Completed: photo-size tolerance is applied to every media type. This proves
a missing integrity check, not that Telethon routinely returns truncated transfers.

**Code:** `interactive_downloader.py:959–976`, `:977–1005`, `:1076–1129`, `:1230–1247`.

**Fix:** distinguish Pause/Stop from Discard, preserve useful partials with media
identity metadata, and validate document lengths before promotion. Handle photo
size estimates separately. Add restart/resume tests using a normal close path.

### 12. P2: Account reset leaves stale UI controls from the old account

The model clears its chats/filter, but existing sidebar widgets are never removed
and the queue header is not reset. Old account chat names remain clickable, leading
to empty filters and stale information.

**Code:** `interactive_downloader.py:1367–1377`, `:2017–2046`.

**Fix:** reconcile sidebar widgets against current account/job IDs, remove missing
entries, and reset header/search/filter state as part of the account transition.

### 13. P2: Multi-group status and controls are underspecified

All Groups rows omit the source chat. Selecting one group still shows global
summary totals. Completed/discovered ratios do not say whether discovery is running,
finished, or retrying. There are no per-group pause/cancel/retry controls. Retry Failed
also retries intentionally cancelled items; Clear Finished removes failed/cancelled
entries along with completed ones.

**Code:** `interactive_downloader.py:292–297`, `:1164–1190`, `:1614–1619`,
`:1961–2057`.

**Fix:** add source-chat labels and explicit per-job states (Discovering, Downloading,
Waiting for Telegram, Paused, Failed, Finished). Separate filtered and global totals.
Make action scope explicit and preserve recoverable failed items until dismissed.

### 14. P2: Repeated format scans can race and replace current choices

Scan Selected Chats has no busy guard or cancellation. Scans traverse chats
sequentially and can run unlimited history. Multiple scans may finish out of order;
the last result rebuilds format choices even if it belongs to an older selection.

**Code:** `interactive_downloader.py:587–621`, `:1402–1406`, `:1570–1573`, `:2094–2104`.

**Fix:** track one scan operation or an explicit scan queue, associate results with
selection/run IDs, cancel obsolete scans, and show per-chat progress and partial
results without resetting deliberate format choices.

## Recommended implementation order

1. **Correctness and data protection.** Introduce per-chat jobs and run ownership;
   make enqueue idempotent; fix cursor commits, duplicate insertion, cancel/retry
   termination, destination reservation, and file identity. Add focused failing
   regression cases before each fix.
2. **Bounded and fair execution.** Separate discovery from transfers, limit both,
   enforce FloodWait on discovery, and schedule ready jobs fairly. Put all backend
   mutations on its event loop. Keep the existing GUI toolkit during this work.
3. **Responsive data/view boundary.** Replace queue scans with incremental counters
   and small snapshots; bound event work; move filesystem operations off Tk; correct
   viewport sizing and stop unnecessary hidden-page work.
4. **UI/UX clarity and recovery.** Add per-group state/control, consistent filtered
   summaries, explicit format modes, durable error details, safe cleanup, and true
   pause/resume. Remove stale account UI state. Add keyboard navigation,
   Enter-to-submit login, full-name inspection, and consistent wheel handling.

The natural architecture is a small job model + bounded discovery service + fair
transfer scheduler + incremental view state. Split these responsibilities out of
the large downloader module as they are fixed, rather than rewriting everything
or changing GUI frameworks first.

## Validation and acceptance criteria

Current checks: **124 smoke checks passed; 17 authentication tests passed.**
`bench_ui.py after` aborted with exit code 134 at Tk startup. The user previously
verified profile-photo rendering; this audit did not repeat live Telegram login.

Existing coverage misses the reported failure modes:

- `tests_smoke.py:243–271` asserts eventual completion for two groups, including a
  fairness assertion based only on final counts.
- Filename-collision coverage runs with `parallel=1` (`tests_smoke.py:375`).
- UI tests equate gridded rows with visible rows and do not check viewport bounds.
- `bench_ui.py:45–111` times direct repaint calls with seven synthetic groups, not
  real enqueue-to-response latency, event backlog, discovery fanout, or slow disks.

Proposed acceptance criteria (targets, not measurements already achieved):

- Run 1/5/20/100 synthetic groups with mixed history latency, tiny files, long files,
  existing files, and overlapping names. Discovery concurrency and pending memory
  stay within configured bounds.
- Use a 20 ms Tk heartbeat: target p95 interaction delay below 50 ms and max below
  200 ms during normal enqueue/scroll/cancel workloads; move slow filesystem work
  entirely out of callbacks. Record hardware/environment with results.
- Every selected group gets a bounded turn as it becomes ready; test time to first
  transfer and overlapping activity, not just eventual completion.
- After Cancel completes, no old run starts another transfer or mutates a new run.
  Retry waits for cancellation to settle.
- Mid-page failures lose zero messages; duplicate enqueue produces zero duplicate
  items; same-name transfers preserve all distinct media.
- Resizing/large-font tests can reach the final chat and final queue action at
  1024×768 and 1150×760. Keyboard and pointer interactions both work.
- Normal pause/close resumes partials; cleanup cannot remove an active partial;
  switching accounts leaves no old sidebar rows/header state.

## Reproducing the offline evidence

Run from the repository root:

```bash
.venv/bin/python docs/audits/2026-09-26/probes/scheduler.py
.venv/bin/python docs/audits/2026-09-26/probes/scheduler_load.py
.venv/bin/python docs/audits/2026-09-26/probes/file_integrity.py
.venv/bin/python docs/audits/2026-09-26/probes/queue_costs.py
```

The scripts are audit probes of the current implementation, not passing regression
tests or production changes. Several intentionally inject failures; associated
error output is expected. Saved results capture behavior at the audited commit.
