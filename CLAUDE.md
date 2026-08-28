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
check-in toast and the checkout note prompt.

Then open `http://localhost:5000` (kiosk display) and
`http://localhost:5000/dashboard` in a browser. There is no test suite or
linter configured — verify changes by running the dev server and exercising
the routes/UI directly.

`scripts/setup.sh` is the Pi deployment installer only (installs
`pcscd`/`opensc`, kiosk Chromium, systemd services) — never run it in dev.

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

The toast and the "reading card" overlay are full-screen `position: fixed`
layers. When a background video is configured they go translucent so it
stays visible through the whole tap flow; with no video they stay fully
opaque, which lets the compositor skip painting the board underneath
entirely. That's why every rule is gated on `body.has-bg` rather than
applied unconditionally — see Performance below.

Three pieces have to move together, and missing any one of them looks
broken rather than subtly wrong:

- **`body.is-overlay`** is toggled by `syncOverlayState()`, which *derives*
  the flag from whether either overlay currently has `is-visible`. It is
  deliberately not two independent togglers: the two overlays overlap when
  a tap completes and the toast replaces the reading overlay, and
  independent toggles race there. Any new code path that shows or hides the
  toast must go through `setToastVisible()`, not `classList` directly.
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
  between physical tap and PKCS#11 read completing). It also holds
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
  - `_read_piv_auth_cert_der` retries for ~3s waiting for a PKCS#11 token:
    PC/SC reports a card present as soon as it's electrically detected,
    before OpenSC has finished exposing it as a token.
- **`health.py`** — daemon thread started at import time from `app.py`,
  logging one `health ...` line a minute to the `labtrack.health` logger.
  Pure `/proc`, `/sys` and `vcgencmd` reads, no dependencies. It exists for
  post-mortems on multi-week runs: the failure modes that matter there
  (Chromium leaking until the OOM killer fires, a marginal PSU browning the
  Pi out) leave nothing in the app's own logs, so the trend line *is* the
  evidence. Every probe is individually guarded and yields `?` on failure,
  and the whole loop body is wrapped — a monitor that dies quietly partway
  through a soak test is worse than no monitor at all. The line crosses to
  WARNING when memory, disk, temperature, `vcgencmd get_throttled` or kiosk
  silence look wrong, so a week-long run can be reviewed with `journalctl -p
  warning`. Also served on demand at `/api/health`, and summarised across a
  whole run by `scripts/soak-report.sh`.
- **`database.py`** — all SQLite access goes through `get_conn()`, which
  keeps one connection per thread (`threading.local`) since sqlite3
  connections aren't safe to share across threads; this matters because the
  CAC reader thread and Flask request threads both hit the DB. Schema is two
  tables: `members` (synced from `config/members.json` on every startup via
  `sync_members_from_config()` — upserts by `edipi`, never deletes) and
  `events` (append-only check-in/out log; `action` is `'in'`/`'out'`,
  current status for a member = the most recent event, `note` is the
  optional checkout comment). `get_weekly_hours()` computes hours by pairing
  consecutive in/out events over the last 7 days, counting an unmatched
  trailing `in` up to now.
  - **Schema changes need a hand-written migration.** `init_db()`'s
    `CREATE TABLE IF NOT EXISTS` is a no-op on existing installs, so adding
    a column means a `_migrate_*` helper that checks
    `PRAGMA table_info` and `ALTER TABLE`s if missing — see
    `_migrate_add_note_column()` for the pattern. It must be idempotent;
    it runs on every startup.
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
- **`config/members.json`** — EDIPI → display name roster. Edited by the lab
  admin directly; re-synced into the DB on every app startup (restart
  required to pick up changes).
- **`config/objectives.json`** — kiosk screensaver text. Each objective
  becomes one full-panel slide in the media rotation (see below). Re-read by
  the frontend every 60s with no restart needed (`/api/objectives`); a change
  restarts the rotation from the first slide.
- **Frontend** (`templates/` + `static/js/`) — no build step, no framework;
  plain JS polling JSON endpoints. Every render function is guarded by a
  change check (see Performance above) — the polls are frequent, the data
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
  `/home/admin/labtrack`), a labwc (Wayland) autostart entry for kiosk-mode
  Chromium, a polkit rule so `pcscd` authorizes the service user (no
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
