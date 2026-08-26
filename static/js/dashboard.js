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

function fmtDateTime(iso) {
  const d = new Date(iso);
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : str;
  return div.innerHTML;
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
        ${m.note ? `<div class="roster__note">${escapeHtml(m.note)}</div>` : ''}
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
      <td>${e.note ? escapeHtml(e.note) : '—'}</td>
    </tr>
  `).join("");
}

async function refresh() {
  try {
    const [stateRes, hoursRes, eventsRes] = await Promise.all([
      fetch("/api/state"),
      fetch("/api/weekly-hours"),
      fetch("/api/events?limit=30"),
    ]);
    renderRoster((await stateRes.json()).roster);
    renderHours(await hoursRes.json());
    renderEvents(await eventsRes.json());
    document.getElementById("updated-at").textContent = new Date().toLocaleTimeString();
  } catch (e) {
    console.error("dashboard refresh failed", e);
  }
}

setInterval(refresh, POLL_MS);
refresh();
