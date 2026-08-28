import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

import database as db
import identity
from cac_reader import get_reader_presence, start_cac_monitor, start_reader_watch
from health import start_health_monitor, sample as health_sample

# Deliberately no timestamp in the format: in production every line goes to
# stderr, which systemd stamps and indexes itself, so a second timestamp is
# just noise in `journalctl` output. Use `journalctl -o short-iso` if you
# want the precise time. See README "Watching a long run".
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("labtrack")

app = Flask(__name__)

OBJECTIVES_PATH = Path(__file__).parent / "config" / "objectives.json"

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

UNRECOGNIZED_MESSAGES = {
    "unreadable": "Could not read card",
    "no_edipi": "Card not recognized",
    "error": "Card read error",
}


def _set_reading(active: bool):
    with _state_lock:
        _reader_status["reading"] = active


def _push_event(display_name, action, message=None, checkin_event_id=None):
    with _state_lock:
        _last_event["id"] += 1
        _last_event["event_id"] = _last_event["id"]
        _last_event["display_name"] = display_name
        _last_event["action"] = action
        _last_event["timestamp"] = datetime.now().isoformat(timespec="seconds")
        if message:
            _last_event["message"] = message
        else:
            _last_event.pop("message", None)
        if checkin_event_id is not None:
            _last_event["checkin_event_id"] = checkin_event_id
        else:
            _last_event.pop("checkin_event_id", None)


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

    event = db.toggle_checkin(member["id"])
    log.info("%s checked %s at %s", event["display_name"], event["action"], event["timestamp"])
    _set_reading(False)
    _push_event(event["display_name"], event["action"], checkin_event_id=event["checkin_event_id"])


@app.route("/")
def kiosk():
    """The always-on display: screensaver + toast overlay on check-in/out."""
    return render_template("index.html")


@app.route("/dashboard")
def dashboard():
    """Viewable from another PC on the network: roster status + hours."""
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
        }
    )


@app.route("/api/objectives")
def api_objectives():
    data = json.loads(OBJECTIVES_PATH.read_text())
    return jsonify(data)


@app.route("/api/events")
def api_events():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify(db.get_recent_events(limit=limit))


@app.route("/api/events/<int:event_id>/note", methods=["POST"])
def api_set_note(event_id):
    """
    Attaches an optional note to a checkout event - the "why are you out"
    comment prompt shown on the kiosk right after a checkout tap.
    """
    note = (request.json or {}).get("note", "")
    note = note.strip() if isinstance(note, str) else ""
    db.set_event_note(event_id, note if note else None)
    return jsonify({"ok": True})


@app.route("/api/weekly-hours")
def api_weekly_hours():
    return jsonify(db.get_weekly_hours())


@app.route("/api/manual-toggle", methods=["POST"])
def api_manual_toggle():
    """
    Admin/testing fallback: toggle a member's status without a card tap.
    Useful while you're bringing up the CAC reader, or if the reader is
    ever down and someone needs to be logged manually. Not exposed in the
    kiosk UI - reach it directly if needed, e.g. from the dashboard.
    """
    member_id = request.json.get("member_id")
    if member_id is None:
        return jsonify({"error": "member_id required"}), 400
    event = db.toggle_checkin(int(member_id))
    _push_event(event["display_name"], event["action"], checkin_event_id=event["checkin_event_id"])
    return jsonify(event)


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


db.init_db()
_init_cac_monitor()
# Deliberately started even when _init_cac_monitor() failed: a Pi with no
# working monitor is exactly when the board needs to say the reader is down.
start_reader_watch()
start_health_monitor(kiosk_idle_fn=_kiosk_idle_s)


if __name__ == "__main__":
    # Dev server. In production this is run via gunicorn (see systemd/labtrack.service).
    app.run(host="0.0.0.0", port=5000, debug=False)
