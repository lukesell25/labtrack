const POLL_MS = 5000;

// renderRoster() rebuilds the section with innerHTML, which costs a layout +
// paint of the whole section. The data behind it only changes when someone
// checks in or out, so most 5s refreshes have nothing new to draw -
// skipIfUnchanged() lets those bail out before touching the DOM.
const lastRendered = {};
function skipIfUnchanged(key, data) {
  const json = JSON.stringify(data);
  if (json === lastRendered[key]) return true;
  lastRendered[key] = json;
  return false;
}

// 24-hour time, matching the kiosk. hourCycle "h23" states the convention
// outright rather than leaning on hour12:false - see main.js. Only used for
// "Updated at", i.e. when this page last refreshed - never for anybody's
// status.
const TIME_OPTS = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : str;
  return div.innerHTML;
}

// Labels per status, matching the kiosk (main.js). The note line carries a
// checkout comment or, for away, the location - the server puts whichever
// applies in `note`.
const STATUS_LABELS = { in: "In lab", away: "Away", out: "Out" };

function statusClass(status) {
  return status === "in" ? "is-in" : status === "away" ? "is-away" : "";
}

function noteLine(note) {
  return note ? `<div class="roster__note">${escapeHtml(note)}</div>` : "";
}

function renderRoster(roster) {
  if (skipIfUnchanged("roster", roster)) return;
  const el = document.getElementById("dash-roster");
  el.innerHTML = roster.map(m => `
    <div class="roster__card ${statusClass(m.status)}">
      <div class="roster__ring"></div>
      <div class="roster__meta">
        <div class="roster__name">${escapeHtml(m.display_name)}</div>
        <div class="roster__status">${STATUS_LABELS[m.status] || escapeHtml(m.status)}</div>
        ${noteLine(m.note)}
      </div>
    </div>
  `).join("");
}

async function refresh() {
  try {
    const res = await fetch("/api/state");
    const state = await res.json();
    renderRoster(state.roster);
    document.getElementById("updated-at").textContent =
      new Date().toLocaleTimeString([], { ...TIME_OPTS, second: "2-digit" });
  } catch (e) {
    console.error("dashboard refresh failed", e);
  }
}

setInterval(refresh, POLL_MS);
refresh();
