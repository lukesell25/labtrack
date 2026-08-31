const POLL_MS = 5000;

// Every render below rebuilds its section with innerHTML, which costs a
// layout + paint of that whole section. The data behind them only changes
// when someone taps a card, so most 5s refreshes have nothing new to draw -
// skipIfUnchanged() lets those bail out before touching the DOM.
const lastRendered = {};
function skipIfUnchanged(key, data) {
  const json = JSON.stringify(data);
  if (json === lastRendered[key]) return true;
  lastRendered[key] = json;
  return false;
}

// 24-hour time, matching the kiosk. hourCycle "h23" states the convention
// outright rather than leaning on hour12:false - see main.js.
const TIME_OPTS = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };

function fmtDateTime(iso) {
  const d = new Date(iso);
  return d.toLocaleString([], { month: "short", day: "numeric", ...TIME_OPTS });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : str;
  return div.innerHTML;
}

// "No card" is the mark left by the kiosk's click-to-toggle path (and by
// /api/manual-toggle generally): the person was recorded without a card
// being read. Same wording and colour as the kiosk board, since it means the
// same thing in both places.
const NO_CARD = '<span class="roster__nocard">No card</span>';

function noteText(manual, note) {
  const parts = [];
  if (manual) parts.push(NO_CARD);
  if (note) parts.push(escapeHtml(note));
  return parts.join(" · ");
}

function noteLine(manual, note) {
  const text = noteText(manual, note);
  return text ? `<div class="roster__note">${text}</div>` : "";
}

function renderRoster(roster) {
  if (skipIfUnchanged("roster", roster)) return;
  const el = document.getElementById("dash-roster");
  el.innerHTML = roster.map(m => `
    <div class="roster__card ${m.status === 'in' ? 'is-in' : ''}">
      <div class="roster__ring"></div>
      <div class="roster__meta">
        <div class="roster__name">${escapeHtml(m.display_name)}</div>
        <div class="roster__status">${m.status === 'in' ? 'In lab' : 'Out'}${m.since ? ' · since ' + fmtDateTime(m.since) : ''}</div>
        ${noteLine(m.manual, m.note)}
      </div>
    </div>
  `).join("");
}

function renderHours(hours) {
  if (skipIfUnchanged("hours", hours)) return;
  const tbody = document.querySelector("#hours-table tbody");
  tbody.innerHTML = Object.entries(hours)
    .sort((a, b) => b[1] - a[1])
    .map(([name, hrs]) => `<tr><td>${escapeHtml(name)}</td><td>${hrs}</td></tr>`)
    .join("");
}

function renderEvents(events) {
  if (skipIfUnchanged("events", events)) return;
  const tbody = document.querySelector("#events-table tbody");
  tbody.innerHTML = events.map(e => `
    <tr>
      <td>${fmtDateTime(e.timestamp)}</td>
      <td>${escapeHtml(e.display_name)}</td>
      <td class="action-${e.action}">${e.action === 'in' ? 'Checked in' : 'Checked out'}</td>
      <td>${noteText(e.manual, e.note) || '—'}</td>
      <td class="dash__table-action">
        <button class="row-delete" data-event-id="${e.id}"
                title="Delete this event" aria-label="Delete this event">&times;</button>
      </td>
    </tr>
  `).join("");
}

// --- admin actions ---------------------------------------------------
// The log is append-only everywhere else; these two are the exceptions, for
// a duplicate tap or a fresh start. Both go through confirmation first - the
// row this deletes is the only record that the person was ever here.

function setStatus(text, isError) {
  const el = document.getElementById("admin-status");
  el.textContent = text;
  el.classList.toggle("is-error", !!isError);
}

async function deleteEvent(id, label) {
  if (!window.confirm(`Delete this event?\n\n${label}\n\nThis cannot be undone.`)) return;
  try {
    const res = await fetch(`/api/events/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    setStatus(`Deleted event: ${label}`);
    refresh();
  } catch (e) {
    setStatus(`Could not delete that event: ${e.message}`, true);
  }
}

// Delegated, since renderEvents() replaces the whole tbody on every change.
document.querySelector("#events-table tbody").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".row-delete");
  if (!btn) return;
  const row = btn.closest("tr");
  const cells = row.querySelectorAll("td");
  deleteEvent(btn.dataset.eventId, `${cells[1].textContent} · ${cells[2].textContent} · ${cells[0].textContent}`);
});

// The typed DELETE is checked server-side too (see api_clear_events); this
// only saves a round trip and points at the box when it's empty.
document.getElementById("clear-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = document.getElementById("clear-confirm");
  const btn = document.getElementById("clear-btn");
  if (input.value.trim() !== "DELETE") {
    setStatus('Type DELETE in the box to confirm.', true);
    input.focus();
    return;
  }
  btn.disabled = true;
  try {
    const res = await fetch("/api/events/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "DELETE" }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    input.value = "";
    setStatus(`Cleared ${data.deleted} event(s). Backup saved on the Pi as ${data.backup}.`);
    refresh();
  } catch (e) {
    setStatus(`Could not clear the log: ${e.message}`, true);
  } finally {
    btn.disabled = false;
  }
});

// Refreshes are numbered so a reply that arrives after a newer one is
// dropped. The 5s poll used to be the only caller and order didn't matter;
// the admin actions above call refresh() the instant their request returns,
// so a poll issued before the delete can land after it - repainting the row
// that was just removed, and caching that stale JSON in lastRendered where
// it would sit until the data changed again. Same reasoning as poll() in
// main.js.
let refreshSeq = 0;

async function refresh() {
  const seq = ++refreshSeq;
  try {
    const [stateRes, hoursRes, eventsRes] = await Promise.all([
      fetch("/api/state"),
      fetch("/api/weekly-hours"),
      fetch("/api/events?limit=30"),
    ]);
    const [state, hours, events] = await Promise.all([
      stateRes.json(), hoursRes.json(), eventsRes.json(),
    ]);
    if (seq !== refreshSeq) return;
    renderRoster(state.roster);
    renderHours(hours);
    renderEvents(events);
    document.getElementById("updated-at").textContent =
      new Date().toLocaleTimeString([], { ...TIME_OPTS, second: "2-digit" });
  } catch (e) {
    console.error("dashboard refresh failed", e);
  }
}

setInterval(refresh, POLL_MS);
refresh();
