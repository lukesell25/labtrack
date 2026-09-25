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
import re
import sqlite3
import threading
from datetime import date
from pathlib import Path

import identity

log = logging.getLogger("labtrack.db")

DB_PATH = Path(__file__).parent / "labtrack.db"
MEMBERS_CONFIG = Path(__file__).parent / "config" / "members.json"

# What a pre-hashing members.edipi value looks like, for the migration below.
_PLAINTEXT_EDIPI = re.compile(r"^\d{10}$")

# The three places a member can be. 'away' is "at work but not in the lab" -
# the server room, a lecture hall - as distinct from 'out' (lunch, gone home).
# Where they went is the row's `note`, the same column a checkout comment
# uses, since both are the one line of free text a status carries.
ACTIONS = ("in", "away", "out")

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

        -- One row per member who has ever set a status: where they are now,
        -- and nothing about when. `change_id` is a counter, not a clock - it
        -- orders the board (most recently changed first) and lets a late
        -- checkout note find the checkout it belongs to (set_note), without
        -- recording a time. A member with no row reads as 'out'.
        CREATE TABLE IF NOT EXISTS presence (
            member_id INTEGER PRIMARY KEY REFERENCES members(id),
            status TEXT NOT NULL CHECK(status IN ('in', 'away', 'out')),
            note TEXT,
            manual INTEGER NOT NULL DEFAULT 0,
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
    _migrate_hash_edipi_column()
    _warn_if_roster_key_lost()
    sync_members_from_config()


def _migrate_events_to_presence():
    """
    Installs from when this was a timesheet kept an append-only `events` log,
    every row timestamped. Carry each member's *current* status across into
    `presence` - the last event's action, its note while out or away, and its
    manual flag - then drop the log outright, so the times it held are gone
    rather than merely unread. VACUUM afterwards because DROP TABLE leaves the
    old pages in the freelist, where the timestamps would otherwise survive in
    the file.

    change_id is numbered from the old event ids so the board keeps its
    most-recent-first order across the upgrade. Older installs may lack the
    note/manual columns; those read as NULL/0. Idempotent - once `events` is
    gone this returns immediately.
    """
    conn = get_conn()
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ).fetchone()
    if not exists:
        return
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    note = "note" if "note" in cols else "NULL"
    manual = "manual" if "manual" in cols else "0"
    last = conn.execute(
        f"""
        SELECT e.member_id, e.action, {note} AS note, {manual} AS manual, e.id
        FROM events e
        WHERE e.id = (
            SELECT id FROM events WHERE member_id = e.member_id
            ORDER BY timestamp DESC, id DESC LIMIT 1
        )
        """
    ).fetchall()
    for r in last:
        conn.execute(
            "INSERT OR REPLACE INTO presence (member_id, status, note, manual, change_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (r["member_id"], r["action"], r["note"] if r["action"] != "in" else None,
             1 if r["manual"] else 0, r["id"]),
        )
    conn.execute("DROP TABLE events")
    conn.commit()
    conn.execute("VACUUM")
    log.info(
        "Dropped the old events log; carried %d member status(es) into presence",
        len(last),
    )


def _migrate_hash_edipi_column():
    """
    Installs predating the hashed roster stored the raw 10-digit EDIPI in
    members.edipi. Rename the column and replace each value with its hash,
    in place.

    In place rather than rebuilding the table, because that keeps members.id
    stable - presence.member_id is a foreign key into it, so reinserting would
    either fail or detach each member's current status from them.
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

    Members are deactivated, never deleted. Their presence row references
    members.id with a foreign key, so deleting the member would fail; and
    re-adding someone flips the same row back to active rather than creating
    a second one. get_roster_status() filters on active, so this is all it
    takes for someone to disappear from the kiosk and the dashboard.
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


def current_status(member_id: int) -> str:
    """Returns 'in', 'away' or 'out'. Defaults to 'out' if never set."""
    conn = get_conn()
    row = conn.execute(
        "SELECT status FROM presence WHERE member_id = ?", (member_id,)
    ).fetchone()
    return row["status"] if row else "out"


def set_status(member_id: int, action: str, location: str | None = None,
               manual: bool = False) -> dict:
    """
    Sets where a member is and returns what changed as a dict: {member_id,
    display_name, action, previous, change_id, location, manual}. `previous`
    is the status this replaced, which is what lets the kiosk say "Back in
    lab" rather than "Checked in" for a return from away. change_id
    identifies this change, so the optional checkout note that follows can be
    attached to it via set_note() and to nothing later.

    Overwrites the member's one presence row - nothing is appended, and no
    time is recorded.

    `location` is where an 'away' member went, stored in the row's `note`
    column - the same column a checkout comment lands in, since both are the
    one line of free text a status can carry. It is ignored for 'in'.

    manual=True records that no card was involved - the kiosk's click-a-name
    path and /api/manual-toggle. Nothing else distinguishes the two, and the
    board says so beside the person's name (see get_roster_status).

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
        "INSERT OR REPLACE INTO presence (member_id, status, note, manual, change_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (member_id, action, location, 1 if manual else 0, change_id),
    )
    conn.commit()

    return {
        "member_id": member_id,
        "display_name": member["display_name"],
        "action": action,
        "previous": previous,
        "change_id": change_id,
        "location": location,
        "manual": bool(manual),
    }


def toggle_status(member_id: int, manual: bool = False) -> dict:
    """
    Flips a member between present and not: in -> out, and out *or away* ->
    in. Returns what set_status() does. This is the no-questions-asked path
    (the dev loop's curl, the network fallback); anything that wants to say
    where someone went calls set_status() with 'away' and a location.
    """
    action = "out" if current_status(member_id) == "in" else "in"
    return set_status(member_id, action, manual=manual)


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

    Yesterday's notes ("at lunch") and NO CARD marks go too, on everyone -
    they described a moment that is over. Rows keep their change_id, so the
    board's order is undisturbed.

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
            "UPDATE presence SET status = 'out', note = NULL, manual = 0 "
            "WHERE status != 'out' OR note IS NOT NULL OR manual != 0"
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

    Most recently changed first, so the newest check-in/out lands leftmost on
    the board - ordered by change_id, a counter, not a time. Members who have
    never set a status sort last, alphabetically among themselves.
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT m.id, m.display_name, p.status, p.note, p.manual
        FROM members m LEFT JOIN presence p ON p.member_id = m.id
        WHERE m.active = 1
        ORDER BY COALESCE(p.change_id, 0) DESC, m.display_name
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
            # Whether what put them in this state was a click rather than a
            # tap. Unlike the note, this applies to both directions: an
            # unverified check-*in* is the half worth flagging, since nobody's
            # card was ever present for it.
            "manual": bool(r["manual"]),
        }
        for r in rows
    ]
