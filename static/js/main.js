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

// --- slide rotation --------------------------------------------------
// The media panel cycles a single combined playlist: every objective from
// config/objectives.json as a text slide, followed by every video in
// MEDIA_FILES. Videos advance when they finish; text slides advance on a
// timer. Slides swap instantly - a crossfade would mean compositing two
// full-panel layers on every rotation (see Performance in CLAUDE.md).
//
// Since the app doesn't have a directory listing endpoint, list media
// filenames here as you add them to static/media/ - keeps this dead simple
// with no extra backend route.
const MEDIA_FILES = [
  // "sim1.mp4", "sim2.mp4",
];

const SLIDE_MS = 12000;            // how long one objective slide stays up
const VIDEO_ERROR_RETRY_MS = 3000;

const videoEl = document.getElementById("media-video");
const slideEl = document.getElementById("media-slide");
const slideTextEl = document.getElementById("slide-text");
const emptyEl = document.getElementById("media-empty");

let objectives = [];
let rotationIndex = 0;
let rotationTimer = null;
let rotationStarted = false;
// Which kind of item is on screen right now. The video element's events fire
// asynchronously and can arrive after the rotation has already moved on (a
// 404 on a media file reports its error a beat later), so the handlers below
// check this before acting - otherwise a stale event cuts a text slide short.
let activeType = null;

function buildPlaylist() {
  return [
    ...objectives.map(text => ({ type: "objective", text })),
    ...MEDIA_FILES.map(file => ({ type: "video", file })),
  ];
}

function showNextSlide() {
  clearTimeout(rotationTimer);

  const playlist = buildPlaylist();
  if (playlist.length === 0) {
    videoEl.style.display = "none";
    slideEl.style.display = "none";
    emptyEl.style.display = "block";
    return;
  }

  const item = playlist[rotationIndex % playlist.length];
  rotationIndex = (rotationIndex + 1) % playlist.length;
  activeType = item.type;
  emptyEl.style.display = "none";

  if (item.type === "objective") {
    videoEl.pause();  // stop decoding while a text slide is up
    videoEl.style.display = "none";
    if (slideTextEl.textContent !== item.text) slideTextEl.textContent = item.text;
    slideEl.style.display = "flex";
    rotationTimer = setTimeout(showNextSlide, SLIDE_MS);
    return;
  }

  slideEl.style.display = "none";
  videoEl.style.display = "block";
  // With a single item there is nothing to advance to, so let the element
  // loop natively rather than re-fetching and re-decoding the same file
  // every time it ends. Note this is also why "loop" is not hardcoded in
  // index.html: with it always on, "ended" never fires and the rotation
  // could never move past the first video.
  videoEl.loop = playlist.length === 1;
  videoEl.src = `/static/media/${item.file}`;
  videoEl.play().catch(() => {});
}

function restartRotation() {
  rotationStarted = true;
  rotationIndex = 0;
  showNextSlide();
}

videoEl.addEventListener("ended", () => {
  if (activeType !== "video" || videoEl.loop) return;
  showNextSlide();
});

// A missing or undecodable file shouldn't strand the board on a black
// panel. Wait before moving on so a playlist of entirely broken files
// can't spin in a tight loop.
videoEl.addEventListener("error", () => {
  if (activeType !== "video") return;
  console.error("media failed to play:", videoEl.currentSrc);
  clearTimeout(rotationTimer);
  rotationTimer = setTimeout(showNextSlide, VIDEO_ERROR_RETRY_MS);
});

async function loadObjectives() {
  try {
    const res = await fetch("/api/objectives");
    const data = await res.json();
    const json = JSON.stringify(data.objectives);
    if (json === loadObjectives._last) return;
    loadObjectives._last = json;
    objectives = data.objectives || [];
    // Only reached when objectives.json actually changed, so restarting the
    // rotation here costs nothing in the steady state.
    restartRotation();
  } catch (e) {
    console.error("failed to load objectives", e);
    // Objectives are unavailable, but any videos should still play.
    if (!rotationStarted) restartRotation();
  }
}

// The rotation is started by the first loadObjectives() rather than here, so
// a video slot never gets loaded and then immediately abandoned when the
// objectives arrive a moment later.
loadObjectives();
setInterval(loadObjectives, 60000); // pick up edits to objectives.json without a restart
