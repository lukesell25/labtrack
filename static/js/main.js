// Kiosk display logic: roster strip, objectives, media loop, tap-confirmation toast.
// No touchscreen assumed - this page just runs unattended, driven entirely by
// polling the backend for CAC tap events.

const POLL_MS = 1500;
const TOAST_VISIBLE_MS = 4000;
const NOTE_PROMPT_TIMEOUT_MS = 15000;

// null = no baseline established yet (first poll after page load hasn't
// happened). Using null rather than 0 as the sentinel matters: real event
// ids start at 1, so if we used 0 as "no baseline" AND the first real event
// happens to arrive right after, the two are indistinguishable and the very
// first toast silently never shows - which is exactly the bug this fixes.
let lastEventId = null;

// --- error reporting -------------------------------------------------
// Nothing reads this page's console: the Pi boots straight into Chromium
// and runs unattended for weeks. So anything worth knowing gets posted to
// /api/client-log, where it lands in the journal next to the app's own
// lines and the kernel's (see "Watching a long run" in README.md).

const CLIENT_LOG_THROTTLE_MS = 5 * 60 * 1000;
const CLIENT_LOG_MAX_KEYS = 50;

// key -> { last: when we last sent this key, suppressed: how many since }
const reportedErrors = new Map();

// Reports once per key immediately, then at most once per throttle window
// with a count of what was suppressed. The throttling is the load-bearing
// part: a failure that repeats every 1.5s (a dead backend, say) would
// otherwise be ~57,000 identical log lines a day and would bury the one
// event you actually wanted to find.
function report(key, detail) {
  try {
    const now = Date.now();
    const seen = reportedErrors.get(key);
    if (seen && now - seen.last < CLIENT_LOG_THROTTLE_MS) {
      seen.suppressed++;
      return;
    }
    const count = seen ? seen.suppressed + 1 : 1;
    // Bound the map: a failure that puts something variable in the key
    // (a URL, a timestamp) would otherwise grow it without limit over a
    // multi-week run. Dropping the counts is fine, this is a safety valve.
    if (!seen && reportedErrors.size >= CLIENT_LOG_MAX_KEYS) reportedErrors.clear();
    reportedErrors.set(key, { last: now, suppressed: 0 });

    console.error(key, detail);
    fetch("/api/client-log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, detail: String(detail), count }),
      keepalive: true,          // still goes out if the page is being torn down
    }).catch(() => {});         // a failed report must never report its own failure
  } catch (e) {
    // Best effort only - reporting an error must never break the caller.
  }
}

window.addEventListener("error", (e) => {
  report("js-error", `${e.message} at ${e.filename}:${e.lineno}:${e.colno}`);
});
window.addEventListener("unhandledrejection", (e) => {
  report("unhandled-rejection", String(e.reason));
});

// 24-hour time everywhere on this board. hourCycle "h23" states the
// convention outright rather than leaning on hour12:false, whose mapping to
// h23 vs h24 (00:00 vs 24:00 for midnight) has varied by locale and ICU
// version; h23 is unambiguous on any of them. The locale stays [] - the
// browser's own - so this pins the clock convention and nothing else.
const TIME_OPTS = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
const CLOCK_TIME_OPTS = { ...TIME_OPTS, second: "2-digit" };
const CLOCK_DATE_OPTS = { weekday: "short", month: "short", day: "numeric" };
const STAMP_DATE_OPTS = { month: "short", day: "numeric" };

// Local calendar day, as a comparable string. Built from the date parts
// rather than an ISO slice because toISOString() is UTC - after 5pm Mountain
// that reports tomorrow, which would put a date on every evening checkout.
function dayKey(d) {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

// Timestamps from today show just the time. Older ones carry their date, so
// yesterday's 5:00 PM checkout can't be read as "this evening" by someone
// walking in the next morning. Deliberately a date rather than "yesterday":
// it stays correct over a weekend or a holiday, and needs no arithmetic to
// interpret from across the room.
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const time = d.toLocaleTimeString([], TIME_OPTS);
  if (dayKey(d) === dayKey(new Date())) return time;
  return `${d.toLocaleDateString([], STAMP_DATE_OPTS)}, ${time}`;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : str;
  return div.innerHTML;
}

let lastClockText = "";
function renderClock() {
  // Date alongside the live time. Same element and the same once-a-second
  // compare as before, so the date costs nothing extra: the string only
  // changes when the second does, and the DOM write is still skipped
  // whenever it hasn't.
  const now = new Date();
  const text = `${now.toLocaleDateString([], CLOCK_DATE_OPTS)} · ` +
               `${now.toLocaleTimeString([], CLOCK_TIME_OPTS)}`;
  if (text === lastClockText) return;
  lastClockText = text;
  document.getElementById("clock").textContent = text;
}
setInterval(renderClock, 1000);
renderClock();

// The third line of a roster card, when there is one. Both things that can
// appear there share one line rather than taking one each: the card's height
// is pinned to three lines (see .kiosk .roster__card in style.css), and a
// manual checkout can carry a typed note as well as the no-card mark.
function rosterNote(m) {
  const parts = [];
  if (m.manual) parts.push('<span class="roster__nocard">No card</span>');
  if (m.note) parts.push(escapeHtml(m.note));
  return parts.length ? `<div class="roster__note">${parts.join(" · ")}</div>` : "";
}

let lastRosterJson = "";
function renderRoster(roster) {
  // Today's date is part of the cache key, not just the roster data: what
  // fmtTime() prints depends on it, so at midnight every "5:00 PM" on the
  // board has to gain a date. The kiosk can sit for days without the roster
  // changing, and without this the strip would keep yesterday's date-less
  // rendering until the next tap - which is precisely the morning-after case
  // this is for. The key changes once a day and costs a string compare that
  // was happening anyway.
  const json = JSON.stringify([dayKey(new Date()), roster]);
  if (json === lastRosterJson) return;
  lastRosterJson = json;

  // A real <button> rather than a div with a click handler: the card is an
  // actual control now (click your name to check yourself in without a card),
  // and this gets focus, Enter/Space and the accessible role for free if a
  // keyboard is ever plugged into the kiosk. The strip is rebuilt wholesale on
  // every change, so the click handler is delegated to the container below
  // rather than reattached per card.
  const el = document.getElementById("roster");
  el.innerHTML = roster.map(m => `
    <button type="button" class="roster__card ${m.status === 'in' ? 'is-in' : ''}"
            data-member-id="${m.id}">
      <div class="roster__ring"></div>
      <div class="roster__meta">
        <div class="roster__name">${escapeHtml(m.display_name)}</div>
        <div class="roster__status">${m.status === 'in' ? 'In lab' : 'Out'}${m.since ? ' · ' + fmtTime(m.since) : ''}</div>
        ${rosterNote(m)}
      </div>
    </button>
  `).join("");
}

// The background video keeps playing behind the confirmation overlays, so the
// body needs to know when one is up (see .is-overlay in style.css). Derived
// from the two overlays' actual state rather than toggled independently by
// each: they overlap when a tap finishes and the toast replaces the "reading
// card" overlay, and independent toggles would race there.
function syncOverlayState() {
  const shown =
    document.getElementById("toast").classList.contains("is-visible") ||
    document.getElementById("confirm").classList.contains("is-visible") ||
    document.getElementById("reading-overlay").classList.contains("is-visible");
  document.body.classList.toggle("is-overlay", shown);
}

function setToastVisible(visible) {
  document.getElementById("toast").classList.toggle("is-visible", visible);
  syncOverlayState();
}

function hideNotePrompt() {
  document.getElementById("toast-note").style.display = "none";
  document.body.classList.remove("is-note-prompt");
  clearTimeout(showToast._noteTimeout);
}

function showToast(event) {
  const toast = document.getElementById("toast");
  toast.classList.remove("is-out", "is-error");
  clearTimeout(showToast._t);
  hideNotePrompt();
  // Something happened - a card tap, or this page's own click-to-toggle.
  // Either way a half-answered confirmation dialog is now about the wrong
  // moment, so it goes rather than reappearing under the toast.
  closeConfirm();

  const hint = document.getElementById("toast-hint");

  if (event.action === "error") {
    document.getElementById("toast-name").textContent = event.message || "Card not recognized";
    document.getElementById("toast-action").textContent = "";
    document.getElementById("toast-time").textContent = fmtTime(event.timestamp);
    hint.style.display = "none";
    toast.classList.add("is-error");
    setToastVisible(true);
    showToast._t = setTimeout(() => setToastVisible(false), TOAST_VISIBLE_MS);
    return;
  }

  document.getElementById("toast-name").textContent = event.display_name;
  document.getElementById("toast-action").textContent =
    event.action === "in" ? "Checked in" : "Checked out";
  document.getElementById("toast-time").textContent = fmtTime(event.timestamp);
  // "You may remove your card now" is the wrong thing to say to someone who
  // just clicked their own name, and the mark it leaves on the board is worth
  // stating at the moment it is made rather than only afterwards.
  hint.textContent = event.manual
    ? "No card used — recorded as a manual entry"
    : "You may remove your card now";
  hint.style.display = "block";
  setToastVisible(true);

  if (event.action === "out" && event.checkin_event_id) {
    // Checking out: offer an optional "why" note instead of auto-hiding on
    // the usual short timer - give the person a moment to type something.
    toast.classList.add("is-out");
    const noteSection = document.getElementById("toast-note");
    const input = document.getElementById("note-input");
    noteSection.style.display = "flex";
    document.body.classList.add("is-note-prompt");
    input.value = "";
    setTimeout(() => input.focus(), 50);

    const finish = () => {
      setToastVisible(false);
      hideNotePrompt();
    };
    const save = async () => {
      const note = input.value.trim();
      if (note) {
        try {
          await fetch(`/api/events/${event.checkin_event_id}/note`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ note }),
          });
        } catch (e) {
          report("note-save-failed", e);
        }
      }
      finish();
    };

    const skipBtn = document.getElementById("note-skip");
    const saveBtn = document.getElementById("note-save");
    document.getElementById("note-save").onclick = save;
    document.getElementById("note-skip").onclick = finish;

    // Enter from the text box moves the selection onto a button rather than
    // acting straight away: what they typed picks which button is offered,
    // and a second Enter on that button commits it. A focused <button>
    // activates on Enter natively, so that second step needs no handler -
    // and because the button isn't focused yet when this keydown fires, the
    // same keypress can't fall through and activate it.
    input.onkeydown = (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); skipBtn.focus(); }
      else if (e.key === "Enter") {
        e.preventDefault();
        (input.value.trim() ? saveBtn : skipBtn).focus();
      }
    };
    skipBtn.onkeydown = (e) => {
      if (e.key === "ArrowUp") { e.preventDefault(); input.focus(); }
      else if (e.key === "ArrowRight") { e.preventDefault(); saveBtn.focus(); }
    };
    saveBtn.onkeydown = (e) => {
      if (e.key === "ArrowUp") { e.preventDefault(); input.focus(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); skipBtn.focus(); }
    };

    showToast._noteTimeout = setTimeout(finish, NOTE_PROMPT_TIMEOUT_MS);
  } else {
    // Checking in (or any non-checkout event): plain toast, auto-hide as before.
    showToast._t = setTimeout(() => setToastVisible(false), TOAST_VISIBLE_MS);
  }
}

// --- click to check in/out -------------------------------------------
// The no-card path: click your name on the roster strip, confirm, done. It
// exists for a reader that's down, a card left at home, and the stretch
// before the reader is even installed - so it has to work with a mouse and
// nothing else, hence a two-button dialog rather than anything typed.
//
// It asks first on purpose. The strip is six large targets along the bottom
// of a screen that sits in the open all day; without a confirmation step a
// single stray click silently checks somebody in or out, and the only trace
// is a line in an append-only log. Every event this writes is flagged
// manual server-side, and the board says "No card" beside that person's
// name until their next tap - an unverified entry should never be
// indistinguishable from a card read.

const CONFIRM_TIMEOUT_MS = 20000;   // an abandoned dialog must not sit on the board

const confirmEl = document.getElementById("confirm");
const confirmNameEl = document.getElementById("confirm-name");
const confirmActionEl = document.getElementById("confirm-action");
const confirmOkBtn = document.getElementById("confirm-ok");
const confirmCancelBtn = document.getElementById("confirm-cancel");

// Which member the open dialog is about; null when it's closed, which is
// also what makes a second Confirm click (or a timeout landing on an
// already-submitted dialog) a no-op.
let confirmMemberId = null;

function closeConfirm() {
  clearTimeout(closeConfirm._t);
  if (confirmMemberId === null) return;
  confirmMemberId = null;
  confirmEl.classList.remove("is-visible");
  document.body.classList.remove("is-confirm");
  syncOverlayState();
}

// Name and current status come off the card that was clicked rather than a
// copy of the roster kept on the side: the strip is already the rendering of
// exactly that data, and re-reading it can't drift out of sync with what the
// person is looking at.
function openConfirm(card) {
  const memberId = Number(card.dataset.memberId);
  if (!memberId) return;
  const goingIn = !card.classList.contains("is-in");

  confirmMemberId = memberId;
  confirmNameEl.textContent = card.querySelector(".roster__name").textContent;
  confirmActionEl.textContent = goingIn ? "Check in?" : "Check out?";
  confirmOkBtn.textContent = goingIn ? "Check in" : "Check out";
  confirmEl.classList.toggle("is-out", !goingIn);
  confirmEl.classList.add("is-visible");
  document.body.classList.add("is-confirm");
  syncOverlayState();

  // Cancel takes focus, not Confirm: if a keyboard is ever plugged in, a
  // stray Enter should land on the harmless button.
  confirmCancelBtn.focus();
  closeConfirm._t = setTimeout(closeConfirm, CONFIRM_TIMEOUT_MS);
}

async function submitConfirm() {
  const memberId = confirmMemberId;
  if (memberId === null) return;
  closeConfirm();       // also clears the id, so a double click can't post twice

  try {
    const res = await fetch("/api/manual-toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ member_id: memberId }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (e) {
    report("manual-toggle-failed", e);
    showToast({
      action: "error",
      message: "Could not record that",
      timestamp: new Date().toISOString(),
    });
    return;
  }

  // The toast is left to the poll rather than raised from the response here,
  // so events reach the screen through exactly one path however they were
  // caused. Polling immediately instead of waiting out POLL_MS is what keeps
  // it feeling instant; the sequence guard in poll() handles the overlap.
  poll();
}

// Delegated, because renderRoster() replaces the whole strip whenever the
// data changes and per-card handlers would go with it.
document.getElementById("roster").addEventListener("click", (e) => {
  const card = e.target.closest(".roster__card");
  if (card) openConfirm(card);
});

confirmOkBtn.onclick = submitConfirm;
confirmCancelBtn.onclick = closeConfirm;

// Clicking the backdrop is a cancel: the dialog covers the screen, so
// "somewhere else" is the instinctive way out of one opened by mistake.
confirmEl.addEventListener("click", (e) => {
  if (e.target === confirmEl) closeConfirm();
});

// Keyboard is a courtesy here (the kiosk has no keyboard), and matches the
// note prompt's arrow-key handling. Enter on a focused button is native.
confirmEl.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.preventDefault(); closeConfirm(); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); confirmCancelBtn.focus(); }
  else if (e.key === "ArrowRight") { e.preventDefault(); confirmOkBtn.focus(); }
});

// --- pointer visibility ----------------------------------------------
// The board hides the cursor (cursor: none on body.kiosk) - a pointer parked
// in the middle of an unattended display for a week is exactly the sort of
// thing nobody comes back to move. But the roster strip is clickable now, so
// it has to come back the moment the mouse moves and go away again once it
// stops.
//
// Cheap by construction: one class check per mousemove and no timer churn
// (the pending timeout re-reads the timestamp and pushes itself out rather
// than being cleared and reset thousands of times), and `cursor` is not a
// rendered property, so neither state costs a paint.

const POINTER_IDLE_MS = 8000;
let lastPointerMove = 0;

function hidePointerWhenIdle() {
  const remaining = POINTER_IDLE_MS - (Date.now() - lastPointerMove);
  if (remaining > 0) {
    hidePointerWhenIdle._t = setTimeout(hidePointerWhenIdle, remaining);
    return;
  }
  document.body.classList.remove("has-pointer");
}

document.addEventListener("mousemove", () => {
  lastPointerMove = Date.now();
  // Already showing: the pending timeout will see the fresh timestamp when it
  // fires and reschedule itself, so there is nothing to do per event.
  if (document.body.classList.contains("has-pointer")) return;
  document.body.classList.add("has-pointer");
  hidePointerWhenIdle._t = setTimeout(hidePointerWhenIdle, POINTER_IDLE_MS);
}, { passive: true });

// --- card reader presence --------------------------------------------
// The board is unattended: an unplugged reader, or a pcscd that died, looks
// exactly like a quiet day unless the board says so. The server samples it
// on a timer (cac_reader.start_reader_watch) and ships the result on
// /api/state, so this side only paints it.

const READER_LABELS = {
  ok: "Reader ready",
  down: "Reader offline",
  unknown: "Reader unknown",
};

// Only the status drives the display, so that's the whole cache key - the
// detail string (which reader, why it's down) is for the journal, not the
// board. Without this compare the header would be rewritten every 1.5s.
let lastReaderStatus = "";
function renderReader(reader) {
  const status = (reader && READER_LABELS[reader.status]) ? reader.status : "unknown";
  if (status === lastReaderStatus) return;
  lastReaderStatus = status;

  const el = document.getElementById("reader-status");
  el.textContent = READER_LABELS[status];
  el.classList.toggle("is-ok", status === "ok");
  el.classList.toggle("is-down", status === "down");
  el.classList.toggle("is-unknown", status === "unknown");
}

function setReading(active) {
  document.getElementById("reading-overlay").classList.toggle("is-visible", active);
  syncOverlayState();
}

// Polls can overlap: submitConfirm() fires one the instant a click has been
// recorded instead of waiting out the interval, so two are briefly in flight.
// If the older reply lands second it carries the pre-click state - repainting
// a stale roster, and re-toasting the event before it, since its id differs
// from the one just shown. Sequence numbers make the loser a no-op.
let pollSeq = 0;
let latestPollApplied = 0;

async function poll() {
  const seq = ++pollSeq;
  try {
    // ?src=kiosk lets the server tell this page's polls apart from the
    // dashboard's, so the health heartbeat can report a kiosk that has
    // stopped polling (a Chromium crash or renderer OOM looks like nothing
    // at all from the server side otherwise).
    const res = await fetch("/api/state?src=kiosk");
    const data = await res.json();
    if (seq < latestPollApplied) return;
    latestPollApplied = seq;
    renderRoster(data.roster);
    renderReader(data.reader);
    setReading(!!data.reading);

    const incomingId = data.last_event && data.last_event.event_id;
    if (lastEventId === null) {
      // First poll after page load: just establish the baseline, without
      // popping a toast for whatever event was already current (which may
      // be stale/from before this page loaded).
      lastEventId = incomingId || 0;
    } else if (incomingId && incomingId !== lastEventId) {
      showToast(data.last_event);
      lastEventId = incomingId;
    }
  } catch (e) {
    report("poll-failed", e);
  }
}
setInterval(poll, POLL_MS);
poll();

// --- slide rotation --------------------------------------------------
// The media panel cycles the objectives from config/objectives.json, one
// full-panel slide at a time, over an optional looping background video.
// Slides swap instantly - a crossfade would mean compositing two full-panel
// layers on every rotation (see Performance in CLAUDE.md).

// Filename of the looping background video, inside static/media/. Leave it
// empty ("") for no background - the panel then just uses its flat card
// colour. There is deliberately no directory-listing endpoint, so name the
// file here by hand after dropping it in. It must be H.264 and no wider than
// 1920px, or the Pi decodes it in software (README step 7). Note that its
// last LOOP_TAIL_S seconds never play - see the wrap-around below.
const BACKGROUND_VIDEO = "background.mp4";

// Seconds of the clip held back from ever playing, so end-of-stream is never
// reached. The Pi's V4L2 hardware decoder (bcm2835-codec) wedges on the
// end-of-stream drain: Chromium stops feeding it, the drain never completes,
// and the ~3.2s of frames still in flight never come out. The picture freezes
// with no error, readyState drops to 2, and it never recovers - so the native
// `loop` attribute is unusable here. Measured on real hardware across six
// clips of three different durations, contents, resolutions and bitrates:
// every one froze at duration - 3.1 to 3.3s. 5s leaves ~1.7s of margin.
const LOOP_TAIL_S = 5;

const SLIDE_MS = 12000;            // how long one objective slide stays up

const bgVideoEl = document.getElementById("media-bg");
const slideEl = document.getElementById("media-slide");
const slideTextEl = document.getElementById("slide-text");
const slideImageEl = document.getElementById("slide-image");
const emptyEl = document.getElementById("media-empty");
const labelEl = document.getElementById("media-label");

let objectives = [];
let rotationIndex = 0;
let rotationTimer = null;

// An objective is either a plain string or {text, image}, where image is a
// filename in static/media/. Both forms are supported so config/objectives.json
// stays hand-editable and old files keep working untouched.
function normalizeObjective(entry) {
  if (typeof entry === "string") return { text: entry, image: null };
  return { text: entry.text || "", image: entry.image || null };
}

// Swapping the src re-decodes the picture, so leave it alone when the same
// objective comes back around and the file hasn't changed.
function setSlideImage(file) {
  if (!file) {
    slideEl.classList.remove("has-image");
    return;
  }
  if (slideImageEl.dataset.file !== file) {
    slideImageEl.dataset.file = file;
    slideImageEl.src = `/static/media/${file}`;
  }
  slideEl.classList.add("has-image");
}

function showNextSlide() {
  clearTimeout(rotationTimer);

  if (objectives.length === 0) {
    slideEl.style.display = "none";
    labelEl.style.display = "none";   // no slides to head up
    emptyEl.style.display = "block";
    return;
  }

  const item = objectives[rotationIndex % objectives.length];
  rotationIndex = (rotationIndex + 1) % objectives.length;
  emptyEl.style.display = "none";

  if (slideTextEl.textContent !== item.text) slideTextEl.textContent = item.text;
  setSlideImage(item.image);
  slideEl.style.display = "flex";
  labelEl.style.display = "block";

  // With a single objective the slide never changes, so don't wake the panel
  // up every SLIDE_MS just to redraw the same thing.
  if (objectives.length > 1) rotationTimer = setTimeout(showNextSlide, SLIDE_MS);
}

function restartRotation() {
  rotationIndex = 0;
  showNextSlide();
}

// A missing or corrupt picture falls back to a text-only slide rather than
// leaving a broken-image box on the board. dataset.file is cleared so the
// next time this objective comes around it retries the load.
slideImageEl.addEventListener("error", () => {
  report("objective-image-failed", slideImageEl.currentSrc);
  slideImageEl.dataset.file = "";
  slideEl.classList.remove("has-image");
});

// --- background video ------------------------------------------------
// The element and its scrim stay display:none until the first frame is
// actually decodable, so a missing file or a slow first load never shows a
// black rect behind the slides - and an unconfigured background costs
// nothing at all, not even a compositing layer.
bgVideoEl.addEventListener("loadeddata", () => document.body.classList.add("has-bg"));

// No retry here, unlike a playlist: this is one hardcoded filename, so if it
// fails once it will fail identically every time. Drop back to the flat panel
// background and leave the reason in the console.
bgVideoEl.addEventListener("error", () => {
  const code = bgVideoEl.error ? bgVideoEl.error.code : "?";
  report("video-load-failed", `${bgVideoEl.currentSrc} (MediaError code ${code})`);
  document.body.classList.remove("has-bg");
});

// Loop by hand, wrapping back to the start before the decoder is ever asked
// to drain. `timeupdate` fires ~4x/second, which is plenty of resolution
// against a 1.7s margin and far cheaper than a rAF loop.
bgVideoEl.addEventListener("timeupdate", () => {
  const wrapAt = bgVideoEl.duration - LOOP_TAIL_S;
  if (wrapAt > 0 && bgVideoEl.currentTime > wrapAt) bgVideoEl.currentTime = 0;
});

bgVideoEl.addEventListener("loadedmetadata", () => {
  if (bgVideoEl.duration <= LOOP_TAIL_S) {
    report("video-too-short",
      `background video is only ${bgVideoEl.duration.toFixed(1)}s; it must be ` +
      `longer than ${LOOP_TAIL_S}s or it will freeze on the Pi (README step 7)`);
  }
});

// --- background video watchdog ---------------------------------------
// The decoder wedge described above announces itself with nothing at all:
// no `error` event, no `stalled`, just frames that stop arriving while the
// element still believes it is playing. Sampling currentTime is the only
// way to see it from here. This runs every VIDEO_CHECK_MS and reads two
// properties - it is nowhere near the render path and costs nothing.
const VIDEO_CHECK_MS = 5000;
const VIDEO_STALL_MS = 15000;      // 3 consecutive dead samples before reporting
const VIDEO_START_TIMEOUT_MS = 30000;

let lastVideoTime = -1;
let videoStalledSince = 0;

setInterval(() => {
  // No has-bg means either no video configured or one we've already given
  // up on - either way there is nothing left to watch.
  if (!document.body.classList.contains("has-bg")) return;
  if (bgVideoEl.paused || bgVideoEl.ended) return;

  if (bgVideoEl.currentTime !== lastVideoTime) {
    lastVideoTime = bgVideoEl.currentTime;
    videoStalledSince = 0;
    return;
  }
  if (!videoStalledSince) {
    videoStalledSince = Date.now();
    return;
  }
  if (Date.now() - videoStalledSince < VIDEO_STALL_MS) return;

  report("video-stall",
    `frozen at ${bgVideoEl.currentTime.toFixed(2)}s of ` +
    `${bgVideoEl.duration.toFixed(2)}s, readyState=${bgVideoEl.readyState}, ` +
    `networkState=${bgVideoEl.networkState}`);

  // No reload attempt on purpose: once the Pi's hardware decoder has wedged
  // it does not come back, so retrying would only re-report the same stall
  // forever. Pausing releases the decoder and dropping has-bg leaves a
  // readable flat board rather than a dead frame for the rest of the run.
  bgVideoEl.pause();
  document.body.classList.remove("has-bg");
  videoStalledSince = 0;
}, VIDEO_CHECK_MS);

if (BACKGROUND_VIDEO) {
  bgVideoEl.src = `/static/media/${BACKGROUND_VIDEO}`;

  // A video that never produces a first frame fires neither `loadeddata`
  // nor `error` - which is exactly what the non-faststart range-request
  // stall looks like (see Performance in CLAUDE.md). Without this check it
  // would be indistinguishable from having no background configured.
  setTimeout(() => {
    if (!document.body.classList.contains("has-bg")) {
      report("video-never-started",
        `no first frame after ${VIDEO_START_TIMEOUT_MS}ms; ` +
        `readyState=${bgVideoEl.readyState}, networkState=${bgVideoEl.networkState}`);
    }
  }, VIDEO_START_TIMEOUT_MS);
}

async function loadObjectives() {
  try {
    const res = await fetch("/api/objectives");
    const data = await res.json();
    const json = JSON.stringify(data.objectives);
    if (json === loadObjectives._last) return;
    loadObjectives._last = json;
    objectives = (data.objectives || []).map(normalizeObjective);
    // Only reached when objectives.json actually changed, so restarting the
    // rotation here costs nothing in the steady state.
    restartRotation();
  } catch (e) {
    report("objectives-load-failed", e);
    // Nothing to show yet; the panel keeps whatever is already up (on first
    // load, that's the "add objectives" placeholder) and retries in 60s.
  }
}

loadObjectives();
setInterval(loadObjectives, 60000); // pick up edits to objectives.json without a restart
