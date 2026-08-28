"""
Periodic health sampling for long unattended runs on the Pi.

The point of this module is post-mortem, not real-time: when the board has
been up for a week and something went wrong overnight, the heartbeat line in
the journal is what turns "it died" into "it died after Chromium's memory
climbed for nineteen hours". Everything here is read from /proc, /sys and
vcgencmd - no dependencies, nothing that costs anything between samples.

It runs as a daemon thread started from app.py at import time, so there is
exactly one of it (systemd/labtrack.service pins gunicorn to -w 1). Every
probe is individually guarded: a missing file or an unexpected format makes
that one field "?" rather than killing the thread, because a monitor that
dies silently partway through a soak test is worse than no monitor at all.
"""

import logging
import os
import subprocess
import threading
import time

log = logging.getLogger("labtrack.health")

SAMPLE_INTERVAL_S = 60

# Thresholds that promote the heartbeat from INFO to WARNING, so a soak test
# can be reviewed with `journalctl -p warning` without reading every line.
MEM_AVAIL_WARN_MB = 150
DISK_FREE_WARN_MB = 500
TEMP_WARN_C = 75.0

# How long /api/state can go unrequested by the kiosk before we assume the
# browser (not the server) has died. The page polls every 1.5s; a couple of
# minutes of silence means Chromium crashed, the renderer was OOM-killed, or
# the display session went away - none of which the server would otherwise
# notice, because nothing errors on this end.
KIOSK_SILENT_WARN_S = 120

# Bits of vcgencmd's throttled word. The low four are "right now", the same
# four shifted up by 16 are "has happened since boot". Undervoltage is the
# single most common cause of a Pi that locks up or reboots for no visible
# reason, and it is invisible from inside the app any other way.
_THROTTLE_BITS = {
    0: "undervoltage now",
    1: "arm frequency capped now",
    2: "currently throttled",
    3: "soft temperature limit now",
    16: "undervoltage since boot",
    17: "arm frequency capped since boot",
    18: "throttled since boot",
    19: "soft temperature limit since boot",
}


def _read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def _meminfo_mb():
    """(available, total) system memory in MB."""
    text = _read("/proc/meminfo")
    if not text:
        return None, None
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemAvailable", "MemTotal"):
            try:
                values[key] = int(rest.split()[0]) // 1024
            except (ValueError, IndexError):
                pass
    return values.get("MemAvailable"), values.get("MemTotal")


def _process_mem_mb(pid):
    """
    Proportional set size for one process, in MB, falling back to RSS.

    Pss is what we actually want for Chromium: it has a dozen-odd processes
    sharing a lot of mapped memory, so summing RSS across them double-counts
    badly. smaps_rollup gives Pss cheaply (one line, kernel-aggregated),
    but it isn't readable for every process, hence the fallback.
    """
    rollup = _read(f"/proc/{pid}/smaps_rollup")
    if rollup:
        for line in rollup.splitlines():
            if line.startswith("Pss:"):
                try:
                    return int(line.split()[1]) / 1024
                except (ValueError, IndexError):
                    break
    status = _read(f"/proc/{pid}/status")
    if status:
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                try:
                    return int(line.split()[1]) / 1024
                except (ValueError, IndexError):
                    break
    return None


def _chromium_mem_mb():
    """Total memory across every Chromium process, and how many there are."""
    total, count = 0.0, 0
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return None, 0
    for pid in pids:
        comm = _read(f"/proc/{pid}/comm")
        if not comm or "chromium" not in comm.lower():
            continue
        # The process can exit between listing and reading it; that's normal
        # and just means it doesn't count toward this sample.
        mb = _process_mem_mb(pid)
        if mb is not None:
            total += mb
            count += 1
    return (total if count else None), count


def _throttled():
    """(raw hex string, list of human-readable flags) from vcgencmd."""
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, []       # not a Pi, or vcgencmd not installed
    _, _, value = out.partition("=")
    try:
        bits = int(value, 16)
    except ValueError:
        return None, []
    return value, [text for bit, text in _THROTTLE_BITS.items() if bits & (1 << bit)]


def _temp_c():
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    try:
        return int(raw.strip()) / 1000.0
    except (AttributeError, ValueError):
        return None


def _disk_free_mb(path="/"):
    try:
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize) // (1024 * 1024)
    except OSError:
        return None


def _fmt(value, suffix="", spec=".0f"):
    return "?" if value is None else f"{value:{spec}}{suffix}"


def sample(kiosk_idle_s=None):
    """
    Take one reading. Returns (message, is_warning, concerns).

    Split out from the loop so it can be called directly - the /api/health
    route uses it, which also makes it checkable on a dev machine where most
    of these probes come back empty.
    """
    mem_avail, mem_total = _meminfo_mb()
    chromium_mb, chromium_procs = _chromium_mem_mb()
    app_mb = _process_mem_mb("self")
    temp = _temp_c()
    disk_free = _disk_free_mb()
    throttle_raw, throttle_flags = _throttled()
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = None

    concerns = list(throttle_flags)
    if mem_avail is not None and mem_avail < MEM_AVAIL_WARN_MB:
        concerns.append(f"only {mem_avail}MB memory available")
    if disk_free is not None and disk_free < DISK_FREE_WARN_MB:
        concerns.append(f"only {disk_free}MB disk free")
    if temp is not None and temp > TEMP_WARN_C:
        concerns.append(f"cpu at {temp:.1f}C")
    if kiosk_idle_s is not None and kiosk_idle_s > KIOSK_SILENT_WARN_S:
        concerns.append(f"kiosk has not polled for {kiosk_idle_s:.0f}s")

    parts = [
        f"mem_avail={_fmt(mem_avail, 'M')}",
        f"mem_total={_fmt(mem_total, 'M')}",
        f"app_mem={_fmt(app_mb, 'M', '.1f')}",
        f"chromium_mem={_fmt(chromium_mb, 'M', '.1f')}",
        f"chromium_procs={chromium_procs}",
        f"load1={_fmt(load1, '', '.2f')}",
        f"temp={_fmt(temp, 'C', '.1f')}",
        f"disk_free={_fmt(disk_free, 'M')}",
        f"throttled={throttle_raw or '?'}",
        f"kiosk_idle={_fmt(kiosk_idle_s, 's')}",
        f"uptime={_fmt(_uptime_s(), 's')}",
    ]
    message = "health " + " ".join(parts)
    if concerns:
        message += " | " + "; ".join(concerns)
    return message, bool(concerns), concerns


def _uptime_s():
    text = _read("/proc/uptime")
    try:
        return float(text.split()[0])
    except (AttributeError, ValueError, IndexError):
        return None


def start_health_monitor(kiosk_idle_fn=None, interval_s=SAMPLE_INTERVAL_S):
    """
    Start the heartbeat thread. kiosk_idle_fn, if given, returns seconds
    since the kiosk page last polled /api/state (None if it never has).
    """

    def loop():
        # One line at startup with the fixed facts, so each boot in the
        # journal is self-identifying without cross-referencing anything.
        _, mem_total = _meminfo_mb()
        log.info(
            "health monitor started: interval=%ss mem_total=%s cpus=%s",
            interval_s, _fmt(mem_total, "M"), os.cpu_count(),
        )
        while True:
            try:
                idle = kiosk_idle_fn() if kiosk_idle_fn else None
                message, is_warning, _ = sample(idle)
                log.warning(message) if is_warning else log.info(message)
            except Exception:
                # Never let a bad sample end the soak test's only heartbeat.
                log.exception("health sample failed")
            time.sleep(interval_s)

    thread = threading.Thread(target=loop, name="health-monitor", daemon=True)
    thread.start()
    return thread
