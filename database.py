"""
database.py - SQLite storage for LabTrack.

Everything goes through get_conn(); SQLite handles concurrent access fine at
this scale (5 users, a handful of events a day) so we don't need anything
heavier than the standard library sqlite3 module.
"""

import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

import identity

log = logging.getLogger("labtrack.db")

DB_PATH = Path(__file__).parent / "labtrack.db"
MEMBERS_CONFIG = Path(__file__).parent / "config" / "members.json"

# What a pre-hashing members.edipi value looks like, for the migration below.
_PLAINTEXT_EDIPI = re.compile(r"^\d{10}$")

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
            edipi_hash TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(id),
            action TEXT NOT NULL CHECK(action IN ('in', 'out')),
            timestamp TEXT NOT NULL,
            note TEXT,
            manual INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_events_member_time
            ON events(member_id, timestamp);
        """
    )
    conn.commit()
    _migrate_add_note_column()
    _migrate_add_manual_column()
    _migrate_hash_edipi_column()
    _warn_if_roster_key_lost()
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


def _migrate_add_manual_column():
    """
    Same pattern as the note column above: installs predating the kiosk's
    click-to-toggle path have no `manual` flag, and CREATE TABLE IF NOT
    EXISTS won't add one. Existing rows default to 0 - every event written
    before this column existed came from a card tap, which is what 0 means.
    Safe to run every startup.
    """
    conn = get_conn()
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()]
    if "manual" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN manual INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def _migrate_hash_edipi_column():
    """
    Installs predating the hashed roster stored the raw 10-digit EDIPI in
    members.edipi. Rename the column and replace each value with its hash,
    in place.

    In place rather than rebuilding the table, because that keeps members.id
    stable - events.member_id is a foreign key into it, so reinserting would
    either fail or detach the attendance history the table exists to keep.
    It also takes the plaintext out of the live database, which is half the
    point of hashing it in the first place. Idempotent (after one pass
    nothing matches _PLAINTEXT_EDIPI), so it is safe on every startup.
    """
    conn = get_conn()
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(members)").fetchall()]
    if "edipi" in cols and "edipi_hash" not in cols:
        conn.execute("ALTER TABLE members RENAME COLUMN edipi TO edipi_hash")
        conn.commit()

    stale = [
        r for r in conn.execute("SELECT id, edipi_hash FROM members")
        if _PLAINTEXT_EDIPI.match(r["edipi_hash"])
    ]
    if not stale:
        return
    for r in stale:
        conn.execute(
            "UPDATE members SET edipi_hash = ? WHERE id = ?",
            (identity.hash_edipi(r["edipi_hash"]), r["id"]),
        )
    conn.commit()
    # An UPDATE leaves the old page content in the freelist, so the plaintext
    # can outlive the rows that held it; VACUUM rewrites the file without it.
    # One-time - this whole branch is skipped once nothing is stale.
    conn.execute("VACUUM")
    log.info("Hashed %d plaintext EDIPI(s) in the members table", len(stale))


def _warn_if_roster_key_lost():
    """
    Having had to *generate* the roster key is unremarkable on a first run and
    a disaster on an existing one: hashes written with the old key can never
    match a tap hashed with the new one, so every member quietly stops being
    recognised while the board still looks fine. Nothing can recover that
    automatically - the point is only that it says so rather than presenting
    as an empty lab. Rows still holding plaintext don't count: those are a
    pre-hashing install being upgraded, which is the normal path.

    Two shapes of the same mistake, because a fresh clone has no rows yet:
    hashes already in the database, and hashes already in members.json.

    Pending placeholders are excluded from both counts. They were not made by
    any key, so they say nothing about whether this one is the right one, and
    counting them would raise this alarm over a roster that has simply not been
    given its EDIPIs yet.
    """
    identity.load_key()
    if not identity.key_was_generated:
        return

    conn = get_conn()
    hashed = [
        r for r in conn.execute("SELECT edipi_hash FROM members")
        if not _PLAINTEXT_EDIPI.match(r["edipi_hash"])
        and not identity.is_pending(r["edipi_hash"])
    ]
    if hashed:
        log.error(
            "%d member(s) were hashed with a different roster key than the one "
            "at %s, which was just generated fresh - no card will be recognised. "
            "Restore the old key file from backup, or re-add everyone with "
            "scripts/add-member.py.",
            len(hashed),
            identity.KEY_PATH,
        )
        return

    # A fresh install has no rows to compare a new key against, so the check
    # above sees nothing - but config/members.json arrives from git already
    # full of hashes made on whatever machine those people were added on, and
    # a key generated here cannot reproduce them. That combination is the
    # quietest failure this system has: the roster syncs, the board shows
    # everyone, and every single tap comes back "Card not recognized".
    try:
        entries = json.loads(MEMBERS_CONFIG.read_text()).get("members", [])
    except (OSError, ValueError):
        return  # a missing or unparseable roster is sync's problem, not this one
    prehashed = [
        m for m in entries
        if m.get("edipi_hash") and not identity.is_pending(m["edipi_hash"])
    ]
    if prehashed:
        log.error(
            "%s lists %d member(s) already hashed, but the roster key at %s was "
            "just generated here - it cannot match hashes made elsewhere, so the "
            "board will look right and no card will be recognised. Copy over the "
            "key those hashes were made with (it is gitignored, so it never "
            "arrives with a clone), or re-add everyone with "
            "scripts/add-member.py.",
            MEMBERS_CONFIG,
            len(prehashed),
            identity.KEY_PATH,
        )


def _entry_hash(entry: dict, name: str) -> str:
    """
    A roster entry identifies someone by edipi_hash. Two hand-edited shapes
    are accepted as well, both with a WARNING naming the person, because a
    half-finished edit shouldn't silently drop somebody off the board and it
    certainly shouldn't stop the app from starting - this runs from init_db()
    at import time, so anything raised here takes the whole board down.

    A plaintext `edipi` is hashed on the fly, but it means the number is
    sitting in the config file, so say so until it gets converted. An entry
    with neither field is someone whose EDIPI hasn't arrived yet, and becomes
    a pending placeholder - see identity.pending_hash() and
    scripts/add-member.py --pending, which is the deliberate way to do this.
    """
    if entry.get("edipi_hash"):
        return str(entry["edipi_hash"]).strip()
    if not entry.get("edipi"):
        log.warning(
            "%s lists %s with no edipi_hash, so they are on the board as a "
            "pending member: they can be checked in by clicking their name, "
            "but no card will match them. Finish the entry with "
            "scripts/add-member.py --replace \"%s\" once you have their EDIPI.",
            MEMBERS_CONFIG,
            name,
            name,
        )
        return identity.pending_hash(name)
    log.warning(
        "%s lists a plaintext EDIPI for %s. It works, but the number is stored "
        "in the clear - run scripts/add-member.py to replace that entry with an "
        "edipi_hash.",
        MEMBERS_CONFIG,
        name,
    )
    return identity.hash_edipi(entry["edipi"])


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

    before = {
        r["edipi_hash"]: r
        for r in conn.execute("SELECT edipi_hash, display_name, active FROM members")
    }

    config_hashes = set()
    added, reactivated = [], []
    for m in entries:
        name = m["display_name"].strip()
        edipi_hash = _entry_hash(m, name)
        config_hashes.add(edipi_hash)
        previous = before.get(edipi_hash)
        if previous is None:
            added.append(name)
        elif not previous["active"]:
            reactivated.append(name)
        conn.execute(
            """
            INSERT INTO members (edipi_hash, display_name, active)
            VALUES (?, ?, 1)
            ON CONFLICT(edipi_hash) DO UPDATE SET
                display_name = excluded.display_name,
                active = 1
            """,
            (edipi_hash, name),
        )

    # A roster that reads as empty is far more likely to be a broken edit -
    # a stray comma, a half-saved file, the wrong key name - than a lab with
    # nobody in it. Deactivating everyone on that basis would blank the board
    # and take a restart to undo, so treat it as bad input and change nothing.
    if not config_hashes:
        log.warning(
            "%s lists no members, so no one was deactivated - check the file "
            "if this wasn't deliberate. The existing roster is unchanged.",
            MEMBERS_CONFIG,
        )
        conn.commit()
        return

    placeholders = ",".join("?" * len(config_hashes))
    params = tuple(config_hashes)
    removed = [
        r["display_name"]
        for r in conn.execute(
            f"SELECT display_name FROM members "
            f"WHERE active = 1 AND edipi_hash NOT IN ({placeholders})",
            params,
        )
    ]
    if removed:
        conn.execute(
            f"UPDATE members SET active = 0 WHERE edipi_hash NOT IN ({placeholders})", params
        )
    conn.commit()

    for label, names in (("added", added), ("reactivated", reactivated), ("deactivated", removed)):
        if names:
            log.info("Roster sync %s %d member(s): %s", label, len(names), ", ".join(names))


def get_member_by_hash(edipi_hash: str):
    """Look a member up by identity.hash_edipi(edipi) - see app._handle_tap()."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM members WHERE edipi_hash = ? AND active = 1", (edipi_hash,)
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


def toggle_checkin(member_id: int, manual: bool = False) -> dict:
    """
    Flips a member's status (in <-> out) and logs the event.
    Returns the event dict: {member_id, display_name, action, timestamp,
    checkin_event_id, manual}. checkin_event_id is the events table row id,
    used to attach an optional note afterward via set_event_note().

    manual=True records that no card was involved - the kiosk's click-a-name
    path and /api/manual-toggle. It is stored per event rather than derived
    later because nothing else in the row distinguishes the two, and the
    board says so beside the person's name (see get_roster_status).
    """
    conn = get_conn()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        raise ValueError(f"Unknown member_id {member_id}")

    new_action = "out" if current_status(member_id) == "in" else "in"
    now = datetime.now().isoformat(timespec="seconds")

    cursor = conn.execute(
        "INSERT INTO events (member_id, action, timestamp, manual) VALUES (?, ?, ?, ?)",
        (member_id, new_action, now, 1 if manual else 0),
    )
    conn.commit()

    return {
        "member_id": member_id,
        "display_name": member["display_name"],
        "action": new_action,
        "timestamp": now,
        "checkin_event_id": cursor.lastrowid,
        "manual": bool(manual),
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


def delete_event(event_id: int) -> bool:
    """
    Remove a single event row - a duplicate tap, or somebody who checked in
    on the wrong name. Returns False if there was no such row.

    The events table is append-only everywhere else in this app for a reason
    (it *is* the attendance record), so this logs what it removed at WARNING:
    once the row is gone the journal is the only remaining evidence that it
    ever existed. Deleting an event re-derives everything computed from the
    log - the member's current status is whatever event is now most recent,
    and get_weekly_hours() re-pairs around the hole - so removing one half of
    an in/out pair leaves the other half unmatched. That is the same
    positional pairing scripts/add-event.py warns about when inserting.
    """
    conn = get_conn()
    row = conn.execute(
        """
        SELECT events.action, events.timestamp, members.display_name
        FROM events JOIN members ON members.id = events.member_id
        WHERE events.id = ?
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()
    log.warning(
        "Deleted event %d: %s checked %s at %s",
        event_id, row["display_name"], row["action"], row["timestamp"],
    )
    return True


def backup_db() -> Path:
    """
    Snapshot the database beside itself as labtrack-<stamp>.bak, through
    sqlite's own backup API rather than a file copy: other threads hold live
    connections (the CAC reader writes from one), and copying the file out
    from under an in-flight transaction can capture a torn page plus none of
    the -wal/-journal that would repair it. The backup API takes a consistent
    snapshot while they run.

    Backups are gitignored and never pruned automatically - clearing the log
    is rare and a stale copy is the whole point of having one.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = DB_PATH.with_name(f"{DB_PATH.stem}-{stamp}.bak")
    target = sqlite3.connect(dest)
    try:
        get_conn().backup(target)
    finally:
        target.close()
    return dest


def clear_events() -> dict:
    """
    Empty the attendance log, keeping the roster. Returns how many rows went
    and the name of the backup taken first.

    Members are deliberately untouched: they are synced from
    config/members.json, so deleting them here would only have them come
    straight back on the next startup, and events reference members.id.
    Wiping the log is enough to reset the board - every member reads as 'out'
    once nothing is more recent, and weekly hours fall to zero.

    A backup is taken unconditionally rather than offered as an option. This
    is the one irreversible action in the app, it is a button on a page
    anyone with the shared password can reach, and the thing it destroys is
    the record this system exists to keep.
    """
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    backup = backup_db()
    conn.execute("DELETE FROM events")
    conn.commit()
    log.warning("Cleared %d event(s) from the log - backup saved to %s", count, backup)
    return {"deleted": count, "backup": backup.name}


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
                    # Whether the event that put them in this state was a click
                    # rather than a tap. Unlike the note, this applies to both
                    # directions: an unverified check-*in* is the half worth
                    # flagging, since nobody's card was ever present for it.
                    "manual": bool(last["manual"]) if last else False,
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
