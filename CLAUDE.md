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

The media panel cycles one combined playlist built by `buildPlaylist()`:
every objective as a text slide, then every file in `MEDIA_FILES` (a
hardcoded array in `main.js` — there's no directory-listing endpoint, so
filenames must be added there by hand after dropping them in
`static/media/`). Text slides advance on a `SLIDE_MS` timer; videos advance
on the `ended` event.

Three things here are easy to break:

- **Never put the `loop` attribute back on the `<video>` in
  `index.html`.** With it always on, `ended` never fires and the rotation
  can't move past the first video — that was a real bug. `main.js` sets
  `videoEl.loop` per item instead, turning it on only when the playlist has
  exactly one entry (nothing to advance to, so loop natively rather than
  re-fetching the same file each time it ends).
- **The video element's `ended`/`error` events can arrive after the
  rotation has already moved on** (a 404 reports its error a beat late).
  Both handlers check `activeType === "video"` first; without that guard a
  stale error cuts the *next* text slide short.
- **The rotation is started by the first `loadObjectives()`, not at script
  load.** Starting earlier meant a video slot could begin fetching and then
  be abandoned a moment later when the objectives arrived.

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
  fully opaque background instead, which also lets the compositor skip
  painting what's behind them.
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

Two rules that matter for anything new:

- **Poll handlers must not touch the DOM when nothing changed.** Every
  render function compares a JSON snapshot of its data first and bails out
  (`skipIfUnchanged()` in `dashboard.js`, `lastRosterJson` /
  `lastClockText` / `loadObjectives._last` in `main.js`). Without that
  guard, `innerHTML` rebuilds relayout the section 40x a minute forever.
  Any new polled section needs the same guard.
- **Screensaver video must be H.264 (AVC) and no wider than 1920px.**
  Confirmed from `chrome://gpu` on the deployed Pi: the only hardware decode
  profiles are h264 baseline/main/high, 32x32 to 1920x1920. HEVC/VP9/AV1, or
  anything above 1920px, falls back to software decode and will peg the CPU.
  Note the kiosk panel is wider than that (3440x1440), so encode media to
  1080p and let it scale up — do not encode at panel resolution.

**The kiosk display is a 3440x1440 ultrawide** — ~4.9M pixels, about 2.4x a
1080p panel. Every full-screen effect costs proportionally more here, which
is why the full-screen blurred overlays hurt as much as they did. Assume any
effect covering the whole screen is expensive on this hardware.

The GPU path was verified on the deployed Pi (V3D 4.2.14.0, Mesa 26.2.0,
Chrome 151): Rasterization, Compositing, Canvas and Video Decode all report
hardware accelerated. `autostart/labwc-autostart` passes
`--enable-gpu-rasterization`; it deliberately does *not* pass
`--ignore-gpu-blocklist`, since the blocklist was shown not to be vetoing
anything on this hardware. If the board ever looks sluggish again, re-check
`chrome://gpu` on the Pi before changing code.

## Architecture

- **`app.py`** — Flask app + routes. Holds two small pieces of in-memory,
  thread-shared state guarded by `_state_lock`: `_last_event` (so the kiosk
  can poll `/api/state` and detect a new event by comparing `event_id`) and
  `_reader_status["reading"]` (so the kiosk can show "reading card..."
  between physical tap and PKCS#11 read completing). This state is
  intentionally not persisted — only `events`/`members` in SQLite are durable.
  `db.init_db()` and `_init_cac_monitor()` run at *import* time, not under
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
