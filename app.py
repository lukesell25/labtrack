import json
import logging
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

import database as db
import identity
import webauth
from cac_reader import get_reader_presence, start_cac_monitor, start_reader_watch
from health import start_health_monitor, sample as health_sample, uptime_s

# Deliberately no timestamp in the format: in production every line goes to
# stderr, which systemd stamps and indexes itself, so a second timestamp is
# just noise in `journalctl` output. Use `journalctl -o short-iso` if you
# want the precise time. See README "Watching a long run".
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("labtrack")

app = Flask(__name__)

OBJECTIVES_PATH = Path(__file__).parent / "config" / "objectives.json"
# Where an 'away' member can say they went. Preset buttons on the kiosk, so
# the choice works with a mouse and no keyboard; "Other" falls back to typing.
LOCATIONS_PATH = Path(__file__).parent / "config" / "locations.json"
STATIC_MEDIA = Path(__file__).parent / "static" / "media"

# Background video for the kiosk, in order of preference; the first one that
# exists wins, and no match means no background (which costs nothing - see
# has-bg in CLAUDE.md). Resolved here rather than hardcoded in main.js because
# the two candidates are produced differently: background.mp4 is the short
# master tracked in git, and background-long.mp4 is the ~900MB file that
# scripts/build-loop.sh concatenates from it on the Pi. The long one is
# strongly preferred - every wrap back to the start is a chance to hit the
# bcm2835_codec stop_streaming oops that freezes the board, and it wraps 50x
# less often. To use different footage, put it in static/media/ and name it
# here; set this to () for no background at all.
BACKGROUND_VIDEO_CANDIDATES = ("background-long.mp4", "background.mp4")

# Last check-in/out event, shared between the CAC-reader thread and the
# Flask request threads. The frontend polls /api/state and compares
# last_event's id to know when to pop the confirmation toast.
_state_lock = threading.Lock()
_last_event = {"id": 0}

# Whether a card is currently mid-read (between physical insertion and the
# PKCS#11 read finishing, roughly 1-3 seconds). Lets the kiosk show "reading
# card..." instead of leaving the person wondering if the tap registered.
_reader_status = {"reading": False}

# When the kiosk page last polled /api/state. The kiosk browser is the one
# part of this system that can die without anything on this end erroring -
# a renderer OOM or a Chromium crash just stops the polls - so the health
# heartbeat reports how long it has been quiet. Only requests tagged
# ?src=kiosk count; the dashboard polls the same endpoint from other PCs and
# must not be able to mask a dead kiosk.
_kiosk_status = {"last_poll": None}

# A tap by someone who is *in* is ambiguous - are they leaving for the day,
# or for the server room? - so it is not written until they say. This holds
# the one unanswered tap: the kiosk sees it on /api/state, puts the question
# on screen, and answers through /api/tap-choice. If nobody answers within
# TAP_CHOICE_TIMEOUT_S the server records a checkout itself, which is what a
# tap meant before there was a question to ask; the deadline lives here, not
# in the page, so a tap is recorded even if the kiosk browser is dead.
# `id` is 0 when nothing is pending. Guarded by _state_lock like the rest.
TAP_CHOICE_TIMEOUT_S = 20
_pending_tap = {"id": 0, "member_id": None, "display_name": None, "deadline": 0.0}

UNRECOGNIZED_MESSAGES = {
    "unreadable": "Could not read card",
    "no_edipi": "Card not recognized",
    "error": "Card read error",
}


def _set_reading(active: bool):
    with _state_lock:
        _reader_status["reading"] = active


def _push_event(display_name, action, message=None, change_id=None, manual=False,
                previous=None, location=None):
    """
    Publishes an event for the kiosk to pick up on its next /api/state poll.
    Returns a snapshot of what it published, which is what /api/manual-toggle
    reports back to its caller. In memory only, and carries no time - the
    toast says who and what, never when.
    """
    with _state_lock:
        _last_event["id"] += 1
        _last_event["event_id"] = _last_event["id"]
        _last_event["display_name"] = display_name
        _last_event["action"] = action
        # What the event replaced and, for 'away', where they went: the toast
        # reads "Back in lab" rather than "Checked in" off the first, and
        # names the place off the second.
        _last_event["previous"] = previous
        _last_event["location"] = location
        if message:
            _last_event["message"] = message
        else:
            _last_event.pop("message", None)
        # Which status change this was, so a checkout's optional note can be
        # attached to it (POST /api/presence/<change_id>/note) and nothing later.
        if change_id is not None:
            _last_event["change_id"] = change_id
        else:
            _last_event.pop("change_id", None)
        # Whether this happened without a card. The toast says so instead of
        # "you may remove your card now", which would be nonsense for a click.
        _last_event["manual"] = bool(manual)
        return dict(_last_event)


def _handle_card_detected():
    """Called from the CAC monitor thread the instant a card is physically inserted."""
    _set_reading(True)


def _handle_unrecognized(reason: str):
    """Called from the CAC monitor thread if a presented card can't be read/identified."""
    _set_reading(False)
    _push_event(None, "error", UNRECOGNIZED_MESSAGES.get(reason, "Card not recognized"))


def _handle_tap(edipi: str):
    """Called from the CAC monitor thread whenever a card is identified."""
    # The EDIPI itself is never stored or logged - only its hash, which is
    # what the roster is keyed on. The prefix is enough to tell repeat taps of
    # the same unknown card apart in the journal, which is persistent for a
    # month (see scripts/setup.sh), without putting a DoD ID in it.
    edipi_hash = identity.hash_edipi(edipi)
    member = db.get_member_by_hash(edipi_hash)
    if member is None:
        log.warning("Unrecognized card tapped (hash %s not in roster)", edipi_hash[:8])
        _set_reading(False)
        _push_event(None, "error", "Card not recognized")
        return

    # A tap by someone who is in means they are leaving - but not whether for
    # the day or for the server room, so ask rather than guess. Everyone else
    # is standing at the lab's reader, which answers the question by itself:
    # they are in the lab.
    if db.current_status(member["id"]) == "in":
        _set_reading(False)
        _open_tap_choice(member)
        return

    # Deliberately not logged: the journal is timestamped and kept for a
    # month, so a line per check-in would be the timesheet this app no longer
    # keeps. See "No time tracking" in CLAUDE.md.
    event = db.set_status(member["id"], "in")
    _set_reading(False)
    _push_event(
        event["display_name"],
        event["action"],
        change_id=event["change_id"],
        manual=False,
        previous=event["previous"],
        location=event["location"],
    )


# --- the tap question --------------------------------------------------

def _open_tap_choice(member):
    """
    Park a tap from a member who is in until the kiosk says what it meant.
    Any earlier unanswered tap is a different person who walked off without
    choosing: it gets its default (a checkout) now rather than later, so there
    is only ever one question on the board. The same person tapping again
    just restarts their clock.
    """
    with _state_lock:
        if _pending_tap["id"] and _pending_tap["member_id"] == member["id"]:
            _pending_tap["deadline"] = time.monotonic() + TAP_CHOICE_TIMEOUT_S
            tap_id = _pending_tap["id"]
        else:
            tap_id = None
    if tap_id is not None:
        _arm_tap_timeout(tap_id)
        return

    _resolve_tap(None, "out", timed_out=True)   # whoever was pending, if anyone

    with _state_lock:
        _pending_tap["id"] += 1
        tap_id = _pending_tap["id"]
        _pending_tap.update(
            member_id=member["id"],
            display_name=member["display_name"],
            deadline=time.monotonic() + TAP_CHOICE_TIMEOUT_S,
        )
    _arm_tap_timeout(tap_id)


def _arm_tap_timeout(tap_id):
    # A stale timer is harmless: _resolve_tap() checks the id and the
    # deadline, so a re-tap that pushed the deadline out just makes the first
    # timer a no-op and the second one the real one.
    timer = threading.Timer(TAP_CHOICE_TIMEOUT_S, _resolve_tap, args=(tap_id, "out"),
                            kwargs={"timed_out": True})
    timer.daemon = True
    timer.start()


def _take_pending_tap(tap_id=None, member_id=None):
    """
    Claim the pending tap and clear the slot, or None if there isn't one (or
    it isn't the one asked for, by id or by member). Atomic, so a choice and
    a timeout arriving together resolve it exactly once.
    """
    with _state_lock:
        if not _pending_tap["id"]:
            return None
        if tap_id is not None and _pending_tap["id"] != tap_id:
            return None
        if member_id is not None and _pending_tap["member_id"] != member_id:
            return None
        taken = dict(_pending_tap)
        _pending_tap.update(id=0, member_id=None, display_name=None, deadline=0.0)
        return taken


def _resolve_tap(tap_id, action, location=None, timed_out=False):
    """
    Write the event a parked tap turns out to have meant, and publish it.
    Returns the published event, or None if there was nothing to resolve.
    With timed_out the caller is a timer or a superseding tap, and only a tap
    whose deadline has actually passed (or no id at all) is taken - a timer
    from before a re-tap must not fire early.
    """
    if timed_out and tap_id is not None:
        with _state_lock:
            if _pending_tap["id"] != tap_id or time.monotonic() < _pending_tap["deadline"]:
                return None
    taken = _take_pending_tap(tap_id)
    if taken is None:
        return None
    try:
        event = db.set_status(taken["member_id"], action, location=location)
    except ValueError:
        # Their status changed under the question (a manual toggle from the
        # dashboard, say) and the answer no longer applies. Nothing to write.
        return None
    return _push_event(
        event["display_name"],
        event["action"],
        change_id=event["change_id"],
        manual=False,
        previous=event["previous"],
        location=event["location"],
    )


def _pending_tap_view():
    """What /api/state tells the kiosk, or None when no tap is waiting."""
    with _state_lock:
        if not _pending_tap["id"]:
            return None
        return {
            "id": _pending_tap["id"],
            "member_id": _pending_tap["member_id"],
            "display_name": _pending_tap["display_name"],
            "expires_in_s": max(0, round(_pending_tap["deadline"] - time.monotonic())),
        }


def _background_video():
    """Filename of the background video to serve, or "" if none is present."""
    for name in BACKGROUND_VIDEO_CANDIDATES:
        if (STATIC_MEDIA / name).is_file():
            return name
    return ""


# --- unattended recovery ---------------------------------------------
# Some failures leave the board dead in a way nothing on this end can undo.
# The one we have actually seen: a wrap-around seek trips a race in the Pi's
# bcm2835_codec stop_streaming, the kernel oopses in a workqueue, and tasks
# start piling up in uninterruptible sleep until the compositor is one of
# them and the screen stops updating. gunicorn keeps serving and the page
# keeps polling throughout - only the picture is gone - so nothing here
# errors and only a reboot clears it. See "Background video freezes" in
# README.md for the trace.

REBOOT_WARNING_S = 30

# Nothing may reboot a Pi that only just came up. A condition that reasserts
# itself every boot would otherwise cycle the board forever, and a kiosk
# stuck in a reboot loop is far worse than one showing a flat background: the
# board self-heals from the freeze, but nobody can check in during a loop.
# The stall watchdog needs ~20s to fire, so anything inside this window is a
# fault that survived the last reboot and will survive the next one too.
REBOOT_MIN_UPTIME_S = 30 * 60

# Absolute path for the same reason health.py resolves vcgencmd absolutely:
# the unit file sets a venv-first PATH, and a bare name is unfindable from
# the service even though it works in an interactive shell.
SYSTEMCTL = "/usr/bin/systemctl"

# Client-log keys that mean the display is unrecoverable. Deliberately a
# small explicit set, not "any error": these arrive on /api/client-log, which
# anything on the lab network holding the dashboard password can post to.
REBOOT_ON_CLIENT_KEYS = {"video-stall"}

_reboot_lock = threading.Lock()
_reboot_state = {"at": None, "reason": None}


_REBOOT_HELP = (
    "is systemd/40-labtrack-reboot.rules installed in /etc/polkit-1/rules.d/? "
    "scripts/setup.sh installs it; an install predating it needs the file "
    "copied and polkit restarted."
)


def _reboot_failed(detail):
    """
    Give up on a scheduled reboot, loudly, and clear the countdown.

    Clearing it matters as much as the log line: the kiosk derives its notice
    from this state, so leaving it set would park the board under "restarting
    in 0s" forever for a reboot that is never coming.
    """
    log.error("reboot failed (%s): %s", detail, _REBOOT_HELP)
    with _reboot_lock:
        _reboot_state.update(at=None, reason=None)


def _do_reboot(reason):
    log.warning("rebooting now: %s", reason)
    try:
        # --no-block for the same reason labtrack-reboot.service uses it: a
        # blocking systemctl reboot waits on a job that has to stop this very
        # service first, so the two would wait on each other.
        proc = subprocess.run(
            [SYSTEMCTL, "--no-block", "reboot"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        _reboot_failed(e)
        return

    # check=False on purpose: the likeliest failure by far is a polkit denial,
    # and that *exits non-zero* rather than raising - so a bare check=False
    # with no test here would swallow exactly the error worth reporting.
    # systemctl writes the reason ("Access denied", "Interactive
    # authentication required") to stderr, so pass it straight through.
    if proc.returncode != 0:
        _reboot_failed(
            f"{SYSTEMCTL} exited {proc.returncode}: "
            f"{(proc.stderr or '').strip() or 'no output'}"
        )


def request_reboot(reason, delay_s=REBOOT_WARNING_S):
    """
    Schedule a reboot, giving the kiosk delay_s seconds to say so on screen
    first. Returns True if one was scheduled by this call.
    """
    up = uptime_s()
    if up is not None and up < REBOOT_MIN_UPTIME_S:
        log.warning(
            "not rebooting for %s: only up %.0fs (need %ds). A fault this "
            "early survives reboots, so cycling the board would not fix it.",
            reason, up, REBOOT_MIN_UPTIME_S,
        )
        return False

    with _reboot_lock:
        if _reboot_state["at"] is not None:
            return False
        _reboot_state.update(at=time.monotonic() + delay_s, reason=reason)

    log.warning("rebooting in %ds: %s", delay_s, reason)
    timer = threading.Timer(delay_s, _do_reboot, args=(reason,))
    timer.daemon = True
    timer.start()
    return True


def _reboot_countdown():
    """What /api/state tells the kiosk, or None when no reboot is pending."""
    with _reboot_lock:
        at, reason = _reboot_state["at"], _reboot_state["reason"]
    if at is None:
        return None
    return {"in_s": max(0, round(at - time.monotonic())), "reason": reason}


@app.before_request
def _require_dashboard_password():
    """
    Everything off this Pi needs the shared password (see webauth.py). The
    kiosk browser is on the Pi and is exempt - it runs unattended and cannot
    answer a prompt.

    This guards the API as much as the page: /api/manual-toggle can check
    anybody in or out, and the app listens on the whole lab network.
    """
    if webauth.is_local(request.remote_addr):
        return None
    if webauth.check(request.authorization and request.authorization.password):
        return None
    # The WWW-Authenticate header is what makes the browser show its own
    # password box, which is why this needs no login page of its own.
    return Response(
        "LabTrack: password required\n",
        401,
        {"WWW-Authenticate": 'Basic realm="LabTrack"'},
    )


@app.route("/")
def kiosk():
    """The always-on display: screensaver + toast overlay on check-in/out."""
    return render_template("index.html", background_video=_background_video())


@app.route("/dashboard")
def dashboard():
    """Viewable from another PC on the network: who is in, away, or out."""
    return render_template("dashboard.html")


def _kiosk_idle_s():
    """Seconds since the kiosk last polled, or None if it never has."""
    with _state_lock:
        last = _kiosk_status["last_poll"]
    return None if last is None else time.monotonic() - last


@app.route("/api/state")
def api_state():
    from_kiosk = request.args.get("src") == "kiosk"
    with _state_lock:
        if from_kiosk:
            _kiosk_status["last_poll"] = time.monotonic()
        last_event = dict(_last_event)
        reading = _reader_status["reading"]
    return jsonify(
        {
            "roster": db.get_roster_status(),
            "last_event": last_event,
            "reading": reading,
            # Sampled on a timer in cac_reader, not measured here - see
            # get_reader_presence(). This endpoint is hit several times a
            # second across the kiosk and every open dashboard, so it must
            # not talk to pcscd itself.
            "reader": get_reader_presence(),
            # A tap waiting on "leaving, or stepping away?" - the kiosk puts
            # the question up while this is non-null and takes it down when
            # it isn't, so the board's dialog follows the server's state
            # rather than its own memory of it.
            "pending_tap": _pending_tap_view(),
            # null except in the seconds between something deciding the board
            # is unrecoverable and the reboot actually happening, so the
            # kiosk can put a countdown on screen rather than blinking out.
            "reboot": _reboot_countdown(),
        }
    )


@app.route("/api/objectives")
def api_objectives():
    data = json.loads(OBJECTIVES_PATH.read_text())
    return jsonify(data)


@app.route("/api/locations")
def api_locations():
    """Preset away locations for the kiosk's buttons; [] when unconfigured."""
    try:
        data = json.loads(LOCATIONS_PATH.read_text())
    except FileNotFoundError:
        data = {}
    names = [str(x).strip() for x in data.get("locations", []) if str(x).strip()]
    return jsonify({"locations": names})


@app.route("/api/tap-choice", methods=["POST"])
def api_tap_choice():
    """
    The kiosk's answer to a parked tap: {"tap_id", "action": "away"|"out",
    "location"?}. The id must match the tap currently pending, so an answer
    to a question that has already timed out (or been superseded by someone
    else's tap) writes nothing - the checkout the timeout recorded stands,
    and a 409 tells the page so. The event is not flagged manual: a card was
    read, this is just the second half of the same tap.
    """
    payload = request.json or {}
    action = payload.get("action")
    if action not in ("away", "out"):
        return jsonify({"error": 'action must be "away" or "out"'}), 400
    location = payload.get("location")
    location = location.strip()[:80] if isinstance(location, str) else None
    try:
        tap_id = int(payload.get("tap_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "tap_id required"}), 400
    pushed = _resolve_tap(tap_id, action, location=location)
    if pushed is None:
        return jsonify({"error": "no such pending tap"}), 409
    return jsonify(pushed)


@app.route("/api/presence/<int:change_id>/note", methods=["POST"])
def api_set_note(change_id):
    """
    Attaches an optional note to a checkout - the "why are you out" comment
    prompt shown on the kiosk right after one. Keyed on the change_id the
    kiosk saw in last_event, so a late request can't land on a later status.
    """
    note = (request.json or {}).get("note", "")
    note = note.strip()[:80] if isinstance(note, str) else ""
    db.set_note(change_id, note if note else None)
    return jsonify({"ok": True})


@app.route("/api/manual-toggle", methods=["POST"])
def api_manual_toggle():
    """
    Record an event for a member without a card tap. Three callers: the
    kiosk's click-a-name flow (tapping the roster card, then choosing), the
    dashboard/curl fallback for when the reader is down, and dev machines
    with no reader at all.

    {"member_id": 1} alone toggles - in -> out, out or away -> in - which is
    what the dev loop and the curl fallback want. Add "action" ("in", "away"
    or "out") to say exactly what to record, and "location" with "away" to say
    where; that is what the kiosk dialog posts. An action that changes
    nothing ("in" while already in) is a 409 - the caller's view is stale.

    Every event written here is flagged manual, because none of them saw a
    card - the board and the log say so beside the person's name so an
    unverified check-in is never mistaken for a tap.

    The response is the event exactly as /api/state would report it, id
    included. The kiosk still re-polls rather than toasting from this reply
    (one path to the screen for every event, whatever caused it - see
    submitChoice() in main.js); it's here for curl and for anything that
    wants to know what the toggle actually did.
    """
    payload = request.json or {}
    member_id = payload.get("member_id")
    if member_id is None:
        return jsonify({"error": "member_id required"}), 400
    action = payload.get("action")
    if action is not None and action not in db.ACTIONS:
        return jsonify({"error": 'action must be "in", "away" or "out"'}), 400
    location = payload.get("location")
    location = location.strip()[:80] if isinstance(location, str) else None
    try:
        member_id = int(member_id)
        # If a tap of theirs is waiting on the question, this is the answer:
        # claim it before writing, so a timeout can't land in between and
        # record a checkout on top.
        _take_pending_tap(member_id=member_id)
        if action is None:
            event = db.toggle_status(member_id, manual=True)
        else:
            event = db.set_status(member_id, action, location=location, manual=True)
    except (TypeError, ValueError) as e:
        if action is not None and str(e).startswith("Already"):
            # The page that posted this is showing a stale status - somebody
            # tapped in between. Its next poll will straighten it out.
            return jsonify({"error": str(e).lower()}), 409
        # An unknown or non-numeric member_id: a stale kiosk page holding ids
        # from before a roster change, not a server fault, so don't 500 it.
        return jsonify({"error": "unknown member_id"}), 400
    pushed = _push_event(
        event["display_name"],
        event["action"],
        change_id=event["change_id"],
        manual=True,
        previous=event["previous"],
        location=event["location"],
    )
    return jsonify(pushed)


# --- diagnostics ------------------------------------------------------
# The kiosk runs headless: nothing reads its browser console, so anything
# the frontend notices has to be posted back here to reach the journal.

# A runaway client (a failure that repeats every few seconds for a week)
# must not be able to flood the journal, so drop anything past this rate and
# report how much was dropped once the window closes. main.js throttles per
# message as well, so hitting this cap at all means something unexpected.
_CLIENT_LOG_MAX_PER_MIN = 30
_client_log_lock = threading.Lock()
_client_log_window = {"start": 0.0, "logged": 0, "dropped": 0}


def _client_log_allowed():
    now = time.monotonic()
    with _client_log_lock:
        window = _client_log_window
        if now - window["start"] > 60:
            dropped = window["dropped"]
            window.update(start=now, logged=0, dropped=0)
            if dropped:
                log.warning("client-log: dropped %d message(s) over the rate limit", dropped)
        if window["logged"] >= _CLIENT_LOG_MAX_PER_MIN:
            window["dropped"] += 1
            return False
        window["logged"] += 1
        return True


@app.route("/api/client-log", methods=["POST"])
def api_client_log():
    """
    Errors reported by the kiosk page (see report() in main.js): JS
    exceptions, failed polls, and the background-video stall watchdog. The
    video freeze in particular emits no error event of its own, so this
    endpoint is the only way it ever reaches a log.
    """
    payload = request.json or {}
    key = str(payload.get("key", "unknown"))[:80]
    detail = str(payload.get("detail", ""))[:500]
    count = payload.get("count")
    if _client_log_allowed():
        repeat = f" (x{count} since last report)" if isinstance(count, int) and count > 1 else ""
        log.warning("client %s: %s%s", key, detail, repeat)
    # Reported before the reboot is requested, so the journal always carries
    # the reason ahead of the reboot it caused.
    if key in REBOOT_ON_CLIENT_KEYS:
        request_reboot(f"client reported {key}: {detail}")
    return jsonify({"ok": True})


@app.route("/api/health")
def api_health():
    """
    One health sample on demand - the same line the heartbeat thread writes
    every minute, for checking the Pi from the dashboard machine without an
    ssh session.
    """
    message, is_warning, concerns = health_sample(_kiosk_idle_s())
    return jsonify({"summary": message, "ok": not is_warning, "concerns": concerns})


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    """
    Anything that escapes a route: log it with a traceback and which request
    caused it, rather than letting Flask return a bare 500 with the reason
    visible nowhere. HTTPExceptions (404s, 405s) are the framework working
    as intended, so they pass straight through unlogged.
    """
    if isinstance(e, HTTPException):
        return e
    log.exception("Unhandled error serving %s %s", request.method, request.path)
    return jsonify({"error": "internal error"}), 500


# Keep references at module scope so the monitor/observer aren't GC'd.
_cac_monitor = None
_cac_observer = None


def _init_cac_monitor():
    global _cac_monitor, _cac_observer
    try:
        _cac_monitor, _cac_observer = start_cac_monitor(
            _handle_tap,
            on_card_detected=_handle_card_detected,
            on_unrecognized=_handle_unrecognized,
        )
    except RuntimeError as e:
        # Expected on a dev machine without pyscard installed - see
        # cac_reader.py's _PYSCARD_AVAILABLE check. No traceback needed for
        # something this routine.
        log.warning(str(e))
    except Exception:
        log.exception(
            "Failed to start CAC monitor - is a reader plugged in and pcscd running? "
            "The app will keep running; use /api/manual-toggle in the meantime."
        )


def _start_daily_reset():
    """
    Everyone in or away goes back to out once per calendar day (see
    db.reset_if_new_day). Checked at startup and then once a minute, so it
    lands just after midnight on a Pi that stays up and on the first boot of
    the day on one that doesn't - and a midday restart changes nothing,
    because the day it keys on hasn't moved.
    """
    def loop():
        while True:
            try:
                db.reset_if_new_day()
            except Exception:
                log.exception("daily status reset failed")
            time.sleep(60)

    threading.Thread(target=loop, name="daily-reset", daemon=True).start()


db.init_db()
_start_daily_reset()
# Read (and, on a first run, create) the dashboard password at startup, so
# the file exists and its one-time warning lands at boot rather than
# whenever the first request from another PC happens to arrive.
webauth.load_password()
_init_cac_monitor()
# Deliberately started even when _init_cac_monitor() failed: a Pi with no
# working monitor is exactly when the board needs to say the reader is down.
start_reader_watch()
start_health_monitor(kiosk_idle_fn=_kiosk_idle_s)


if __name__ == "__main__":
    # Dev server. In production this is run via gunicorn (see systemd/labtrack.service).
    app.run(host="0.0.0.0", port=5000, debug=False)
