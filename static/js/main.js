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
    toast.classList.add("is-error", "is-visible");
    showToast._t = setTimeout(() => toast.classList.remove("is-visible"), TOAST_VISIBLE_MS);
    return;
  }

  document.getElementById("toast-name").textContent = event.display_name;
  document.getElementById("toast-action").textContent =
    event.action === "in" ? "Checked in" : "Checked out";
  document.getElementById("toast-time").textContent = fmtTime(event.timestamp);
  hint.style.display = "block";
  toast.classList.add("is-visible");

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
      toast.classList.remove("is-visible");
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

    input.onkeydown = (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); skipBtn.focus(); }
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
    showToast._t = setTimeout(() => toast.classList.remove("is-visible"), TOAST_VISIBLE_MS);
  }
}

function setReading(active) {
  document.getElementById("reading-overlay").classList.toggle("is-visible", active);
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

async function loadObjectives() {
  try {
    const res = await fetch("/api/objectives");
    const data = await res.json();
    const json = JSON.stringify(data.objectives);
    if (json === loadObjectives._last) return;
    loadObjectives._last = json;
    const list = document.getElementById("objectives-list");
    list.innerHTML = data.objectives.map(o => `<li>${escapeHtml(o)}</li>`).join("");
  } catch (e) {
    console.error("failed to load objectives", e);
  }
}
loadObjectives();
setInterval(loadObjectives, 60000); // pick up edits to objectives.json without a restart

// --- media loop -----------------------------------------------------
// Cycles through any .mp4 files placed in static/media/. Since the app
// doesn't have a directory listing endpoint, define the filenames here as
// you add them - keeps this dead simple with no extra backend route.
const MEDIA_FILES = [
  // "sim1.mp4", "sim2.mp4",
];

let mediaIndex = 0;
const videoEl = document.getElementById("media-video");
const emptyEl = document.getElementById("media-empty");

function playNextMedia() {
  if (MEDIA_FILES.length === 0) {
    videoEl.style.display = "none";
    emptyEl.style.display = "block";
    return;
  }
  emptyEl.style.display = "none";
  videoEl.style.display = "block";
  videoEl.src = `/static/media/${MEDIA_FILES[mediaIndex]}`;
  videoEl.play().catch(() => {});
  mediaIndex = (mediaIndex + 1) % MEDIA_FILES.length;
}

videoEl.addEventListener("ended", playNextMedia);
playNextMedia();
