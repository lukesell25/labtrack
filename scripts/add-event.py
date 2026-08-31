#!/usr/bin/env python3
"""
Log a check-in or check-out at a time you name, for when the reader was down
or someone forgot to tap.

    python3 scripts/add-event.py "Luke Sellmayer" in  "2026-08-27 08:15"
    python3 scripts/add-event.py "Luke Sellmayer" out "2026-08-27 16:40" --note "left early"
    python3 scripts/add-event.py --list

/api/manual-toggle is the other manual path, but it stamps the current time
and flips whatever the member's status happens to be; this one places a
specific action at a specific moment, which is what backfilling needs.

`events` is append-only and the hours report pairs consecutive in/out rows
per member, so a backfilled event lands in the middle of an existing
sequence and can quietly corrupt it - two 'in's in a row makes the first one
unpaired, and an unpaired 'in' counts as time in the lab right up to now.
The insert is therefore previewed against its neighbours and confirmed
before it happens. Nothing here needs an app restart: the kiosk and
dashboard re-read the DB on their next poll.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database  # noqa: E402

# What database.toggle_checkin() writes, and what get_weekly_hours() parses
# back with fromisoformat(). Local time, no timezone suffix.
TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


def resolve_member(conn, name: str):
    """
    Find one member by display name: exact (case-insensitive) first, then a
    unique substring match so "sellmayer" works. Inactive members are
    matched too - their history is still on the board's hours report, and
    backfilling a shift someone worked before they came off the roster is a
    reasonable thing to want.
    """
    rows = conn.execute("SELECT id, display_name, active FROM members").fetchall()

    exact = [r for r in rows if r["display_name"].lower() == name.lower()]
    matches = exact or [r for r in rows if name.lower() in r["display_name"].lower()]

    if not matches:
        raise SystemExit(f'No member matching "{name}". Use --list to see the roster.')
    if len(matches) > 1:
        names = ", ".join(f'"{r["display_name"]}"' for r in matches)
        raise SystemExit(f'"{name}" matches more than one member: {names}. Be more specific.')
    return matches[0]


def parse_timestamp(text: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        raise SystemExit(
            f'Could not read "{text}" as a date and time. '
            'Use "YYYY-MM-DD HH:MM", e.g. "2026-08-27 08:15".'
        )
    if stamp.tzinfo is not None:
        raise SystemExit(
            "Give a local time with no timezone offset - that's what the rest "
            "of the table holds, and a mix of the two makes the hours report wrong."
        )
    if ":" not in text:
        raise SystemExit(f'"{text}" has no time of day, which would log this at midnight.')
    if stamp > datetime.now():
        raise SystemExit(
            f"{stamp:{TS_FORMAT}} is in the future. The newest event is the one the "
            "kiosk shows, so a future check-in would mark them present until it passes."
        )
    return stamp.replace(microsecond=0)


def neighbours(conn, member_id: int, stamp: str):
    """The events immediately before and after the new one, in time order."""
    before = conn.execute(
        """
        SELECT id, action, timestamp FROM events
        WHERE member_id = ? AND timestamp <= ?
        ORDER BY timestamp DESC, id DESC LIMIT 1
        """,
        (member_id, stamp),
    ).fetchone()
    after = conn.execute(
        """
        SELECT id, action, timestamp FROM events
        WHERE member_id = ? AND timestamp > ?
        ORDER BY timestamp ASC, id ASC LIMIT 1
        """,
        (member_id, stamp),
    ).fetchone()
    return before, after


def describe(row) -> str:
    return f"{row['timestamp']}  {row['action']:<3} (id {row['id']})"


def warnings_for(action: str, before, after, note: str | None) -> list[str]:
    """
    Everything about this insert that would produce a wrong hours figure or a
    wrong-looking board, phrased as what it will actually do.
    """
    out = []
    if before is not None and before["action"] == action:
        out.append(
            f"The previous event is also '{action}', so the sequence reads "
            f"{action}/{action}. get_weekly_hours() pairs consecutive in/out "
            "events - one of the two will go unpaired."
        )
    if after is not None and after["action"] == action:
        out.append(
            f"The next event is also '{action}', same problem in the other direction."
        )
    if after is None:
        out.append(
            "This becomes the member's newest event, so it sets what the kiosk "
            f"and dashboard show right now: {'checked in' if action == 'in' else 'checked out'}."
        )
        if action == "in":
            out.append(
                "An 'in' with no 'out' after it counts as time in the lab up to "
                "now on the hours report - add the matching 'out' as well."
            )
    if note and action == "in":
        out.append(
            "Notes are the checkout comment: get_roster_status() only surfaces one "
            "while the member is out, so a note on an 'in' event is stored and never shown."
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Log a check-in/check-out at a specific time.",
    )
    parser.add_argument("display_name", nargs="?", help="Member's name, or part of it")
    parser.add_argument("action", nargs="?", choices=["in", "out"], help="Check in or out")
    parser.add_argument("timestamp", nargs="?", help='Local time, e.g. "2026-08-27 08:15"')
    parser.add_argument("--note", help="Optional checkout comment (shown while they're out)")
    parser.add_argument("--list", action="store_true", help="list members and exit")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    args = parser.parse_args()

    conn = database.get_conn()

    if args.list:
        for row in conn.execute("SELECT id, display_name, active FROM members ORDER BY id"):
            mark = "" if row["active"] else "   (inactive)"
            print(f"{row['id']:>4}  {row['display_name']}{mark}")
        return 0

    if not (args.display_name and args.action and args.timestamp):
        parser.error("give a name, an action (in/out) and a timestamp, or use --list")

    member = resolve_member(conn, args.display_name)
    stamp = parse_timestamp(args.timestamp).strftime(TS_FORMAT)
    before, after = neighbours(conn, member["id"], stamp)

    print(f"{member['display_name']}" + ("" if member["active"] else "  (inactive)"))
    print(f"  {describe(before) if before else '(no earlier events)'}")
    print(f"> {stamp}  {args.action:<3} " + (f"note: {args.note}" if args.note else ""))
    print(f"  {describe(after) if after else '(no later events)'}")

    for warning in warnings_for(args.action, before, after, args.note):
        print(f"\n  ! {warning}")

    if not args.yes:
        if input("\nAdd this event? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing written.")
            return 1

    cursor = conn.execute(
        "INSERT INTO events (member_id, action, timestamp, note) VALUES (?, ?, ?, ?)",
        (member["id"], args.action, stamp, args.note),
    )
    conn.commit()

    print(f"Added event id {cursor.lastrowid}. The kiosk picks it up on its next poll.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
