"""
identity.py - one-way hashing of EDIPIs for the roster.

The EDIPI is only ever used as a lookup key: a card is tapped, its EDIPI is
hashed, and the hash selects one row in `members`. Nothing displays it or
needs it back, so it is never stored - config/members.json and the database
hold `hash_edipi(edipi)` and nothing else.

Why scrypt rather than SHA-256 or HMAC: an EDIPI is 10 digits, so the entire
keyspace is 10^10. A plain hash of that is brute-forced in minutes on a GPU,
and an HMAC is too the moment the key file leaks alongside the roster it
protects. At n=2**14 a full sweep costs decades of CPU time *even with the
key in hand*, which is the property worth having here. The cost is ~100-200ms
per tap on a Pi 4, paid on the CAC reader thread (never a Flask request
thread) inside the 1-3s the PKCS#11 read already takes.
"""

import logging
import os
import re
import secrets
from pathlib import Path
from hashlib import scrypt

log = logging.getLogger("labtrack.identity")

KEY_PATH = Path(__file__).parent / "config" / "roster.key"

# n=2**14 with r=8 needs ~16MB of state. maxmem is passed explicitly because
# OpenSSL's default ceiling (32MB) is close enough to that, once overhead is
# counted, to raise instead of hashing on some builds.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 2**26
HASH_BYTES = 16

# A member who is on the roster but has no EDIPI yet - new to the lab, still
# waiting on their card, or here before the reader is. `edipi_hash` is only
# ever a lookup key, so a placeholder in that column costs nothing: the person
# shows on the board and can be checked in by clicking their name, and no card
# can ever select their row, because a real hash is 32 hex characters and this
# deliberately isn't one. Giving them a real member row from day one is what
# lets their history survive the later swap - see scripts/add-member.py
# --replace, which rewrites this value in place rather than adding a row.
PENDING_PREFIX = "pending-"


def pending_hash(display_name: str) -> str:
    """The placeholder edipi_hash for a member whose EDIPI isn't known yet."""
    slug = re.sub(r"[^a-z0-9]+", "-", display_name.strip().lower()).strip("-")
    return PENDING_PREFIX + (slug or "member")


def is_pending(edipi_hash: str) -> bool:
    """True for a placeholder from pending_hash(), never for a real hash."""
    return str(edipi_hash).startswith(PENDING_PREFIX)


_key = None

# True if load_key() had to create the key file. database.init_db() uses this
# to tell a first run (nothing to lose) from a lost key (every card stops
# matching), which are otherwise indistinguishable from here.
key_was_generated = False


def load_key() -> bytes:
    """
    The secret salt every EDIPI is hashed with, from config/roster.key.
    Created on first use if absent. Cached - it's read on every tap.
    """
    global _key, key_was_generated
    if _key is not None:
        return _key

    if not KEY_PATH.exists():
        try:
            # O_EXCL so two processes starting at once (the app and
            # scripts/add-member.py) can't each write a different key and
            # leave whichever lost the race hashing against the wrong salt.
            fd = os.open(KEY_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_bytes(32).hex() + "\n")
            key_was_generated = True
            log.warning(
                "Generated a new roster key at %s. BACK THIS FILE UP - without "
                "it no card matches the roster and every member has to be "
                "re-added.",
                KEY_PATH,
            )
        except FileExistsError:
            pass  # someone else won the race; their key is the real one

    _key = bytes.fromhex(KEY_PATH.read_text().strip())
    return _key


def hash_edipi(edipi: str) -> str:
    """The 32-character hex string an EDIPI is stored and matched as."""
    return scrypt(
        str(edipi).strip().encode(),
        salt=load_key(),
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=HASH_BYTES,
    ).hex()
