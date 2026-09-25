// Kiosk display logic: roster strip, keyboard check-in, objectives, media
// loop, confirmation toast. No touchscreen assumed - a keyboard (and
// optionally a mouse) in front of an otherwise unattended board, which polls
// the backend for status changes.

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

// 24-hour time for the header clock. hourCycle "h23" states the convention
// outright rather than leaning on hour12:false, whose mapping to h23 vs h24
// (00:00 vs 24:00 for midnight) has varied by locale and ICU version; h23 is
// unambiguous on any of them. The locale stays [] - the browser's own - so
// this pins the clock convention and nothing else. The clock is the only
// time on this board: nobody's status carries one (see "No time tracking"
// in CLAUDE.md).
const TIME_OPTS = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
const CLOCK_TIME_OPTS = { ...TIME_OPTS, second: "2-digit" };
const CLOCK_DATE_OPTS = { weekday: "short", month: "short", day: "numeric" };

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

// What the status line says for each state. Matches dashboard.js.
const STATUS_LABELS = { in: "In lab", away: "Away", out: "Out" };

// The third line of a roster card, when there is one: a checkout comment or,
// for someone away, where they went - the server sends whichever applies as
// `note`. The card's height is pinned to three lines (see .kiosk
// .roster__card in style.css), so this never grows a fourth.
function rosterNote(m) {
  return m.note ? `<div class="roster__note">${escapeHtml(m.note)}</div>` : "";
}

function statusClass(status) {
  return status === "in" ? "is-in" : status === "away" ? "is-away" : "";
}

let lastRosterJson = "";
function renderRoster(roster) {
  const json = JSON.stringify(roster);
  if (json === lastRosterJson) return;
  lastRosterJson = json;

  // A real <button> so a mouse click works as well as the keyboard. The
  // strip is rebuilt wholesale on every change, so the click handler is
  // delegated to the container below rather than reattached per card, and
  // the keyboard highlight is re-applied from highlightedId afterwards.
  // tabindex -1 keeps the cards out of the browser's own Tab order: the
  // arrow keys are the one way to move along the strip (see "keyboard").
  const el = document.getElementById("roster");
  el.innerHTML = roster.map(m => `
    <button type="button" tabindex="-1" class="roster__card ${statusClass(m.status)}"
            data-member-id="${m.id}" data-status="${escapeHtml(m.status)}">
      <div class="roster__ring"></div>
      <div class="roster__meta">
        <div class="roster__name">${escapeHtml(m.display_name)}</div>
        <div class="roster__status">${STATUS_LABELS[m.status] || escapeHtml(m.status)}</div>
        ${rosterNote(m)}
      </div>
    </button>
  `).join("");
  paintHighlight();
}

// The background video keeps playing behind the confirmation overlays, so the
// body needs to know when one is up (see .is-overlay in style.css). Derived
// from the overlays' actual state rather than toggled independently by each:
// they overlap when a check-in completes and the toast replaces the dialog,
// and independent toggles would race there.
const OVERLAY_IDS = ["toast", "confirm", "reboot"];
function syncOverlayState() {
  const shown = OVERLAY_IDS.some(
    (id) => document.getElementById(id).classList.contains("is-visible"));
  document.body.classList.toggle("is-overlay", shown);
}

function setToastVisible(visible) {
  document.getElementById("toast").classList.toggle("is-visible", visible);
  syncOverlayState();
}

function hideNotePrompt() {
  if (document.body.classList.contains("is-note-prompt")) releaseFocus();
  document.getElementById("toast-note").style.display = "none";
  document.body.classList.remove("is-note-prompt");
  clearTimeout(showToast._noteTimeout);
}

// Drop focus from whatever control had it (a dialog button, the note box) so
// keys go back to the document-level handler that drives the roster strip.
function releaseFocus() {
  if (document.activeElement && document.activeElement !== document.body) {
    document.activeElement.blur();
  }
}

const ACTION_LABELS = { in: "Checked in", away: "Stepped away", out: "Checked out" };

function showToast(event) {
  const toast = document.getElementById("toast");
  toast.classList.remove("is-out", "is-away", "is-error");
  clearTimeout(showToast._t);
  hideNotePrompt();
  // Something happened - possibly from another machine. A half-answered
  // dialog is now about the wrong moment, so it goes rather than
  // reappearing under the toast.
  closeConfirm();

  if (event.action === "error") {
    document.getElementById("toast-name").textContent = event.message || "Something went wrong";
    document.getElementById("toast-action").textContent = "";
    toast.classList.add("is-error");
    setToastVisible(true);
    showToast._t = setTimeout(() => setToastVisible(false), TOAST_VISIBLE_MS);
    return;
  }

  document.getElementById("toast-name").textContent = event.display_name;
  let actionText = ACTION_LABELS[event.action] || event.action;
  if (event.action === "in" && event.previous === "away") actionText = "Back in lab";
  if (event.action === "away" && event.location) actionText = `Away · ${event.location}`;
  document.getElementById("toast-action").textContent = actionText;
  toast.classList.toggle("is-away", event.action === "away");
  setToastVisible(true);

  if (event.action === "out" && event.change_id) {
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
          await fetch(`/api/presence/${event.change_id}/note`, {
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
    saveBtn.onclick = save;
    skipBtn.onclick = finish;

    // Enter from the text box moves the selection onto a button rather than
    // acting straight away: what they typed picks which button is offered,
    // and a second Enter on that button commits it. A focused <button>
    // activates on Enter natively, so that second step needs no handler -
    // and because the button isn't focused yet when this keydown fires, the
    // same keypress can't fall through and activate it. Escape anywhere in
    // the prompt is Skip. The prompt owns its keys while it is up
    // (stopPropagation), for the same reason the dialog does.
    input.onkeydown = (e) => {
      e.stopPropagation();
      if (e.key === "Escape") { e.preventDefault(); finish(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); skipBtn.focus(); }
      else if (e.key === "Enter") {
        e.preventDefault();
        (input.value.trim() ? saveBtn : skipBtn).focus();
      }
    };
    skipBtn.onkeydown = (e) => {
      e.stopPropagation();
      if (e.key === "Escape") { e.preventDefault(); finish(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); input.focus(); }
      else if (e.key === "ArrowRight") { e.preventDefault(); saveBtn.focus(); }
    };
    saveBtn.onkeydown = (e) => {
      e.stopPropagation();
      if (e.key === "Escape") { e.preventDefault(); finish(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); input.focus(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); skipBtn.focus(); }
    };

    showToast._noteTimeout = setTimeout(finish, NOTE_PROMPT_TIMEOUT_MS);
  } else {
    // Checking in (or any non-checkout event): plain toast, auto-hide.
    showToast._t = setTimeout(() => setToastVisible(false), TOAST_VISIBLE_MS);
  }
}

// --- the check-in / leaving dialog ---------------------------------------
// Opened for one member, from the keyboard (arrows to highlight a name, then
// Enter) or a mouse click on their card. It asks first on purpose: the
// strip sits in the open all day, and without a confirmation step a single
// stray keypress or click would silently move somebody in or out.
//
// The buttons depend on where the person is now. Out: [Cancel] [Check in].
// In: [Cancel] [Check out] plus the "still at work, elsewhere" row - a button
// per preset in config/locations.json ("Server room", ...) and an "Other..."
// that reveals a text box. Away: [Cancel] [Back in lab] [Check out].
//
// The likeliest action takes focus, so the keyboard path is Enter to open,
// Enter to confirm; arrows move between buttons and Escape cancels.

const CONFIRM_TIMEOUT_MS = 20000;   // an abandoned dialog must not sit on the board

const confirmEl = document.getElementById("confirm");
const confirmNameEl = document.getElementById("confirm-name");
const confirmActionEl = document.getElementById("confirm-action");
const confirmCancelBtn = document.getElementById("confirm-cancel");
const confirmInBtn = document.getElementById("confirm-in");
const confirmOutBtn = document.getElementById("confirm-out");
const confirmAwayEl = document.getElementById("confirm-away");
const confirmLocationsEl = document.getElementById("confirm-locations");
const confirmOtherForm = document.getElementById("confirm-other");
const confirmOtherInput = document.getElementById("confirm-other-input");

// What the open dialog is about - { memberId, status, name } - or null when
// closed, which is also what makes a second Enter or click on a button (or a
// timeout landing on an already-submitted dialog) a no-op.
let dialog = null;

// Preset away locations, from /api/locations (config/locations.json).
let locations = [];

function closeConfirm() {
  clearTimeout(closeConfirm._t);
  if (dialog === null) return;
  dialog = null;
  confirmEl.classList.remove("is-visible");
  document.body.classList.remove("is-confirm");
  releaseFocus();
  syncOverlayState();
}

// Rebuilt each time a leaving dialog opens rather than kept in sync with the
// 60s locations reload: it is a handful of buttons, built at most a few times
// a day. Buttons carry an index, not the name, so a name with a quote in it
// can't break out of the attribute.
function renderLocationButtons() {
  confirmLocationsEl.innerHTML = locations.map((name, i) =>
    `<button type="button" class="confirm__btn confirm__btn--away" data-location="${i}">` +
    `${escapeHtml(name)}</button>`
  ).join("") +
  `<button type="button" class="confirm__btn" data-other>Other…</button>`;
}

// Name and current status come off the card rather than a copy of the
// roster kept on the side: the strip is already the rendering of exactly
// that data, and re-reading it can't drift out of sync with what the person
// is looking at.
function openConfirm(card) {
  const memberId = Number(card.dataset.memberId);
  if (!memberId) return;
  const status = card.dataset.status;
  dialog = { memberId, status, name: card.querySelector(".roster__name").textContent };

  // Whoever is at the board now outranks a toast still up from the last
  // person (a note prompt included), which would otherwise sit on top of
  // this dialog for its remaining seconds.
  clearTimeout(showToast._t);
  hideNotePrompt();
  setToastVisible(false);

  confirmNameEl.textContent = dialog.name;
  confirmActionEl.textContent =
    status === "in" ? "Leaving the lab?" : status === "away" ? "Back in the lab?" : "Check in?";
  confirmEl.classList.toggle("is-out", status === "in");
  confirmEl.classList.toggle("is-away", status === "away");

  confirmInBtn.hidden = status === "in";
  confirmInBtn.textContent = status === "away" ? "Back in lab" : "Check in";
  confirmOutBtn.hidden = status === "out";
  confirmAwayEl.hidden = status !== "in";
  if (status === "in") {
    renderLocationButtons();
    confirmOtherForm.hidden = true;
    confirmOtherInput.value = "";
  }

  confirmEl.classList.add("is-visible");
  document.body.classList.add("is-confirm");
  syncOverlayState();

  // The likeliest answer takes focus, so Enter-Enter is the whole flow:
  // someone out is checking in, someone in is checking out, someone away is
  // coming back.
  (status === "in" ? confirmOutBtn : confirmInBtn).focus();
  closeConfirm._t = setTimeout(closeConfirm, CONFIRM_TIMEOUT_MS);
}

async function submitChoice(action, location) {
  const d = dialog;
  if (d === null || d.saving) return;   // a double press can't post twice
  // The dialog stays up until the toast replaces it (showToast closes it).
  // Closing it here instead flashed the board back for the length of two
  // requests - with its scrim and slide re-rasterized, only to be covered
  // again by the toast - and on a slow request left the screen looking as if
  // the keypress had been ignored. Focus goes, so Enter and the arrows fall
  // through to the document handler, which does nothing while a dialog is
  // open. The 20s dialog timeout is left running as a backstop against a
  // request that never returns.
  d.saving = true;
  releaseFocus();
  confirmActionEl.textContent = "Saving…";

  try {
    const res = await fetch("/api/set-status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ member_id: d.memberId, action, location }),
    });
    // 409: the choice no longer applies - the status changed under it (from
    // another machine, say). Whatever the board shows next is the truth.
    if (!res.ok && res.status !== 409) throw new Error(`HTTP ${res.status}`);
  } catch (e) {
    report("choice-failed", e);
    showToast({ action: "error", message: "Could not record that" });
    return;
  }

  // The toast is left to the poll rather than raised from the response here,
  // so events reach the screen through exactly one path however they were
  // caused. Polling immediately instead of waiting out POLL_MS is what keeps
  // it feeling instant; the sequence guard in poll() handles the overlap.
  await poll();
  // Still ours and still up: nothing toasted - a 409, or the poll failed (the
  // next one will bring the toast). Checked against d so a dialog opened for
  // someone else in the meantime is left alone.
  if (dialog === d) closeConfirm();
}

// Delegated, because renderRoster() replaces the whole strip whenever the
// data changes and per-card handlers would go with it. A click also moves
// the keyboard highlight there, so the two never disagree about who is
// selected.
document.getElementById("roster").addEventListener("click", (e) => {
  const card = e.target.closest(".roster__card");
  if (!card) return;
  setHighlight(Number(card.dataset.memberId));
  openConfirm(card);
});

// One handler for every button in the dialog, since the location row is
// rebuilt on open. Clicking the backdrop cancels: it covers the screen, so
// "somewhere else" is the instinctive way out of one opened by mistake.
confirmEl.addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) {
    if (e.target === confirmEl) closeConfirm();
    return;
  }
  if (btn === confirmCancelBtn) closeConfirm();
  else if (btn.dataset.action) submitChoice(btn.dataset.action, null);
  else if (btn.dataset.location !== undefined) submitChoice("away", locations[Number(btn.dataset.location)]);
  else if (btn.hasAttribute("data-other")) {
    confirmOtherForm.hidden = false;
    confirmOtherInput.focus();
  }
});

confirmOtherForm.addEventListener("submit", (e) => {
  e.preventDefault();
  // Empty is allowed - "away, somewhere" is still true.
  submitChoice("away", confirmOtherInput.value.trim() || null);
});

// Inside the dialog every arrow key steps through whichever buttons are
// showing (the location row wraps onto more than one line, so up/down act
// like left/right rather than trying to be spatial). Enter on a focused
// button is native, and Enter in the text box submits its form.
confirmEl.addEventListener("keydown", (e) => {
  // The dialog owns every key while it is up. Without this, the Escape that
  // closes it would bubble on to the document handler below - which by then
  // sees no dialog - and clear the roster highlight as well.
  e.stopPropagation();
  if (e.key === "Escape") {
    e.preventDefault();
    // From the "Other" box, Escape backs out to the buttons first.
    if (e.target === confirmOtherInput) {
      confirmOtherForm.hidden = true;
      confirmEl.querySelector("[data-other]").focus();
    } else {
      closeConfirm();
    }
    return;
  }
  const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
  if (!step) return;
  if (e.target === confirmOtherInput && (e.key === "ArrowLeft" || e.key === "ArrowRight")) return;
  e.preventDefault();
  const buttons = [...confirmEl.querySelectorAll("button")]
    .filter((b) => !b.hidden && !b.closest("[hidden]"));
  if (!buttons.length) return;
  const at = buttons.indexOf(document.activeElement);
  buttons[(at + step + buttons.length) % buttons.length].focus();
});

// --- keyboard --------------------------------------------------------
// The kiosk's front door: a keyboard in front of the board. Left/right move
// a highlight along the roster strip, Enter opens the dialog for whoever is
// highlighted (see the hint in the header, #keys-hint in index.html). The
// highlight is a class plus a CSS outline rather than DOM focus, so it
// survives renderRoster() rebuilding the strip, and it is dropped after
// HIGHLIGHT_IDLE_MS so a ring isn't left parked on one name all day.
//
// Handled at the document so it works however focus has wandered - Chromium
// in kiosk mode starts with focus on the page itself, not on any control.
// While the dialog or the note prompt is up, their own handlers own the keys.

const HIGHLIGHT_IDLE_MS = 30000;
let highlightedId = null;

function rosterCards() {
  return [...document.querySelectorAll("#roster .roster__card")];
}

// Apply highlightedId to the current strip. A member who has left the roster
// takes the highlight with them.
function paintHighlight() {
  let found = false;
  for (const card of rosterCards()) {
    const on = Number(card.dataset.memberId) === highlightedId;
    card.classList.toggle("is-highlighted", on);
    found = found || on;
  }
  if (!found) highlightedId = null;
  document.body.classList.toggle("has-highlight", highlightedId !== null);
}

function setHighlight(memberId) {
  highlightedId = memberId;
  paintHighlight();
  clearTimeout(setHighlight._t);
  if (memberId !== null) {
    setHighlight._t = setTimeout(() => setHighlight(null), HIGHLIGHT_IDLE_MS);
  }
}

function moveHighlight(step) {
  const cards = rosterCards();
  if (!cards.length) return;
  const at = cards.findIndex((c) => Number(c.dataset.memberId) === highlightedId);
  // Nothing highlighted yet: right starts at the first name, left at the last.
  const next = at === -1
    ? (step > 0 ? 0 : cards.length - 1)
    : Math.min(cards.length - 1, Math.max(0, at + step));
  setHighlight(Number(cards[next].dataset.memberId));
}

document.addEventListener("keydown", (e) => {
  if (dialog !== null || document.body.classList.contains("is-note-prompt")) return;
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  const cards = rosterCards();
  switch (e.key) {
    case "ArrowRight": case "ArrowDown": moveHighlight(1); break;
    case "ArrowLeft":  case "ArrowUp":   moveHighlight(-1); break;
    case "Home": if (cards.length) setHighlight(Number(cards[0].dataset.memberId)); break;
    case "End":  if (cards.length) setHighlight(Number(cards[cards.length - 1].dataset.memberId)); break;
    case "Escape": setHighlight(null); break;
    // Enter only, not Space: a button activates on Space's *keyup*, which
    // would land on the dialog's freshly focused "Check in" and confirm it
    // before the person has seen the question.
    case "Enter": {
      // Enter with nothing highlighted just starts the highlight - opening a
      // dialog for whoever happens to be first would be a guess.
      const card = cards.find((c) => Number(c.dataset.memberId) === highlightedId);
      if (card) { setHighlight(highlightedId); openConfirm(card); }
      else moveHighlight(1);
      break;
    }
    default: return;
  }
  // Keeps a card that took focus from a mouse click from also activating
  // natively on Enter, which would open the dialog twice.
  e.preventDefault();
});

// --- pointer visibility ----------------------------------------------
// The board hides the cursor (cursor: none on body.kiosk) - a pointer parked
// in the middle of an unattended display for a week is exactly the sort of
// thing nobody comes back to move. But the roster strip is clickable, so it
// has to come back the moment the mouse moves and go away again once it
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

// --- reboot notice ---------------------------------------------------
// The server schedules a reboot when the board reaches a state only a reboot
// clears (see request_reboot in app.py). It is worth announcing rather than
// just doing: this screen stands in the open all day, and a board that blanks
// with no explanation reads as a dead Pi to whoever is standing in front of
// it. The countdown is short because the display is already broken by the
// time this appears - it buys an explanation, not a chance to intervene.

const rebootEl = document.getElementById("reboot");
const rebootCountEl = document.getElementById("reboot-count");

// null = not counting down. Held as a local deadline rather than re-read from
// each poll, so the number ticks evenly regardless of poll jitter and a reply
// that arrives out of order can't make it jump back up.
let rebootDeadline = null;
let rebootTimer = null;

function tickReboot() {
  const left = Math.max(0, Math.round((rebootDeadline - Date.now()) / 1000));
  rebootCountEl.textContent = left > 0 ? `Restarting in ${left}s` : "Restarting now\u2026";
}

function renderReboot(reboot) {
  if (!reboot) {
    // Cancelled - the only way back is request_reboot failing to reach
    // systemctl, which un-schedules itself so the board doesn't sit under a
    // notice for a reboot that is never coming.
    if (rebootDeadline === null) return;
    rebootDeadline = null;
    clearInterval(rebootTimer);
    rebootTimer = null;
    rebootEl.classList.remove("is-visible");
    syncOverlayState();
    return;
  }
  if (rebootDeadline !== null) return;   // already counting down
  rebootDeadline = Date.now() + (reboot.in_s || 0) * 1000;
  rebootEl.classList.add("is-visible");
  syncOverlayState();
  tickReboot();
  rebootTimer = setInterval(tickReboot, 1000);
}

// Polls can overlap: submitChoice() fires one the instant a choice has been
// recorded instead of waiting out the interval, so two are briefly in flight.
// If the older reply lands second it carries the pre-click state - repainting
// a stale roster, and re-toasting the event before it, since its id differs
// from the one just shown. Sequence numbers make the loser a no-op.
let pollSeq = 0;
let latestPollApplied = 0;

// The background video's frame rate over the last minute, as extra query
// parameters on the poll - see "frame-rate telemetry" below. Empty while there
// is no video playing or no full window measured yet. Declared up here because
// poll() first runs before that section does.
let videoStatsQuery = "";

async function poll() {
  const seq = ++pollSeq;
  try {
    // ?src=kiosk lets the server tell this page's polls apart from the
    // dashboard's, so the health heartbeat can report a kiosk that has
    // stopped polling (a Chromium crash or renderer OOM looks like nothing
    // at all from the server side otherwise).
    const res = await fetch("/api/state?src=kiosk" + videoStatsQuery);
    const data = await res.json();
    if (seq < latestPollApplied) return;
    latestPollApplied = seq;
    renderRoster(data.roster);
    renderReboot(data.reboot);

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

// Filename of the looping background video, inside static/media/. Empty
// means no background - the panel then just uses its flat card colour.
//
// Chosen server-side and rendered into data-video on the element (see
// BACKGROUND_VIDEO_CANDIDATES in app.py) rather than named here, because
// which file exists differs per machine: the Pi runs scripts/build-loop.sh to
// produce a long-playing background-long.mp4, and a dev checkout has only the
// short background.mp4 that ships in git. Whatever it names must be H.264 and
// no wider than 1920px, or the Pi decodes it in software (README step 7), and
// its last LOOP_TAIL_S seconds never play - see the wrap-around below.
const BACKGROUND_VIDEO = document.getElementById("media-bg").dataset.video || "";

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

// --- frame-rate telemetry --------------------------------------------
// "The video lags" is otherwise only ever an impression. Once a minute, work
// out how many frames the background actually got on screen and how many
// Chromium dropped for arriving too late to show, and ride them along on the
// kiosk's next polls (no request of its own). health.py prints them as
// video_fps= / video_dropped= in the heartbeat, so a change meant to make the
// video smoother can be judged by numbers from the Pi rather than by eye.
// Computed here rather than server-side so /api/health, which samples on
// demand, can't disturb the window.
const VIDEO_STATS_MS = 60000;
let lastVideoQuality = null;

setInterval(() => {
  if (!document.body.classList.contains("has-bg") || bgVideoEl.paused ||
      !bgVideoEl.getVideoPlaybackQuality) {
    lastVideoQuality = null;
    videoStatsQuery = "";
    return;
  }
  const q = bgVideoEl.getVideoPlaybackQuality();
  const now = performance.now();
  const prev = lastVideoQuality;
  lastVideoQuality = { at: now, total: q.totalVideoFrames, dropped: q.droppedVideoFrames };
  // Counters restart with the media pipeline; skip the window that spans that.
  if (!prev || q.totalVideoFrames < prev.total || q.droppedVideoFrames < prev.dropped) {
    videoStatsQuery = "";
    return;
  }
  const seconds = (now - prev.at) / 1000;
  const dropped = q.droppedVideoFrames - prev.dropped;
  const shown = q.totalVideoFrames - prev.total - dropped;
  videoStatsQuery = `&vfps=${(shown / seconds).toFixed(1)}&vdrop=${dropped}`;
}, VIDEO_STATS_MS);

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

// Preset away locations for the leaving dialog. Same cadence as the
// objectives so an edit to config/locations.json lands without a restart;
// they are read into `locations` and only rendered when a dialog opens.
async function loadLocations() {
  try {
    const res = await fetch("/api/locations");
    const data = await res.json();
    locations = Array.isArray(data.locations) ? data.locations : [];
  } catch (e) {
    report("locations-load-failed", e);
  }
}

loadObjectives();
loadLocations();
// pick up edits to objectives.json / locations.json without a restart
setInterval(() => { loadObjectives(); loadLocations(); }, 60000);
