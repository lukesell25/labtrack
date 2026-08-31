"""
webauth.py - the shared password that guards LabTrack from other machines.

The app binds to every interface so the dashboard can be read from a PC on
the lab network (see README "View the dashboard from another PC"). That also
puts /api/manual-toggle within reach of anyone on that network, who could
otherwise check people in and out at will, so everything off-box is behind a
password.

HTTP Basic auth, checked in one before_request hook in app.py. No login page,
no session cookie, no signing key to manage: the browser prompts once and
replays the header for the rest of its session, which is what a dashboard
somebody leaves open all day wants. Any username is accepted - only the
password is checked.

Two things worth knowing:

- **Requests from the Pi itself are exempt.** The kiosk Chromium loads
  http://localhost:5000 on a board that stands unattended for weeks, and it
  cannot answer a password prompt. Nothing off-box can claim that exemption:
  a packet arriving on a real interface carrying a loopback source address is
  dropped by the kernel as a martian, so remote_addr is trustworthy here.
  It is trustworthy *because gunicorn holds the listening socket directly*
  (systemd/labtrack.service). Putting nginx or Caddy in front would make
  every request arrive from 127.0.0.1 and silently disable this check for the
  whole network - if that ever happens, this has to start reading a
  proxy-set header instead.

- **The password is stored in the clear, and deliberately not hashed.** The
  obvious move is to reuse identity.hash_edipi, but scrypt costs 100-200ms on
  a Pi 4 and dashboard.js polls three endpoints every 5s, so each open
  dashboard would spend ~120ms of every second hashing on a Flask request
  thread - the exact thing CLAUDE.md rules out. secrets.compare_digest
  against the plaintext is microseconds and still constant-time. The file is
  mode 0600 on a machine whose only interactive account is the one running
  the app, and there is no TLS on this hop anyway, so the wire is the weak
  link here, not the disk.

To be clear about what this does and does not buy: it stops people who wander
onto the lab network. It is not confidentiality. Traffic is plain HTTP, so
the password and the page cross the network readable by anyone sniffing.
"""

import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("labtrack.webauth")

PASSWORD_PATH = Path(__file__).parent / "config" / "dashboard.key"

# Addresses that skip the password. Both forms appear depending on whether
# the kiosk's "localhost" resolves to IPv4 or IPv6.
LOOPBACK = ("127.0.0.1", "::1")

# Long enough to be worth nothing to a guesser, short enough to type into a
# browser prompt once. urlsafe so it survives being read off a screen.
GENERATED_LENGTH = 12

_password = None


def load_password() -> str:
    """
    The shared dashboard password, from config/dashboard.key. Created with a
    random value on first use. Cached - it is checked on every request from
    off the Pi, and changing it is a restart, like config/members.json.

    Returns "" if the file exists but is empty, which check() treats as
    "refuse everything" rather than "accept everything" - see there.
    """
    global _password
    if _password is not None:
        return _password

    if not PASSWORD_PATH.exists():
        try:
            # O_EXCL for the same reason identity.load_key() uses it: two
            # processes starting at once must not each write a different
            # password and leave whichever lost the race rejecting logins.
            fd = os.open(PASSWORD_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(GENERATED_LENGTH) + "\n")
            # Deliberately not logged: the journal outlives the password and
            # is read over the shoulder during a demo. Say where it is
            # instead.
            log.warning(
                "Generated a dashboard password at %s - read it with `cat %s` "
                "to view the dashboard from another PC, or replace it with a "
                "passphrase of your own and restart the service.",
                PASSWORD_PATH,
                PASSWORD_PATH,
            )
        except FileExistsError:
            pass  # someone else won the race; their password is the real one

    _password = PASSWORD_PATH.read_text().strip()
    if not _password:
        log.error(
            "%s is empty, so nothing off this Pi can reach the dashboard. "
            "Put a passphrase in it (or delete it to have one generated) and "
            "restart the service.",
            PASSWORD_PATH,
        )
    return _password


def is_local(remote_addr) -> bool:
    """True for the kiosk browser on the Pi itself, which is never prompted."""
    return remote_addr in LOOPBACK


def check(supplied) -> bool:
    """
    Whether a supplied Basic-auth password matches. Fails closed on an empty
    stored password: compare_digest("", "") is True, so without this guard an
    unreadable or truncated key file would silently let the whole network in
    with no password at all - the one failure here that would look exactly
    like everything working.
    """
    expected = load_password()
    if not expected:
        return False
    return secrets.compare_digest(str(supplied or ""), expected)
