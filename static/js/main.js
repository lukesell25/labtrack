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

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : str;
  return div.innerHTML;
}

let lastClockText = "";
function renderClock() {
  const text = new Date().toLocaleTimeString();
  if (text === lastClockText) return;
  lastClockText = text;
  document.getElementById("clock").textContent = text;
}
setInterval(renderClock, 1000);
renderClock();

let lastRosterJson = "";
function renderRoster(roster) {
  const json = JSON.stringify(roster);
  if (json === lastRosterJson) return;
  lastRosterJson = json;

  const el = document.getElementById("roster");
  el.innerHTML = roster.map(m => `
    <div class="roster__card ${m.status === 'in' ? 'is-in' : ''}">
      <div class="roster__ring"></div>
      <div class="roster__meta">
        <div class="roster__name">${escapeHtml(m.display_name)}</div>
        <div class="roster__status">${m.status === 'in' ? 'In lab' : 'Out'}${m.since ? ' · ' + fmtTime(m.since) : ''}</div>
        ${m.note ? `<div class="roster__note">${escapeHtml(m.note)}</div>` : ''}
      </div>
    </div>
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
          console.error("failed to save note", e);
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

function setReading(active) {
  document.getElementById("reading-overlay").classList.toggle("is-visible", active);
  syncOverlayState();
}

async function poll() {
  try {
    const res = await fetch("/api/state");
    const data = await res.json();
    renderRoster(data.roster);
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
    console.error("poll failed", e);
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
// 1920px, or the Pi decodes it in software (README step 7).
const BACKGROUND_VIDEO = "background.mp4";

const SLIDE_MS = 12000;            // how long one objective slide stays up

const bgVideoEl = document.getElementById("media-bg");
const slideEl = document.getElementById("media-slide");
const slideTextEl = document.getElementById("slide-text");
const slideImageEl = document.getElementById("slide-image");
const emptyEl = document.getElementById("media-empty");

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
    emptyEl.style.display = "block";
    return;
  }

  const item = objectives[rotationIndex % objectives.length];
  rotationIndex = (rotationIndex + 1) % objectives.length;
  emptyEl.style.display = "none";

  if (slideTextEl.textContent !== item.text) slideTextEl.textContent = item.text;
  setSlideImage(item.image);
  slideEl.style.display = "flex";

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
  console.error("objective image failed to load:", slideImageEl.currentSrc);
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
  console.error("background video failed to load:", bgVideoEl.currentSrc);
  document.body.classList.remove("has-bg");
});

if (BACKGROUND_VIDEO) {
  bgVideoEl.src = `/static/media/${BACKGROUND_VIDEO}`;
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
    console.error("failed to load objectives", e);
    // Nothing to show yet; the panel keeps whatever is already up (on first
    // load, that's the "add objectives" placeholder) and retries in 60s.
  }
}

loadObjectives();
setInterval(loadObjectives, 60000); // pick up edits to objectives.json without a restart
