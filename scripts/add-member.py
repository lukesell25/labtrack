#!/usr/bin/env python3
"""
Add a lab member to config/members.json without their EDIPI ever touching the
file, the terminal, or shell history.

    python3 scripts/add-member.py "Luke Sellmayer"
    python3 scripts/add-member.py --hash-only     # just print a hash

The roster stores hash_edipi(edipi), which is one-way: the number can't be
read back out of the file, and a typo can't be spotted after the fact either
(the card simply never matches), so the EDIPI is asked for twice. Removing
someone is still a hand-edit - their display name is right there in the file.

Run this on the machine that owns config/roster.key: hashes made with one
key mean nothing to a copy of the app holding a different one.
"""

import argparse
import getpass
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Add a member to the LabTrack roster.")
    parser.add_argument("display_name", nargs="?", help="Name shown on the kiosk and dashboard")
    parser.add_argument(
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

    data = json.loads(MEMBERS_CONFIG.read_text())
    members = data.setdefault("members", [])
    edipi_hash = identity.hash_edipi(prompt_edipi())

    existing = next((m for m in members if m.get("edipi_hash") == edipi_hash), None)
    if existing:
        print(f"That EDIPI is already on the roster as \"{existing['display_name']}\".")
        return 1

    members.append({"edipi_hash": edipi_hash, "display_name": name})
    MEMBERS_CONFIG.write_text(json.dumps(data, indent=2) + "\n")

    print(f"Added {name} to {MEMBERS_CONFIG}.")
    print("Restart the app to pick it up:  sudo systemctl restart labtrack")
    return 0


if __name__ == "__main__":
    sys.exit(main())
