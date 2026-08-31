# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Flask app for a Raspberry Pi that identifies lab members by tapping a DoD
CAC (smart card) on a USB reader (no PIN), logs check-in/check-out events to
SQLite, drives an always-on kiosk display (status board / screensaver +
toast confirmation), and serves a dashboard viewable from other PCs on the
network.

## Developing locally (off the Pi)

You are almost certainly developing off the Pi (Windows/WSL/Mac). Use the
lean dependency set — `requirements.txt` includes `pyscard`/`python-pkcs11`,
which are hardware-only packages that need a real smart card reader driver
stack to even compile against, and will fail to install here.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
python3 app.py
```

`cac_reader.py` detects that `pyscard` isn't installed and logs a warning
instead of crashing — every route still works. Simulate a card tap instead
of tapping a real card:

```bash
curl -X POST http://localhost:5000/api/manual-toggle \
     -H "Content-Type: application/json" \
     -d '{"member_id": 1}'
```

Each call toggles that member in/out, so run it twice to exercise both the
check-in toast and the checkout note prompt. Clicking a name on the kiosk
page does the same thing through the UI (see "Checking in without a card"
below). Either way the event is flagged `manual` and carries a "No card"
mark on the board - that is production behaviour, not a dev shortcut, so
neither is a byte-for-byte stand-in for a tap.

Then open `http://localhost:5000` (kiosk display) and
`http://localhost:5000/dashboard` in a browser. There is no test suite or
linter configured — verify changes by running the dev server and exercising
the routes/UI directly.

`scripts/setup.sh` is the Pi deployment installer only (installs
`pcscd`/`opensc`, kiosk Chromium, systemd services) — never run it in dev.

### Checking in without a card

Clicking a name on the roster strip checks that person in or out without a
CAC. It exists for a dead reader, a card left at home, and the stretch
before the reader is installed at all, so it has to work with a mouse and
nothing else - hence a two-button confirmation dialog (`.confirm`,
`openConfirm()`/`submitConfirm()` in `main.js`) rather than anything typed.
It posts to the same `/api/manual-toggle` the dev loop and the network
fallback use.

- **Every event it writes is flagged `manual`, and the board says so.** The
  kiosk prints an amber `NO CARD` under that person's name until their next
  tap, and the dashboard shows it in the roster and the activity log's Note
  column. This is the point of the feature's design, not decoration: a CAC
  tap means something precisely because a card was read, and an entry
  anybody could have clicked must not be indistinguishable from one. The
  flag lives on the event row rather than being derived later, because
  nothing else in the row distinguishes the two.
- **The mark shares the roster card's note line rather than adding one.**
  `.kiosk .roster__card` has a `min-height` pinned to a three-line card
  (name + status + note), so a fourth line would start the whole bottom bar
  wobbling again - see the comment on that rule. A manual checkout can
  carry a typed note too, so both go on one line as `NO CARD · at lunch`
  (`rosterNote()` in `main.js`, `noteText()`/`noteLine()` in
  `dashboard.js`).
- **It always confirms first.** The strip is six large targets along the
  bottom of a screen standing in the open all day; without the dialog one
  stray click silently logs somebody in or out and the only trace is a line
  in an append-only log. The dialog cancels on a backdrop click, on
  Escape, and on a 20s timeout, so an abandoned one can't sit on the board.
  A card tap arriving mid-dialog supersedes it - `showToast()` calls
  `closeConfirm()`.
- **The pointer is hidden by default and revealed by movement.**
  `body.kiosk` is `cursor: none`; a `mousemove` listener adds `has-pointer`
  and drops it again after `POINTER_IDLE_MS` (8s), so the strip is
  clickable without leaving a cursor parked on an unattended board for a
  week. `is-note-prompt` and `is-confirm` force it visible regardless,
  because both put controls on screen and then wait. The listener does one
  class check per event and the pending timeout reschedules itself rather
  than being cleared and reset thousands of times; `cursor` is not a
  rendered property, so neither state costs a paint.
- **Roster cards are `<button>`s on the kiosk and plain `<div>`s on the
  dashboard.** Only the kiosk's are controls. The four UA-reset properties
  on `.roster__card` (`appearance`, `border`, `font`, `text-align`) are what
  keep the two rendering identically; without them the kiosk strip picks up
  a native border and centred system text. The click handler is delegated
  to `#roster`, since `renderRoster()` replaces the strip wholesale.
- **`poll()` numbers its own requests and drops out-of-order replies.**
  `submitConfirm()` fires a poll the instant the POST returns instead of
  waiting out `POLL_MS`, so two are briefly in flight; the older reply
  carries pre-click state and would repaint a stale roster and re-toast the
  event before it. That is also why the toast is left to the poll rather
  than raised from the POST response - events reach the screen through
  exactly one path however they were caused.

### Kiosk slide rotation

The media panel cycles the objectives from `config/objectives.json`, one
full-panel slide at a time on a `SLIDE_MS` timer, drawn over an optional
looping background video. **Videos used to be playlist items interspersed
with the objectives; they aren't any more** (there is no `MEDIA_FILES` array
and no `buildPlaylist()` — if you find code or docs referring to them,
they're stale). There is exactly one video now and it is scenery.

An objective is **either a plain string or `{text, image}`**, where `image`
is a filename in `static/media/`; `normalizeObjective()` accepts both so
hand-edited files that predate the image support keep working. A slide with
a picture gets the `has-image` class, which switches it to a side-by-side
layout — `setSlideImage()` only touches `src` when the filename actually
changed, so an objective coming back around on the next rotation doesn't
re-decode the same picture. A picture that fails to load drops back to a
text-only slide and clears `dataset.file`, so the next rotation retries it.

The background video is named by the `BACKGROUND_VIDEO` constant in
`main.js` (empty string = none); there's no directory-listing endpoint, so
it's set by hand after dropping the file in `static/media/`. Only
`background.mp4` itself is tracked in git — `.gitignore` excludes every
other video in that directory, because encode experiments and multi-MB
sources are permanent once committed (the repo's history had to be
rewritten once already to get 131MB of them back out). Keep sources
elsewhere, or let the ignore rule do its job. Things worth knowing before
changing the video:

- **The `<video>` must not carry a `loop` attribute, and playback must
  never be allowed to reach the end of the file.** `main.js` loops it by
  hand: a `timeupdate` handler seeks back to 0 once `currentTime` passes
  `duration - LOOP_TAIL_S` (5s). This is not a style choice — reaching
  end-of-stream permanently wedges the Pi's hardware decoder. See
  Performance below for the measurements. The last 5s of the file
  therefore never play; the shipped `background.mp4` handles that by being
  a seamless 12s loop with a repeat of its own first 5s appended, so the
  visible portion is the whole loop and the wrap lands exactly on the
  source's loop point (measured: the seam is 26.8dB against 27.0dB for an
  ordinary 30fps frame step, i.e. indistinguishable). README step 7 has the
  encode. Keep any replacement comfortably longer than 5s — `main.js` logs
  an error if it isn't. Two separate reasons have now banned `loop` here —
  the older one was that back when videos were playlist entries, `loop`
  meant `ended` never fired and the rotation could never advance. That
  reason is gone; this one is not.
- **`has-bg` goes on `<body>`, and is added on `loadeddata`, not when the
  src is set.** The video and its scrim are `display: none` until a first
  frame actually decodes, so a missing file or a slow load never shows a
  black rect behind the slides, and an unconfigured background costs
  nothing — not even a compositing layer. It sits on `<body>` rather than
  `#media` because the toast and reading overlays are outside the panel and
  branch on it too (below). The `error` handler removes the class again and
  does *not* retry: it's one hardcoded filename, so a failure repeats
  exactly.
- **A 5s watchdog samples `currentTime` and reports `video-stall` if it has
  not moved for 15s.** The decoder wedge described under Performance is
  silent by construction — no `error`, no `stalled`, just frames that stop
  arriving while the element still believes it is playing — so sampling is
  the only way to observe it at all, and without this a frozen board is
  indistinguishable from a working one to anything off the panel. On firing
  it pauses the video and drops `has-bg`, degrading to the flat background
  rather than leaving a dead frame up for the rest of the run. It
  deliberately does not attempt recovery: the wedged decoder never comes
  back, so a retry loop would only re-report the same stall forever. A
  sibling timeout reports `video-never-started` when no first frame arrives
  within 30s, which is what the non-faststart range-request stall looks like
  from the page's side.
- **Slide text is kept readable with a flat `rgba()` scrim
  (`.media__scrim`), never `backdrop-filter`.** Blurring live video every
  frame is the single most expensive thing this hardware could be asked to
  do — see Performance below.
- **A single objective doesn't reschedule the timer.** With one slide
  nothing ever changes, so `showNextSlide()` skips the `setTimeout` rather
  than waking the panel every 12s to redraw identical content.

### Overlays over the background video

The toast, the "reading card" overlay and the click-to-toggle confirmation
dialog are full-screen `position: fixed` layers. When a background video is
configured they go translucent so it stays visible through the whole tap
flow; with no video they stay fully opaque, which lets the compositor skip
painting the board underneath entirely. That's why every rule is gated on
`body.has-bg` rather than applied unconditionally — see Performance below.

Three pieces of that treatment have to move together, and missing any one
of them looks broken rather than subtly wrong:

- **`body.is-overlay`** is toggled by `syncOverlayState()`, which *derives*
  the flag from whether any of the three currently has `is-visible`. It is
  deliberately not one toggler per overlay: they overlap when a tap
  completes and the toast replaces the reading overlay (or the confirmation
  dialog), and independent toggles race there. Any new code path that shows
  or hides one must go through `setToastVisible()` / `closeConfirm()` and
  then `syncOverlayState()`, not `classList` directly. They stack
  reading (55) → confirm (58) → toast (60): a card presented while a dialog
  is open is the more important thing to say, and the toast supersedes both.
- **`.media__scrim` is hidden while an overlay is up.** Stacking the
  panel's 0.72 scrim under the overlay's 0.72 scrim leaves only ~8% of the
  video coming through — visibly black, and the reason the effect looks
  like it isn't working if this rule is dropped.
- **`.slide` is hidden with `visibility`, not `display`.** `main.js` owns
  the slide's inline `display`, so a `display` rule in the stylesheet loses
  the specificity fight. Hiding it at all matters because the slide text
  would otherwise ghost through behind the person's name.

## Performance (read before any UI change)

The target hardware is a Raspberry Pi 4B driving an always-on Chromium
kiosk. **Treat rendering cost as a hard constraint, and prefer a plainer
look over a smoother one** — visual polish that costs frames is not worth
it here. This is a standing preference from the lab admin, not a one-off
cleanup.

What was already removed for this reason (don't reintroduce it):

- **`backdrop-filter` / `filter: blur()`** — the worst offender on this
  hardware. A full-screen blurred overlay makes the GPU re-read and blur
  everything beneath it every frame. The toast and reading overlays use a
  flat fill instead — opaque when there's no background video (the
  compositor then skips painting what's behind them), a flat `rgba()` when
  there is. Never a blur either way.
- **Full-screen decorative overlays** — a `.scanlines` repeating-gradient
  layer sat over the whole screen and forced a compositing pass on every
  repaint underneath.
- **`box-shadow` glows** — each is a separate blur rasterization. Use flat
  color; use `outline` (not `box-shadow`) for focus rings.
- **Animating anything but `opacity`/`transform`** — the "reading card"
  dot animates opacity only. Avoid animating a `transform` on top of a
  `box-shadow`, which re-rasterizes the shadow every frame.
- **Webfonts** — the Google Fonts `<link>` was render-blocking on every
  kiosk boot and a hard dependency on outbound internet the lab may not
  have. `--mono`/`--sans` are system stacks now; keep it that way.
- **Overlays parked at `opacity: 0`** — they stay in the paint/composite
  tree. Hide with `display: none` so idle costs nothing (the kiosk is idle
  ~99% of the time).
- **`:has()` on `<body>`** — re-runs selector matching on every DOM
  mutation, and the roster re-renders on a timer. Toggle a class from JS.

Rules that matter for anything new:

- **Poll handlers must not touch the DOM when nothing changed.** Every
  render function compares a JSON snapshot of its data first and bails out
  (`skipIfUnchanged()` in `dashboard.js`, `lastRosterJson` /
  `lastClockText` / `loadObjectives._last` in `main.js`). Without that
  guard, `innerHTML` rebuilds relayout the section 40x a minute forever.
  Any new polled section needs the same guard.
- **The background video must be muxed with `-movflags +faststart`.** With
  the `moov` index at the end of the file (ffmpeg's default), Chromium has
  to range-request the tail before it can play, and on the Pi that fetch
  pattern stalls partway through — the video freezes after a few seconds
  with `readyState` 2 and *no* error code, so the `error` fallback never
  fires and the board just sits on a dead frame. Confirmed on real
  hardware: playback died needing byte 4,695,155 of 6,258,671 (75%), with
  `moov` at 99.9%. It does not reproduce on a dev machine, which buffers
  the whole file before the pattern matters. `grep -abo moov file.mp4 |
  head -1` should report a low offset.
- **The background video must be H.264 (AVC) and no wider than 1920px.**
  Confirmed from `chrome://gpu` on the Pi: the only hardware decode profiles
  are h264 baseline/main/high, 32x32 to 1920x1920. HEVC/VP9/AV1, or anything
  above 1920px, falls back to software decode and will peg the CPU. 1080p
  H.264 is both the panel's native resolution and inside that ceiling, so
  encode to exactly that, at `-level:v 4.0`. It now loops **continuously**,
  not just during its slot in a playlist, so it is the one thing on this
  board with a permanent per-frame cost: hold it to 30fps, strip the audio
  track (`-an`; the kiosk plays muted), and don't stack anything expensive
  on top of it. Duration is *not* the thing to minimise — it costs nothing
  per frame, and the file must be longer than `LOOP_TAIL_S` anyway. Bitrate
  is a straight size/quality trade with no measured decode penalty: the
  shipped clip is 12.4 Mb/s at level 4.0 and verified on the Pi, up from
  4.1 Mb/s, which was visibly soft. Above ~20 Mb/s x264 needs level 4.2,
  which is untested on this hardware.
- **The background video must never be played to its end.** Chromium drains
  the hardware decoder at end-of-stream, and the Pi's `bcm2835-codec` V4L2
  drain never completes: it stops returning frames, the picture freezes with
  **no error code**, `readyState` drops from 4 to 2, and it never recovers.
  Because nothing errors, the `error` fallback never fires and the board sits
  on a dead frame forever. Confirmed on real hardware across six clips —
  12.07s, 9.07s and 30.0s durations; the lab footage and a synthetic
  `testsrc2` pattern; 1080p and 720p; 4.1 and 1.2 Mb/s; High, Main and
  Baseline profiles; with and without B-frames. **Every one froze at
  `duration` minus 3.1–3.3s**, i.e. as decode approached EOS, and nothing
  about the frames at that point was unusual. Two things isolate it: the
  same files play indefinitely under `--disable-accelerated-video-decode`
  (software decode has no drain path), and wrapping back to 0 before EOS
  loops forever with hardware decode on. Hence `LOOP_TAIL_S` in `main.js`.
  Don't "simplify" that back into a `loop` attribute.
- **Objective slide pictures render at most ~780px wide** (`.slide__image`
  is capped at 45% of the slide's content box, which is ~1730px on a 1080p
  panel). ~800px wide is the right size; anything larger is decoded and
  scaled down for nothing.

**The production kiosk panel is a standard 1080p display** (~2.1M pixels).
Development happens on a 3440x1440 ultrawide, so check layout changes at
1920x1080 — that's the size that ships. A full-screen effect still costs a
V3D-class GPU real time at either resolution; assume anything covering the
whole screen is expensive.

The GPU path was verified on the Pi (V3D 4.2.14.0, Mesa 26.2.0, Chrome 151):
Rasterization, Compositing, Canvas and Video Decode all report hardware
accelerated. (That dump's "Display(s) Information" shows 3440x1440 because
the Pi was on the dev monitor at the time — the driver findings hold
regardless of which panel is attached.) `autostart/labwc-autostart` passes
`--enable-gpu-rasterization`; it deliberately does *not* pass
`--ignore-gpu-blocklist`, since the blocklist was shown not to be vetoing
anything on this hardware. If the board ever looks sluggish again, re-check
`chrome://gpu` on the Pi before changing code.

## Architecture

- **`app.py`** — Flask app + routes. Holds two small pieces of in-memory,
  thread-shared state guarded by `_state_lock`: `_last_event` (so the kiosk
  can poll `/api/state` and detect a new event by comparing `event_id`) and
  `_reader_status["reading"]` (so the kiosk can show "reading card..."
  between physical tap and PKCS#11 read completing). `_push_event()` also
  returns a snapshot of what it published, which is what `/api/manual-toggle`
  reports back to its caller. It also holds
  `_kiosk_status["last_poll"]`, stamped only by requests carrying
  `?src=kiosk`, so the health heartbeat can tell a dead kiosk browser from a
  live one — the dashboard polls the same endpoint from other PCs and must
  not be able to mask it. This state is
  intentionally not persisted — only `events`/`members` in SQLite are durable.
  An `@app.errorhandler(Exception)` logs a traceback plus the offending
  method and path for anything that escapes a route, passing `HTTPException`
  straight through so ordinary 404s stay unlogged.
  `db.init_db()`, `_init_cac_monitor()` and `start_health_monitor()` run at *import* time, not under
  `__main__`, so they also run under gunicorn — which is why
  `systemd/labtrack.service` pins `-w 1`. More than one worker would mean
  multiple CAC monitors fighting over the reader and per-worker copies of
  `_last_event`, so the kiosk would miss toasts depending on which worker
  answered the poll. Keep it single-worker.
- **`/api/manual-toggle`** — toggles a member without a card, for the kiosk's
  click-a-name flow, the network fallback when the reader is down, and dev
  machines with no reader at all. Everything it writes is flagged `manual`,
  because none of it saw a card. An unknown `member_id` is a 400, not a 500:
  a kiosk page left open across a roster change is holding stale ids, which
  is not a server fault.
- **`cac_reader.py`** — background thread (pyscard's `CardMonitor`) that
  watches the physical reader and identifies the card via PKCS#11, without
  requiring a PIN (reading the PIV Authentication cert, a public object, is
  allowed without one). Only `start_cac_monitor()` requires `pyscard`, so
  the rest of the module (EDIPI extraction logic) can be imported and even
  unit-exercised without hardware. Fires three callbacks into `app.py`:
  `on_card_detected` (immediate, before the ~1-3s read), `on_tap(edipi)`
  (successful read+identify), `on_unrecognized(reason)` where reason is one
  of `unreadable`/`no_edipi`/`error`, mapped to display text by
  `UNRECOGNIZED_MESSAGES` in `app.py`. Debounces so one physical tap fires
  exactly one event.
  - `PKCS11_MODULE_PATH` is hardware/distro-specific and the constant most
    likely to need editing per-Pi (see README "Step 5").
  - `_extract_edipi_from_cert` tries the Subject CN's trailing 10 digits
    first (CACs format it `LAST.FIRST.MIDDLE.0123456789`), falling back to
    the UPN SAN otherName. Deliberately does *not* use the DoD Person
    Identifier SAN OID (2.16.840.1.101.3.6.6) — that field holds a packed
    binary FASC-N, not text, on real-world CACs.
  - `start_reader_watch()` is a second, independent thread that answers a
    question the tap path never can: *is a reader even plugged in?* No taps
    and an unplugged reader look identical from here, so it polls
    `smartcard.System.readers()` every `READER_POLL_S` (10s) and caches
    `{status, detail}` for `get_reader_presence()` — `ok`, `down` (no reader
    *or* pcscd unreachable; both mean nobody can check in), or `unknown`.
    Polling on a timer rather than per request is deliberate twice over:
    `/api/state` is hit several times a second, and a wedged pcscd stalls
    this thread instead of a Flask request thread. A sample older than
    `READER_STALE_S` reports `unknown` rather than its last value, so a
    checker that died can't leave a green dot up forever. Only *transitions*
    are logged (INFO on connect, WARNING on loss) — a line per sample would
    be ~9k a day. It's started even when `_init_cac_monitor()` failed, which
    is exactly when the board needs to say so.
  - `_read_piv_auth_cert_der` retries for ~3s waiting for a PKCS#11 token:
    PC/SC reports a card present as soon as it's electrically detected,
    before OpenSC has finished exposing it as a token.
- **`health.py`** — daemon thread started at import time from `app.py`,
  logging one `health ...` line a minute to the `labtrack.health` logger.
  Pure `/proc`, `/sys` and `vcgencmd` reads, no dependencies. It exists for
  post-mortems on multi-week runs: the failure modes that matter there
  (Chromium leaking until the OOM killer fires, a marginal PSU browning the
  Pi out) leave nothing in the app's own logs, so the trend line *is* the
  evidence. `vcgencmd` is located by absolute path rather than by name: the
  unit file sets `PATH`, and a venv-only `PATH` leaves a bare `vcgencmd`
  unfindable from the service while still working in an interactive shell —
  which is exactly how `throttled=?` happened once. Every probe is
  individually guarded and yields `?` on failure, with a one-per-boot
  WARNING naming which probe failed and why (a silent `?` reads like a
  reading rather than a missing measurement),
  and the whole loop body is wrapped — a monitor that dies quietly partway
  through a soak test is worse than no monitor at all. The line crosses to
  WARNING when memory, disk, temperature, `vcgencmd get_throttled` or kiosk
  silence look wrong, so a week-long run can be reviewed with `journalctl -p
  warning`. Also served on demand at `/api/health`, and summarised across a
  whole run by `scripts/soak-report.sh`.
- **`identity.py`** — the only module that knows how an EDIPI becomes a
  hash. Nothing stores the number: `config/members.json` and `members.edipi_hash`
  hold `hash_edipi(edipi)`, and `_handle_tap()` identifies a card by hashing
  what it read and matching that. `scrypt`, not SHA-256 or HMAC — the EDIPI
  keyspace is 10^10, small enough that a plain hash falls to a GPU in minutes
  and an HMAC does too the moment `config/roster.key` leaks alongside the
  roster; scrypt's work factor keeps a full sweep at decades of CPU time even
  with the key in hand. `maxmem` is passed explicitly because `n=2**14, r=8`
  sits close enough to OpenSSL's 32MB default ceiling to raise instead of
  hashing. Cost is ~100-200ms per tap on the Pi, paid on the CAC reader
  thread — never a Flask request thread — inside the 1-3s the PKCS#11 read
  already takes. Don't move hashing into a request path or a poll handler.
  - `config/roster.key` is the salt, generated on first use with mode 0600
    and gitignored. It is unrecoverable: hashes made with one key mean
    nothing to another, so losing it means re-adding every member. Because
    a *generated* key is normal on a first run and a catastrophe on an
    existing one, `_warn_if_roster_key_lost()` logs an ERROR when it sees a
    fresh key alongside hashes it can't have made — the failure is otherwise
    silent, every card simply stopping working at once. It checks two
    places, because they catch different halves of the same mistake:
    already-hashed rows in `members` (the key was lost on a running
    install), and already-hashed entries in `config/members.json` (a fresh
    clone, where there are no rows to compare against yet and the roster
    arrived from git carrying another machine's hashes). The key is
    gitignored, so a clone never brings it along — that second case is the
    normal way to get this wrong.
- **`database.py`** — all SQLite access goes through `get_conn()`, which
  keeps one connection per thread (`threading.local`) since sqlite3
  connections aren't safe to share across threads; this matters because the
  CAC reader thread and Flask request threads both hit the DB. Schema is two
  tables: `members` (synced from `config/members.json` on every startup via
  `sync_members_from_config()` — upserts by `edipi_hash`, and sets `active = 0`
  for anyone no longer listed, which is what takes them off the kiosk and
  dashboard. It never DELETEs: `events.member_id` is a foreign key into
  this table, so dropping the row would either fail or orphan the attendance
  history the table exists to keep. Re-adding an `edipi_hash` flips `active` back
  to 1 on the same row rather than creating a second one, so the person's
  history reconnects. A config that parses but lists no members is treated
  as a bad edit and deactivates nobody — otherwise one stray comma blanks
  the whole board until someone notices and restarts) and
  `events` (append-only check-in/out log; `action` is `'in'`/`'out'`,
  current status for a member = the most recent event, `note` is the
  optional checkout comment, `manual` is 1 when no card was read - see
  "Checking in without a card" above. `get_roster_status()` surfaces
  `manual` for whichever event set the member's current status, in both
  directions, unlike `note`; an unverified check-*in* is the half worth
  flagging). `get_weekly_hours()` computes hours by pairing
  consecutive in/out events over the last 7 days, counting an unmatched
  trailing `in` up to now.
  - Backfilling attendance by hand is `scripts/add-event.py` (name, `in`/`out`,
    timestamp), for a reader outage or a missed tap. `/api/manual-toggle` can't
    do it: it stamps `datetime.now()` and flips whatever the current status is.
    Because the pairing above is positional, an event inserted into the middle
    of an existing sequence can silently unpair it, so the script previews the
    insert against its neighbours and warns about a repeated action, an
    unmatched trailing `in`, or a note on an `in` (never displayed) before
    committing.
  - **Schema changes need a hand-written migration.** `init_db()`'s
    `CREATE TABLE IF NOT EXISTS` is a no-op on existing installs, so adding
    a column means a `_migrate_*` helper that checks
    `PRAGMA table_info` and `ALTER TABLE`s if missing — see
    `_migrate_add_note_column()` and `_migrate_add_manual_column()` for the
    pattern, and
    `_migrate_hash_edipi_column()` for one that rewrites data as well as
    shape (it renames `edipi` → `edipi_hash` and rehashes in place — in
    place specifically so `members.id`, and therefore every `events` row
    hanging off it, survives). It must be idempotent; it runs on every
    startup.
- **Kiosk timestamps** — `fmtTime()` in `main.js` prints just the time for
  something that happened today, and prepends the date when it didn't, so
  yesterday's 5:00 PM checkout can't be misread as this evening's by someone
  walking in the next morning. Two things it depends on. The day is compared
  with **local** date parts, never an ISO/UTC slice — after ~5pm Mountain the
  UTC date is already tomorrow, which would stamp a date on every evening
  checkout. And `renderRoster`'s change-detection key includes today's date,
  because the board can sit for days with no roster change and would
  otherwise carry yesterday's date-less rendering straight through midnight,
  failing in exactly the case the date exists for. That dated form is also
  what sizes `.roster__status` — it is the longest string the line ever
  carries, and 1.1rem is the largest that fits it on a six-card strip.
  `dashboard.js`'s `fmtDateTime()` is separate and always shows the date:
  it's a log read from a foot away, not a glance from across the room.
- **Reader indicator** — the dot and label beside the kiosk clock
  (`.board__reader`, `renderReader()` in `main.js`), fed by the `reader`
  field on `/api/state`. Green/quiet when a reader is attached, red "Reader
  offline" when not, amber "Reader unknown" when nothing is checking (no
  pyscard, or stale samples — see `start_reader_watch()`). Deliberately
  never animated: it is on screen permanently, so a blinking dot would cost
  the Pi a repaint forever. The page only paints what the server sampled —
  don't move the `readers()` call into the request path.
- **All times are 24-hour**, on both pages, via `hourCycle: "h23"` in the
  shared `TIME_OPTS` at the top of each of `main.js` and `dashboard.js`. Use
  those options for any new timestamp rather than a bare `toLocaleTimeString()`,
  which reintroduces AM/PM. `h23` is stated outright rather than relying on
  `hour12: false`, whose mapping to h23 vs h24 (00:00 vs 24:00 at midnight)
  has varied by locale and ICU version. The locale itself stays the
  browser's. The kiosk's top-right clock carries the weekday and date beside
  the running time; it is still one string compared once a second, so the
  date costs nothing on top of the clock that was already there.
- **Checkout notes** — an optional "why are you out" comment, threaded
  through several layers: `toggle_checkin()` returns `checkin_event_id`
  (the new row's id) → `_push_event()` puts it on `_last_event` → the kiosk
  sees it in `/api/state` and, only for `action === "out"`, shows a text
  input instead of auto-hiding the toast (15s timeout, arrow keys move
  between input/Skip/Save for the no-touchscreen kiosk) → `POST
  /api/events/<id>/note`. `set_event_note()` only updates rows where
  `action = 'out'`, so a stale/late request can't graft a note onto an
  unrelated check-in. `get_roster_status()` surfaces `note` only while the
  member is currently out — it's tied to that checkout, not a profile field.
- **`config/members.json`** — hashed EDIPI → display name roster. Written by
  `scripts/add-member.py` (prompts for the EDIPI with `getpass`, so it stays
  out of the file, the terminal and shell history); removing someone is still
  a hand-edit. Re-synced into the DB on every app startup (restart required to
  pick up changes). An entry carrying a plaintext `edipi` instead is still
  accepted and hashed on the fly, with a WARNING naming the person — a
  half-finished hand-edit shouldn't silently drop somebody off the board.
  - **A member with no EDIPI yet is a real member row carrying a placeholder
    hash.** `identity.pending_hash(name)` returns `pending-<slug>`, written by
    `add-member.py --pending` and also produced by `_entry_hash()` for an
    entry hand-added with neither field — that path runs from `init_db()` at
    import time, so raising there would take the whole board down over a typo.
    A pending member shows on the kiosk and dashboard and checks in through
    the click-a-name flow like anyone else; no card can ever select the row,
    because a real hash is 32 hex characters and the placeholder isn't one.
    `add-member.py --replace` fills the EDIPI in later and rewrites
    `members.edipi_hash` **in place**, for the same reason
    `_migrate_hash_edipi_column()` does: `sync_members_from_config()` upserts
    on `edipi_hash`, so editing the roster file alone reads as one member
    leaving and another joining, and leaves the deactivated pending row
    holding every event logged against it. It updates the DB *before* the
    JSON, so a refusal leaves the roster unconverted rather than
    half-converted, and prints the equivalent `UPDATE` to run by hand when
    there's no local DB — the usual case, since hashes are made wherever
    `roster.key` lives and that isn't the Pi. Placeholders are excluded from
    `_warn_if_roster_key_lost()`'s counts: no key made them, so they say
    nothing about whether the current one is right.
- **`config/objectives.json`** — kiosk screensaver text. Each objective
  becomes one full-panel slide in the media rotation (see below). Re-read by
  the frontend every 60s with no restart needed (`/api/objectives`); a change
  restarts the rotation from the first slide.
- **Frontend** (`templates/` + `static/js/`) — no build step, no framework;
  plain JS polling JSON endpoints. Every render function is guarded by a
  change check (see Performance above; `renderRoster`'s key folds in today's
  date, because what a timestamp prints depends on it — see Kiosk timestamps
  below) — the polls are frequent, the data
  almost never changes. `main.js` drives the kiosk
  (`templates/index.html`): polls `/api/state` every `POLL_MS` (1.5s) to show
  toast confirmations and the "reading card..." overlay, drives the slide
  rotation, and reloads objectives every 60s. `lastEventId` starts as `null`, not `0`, deliberately — the first poll
  only establishes a baseline so a stale event doesn't pop a toast on page
  load, and `0` would make "no baseline" indistinguishable from a real
  first event. `dashboard.js` drives `templates/dashboard.html`: polls
  `/api/state` + `/api/weekly-hours` + `/api/events` every 5s.
  - **Nothing reads the kiosk's browser console** — the Pi boots straight
    into Chromium and runs unattended — so `main.js` posts anything worth
    knowing to `/api/client-log` through `report(key, detail)`, which also
    backs `window.onerror` and `unhandledrejection`. New kiosk failure paths
    should call it rather than `console.error` alone. It throttles per key
    (first occurrence immediately, then at most one per 5 minutes carrying a
    count of what was suppressed) because everything it reports sits on a
    1.5s or 5s timer — unthrottled, a single dead backend is ~57k identical
    lines a day and buries whatever you were looking for. `app.py`
    rate-limits again on its side as a backstop against a runaway client.
    `dashboard.js` deliberately does *not* use this: it runs on a PC with a
    human in front of it who can open devtools.
- **Pi deployment** (`scripts/setup.sh`, `systemd/`, `autostart/`) — installs
  system packages, a systemd service (gunicorn, assumes path
  `/home/admin/labtrack`), a timer that reboots the Pi nightly at 00:00
  (`labtrack-reboot.timer`; `Persistent=false` so a Pi that was off overnight
  doesn't reboot itself on the next power-up, and the service calls
  `systemctl --no-block reboot` because a blocking call would wait on a job
  that has to stop the caller first), a labwc (Wayland) autostart entry for
  kiosk-mode Chromium, a polkit rule so `pcscd` authorizes the service user (no
  interactive login session), and a Chromium flag drop-in to skip the
  login-keyring prompt. See README.md for the full hardware bring-up
  walkthrough and troubleshooting (polkit denial, labwc autostart quirks,
  Chromium binary naming, NetworkManager/keyring interaction) — these are
  Pi-specific footguns already solved there; consult it before re-deriving
  from scratch.

## Security note

CAC identification here reads the PIV Authentication certificate without a
PIN — sufficient to identify who tapped (EDIPI is embedded in the cert) but
not a cryptographic proof of physical key possession the way a PIN-backed
challenge would be. This is an accepted tradeoff for a small lab attendance
log, not an oversight — don't "fix" it by adding PIN prompts unless asked.

The roster stores hashed EDIPIs (see `identity.py` above), so the ID numbers
exist only on the cards themselves — not in the config file, the database, or
the journal (`_handle_tap()` logs an 8-character hash prefix for an unknown
card, enough to correlate repeat taps, and never the number). Display names
are not protected and are still plainly in the repo. This repo is public, and
the plaintext EDIPIs it used to carry were purged from history with
`git filter-repo`; don't reintroduce a real one, in a config file, a test, or
a comment example — `cac_reader.py`'s UPN samples are deliberately fake.
