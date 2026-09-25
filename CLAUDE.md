# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Flask app for a Raspberry Pi that shows who's in the lab: each member's
current status (in the lab / away on campus / out) lives in SQLite, people
change their own from a **keyboard** in front of an always-on kiosk display
(status board / screensaver + toast confirmation), and a read-only dashboard
is viewable from other PCs on the network behind a shared password (see
`webauth.py`).

There is **no card reader and no identity check**. It used to identify
people by tapping a DoD CAC; nobody used it, and the lab doesn't want to
enforce it, so the CAC stack (`cac_reader.py`, EDIPI hashing in
`identity.py`, `pcscd`/`opensc`, the tap question, the "NO CARD" mark) was
removed outright. Anyone at the kiosk can set anyone's status, the same as
a whiteboard - that is the intended trust model, not a gap to close.

### No time tracking

This is a "who's here right now" board, **not a timesheet**, and that is a
deliberate product decision by the lab, not a missing feature. It used to
keep a timestamped event log with weekly hours, an activity log and an
admin panel for editing it; all of that was removed. Don't reintroduce any
of it:

- **The database holds current status only** - one `presence` row per
  member (`status`, `note`, `change_id`) that is overwritten, never
  appended to. No per-person row carries a time. `change_id` is a counter:
  it keys the checkout note, and must not become a clock.
  `_migrate_events_to_presence()` carried statuses across from the old
  `events` table, dropped it and VACUUMed so the old timestamps don't
  survive in the freelist.
- **The UI shows no check-in/out times.** Not on roster cards, not on the
  toast, not on the dashboard. The kiosk's header clock and the dashboard's
  "Updated" stamp are the only times on either page.
- **Status changes are not logged to the journal.** The journal is
  timestamped and kept a month, so a line per check-in would rebuild the
  timesheet there. Errors are logged as ever - just never "X checked in".
- **Everyone is reset to out once a day** (`reset_if_new_day()`, run at
  startup and once a minute by a thread in `app.py`), because without a
  time on the card a forgotten checkout would otherwise read "In lab"
  forever. It keys on the date of the last reset in the `meta` table - one
  system-wide value, not per-person - so a midday restart or self-reboot
  changes nothing, and a Pi that was off overnight still starts clean. The
  reset also clears yesterday's notes.

## Developing locally (off the Pi)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

Then open `http://localhost:5000` (kiosk display) and
`http://localhost:5000/dashboard` in a browser. The kiosk is driven exactly
as in production: ← → to highlight a name, Enter to open its dialog (or
click the name). To change a status without the page:

```bash
curl -X POST http://localhost:5000/api/set-status \
     -H "Content-Type: application/json" \
     -d '{"member_id": 1}'
```

Each call toggles that member in/out, so run it twice to exercise both the
check-in toast and the checkout note prompt. Add `"action": "away",
"location": "Server room"` (or `"action": "in"`/`"out"`) to record a
specific state rather than a toggle.

There is no test suite or linter configured — verify changes by running the
dev server and exercising the routes/UI directly. For the keyboard flow that
means real key presses in a browser, not just the API: several of its bugs
only exist in how key events bubble between the dialog and the page.

`scripts/setup.sh` is the Pi deployment installer only (installs kiosk
Chromium, ffmpeg, systemd services) — never run it in dev.

### Three states: in, away, out

A member is `in` (in the lab), `away` (at work but not in the lab - the
server room, a lecture hall) or `out` (gone home, at lunch). `away` exists
because "not in the lab" and "not at work" are different facts. Things that
follow from it:

- **Leaving asks where to.** The dialog for someone who is `in` offers
  Check out *and* a row of "still at work, elsewhere" places, so one
  choice records either fact.
- **Where they went is the status's `note`.** Same column as a checkout
  comment: both are the one line of free text a status carries, and
  `get_roster_status()` surfaces `note` for whichever of `out`/`away` is
  current (never for `in`). On the kiosk it is the roster card's third
  line - the card's height is pinned to three lines (below). Presets come
  from `config/locations.json` (`/api/locations`, re-read every 60s
  alongside the objectives, rendered as buttons only when the dialog
  opens); "Other…" reveals a text box, and an empty location is allowed.
- **Repeats.** `away` while already `away` is allowed (a change of
  location); `in` while `in` or `out` while `out` is a `ValueError` in
  `set_status()` and a 409 from the API - it says nothing new, and means
  the caller's view of the board is stale.
- **The colour is its own** (`--away`, blue) with a hollow ring on the
  roster card, so "away" never reads as "in" from across the room.

### Checking in: the keyboard

A USB keyboard in front of the board is the front door; a mouse works too
but is optional. The header says how (`#keys-hint` in `index.html`: "← →
choose your name · Enter check in / out"), and the dialog and note prompt
carry their own key hints.

- **On the board**, ← / → (and ↑ / ↓, Home, End) move a highlight along the
  roster strip; Enter opens the dialog for the highlighted person (with
  nothing highlighted, Enter just starts the highlight - opening a dialog
  for whoever happens to be first would be a guess); Escape drops the
  highlight. This lives in one `keydown` listener on `document` ("keyboard"
  in `main.js`), because Chromium in kiosk mode puts focus on the page, not
  on any control.
- **The highlight is a class, not DOM focus** (`.is-highlighted`, tracked
  as `highlightedId`). `renderRoster()` rebuilds the strip with innerHTML,
  which would drop focus; a class is simply re-applied by `paintHighlight()`
  after each render. It is an inset `outline` plus a flat background -
  neither changes the card's box (the strip height is pinned) and neither
  rasterizes like a `box-shadow` would. It clears itself after
  `HIGHLIGHT_IDLE_MS` (30s) so a ring isn't left on one name all day.
  Cards are `tabindex="-1"`: the arrows are the one way along the strip.
- **The roster is alphabetical and stable** (`get_roster_status()`), so
  people learn where their name sits. It used to reorder most-recent-first,
  which would drag the highlight around as statuses changed; don't bring
  that back.
- **In the dialog**, the likeliest action is focused as it opens (out →
  Check in, in → Check out, away → Back in lab), so the whole common flow
  is Enter, Enter. Every arrow key steps through the visible buttons
  (the location row wraps, so up/down aren't spatial); Escape cancels, or
  from the "Other" box backs out to the buttons first. `.confirm__btn:focus`
  is plain `:focus`, not `:focus-visible`, because Enter fires whatever is
  focused and the ring must show even after a mouse opened the dialog.
- **Overlays own their keys: `stopPropagation()` in the dialog's and the
  note prompt's keydown handlers is load-bearing.** Without it the Escape
  that closes the dialog bubbles on to the document handler, which by then
  sees no dialog and clears the highlight too. The document handler also
  bails while `dialog !== null` or `is-note-prompt` is set.
- **Enter only, never Space, opens the dialog.** A button activates on
  Space's *keyup*; if keydown opened the dialog and focused "Check in", the
  keyup would land on it and confirm before the question was seen. Enter
  activates on keydown, which the document handler `preventDefault()`s.
- **Opening a dialog hides a toast still up from the last person** (a note
  prompt included) - whoever is at the board now outranks it, and the
  dialog sits below the toast in the stack.
- **The note prompt after a checkout** takes focus in its text box; Enter
  moves to Save (or Skip if empty), a second Enter commits, Escape skips.

### The dialog

`openConfirm()` / `submitChoice()` in `main.js`, element `.confirm`. It
posts to `/api/set-status` with an explicit `action` (and `location` for
away). Buttons depend on the member's current status: out → Check in; in →
Check out plus the away presets; away → Back in lab / Check out. Cancel is
always there.

- **It always confirms first.** The strip sits in the open all day; without
  the dialog one stray keypress or click silently moves somebody in or out.
  The dialog cancels on Escape, a backdrop click and a 20s timeout, so an
  abandoned one can't sit on the board. An event arriving mid-dialog (from
  another machine, say) supersedes it - `showToast()` calls
  `closeConfirm()`.
- **The pointer is hidden by default and revealed by movement.**
  `body.kiosk` is `cursor: none`; a `mousemove` listener adds `has-pointer`
  and drops it again after `POINTER_IDLE_MS` (8s), so the strip is
  clickable without leaving a cursor parked on an unattended board for a
  week. `is-note-prompt` and `is-confirm` force it visible regardless,
  because both put controls on screen and then wait. The listener does one
  class check per event and the pending timeout reschedules itself rather
  than being cleared and reset thousands of times; `cursor` is not a
  rendered property, so neither state costs a paint. A click on a card
  also moves the keyboard highlight there, so the two never disagree.
- **Roster cards are `<button>`s on the kiosk and plain `<div>`s on the
  dashboard.** Only the kiosk's are controls. The four UA-reset properties
  on `.roster__card` (`appearance`, `border`, `font`, `text-align`) are what
  keep the two rendering identically; without them the kiosk strip picks up
  a native border and centred system text. The click handler is delegated
  to `#roster`, since `renderRoster()` replaces the strip wholesale.
- **`poll()` numbers its own requests and drops out-of-order replies.**
  `submitChoice()` fires a poll the instant the POST returns instead of
  waiting out `POLL_MS`, so two are briefly in flight; the older reply
  carries pre-choice state and would repaint a stale roster and re-toast
  the event before it. That is also why the toast is left to the poll
  rather than raised from the POST response - events reach the screen
  through exactly one path however they were caused.

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

The background video is chosen **server-side** by
`BACKGROUND_VIDEO_CANDIDATES` in `app.py` — the first filename in that tuple
that exists in `static/media/` wins, and no match means no background.
`kiosk()` renders it into `data-video` on the `<video>` element and `main.js`
reads it from there; `BACKGROUND_VIDEO` in `main.js` is no longer a
hand-edited constant. The indirection exists because the two candidates are
produced differently: `background.mp4` is the short master tracked in git,
and `background-long.mp4` is the ~900MB file `scripts/build-loop.sh`
concatenates from it **on the Pi**, so which one is present differs per
machine and a hardcoded name would be wrong on one of them.

Only `background.mp4` itself is tracked in git — `.gitignore` excludes every
other video in that directory (the generated long file included), because
encode experiments and multi-MB sources are permanent once committed (the
repo's history had to be rewritten once already to get 131MB of them back
out). Keep sources elsewhere, or let the ignore rule do its job. Things
worth knowing before changing the video:

- **The loop is long on purpose, and rebuilding it after any change to
  `background.mp4` is not optional.** Every wrap-around seek is a chance to
  hit a `bcm2835_codec` `stop_streaming` race that oopses the kernel and
  freezes the entire display — not just the video (see "The stop_streaming
  freeze" below). Observed at roughly one failure per 2,000 seeks, which on
  the 12s master is about once every seven hours. `scripts/build-loop.sh`
  stream-copies the master into a 10-minute loop, so the seek happens 50x
  less often and the expected interval goes to about two weeks. It cuts the
  master **by frame count, never by `-t`**: a stream copy keeps whole
  packets and `-t 12` hands back 362 frames rather than 360, which would
  replay two frames at every one of the 50 joins. Verified seamless — the
  join measures 26.8dB against 27.1-27.3dB for an ordinary frame step, the
  same figure as the master's own loop point.

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

The toast, the check-in dialog and the reboot notice are full-screen
`position: fixed` layers. When a background video is configured they go
translucent so it stays visible through the whole check-in flow; with no video they stay fully opaque, which lets the compositor skip
painting the board underneath entirely. That's why every rule is gated on
`body.has-bg` rather than applied unconditionally — see Performance below.

Three pieces of that treatment have to move together, and missing any one
of them looks broken rather than subtly wrong:

- **`body.is-overlay`** is toggled by `syncOverlayState()`, which *derives*
  the flag from whether any of them currently has `is-visible`. It is
  deliberately not one toggler per overlay: they overlap when a choice
  completes and the toast replaces the dialog, and independent toggles race
  there. Any new code path that shows
  or hides one must go through `setToastVisible()` / `closeConfirm()` /
  `renderReboot()` and then `syncOverlayState()`, not `classList` directly.
  `OVERLAY_IDS` is the single list the flag is derived from — a new overlay
  goes in there, not into another `||`. They stack confirm (58) → toast
  (60) → reboot (70): the toast is what the dialog turns into, and the
  screen going away shortly supersedes everything.
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
- **Animating anything but `opacity`/`transform`** — avoid animating a `transform` on top of a
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

- **The wrap-around seek is itself dangerous, and the loop length is the
  mitigation.** `LOOP_TAIL_S` keeps playback away from end-of-stream, but
  `currentTime = 0` makes Chromium issue `VIDIOC_STREAMOFF` on the V4L2 m2m
  decoder, and `bcm2835_codec` has a race in `stop_streaming` that leaves
  buffers active. vb2 warns (`driver bug: stop_streaming operation is leaving
  buffer N in active state`), then a kernel workqueue thread NULL-derefs
  freeing the dma-buf (`dma_release_from_dev_coherent` ← `vb2_dc_put` ←
  `dma_buf_release` ← `delayed_fput`) and dies holding locks nothing will
  release. Tasks then block in uninterruptible sleep one at a time until the
  compositor is among them and the **whole display** freezes — measured at
  ~45 minutes after the oops. Confirmed on real hardware, kernel
  `6.18.39+rpt-rpi-v8`. Nothing in userspace can undo it; only a reboot can.
  Three things follow, and all three are load-bearing:
  - the loop is 10 minutes rather than 12 seconds
    (`scripts/build-loop.sh`), because the hazard is per-seek — roughly one
    failure per 2,000 of them;
  - a `video-stall` report reboots the Pi (`request_reboot()` in `app.py`);
  - `health.py` reports `dstate=` so the wedge is visible before the freeze.

  **The signature to recognise, because every other probe reads normal:**
  load average pinned at exactly the core count while the CPU runs *cold*.
  Linux counts D-state tasks in load average, so `load1=4.00` at 37°C on a Pi
  4 means nothing is running and four things are stuck — four busy cores
  would be 65-80°C. Throughout the incident `/api/health` reported
  `"ok": true`, gunicorn had zero restarts, and the kiosk page kept polling
  (`kiosk_idle=1s`) with a dead screen, because only the display was gone.

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

- **`app.py`** — Flask app + routes. Holds a small piece of in-memory,
  thread-shared state guarded by `_state_lock`: `_last_event` (so the kiosk
  can poll `/api/state` and detect a new event by comparing `event_id`).
  `_push_event()` also returns a snapshot of what it published, which is
  what `/api/set-status` reports back to its caller; it carries `previous`
  (the status the event replaced, so the toast can say "Back in lab") and
  `location`. It also holds `_kiosk_status["last_poll"]`, stamped only by
  requests carrying `?src=kiosk`, so the health heartbeat can tell a dead
  kiosk browser from a live one — the dashboard polls the same endpoint from
  other PCs and must not be able to mask it. And `_reboot_state` (under its
  own `_reboot_lock`) holds a pending self-reboot, surfaced on `/api/state`
  as `reboot` so the kiosk can count down on screen. This state is
  intentionally not persisted — only `members`/`presence`/`meta` in SQLite
  are durable. An `@app.errorhandler(Exception)` logs a traceback plus the
  offending method and path for anything that escapes a route, passing
  `HTTPException` straight through so ordinary 404s stay unlogged.
  `db.init_db()`, `_start_daily_reset()` and `start_health_monitor()` run at
  *import* time, not under `__main__`, so they also run under gunicorn —
  which is why `systemd/labtrack.service` pins `-w 1`. More than one worker
  would mean per-worker copies of `_last_event` (the kiosk would miss toasts
  depending on which worker answered the poll) and of each background
  thread. Keep it single-worker.
- **`/api/set-status`** — sets a member's status: the kiosk dialog, curl
  from another machine, and the dev loop. `{member_id}` alone toggles (in →
  out, out or away → in); `action` (`in`/`away`/`out`) plus `location` says
  exactly what to write. An unknown `member_id` is a 400, not a 500: a kiosk
  page left open across a roster change is holding stale ids, which is not
  a server fault. An action that changes nothing is a 409 for the same
  reason - the page is behind, and its next poll fixes that.
- **`/api/locations`** — the preset away places from `config/locations.json`,
  `[]` when the file is missing.
- **`request_reboot()`** — the board's only self-recovery path, for the one
  failure it cannot otherwise survive: the `stop_streaming` freeze
  (Performance above) kills the display while gunicorn keeps serving and the
  page keeps polling, so nothing errors and nothing here can undo it. Called
  from `/api/client-log` when the key is in `REBOOT_ON_CLIENT_KEYS`
  (`video-stall`), it warns on screen for `REBOOT_WARNING_S` (30s) and then
  runs `systemctl --no-block reboot` — `--no-block` for the same reason
  `labtrack-reboot.service` uses it, since the reboot job has to stop this
  service first. Three things hold it together:
  - **`REBOOT_MIN_UPTIME_S` (30 min) is what stops a reboot loop.** A fault
    that reasserts every boot would otherwise cycle the board forever, and a
    kiosk that never finishes booting is far worse than one showing a flat
    background — the freeze still lets people check in, a loop does not. The
    stall watchdog needs ~20s to fire, so anything inside this window is a
    fault that survived the last reboot and will survive the next.
  - **The trigger set is explicit, not "any client error".**
    `/api/client-log` is reachable by anything on the lab network holding the
    dashboard password, so the set of keys that can reboot the Pi is a
    deliberate allowlist and the rate limit already bounds it.
  - **It needs `systemd/40-labtrack-reboot.rules`.** The service runs as
    `admin`, not root, and logind refuses without the polkit rule. A failure
    un-schedules itself and logs what is missing, rather than leaving the
    board under a countdown for a reboot that never comes.
- **The dashboard is read-only.** It shows the roster and nothing else -
  no hours, no activity log, and no admin panel (there is nothing
  historical left to edit). Its cards are the same markup as the kiosk's,
  as `<div>`s rather than buttons.
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
  WARNING when memory, disk, temperature, `vcgencmd get_throttled`, kiosk
  silence or stuck processes look wrong, so a week-long run can be reviewed
  with `journalctl -p warning`.
  - `dstate=` counts processes in uninterruptible sleep, and sits next to
    `load1=` deliberately: together they separate a Pi that is *busy* from a
    Pi that is *stuck*, because load average counts D-state tasks too. It
    exists because of the `stop_streaming` freeze (Performance above), where
    every other probe in this module read perfectly normal for the five hours
    the display was dead. A pid is only *named* once it has been in D for
    `DSTATE_STUCK_S` (5 min) — ordinary disk I/O passes through D constantly,
    so a single reading means nothing, while the same pid minutes later means
    it is behind a lock that is never coming back. Measured in wall time
    rather than sample count because `sample()` is also called on demand by
    `/api/health`, and counting calls would let an extra poll push a
    transient over the line. Also served on demand at `/api/health`, and summarised across a
  whole run by `scripts/soak-report.sh`.
- **`webauth.py`** — the shared password guarding everything that isn't the
  kiosk. The service binds `0.0.0.0` so the dashboard is readable from other
  PCs, which also exposes `/api/set-status` to the lab network, so a
  single `@app.before_request` hook in `app.py` demands HTTP Basic auth.
  Three things hold it together:
  - **Requests from the Pi are exempt**, because the kiosk Chromium loads
    `http://localhost:5000` and cannot answer a password prompt. Nothing
    off-box can claim that: the kernel drops a packet arriving on a real
    interface with a loopback source. That reasoning depends on gunicorn
    holding the listening socket itself — **putting nginx or Caddy in front
    would make every request look local and silently disable the password
    for the whole network.** It fails open, so it needs saying twice.
  - **The password is stored in the clear and must not be hashed.** A slow
    hash like scrypt is 100-200ms on a Pi and the password is checked on
    every request (`dashboard.js` polls every 5s), so each open dashboard
    would spend a real share of every second hashing on a Flask request
    thread — the case Performance rules out outright.
    `secrets.compare_digest` is microseconds and still constant-time, and
    with no TLS on the hop the wire is the weak link, not a 0600 file.
  - **An empty key file denies everything** rather than accepting anything.
    `compare_digest("", "")` is True, so a truncated or unreadable file would
    otherwise let the whole network in with no password — the one failure
    here that looks exactly like the feature working.

  `config/dashboard.key` is generated with a random value on first start
  (0600, gitignored, per-Pi) and cached at import, so changing it is a
  restart. The generated value is deliberately *not* logged — it would put
  a live credential in the journal. What this buys is a lock against people
  who wander onto the lab network; it is not confidentiality, since plain
  HTTP carries the password and the page in the clear. See README step 9 for
  the stronger options.
- **`database.py`** — all SQLite access goes through `get_conn()`, which
  keeps one connection per thread (`threading.local`) since sqlite3
  connections aren't safe to share across threads; this matters because the
  daily-reset thread and Flask request threads both hit the DB. Schema is
  three tables. `members` is keyed on `display_name` and synced from
  `config/members.json` on every startup via `sync_members_from_config()` —
  upserts by name, and sets `active = 0` for anyone no longer listed, which
  is what takes them off the kiosk and dashboard. It never DELETEs:
  `presence.member_id` is a foreign key into this table. Re-adding a name
  flips `active` back to 1 on the same row rather than creating a second
  one; renaming someone makes a new member, starting out as `out`. A config
  that parses but lists no members is treated as a bad edit and deactivates
  nobody — otherwise one stray comma blanks the whole board until someone
  notices and restarts. `presence` is one row per member, overwritten by
  every path that changes a status - all of them through `set_status()`,
  which `toggle_status()` wraps: `status` is one of `ACTIONS` =
  `'in'`/`'away'`/`'out'` (no row reads as `out`), `note` is the optional
  checkout comment or the away location, and `change_id` is a global
  counter bumped on each change (see "No time tracking").
  `get_roster_status()` returns active members alphabetically (see "Checking
  in: the keyboard") and surfaces `note` only while out or away. `meta` is
  system key/values - just `last_reset` for `reset_if_new_day()`.
  - **Schema changes need a hand-written migration.** `init_db()`'s
    `CREATE TABLE IF NOT EXISTS` is a no-op on existing installs, so adding
    a column means a `_migrate_*` helper that checks `PRAGMA table_info` and
    `ALTER TABLE`s if missing (`_migrate_drop_manual_column()` is the small
    case). See `_migrate_events_to_presence()` for one that replaces a table
    outright (it carries each member's last status across, then drops
    `events` and VACUUMs), and `_migrate_members_by_name()` for a rebuild
    that SQLite can't `ALTER` (a new `UNIQUE` key): it copies rows into a
    new table **with ids intact**, so `presence` rows stay attached, merges
    old rows that shared a name (the active one wins), switches
    `foreign_keys` off for the swap so dropping the old table can't
    cascade, and VACUUMs so the CAC-era EDIPI hashes leave the file. It
    must be idempotent; it runs on every startup.
- **Kiosk header logo** — `static/img/fair-logo-dark.png`, the reversed
  (dark-background) FAIR logo, shown top left in place of the old "FALCON
  AI RESEARCH LAB" text (the dashboard still uses the text). It is
  *generated* from the supplied `static/img/fair-logo.png`, which is navy
  and royal blue on a solid white box: each pixel was un-mixed from the
  white background to recover its alpha, navy turned white and royal blue
  lightened to `#5c8ce6`, then cropped to the artwork and scaled to 240px
  tall - 2x the 120px it is shown at. If the lab supplies an official
  reversed logo, swap that in instead. Displayed size is fixed by
  `width`/`height` on the `<img>`, so the header doesn't reflow when it
  loads.
- **The clocks are 24-hour** — the kiosk header clock and the dashboard's
  "Updated" stamp, the only times either page shows (see "No time
  tracking") — via `hourCycle: "h23"` in the shared `TIME_OPTS` at the top
  of each of `main.js` and `dashboard.js`. A bare `toLocaleTimeString()`
  reintroduces AM/PM. `h23` is stated outright rather than relying on
  `hour12: false`, whose mapping to h23 vs h24 (00:00 vs 24:00 at midnight)
  has varied by locale and ICU version. The locale itself stays the
  browser's. The kiosk's top-right clock carries the weekday and date beside
  the running time; it is still one string compared once a second, so the
  date costs nothing on top of the clock that was already there.
- **Checkout notes** — an optional "why are you out" comment, threaded
  through several layers: `set_status()` returns `change_id` →
  `_push_event()` puts it on `_last_event` → the kiosk sees it in
  `/api/state` and, only for `action === "out"`, shows a text input instead
  of auto-hiding the toast (15s timeout; Enter/arrows/Escape drive it from
  the keyboard) → `POST
  /api/presence/<change_id>/note`. `set_note()` only updates the row whose
  `change_id` still matches and whose status is still `out`, so a
  stale/late request can't graft a note onto a later check-in or away.
  `get_roster_status()` surfaces `note` only while the member is out or
  away — it's tied to that status, not a profile field.
- **`config/members.json`** — the roster: `{"members": ["Name", ...]}`, a
  plain list of display names, hand-edited. Re-synced into the DB on every
  app startup (restart required to pick up changes). Objects of the old
  CAC-era shape (`{"display_name", "edipi_hash"}`) are still accepted, with
  the hash ignored, so an un-updated file keeps loading; blank and repeated
  names are skipped rather than raised, because this runs from `init_db()`
  at import time and a typo must not take the board down.
- **`config/locations.json`** — preset places for the away state, shown as
  buttons on the kiosk's leaving dialog. Re-read every 60s with the
  objectives; "Other…" always exists as well, so the file only has to list
  the common ones.
- **`config/objectives.json`** — kiosk screensaver text. Each objective
  becomes one full-panel slide in the media rotation (see below). Re-read by
  the frontend every 60s with no restart needed (`/api/objectives`); a change
  restarts the rotation from the first slide.
- **Frontend** (`templates/` + `static/js/`) — no build step, no framework;
  plain JS polling JSON endpoints. Every render function is guarded by a
  change check (see Performance above) — the polls are frequent, the data
  almost never changes. `main.js` drives the kiosk
  (`templates/index.html`): polls `/api/state` every `POLL_MS` (1.5s) to show
  toast confirmations and keep the roster current, runs the keyboard
  check-in flow (above), drives the slide rotation, and reloads objectives and locations every 60s. `lastEventId` starts as `null`, not `0`, deliberately — the first poll
  only establishes a baseline so a stale event doesn't pop a toast on page
  load, and `0` would make "no baseline" indistinguishable from a real
  first event. `dashboard.js` drives `templates/dashboard.html`: polls
  `/api/state` every 5s and renders the roster.
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
  kiosk-mode Chromium, a polkit rule so the service user (which has no
  interactive login session) may reboot the Pi — see `request_reboot()`
  above — and a Chromium flag drop-in to skip the
  login-keyring prompt. It also installs `ffmpeg` and runs
  `scripts/build-loop.sh`, because the long background loop is generated per
  machine rather than tracked in git.
  - **`config/decode-mode`** switches the kiosk between hardware and software
    video decode. One word, read by `autostart/labwc-autostart` at every
    Chromium launch (so a reboot applies it), written by
    `scripts/set-decode.sh`, gitignored because it is a per-Pi
    troubleshooting setting that `git pull` must not fight. Absent means
    hardware. Software decode costs about a core but touches no V4L2 at all,
    which is the point: both video failures this board has had came from that
    stack.  The chosen mode is logged to `labtrack-chromium` on every launch,
    since it is otherwise invisible when reading back a freeze. See README.md for the full hardware bring-up
  walkthrough and troubleshooting (polkit denial, labwc autostart quirks,
  Chromium binary naming, NetworkManager/keyring interaction) — these are
  Pi-specific footguns already solved there; consult it before re-deriving
  from scratch.

## Security note

There is no identity check at all: anyone at the kiosk can set anyone's
status, and anyone on the lab network holding the dashboard password can
do the same through `/api/set-status`. That is the lab's choice - it is a
whiteboard, not an access-control or attendance system - so don't add
PINs, badges or per-person logins unless asked.

Display names are not protected and are plainly in the repo, and the
dashboard password in `config/dashboard.key` protects access, not the
traffic — it crosses the network as plain HTTP (`webauth.py`). This repo is
public. It used to carry DoD ID numbers (EDIPIs) from the CAC era; those
were purged from history with `git filter-repo`, and the database
migration VACUUMs away the hashes that replaced them. Don't reintroduce one,
in a config file, a test, or a comment example.
