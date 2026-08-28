"""
database.py - SQLite storage for LabTrack.

Everything goes through get_conn(); SQLite handles concurrent access fine at
this scale (5 users, a handful of events a day) so we don't need anything
heavier than the standard library sqlite3 module.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("labtrack.db")

DB_PATH = Path(__file__).parent / "labtrack.db"
MEMBERS_CONFIG = Path(__file__).parent / "config" / "members.json"

# sqlite3 connections aren't thread-safe to share across threads by default;
# each thread (Flask request thread, CAC reader thread) gets its own.
_local = threading.local()


def get_conn():
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB_PATH, timeout=10)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA foreign_keys = ON")
    return _local.conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            edipi TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(id),
            action TEXT NOT NULL CHECK(action IN ('in', 'out')),
            timestamp TEXT NOT NULL,
            note TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_events_member_time
            ON events(member_id, timestamp);
        """
    )
    conn.commit()
    _migrate_add_note_column()
    sync_members_from_config()


def _migrate_add_note_column():
    """
    Existing installs created the events table before the note column
    existed - CREATE TABLE IF NOT EXISTS above is a no-op on those, so add
    the column by hand if it's missing. Safe to run every startup.
    """
    conn = get_conn()
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()]
    if "note" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN note TEXT")
        conn.commit()


def sync_members_from_config():
    """
    Reconcile the members table with config/members.json: add new people,
    update renamed ones, and deactivate anyone no longer listed. Safe to
    call repeatedly - it runs on every startup.

    Members are deactivated, never deleted. Their events reference members.id
    with a foreign key, so deleting the row would either fail or orphan the
    attendance history it exists to preserve; `active = 0` takes them off the
    board while leaving the log intact. get_roster_status() and
    get_weekly_hours() both filter on active, so this is all it takes for
    someone to disappear from the kiosk and the dashboard.
    """
    if not MEMBERS_CONFIG.exists():
        return
    data = json.loads(MEMBERS_CONFIG.read_text())
    entries = data.get("members", [])
    conn = get_conn()

    before = {r["edipi"]: r for r in conn.execute("SELECT edipi, display_name, active FROM members")}

    config_edipis = set()
    added, reactivated = [], []
    for m in entries:
        edipi = str(m["edipi"]).strip()
        name = m["display_name"].strip()
        config_edipis.add(edipi)
        previous = before.get(edipi)
        if previous is None:
            added.append(name)
        elif not previous["active"]:
            reactivated.append(name)
        conn.execute(
            """
            INSERT INTO members (edipi, display_name, active)
            VALUES (?, ?, 1)
            ON CONFLICT(edipi) DO UPDATE SET
                display_name = excluded.display_name,
                active = 1
            """,
            (edipi, name),
        )

    # A roster that reads as empty is far more likely to be a broken edit -
    # a stray comma, a half-saved file, the wrong key name - than a lab with
    # nobody in it. Deactivating everyone on that basis would blank the board
    # and take a restart to undo, so treat it as bad input and change nothing.
    if not config_edipis:
        log.warning(
            "%s lists no members, so no one was deactivated - check the file "
            "if this wasn't deliberate. The existing roster is unchanged.",
            MEMBERS_CONFIG,
        )
        conn.commit()
        return

    placeholders = ",".join("?" * len(config_edipis))
    params = tuple(config_edipis)
    removed = [
        r["display_name"]
        for r in conn.execute(
            f"SELECT display_name FROM members "
            f"WHERE active = 1 AND edipi NOT IN ({placeholders})",
            params,
        )
    ]
    if removed:
        conn.execute(
            f"UPDATE members SET active = 0 WHERE edipi NOT IN ({placeholders})", params
        )
    conn.commit()

    for label, names in (("added", added), ("reactivated", reactivated), ("deactivated", removed)):
        if names:
            log.info("Roster sync %s %d member(s): %s", label, len(names), ", ".join(names))


def get_member_by_edipi(edipi: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM members WHERE edipi = ? AND active = 1", (edipi,)
    ).fetchone()
    return dict(row) if row else None


def _last_event_for_member(conn, member_id: int):
    return conn.execute(
        """
        SELECT * FROM events WHERE member_id = ?
        ORDER BY timestamp DESC, id DESC LIMIT 1
        """,
        (member_id,),
    ).fetchone()


def current_status(member_id: int) -> str:
    """Returns 'in' or 'out'. Defaults to 'out' if no events yet."""
    conn = get_conn()
    row = _last_event_for_member(conn, member_id)
    return row["action"] if row else "out"


def toggle_checkin(member_id: int) -> dict:
    """
    Flips a member's status (in <-> out) and logs the event.
    Returns the event dict: {member_id, display_name, action, timestamp,
    checkin_event_id}. checkin_event_id is the events table row id, used to
    attach an optional note afterward via set_event_note().
    """
    conn = get_conn()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        raise ValueError(f"Unknown member_id {member_id}")

    new_action = "out" if current_status(member_id) == "in" else "in"
    now = datetime.now().isoformat(timespec="seconds")

    cursor = conn.execute(
        "INSERT INTO events (member_id, action, timestamp) VALUES (?, ?, ?)",
        (member_id, new_action, now),
    )
    conn.commit()

    return {
        "member_id": member_id,
        "display_name": member["display_name"],
        "action": new_action,
        "timestamp": now,
        "checkin_event_id": cursor.lastrowid,
    }


def set_event_note(event_id: int, note: str | None):
    """
    Attaches an optional note to an event - used for the "why are you out"
    comment a person can add right after checking out. Only applies to
    'out' events; silently no-ops otherwise so a stray/late request can't
    graft a note onto an unrelated check-in row.
    """
    conn = get_conn()
    conn.execute(
        "UPDATE events SET note = ? WHERE id = ? AND action = 'out'",
        (note, event_id),
    )
    conn.commit()


def get_roster_status():
    """All active members with their current status, for the kiosk/dashboard."""
    conn = get_conn()
    members = conn.execute(
        "SELECT * FROM members WHERE active = 1 ORDER BY display_name"
    ).fetchall()
    rows = []
    for m in members:
        last = _last_event_for_member(conn, m["id"])
        status = last["action"] if last else "out"
        rows.append(
            (
                last,
                {
                    "id": m["id"],
                    "display_name": m["display_name"],
                    "status": status,
                    "since": last["timestamp"] if last else None,
                    # Only surface the note while they're actually out - it's tied
                    # to that specific checkout, not a persistent profile field.
                    "note": last["note"] if (last and status == "out") else None,
                },
            )
        )

    # Most recently active member first, so the newest check-in/out lands
    # leftmost on the board. Timestamps are ISO strings, so they sort
    # chronologically as text; the event id breaks ties within a second.
    # Members who have never tapped sort last and, since sort() keeps the
    # order of equal keys even when reversed, stay alphabetical among
    # themselves (the SELECT above is ordered by display_name).
    rows.sort(key=lambda r: (r[0]["timestamp"], r[0]["id"]) if r[0] else ("", 0),
              reverse=True)
    return [entry for _, entry in rows]


def get_recent_events(limit: int = 50):
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT events.*, members.display_name
        FROM events JOIN members ON members.id = events.member_id
        ORDER BY events.timestamp DESC, events.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_weekly_hours():
    """
    Rough hours-in-lab per member over the last 7 days, computed by pairing
    consecutive in/out events. An unmatched trailing 'in' (still checked in)
    counts up to now.
    """
    conn = get_conn()
    since = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    members = conn.execute("SELECT * FROM members WHERE active = 1").fetchall()

    results = {}
    for m in members:
        rows = conn.execute(
            """
            SELECT action, timestamp FROM events
            WHERE member_id = ? AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (m["id"], since),
        ).fetchall()

        total = timedelta()
        pending_in = None
        for r in rows:
            ts = datetime.fromisoformat(r["timestamp"])
            if r["action"] == "in":
                pending_in = ts
            elif r["action"] == "out" and pending_in is not None:
                total += ts - pending_in
                pending_in = None
        if pending_in is not None:
            total += datetime.now() - pending_in

        results[m["display_name"]] = round(total.total_seconds() / 3600, 1)

    return results
