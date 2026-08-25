import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import database as db
from cac_reader import start_cac_monitor

logging.basicConfig(level=logging.INFO)
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
    member = db.get_member_by_edipi(edipi)
    if member is None:
        log.warning("Unrecognized card tapped (EDIPI not in roster): %s", edipi)
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


@app.route("/api/state")
def api_state():
    with _state_lock:
        last_event = dict(_last_event)
        reading = _reader_status["reading"]
    return jsonify(
        {
            "roster": db.get_roster_status(),
            "last_event": last_event,
            "reading": reading,
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


if __name__ == "__main__":
    # Dev server. In production this is run via gunicorn (see systemd/labtrack.service).
    app.run(host="0.0.0.0", port=5000, debug=False)
