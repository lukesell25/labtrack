#!/usr/bin/env python3
"""
Add a lab member to config/members.json without their EDIPI ever touching the
file, the terminal, or shell history.

    python3 scripts/add-member.py "Luke Sellmayer"
    python3 scripts/add-member.py --pending "Luke Sellmayer"   # EDIPI not known yet
    python3 scripts/add-member.py --replace "Luke Sellmayer"   # fill it in later
    python3 scripts/add-member.py --hash-only                  # just print a hash

The roster stores hash_edipi(edipi), which is one-way: the number can't be
read back out of the file, and a typo can't be spotted after the fact either
(the card simply never matches), so the EDIPI is asked for twice. Removing
someone is still a hand-edit - their display name is right there in the file.

--pending puts someone on the board before their EDIPI is known, so they can
check in by clicking their name (see "Checking in without a card"); --replace
swaps that placeholder for the real hash later, rewriting the member row in
place so the attendance they built up in the meantime stays theirs.

Run this on the machine that owns config/roster.key: hashes made with one
key mean nothing to a copy of the app holding a different one. That is often
*not* the machine holding labtrack.db, which is why --replace prints the SQL
to finish the job on the Pi when it can't reach the database itself.
"""

import argparse
import getpass
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database  # noqa: E402
import identity  # noqa: E402

MEMBERS_CONFIG = Path(__file__).resolve().parent.parent / "config" / "members.json"


def prompt_edipi() -> str:
    while True:
        first = getpass.getpass("EDIPI (10 digits, not shown): ").strip()
        if not re.fullmatch(r"\d{10}", first):
            print("  That isn't 10 digits. Try again.")
            continue
        if first != getpass.getpass("Confirm EDIPI: ").strip():
            print("  They don't match. Try again.")
            continue
        return first


def load_roster():
    data = json.loads(MEMBERS_CONFIG.read_text())
    return data, data.setdefault("members", [])


def save_roster(data) -> None:
    MEMBERS_CONFIG.write_text(json.dumps(data, indent=2) + "\n")


def find_by_name(members, name: str):
    """
    One roster entry by display name: exact (case-insensitive) first, then a
    unique substring match so "sellmayer" works. Same rule as
    scripts/add-event.py, so the two scripts take names the same way.
    """
    wanted = name.strip().lower()
    exact = [m for m in members if m.get("display_name", "").strip().lower() == wanted]
    matches = exact or [m for m in members if wanted in m.get("display_name", "").lower()]

    if not matches:
        raise SystemExit(f'No member matching "{name}" in {MEMBERS_CONFIG}.')
    if len(matches) > 1:
        names = ", ".join(f'"{m["display_name"]}"' for m in matches)
        raise SystemExit(f'"{name}" matches more than one member: {names}. Be more specific.')
    return matches[0]


def swap_in_database(placeholder: str, edipi_hash: str) -> list[str]:
    """
    Rewrite the pending member's edipi_hash in the database in place, so their
    members.id - and every events row hanging off it - survives.

    Editing only the roster file would not do this. sync_members_from_config()
    upserts on edipi_hash and deactivates anyone the file no longer lists, so
    changing the hash reads as "the pending member left, a different person
    joined": a second row appears, and the original goes inactive still holding
    the history, which takes it off the board and out of the hours report. If
    they happened to be checked in on the old row, that 'in' is orphaned too
    and never pairs.

    Returns lines to print. Finding no database here is a normal outcome, not
    a failure - hashes are made on the machine that owns roster.key, which is
    usually not the Pi the events are logged on.
    """
    remote_fix = [
        "",
        "Run this on the machine holding labtrack.db, BEFORE restarting the app",
        "(the restart is what would otherwise create the second member row):",
        f"""  sqlite3 labtrack.db "UPDATE members SET edipi_hash = '{edipi_hash}' """
        f"""WHERE edipi_hash = '{placeholder}';\"""",
    ]

    if not database.DB_PATH.exists():
        return [f"No database at {database.DB_PATH}, so nothing to update here."] + remote_fix

    conn = database.get_conn()
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'members'"
    ).fetchone():
        return [f"{database.DB_PATH} has no members table yet, so nothing to update here."]

    clash = conn.execute(
        "SELECT id, display_name FROM members WHERE edipi_hash = ?", (edipi_hash,)
    ).fetchone()
    if clash:
        raise SystemExit(
            f"{database.DB_PATH} already has a member row (id {clash['id']}, "
            f"\"{clash['display_name']}\") with that EDIPI hash. Nothing was changed - "
            "merging two member rows means moving their events across by hand."
        )

    row = conn.execute(
        "SELECT id FROM members WHERE edipi_hash = ?", (placeholder,)
    ).fetchone()
    if row is None:
        return [
            f"{database.DB_PATH} has no row for {placeholder} - the app has never "
            "started against this database with them pending, so there is no "
            "history to preserve and the next restart will add them normally."
        ] + remote_fix

    conn.execute("UPDATE members SET edipi_hash = ? WHERE id = ?", (edipi_hash, row["id"]))
    conn.commit()
    return [
        f"Updated member id {row['id']} in {database.DB_PATH} in place - their "
        "check-ins so far stay attached to them."
    ]


def add_member(name: str) -> int:
    data, members = load_roster()
    edipi_hash = identity.hash_edipi(prompt_edipi())

    existing = next((m for m in members if m.get("edipi_hash") == edipi_hash), None)
    if existing:
        print(f"That EDIPI is already on the roster as \"{existing['display_name']}\".")
        return 1

    members.append({"edipi_hash": edipi_hash, "display_name": name})
    save_roster(data)

    print(f"Added {name} to {MEMBERS_CONFIG}.")
    print("Restart the app to pick it up:  sudo systemctl restart labtrack")
    return 0


def add_pending(name: str) -> int:
    data, members = load_roster()
    placeholder = identity.pending_hash(name)

    existing = next(
        (
            m for m in members
            if m.get("edipi_hash") == placeholder
            or m.get("display_name", "").strip().lower() == name.strip().lower()
        ),
        None,
    )
    if existing:
        print(f"\"{existing['display_name']}\" is already on the roster.")
        return 1

    members.append({"edipi_hash": placeholder, "display_name": name})
    save_roster(data)

    print(f"Added {name} to {MEMBERS_CONFIG} as pending ({placeholder}).")
    print("They show on the board after a restart and can be checked in by")
    print("clicking their name, which logs as manual - no card can match them.")
    print("When their EDIPI arrives, finish the entry with:")
    print(f'  python3 scripts/add-member.py --replace "{name}"')
    print("Restart the app to pick it up:  sudo systemctl restart labtrack")
    return 0


def replace_pending(name: str) -> int:
    data, members = load_roster()
    entry = find_by_name(members, name)
    placeholder = str(entry.get("edipi_hash", ""))

    if not identity.is_pending(placeholder):
        raise SystemExit(
            f"\"{entry['display_name']}\" already has a real EDIPI hash - this only "
            "converts a pending placeholder. To correct a wrong EDIPI, delete the "
            "entry and add the person again; they come back on a new member row, so "
            "check-ins logged under the old one stay with it."
        )

    edipi_hash = identity.hash_edipi(prompt_edipi())
    other = next(
        (m for m in members if m is not entry and m.get("edipi_hash") == edipi_hash), None
    )
    if other:
        print(f"That EDIPI is already on the roster as \"{other['display_name']}\".")
        return 1

    # Database first: if it refuses, the roster file is left untouched rather
    # than half-converted, which is the state that splits the member in two.
    notes = swap_in_database(placeholder, edipi_hash)
    entry["edipi_hash"] = edipi_hash
    save_roster(data)

    print(f"{entry['display_name']} is no longer pending in {MEMBERS_CONFIG}.")
    for line in notes:
        print(line)
    print("Restart the app to pick it up:  sudo systemctl restart labtrack")
    print("Their next tap should be recognised; the NO CARD mark clears with it.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Add a member to the LabTrack roster.")
    parser.add_argument("display_name", nargs="?", help="Name shown on the kiosk and dashboard")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--pending",
        action="store_true",
        help="add someone whose EDIPI you don't have yet (no card will match them)",
    )
    mode.add_argument(
        "--replace",
        action="store_true",
        help="give a pending member their real EDIPI, keeping their history",
    )
    mode.add_argument(
        "--hash-only",
        action="store_true",
        help="print the hash for an EDIPI and exit, without editing the roster",
    )
    args = parser.parse_args()

    if args.hash_only:
        print(identity.hash_edipi(prompt_edipi()))
        return 0

    if not args.display_name:
        parser.error("give the member's display name, or use --hash-only")
    name = args.display_name.strip()

    if args.pending:
        return add_pending(name)
    if args.replace:
        return replace_pending(name)
    return add_member(name)


if __name__ == "__main__":
    sys.exit(main())
