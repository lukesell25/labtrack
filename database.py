"""
database.py - SQLite storage for LabTrack.

Everything goes through get_conn(); SQLite handles concurrent access fine at
this scale (5 users, a handful of changes a day) so we don't need anything
heavier than the standard library sqlite3 module.

This is a status board, not a timesheet: it keeps where each member is *now*
and nothing about when they got there. There is no event log and no
timestamp on any per-person row - see "No time tracking" in CLAUDE.md.
"""

import json
import logging
import sqlite3
import threading
from datetime import date
from pathlib import Path

log = logging.getLogger("labtrack.db")

DB_PATH = Path(__file__).parent / "labtrack.db"
MEMBERS_CONFIG = Path(__file__).parent / "config" / "members.json"

# The three places a member can be. 'away' is "at work but not in the lab" -
# the server room, a lecture hall - as distinct from 'out' (lunch, gone home).
# Where they went is the row's `note`, the same column a checkout comment
# uses, since both are the one line of free text a status carries.
ACTIONS = ("in", "away", "out")

# sqlite3 connections aren't thread-safe to share across threads by default;
# each thread (Flask request threads, the daily-reset thread) gets its own.
_local = threading.local()


def get_conn():
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB_PATH, timeout=10)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA foreign_keys = ON")
        # With WAL (set once, in init_db) NORMAL means a commit is a plain
        # write() to the -wal file and only checkpoints fsync. Under the
        # default rollback journal every commit fsynced twice, and on the Pi's
        # SD card an fsync also flushes whatever else is dirty (the persistent
        # journal, Chromium's profile) - so a check-in or a checkout note
        # could sit for a second or more before the kiosk heard back. The
        # trade is that a power cut can lose the last change or two, which on
        # a whiteboard that is reset daily is nothing. Per-connection, so it
        # lives here rather than in init_db.
        _local.conn.execute("PRAGMA synchronous = NORMAL")
    return _local.conn


def init_db():
    conn = get_conn()
    # Persistent: stored in the file, so this is a no-op after the first run.
    # WAL also stops the kiosk's 1.5s roster read from blocking a commit.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        -- Keyed by display name: config/members.json is just a list of names.
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT UNIQUE NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );

        -- One row per member who has ever set a status: where they are now,
        -- and nothing about when. `change_id` is a counter, not a clock - it
        -- lets a late checkout note find the checkout it belongs to
        -- (set_note) without recording a time. A member with no row reads as
        -- 'out'.
        CREATE TABLE IF NOT EXISTS presence (
            member_id INTEGER PRIMARY KEY REFERENCES members(id),
            status TEXT NOT NULL CHECK(status IN ('in', 'away', 'out')),
            note TEXT,
            change_id INTEGER NOT NULL
        );

        -- System bookkeeping, not per-person data. Holds the date of the last
        -- daily reset (reset_if_new_day).
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()
    _migrate_events_to_presence()
    _migrate_drop_manual_column()
    _migrate_members_by_name()
    sync_members_from_config()


def _migrate_events_to_presence():
    """
    Installs from when this was a timesheet kept an append-only `events` log,
    every row timestamped. Carry each member's *current* status across into
    `presence` - the last event's action, and its note while out or away -
    then drop the log outright, so the times it held are gone rather than
    merely unread. VACUUM afterwards because DROP TABLE leaves the old pages
    in the freelist, where the timestamps would otherwise survive in the file.

    Older installs may lack the note column; that reads as NULL. Idempotent -
    once `events` is gone this returns immediately.
    """
    conn = get_conn()
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ).fetchone()
    if not exists:
        return
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    note = "note" if "note" in cols else "NULL"
    last = conn.execute(
        f"""
        SELECT e.member_id, e.action, {note} AS note, e.id
        FROM events e
        WHERE e.id = (
            SELECT id FROM events WHERE member_id = e.member_id
            ORDER BY timestamp DESC, id DESC LIMIT 1
        )
        """
    ).fetchall()
    for r in last:
        conn.execute(
            "INSERT OR REPLACE INTO presence (member_id, status, note, change_id) "
            "VALUES (?, ?, ?, ?)",
            (r["member_id"], r["action"], r["note"] if r["action"] != "in" else None,
             r["id"]),
        )
    conn.execute("DROP TABLE events")
    conn.commit()
    conn.execute("VACUUM")
    log.info(
        "Dropped the old events log; carried %d member status(es) into presence",
        len(last),
    )


def _migrate_drop_manual_column():
    """
    presence.manual flagged a status set without a CAC tap ("NO CARD" on the
    board). The card reader is gone and every status is now set by hand, so
    the flag means nothing. Idempotent: skipped once the column is gone.
    """
    conn = get_conn()
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(presence)")}
    if "manual" in cols:
        conn.execute("ALTER TABLE presence DROP COLUMN manual")
        conn.commit()


def _migrate_members_by_name():
    """
    Installs from when members were identified by CAC keyed `members` on a
    hashed EDIPI (`edipi_hash`, or a plaintext `edipi` before that). Rebuild
    the table keyed on display_name instead, keeping every surviving row's id
    so presence rows stay attached to the right person, then VACUUM so the
    hashes don't survive in the freelist.

    display_name was not unique back then - a member re-added under a new
    hash left an inactive row with the same name behind. For each name the
    active row wins (else the newest), and the presence rows of the others
    are dropped with them.

    Foreign keys are switched off for the swap, because dropping `members`
    with them on would try to cascade into presence. Runs at import time,
    before any request thread exists, so nothing else is writing. Idempotent:
    keyed on whether the old column is still there.
    """
    conn = get_conn()
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(members)")}
    if "edipi_hash" not in cols and "edipi" not in cols:
        return

    rows = conn.execute(
        "SELECT id, display_name, active FROM members ORDER BY active DESC, id DESC"
    ).fetchall()
    keep, drop = {}, []
    for r in rows:
        name = r["display_name"].strip()
        if name in keep:
            drop.append(r["id"])
        else:
            keep[name] = r

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(
            """
            CREATE TABLE members_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT UNIQUE NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.executemany(
            "INSERT INTO members_new (id, display_name, active) VALUES (?, ?, ?)",
            [(r["id"], name, r["active"]) for name, r in keep.items()],
        )
        conn.executemany("DELETE FROM presence WHERE member_id = ?", [(i,) for i in drop])
        conn.execute("DROP TABLE members")
        conn.execute("ALTER TABLE members_new RENAME TO members")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("VACUUM")
    log.info(
        "Rebuilt members keyed by name (CAC identifiers removed); kept %d, merged away %d",
        len(keep), len(drop),
    )


def _roster_names(entries) -> list[str]:
    """
    Names from config/members.json's "members" list. Each entry is a plain
    string; an object with a "display_name" (the old CAC-era shape, whose
    edipi_hash is simply ignored now) is accepted too, so an un-updated file
    still loads. Blanks and repeats are skipped rather than raised: this runs
    from init_db() at import time, and a typo must not take the board down.
    """
    names, seen = [], set()
    for entry in entries:
        name = entry.get("display_name") if isinstance(entry, dict) else entry
        name = str(name or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def sync_members_from_config():
    """
    Reconcile the members table with config/members.json: add new people and
    deactivate anyone no longer listed. Safe to call repeatedly - it runs on
    every startup.

    Members are deactivated, never deleted. Their presence row references
    members.id with a foreign key, so deleting the member would fail; and
    re-adding someone flips the same row back to active rather than creating
    a second one. get_roster_status() filters on active, so this is all it
    takes for someone to disappear from the kiosk and the dashboard.

    Identity is the name, so renaming someone in the file reads as one person
    leaving and another joining - the new name starts out as 'out'.
    """
    if not MEMBERS_CONFIG.exists():
        return
    data = json.loads(MEMBERS_CONFIG.read_text())
    names = _roster_names(data.get("members", []))
    conn = get_conn()

    before = {
        r["display_name"]: r["active"]
        for r in conn.execute("SELECT display_name, active FROM members")
    }

    added, reactivated = [], []
    for name in names:
        if name not in before:
            added.append(name)
        elif not before[name]:
            reactivated.append(name)
        conn.execute(
            """
            INSERT INTO members (display_name, active) VALUES (?, 1)
            ON CONFLICT(display_name) DO UPDATE SET active = 1
            """,
            (name,),
        )

    # A roster that reads as empty is far more likely to be a broken edit -
    # a stray comma, a half-saved file, the wrong key name - than a lab with
    # nobody in it. Deactivating everyone on that basis would blank the board
    # and take a restart to undo, so treat it as bad input and change nothing.
    if not names:
        log.warning(
            "%s lists no members, so no one was deactivated - check the file "
            "if this wasn't deliberate. The existing roster is unchanged.",
            MEMBERS_CONFIG,
        )
        conn.commit()
        return

    placeholders = ",".join("?" * len(names))
    removed = [
        r["display_name"]
        for r in conn.execute(
            f"SELECT display_name FROM members "
            f"WHERE active = 1 AND display_name NOT IN ({placeholders})",
            names,
        )
    ]
    if removed:
        conn.execute(
            f"UPDATE members SET active = 0 WHERE display_name NOT IN ({placeholders})", names
        )
    conn.commit()

    for label, changed in (("added", added), ("reactivated", reactivated), ("deactivated", removed)):
        if changed:
            log.info("Roster sync %s %d member(s): %s", label, len(changed), ", ".join(changed))


def current_status(member_id: int) -> str:
    """Returns 'in', 'away' or 'out'. Defaults to 'out' if never set."""
    conn = get_conn()
    row = conn.execute(
        "SELECT status FROM presence WHERE member_id = ?", (member_id,)
    ).fetchone()
    return row["status"] if row else "out"


def set_status(member_id: int, action: str, location: str | None = None) -> dict:
    """
    Sets where a member is and returns what changed as a dict: {member_id,
    display_name, action, previous, change_id, location}. `previous` is the
    status this replaced, which is what lets the kiosk say "Back in lab"
    rather than "Checked in" for a return from away. change_id identifies
    this change, so the optional checkout note that follows can be attached
    to it via set_note() and to nothing later.

    Overwrites the member's one presence row - nothing is appended, and no
    time is recorded.

    `location` is where an 'away' member went, stored in the row's `note`
    column - the same column a checkout comment lands in, since both are the
    one line of free text a status can carry. It is ignored for 'in'.

    Raises ValueError for an unknown member or action, and for a change that
    would say nothing new ('in' while in, 'out' while out): the caller is
    working from a stale view of the board. 'away' while away is allowed -
    that is a change of location.
    """
    if action not in ACTIONS:
        raise ValueError(f"Unknown action {action!r}")
    conn = get_conn()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        raise ValueError(f"Unknown member_id {member_id}")

    previous = current_status(member_id)
    if action == previous and action != "away":
        raise ValueError(f"Already {action}")

    location = (location or "").strip() or None
    if action == "in":
        location = None

    change_id = conn.execute(
        "SELECT COALESCE(MAX(change_id), 0) + 1 FROM presence"
    ).fetchone()[0]
    conn.execute(
        "INSERT OR REPLACE INTO presence (member_id, status, note, change_id) "
        "VALUES (?, ?, ?, ?)",
        (member_id, action, location, change_id),
    )
    conn.commit()

    return {
        "member_id": member_id,
        "display_name": member["display_name"],
        "action": action,
        "previous": previous,
        "change_id": change_id,
        "location": location,
    }


def toggle_status(member_id: int) -> dict:
    """
    Flips a member between present and not: in -> out, and out *or away* ->
    in. Returns what set_status() does. This is the no-questions-asked path
    (the dev loop's curl, a check-in from another machine); anything that
    wants to say where someone went calls set_status() with 'away' and a
    location.
    """
    action = "out" if current_status(member_id) == "in" else "in"
    return set_status(member_id, action)


def set_note(change_id: int, note: str | None):
    """
    Attaches the optional "why are you out" comment a person can add right
    after checking out. Keyed on the checkout's change_id and only while that
    row is still 'out', so a stray or late request can't graft a note onto a
    later check-in or anyone else's row - it silently no-ops instead.
    """
    conn = get_conn()
    conn.execute(
        "UPDATE presence SET note = ? WHERE change_id = ? AND status = 'out'",
        (note, change_id),
    )
    conn.commit()


def reset_if_new_day() -> int:
    """
    Once per calendar day, set everyone who is in or away to out. Returns how
    many were reset (0 when it already ran today).

    Without timestamps the board can't show that someone's "In lab" is from
    yesterday, so a forgotten checkout would otherwise stand indefinitely;
    starting each day with everyone out is what keeps the board honest. Keyed
    on the date of the last reset in `meta` - one system-wide value, not a
    per-person time - rather than on process start, so a midday restart or
    self-reboot leaves statuses alone while a Pi that was off overnight still
    starts clean. Called at startup and by a once-a-minute thread in app.py.

    Yesterday's notes ("at lunch") go too, on everyone - they described a
    moment that is over.

    The very first run (no date stored yet - a fresh install, or the upgrade
    from the old events log) only records today: it must not wipe statuses
    that were carried across a midday upgrade.
    """
    today = date.today().isoformat()
    conn = get_conn()
    row = conn.execute("SELECT value FROM meta WHERE key = 'last_reset'").fetchone()
    if row and row["value"] == today:
        return 0
    count = 0
    if row:
        count = conn.execute(
            "UPDATE presence SET status = 'out', note = NULL "
            "WHERE status != 'out' OR note IS NOT NULL"
        ).rowcount
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_reset', ?)", (today,)
    )
    conn.commit()
    if count:
        log.info("Daily reset: cleared %d member status(es)", count)
    return count


def get_roster_status():
    """
    All active members with their current status, for the kiosk/dashboard.

    Alphabetical, and deliberately stable: the kiosk is driven with arrow
    keys, so people learn where their card sits on the strip ("three to the
    right"), and a card that moved whenever its status changed would break
    that - and drag the keyboard highlight along with it.
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT m.id, m.display_name, p.status, p.note
        FROM members m LEFT JOIN presence p ON p.member_id = m.id
        WHERE m.active = 1
        ORDER BY m.display_name COLLATE NOCASE
        """
    ).fetchall()
    return [
        {
            "id": r["id"],
            "display_name": r["display_name"],
            "status": r["status"] or "out",
            # A checkout comment or an away location; never shown on an 'in'
            # (set_status already clears it there - this is belt and braces).
            "note": r["note"] if r["status"] in ("out", "away") else None,
        }
        for r in rows
    ]
