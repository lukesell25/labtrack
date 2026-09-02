#!/usr/bin/env bash
#
# Summarise a long unattended run on the Pi.
#
# Reviewing a week-long soak test by scrolling journalctl is hopeless, so
# this pulls out the handful of things that actually explain a bad night:
# reboots, OOM kills, undervoltage, app errors, whatever the kiosk page
# reported about itself, and the memory trend over the window.
#
# Usage:   scripts/soak-report.sh ["<journalctl --since expression>"]
# Example: scripts/soak-report.sh "2 days ago"
#
# Run it on the Pi. Reading the journal needs membership of the adm or
# systemd-journal group (the default `admin` user has it); if sections come
# back empty and you expected content, re-run with sudo.
set -uo pipefail

SINCE="${1:-7 days ago}"
UNIT=labtrack
CHROMIUM_TAG=labtrack-chromium

rule() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
none() { printf '  (none)\n'; }

# Prints stdin indented, or "(none)" if it was empty - so an empty section
# reads as a clean result rather than looking like the command failed.
body() {
  local out
  out=$(grep -v '^-- No entries --$' | grep -v '^$')
  if [ -z "$out" ]; then none; else printf '%s\n' "$out" | sed 's/^/  /'; fi
}

printf '\033[1mLabTrack soak report\033[0m  (since: %s)\n' "$SINCE"
printf 'generated: %s on %s\n' "$(date -Is)" "$(hostname)"

rule "Right now"
uptime | body
if command -v vcgencmd >/dev/null 2>&1; then
  printf '  throttled: %s  temp: %s\n' \
    "$(vcgencmd get_throttled)" "$(vcgencmd measure_temp 2>/dev/null || echo '?')"
fi
curl -s --max-time 5 http://localhost:5000/api/health | body

rule "Service state"
systemctl is-active "$UNIT" >/dev/null 2>&1 \
  && printf '  active, %s restart(s) recorded\n' \
       "$(systemctl show "$UNIT" -p NRestarts --value)" \
  || printf '  NOT ACTIVE\n'
systemctl show "$UNIT" -p ActiveEnterTimestamp --value | sed 's/^/  running since /'

rule "Boots in this window (more than one = the Pi restarted)"
# journalctl --list-boots ignores --since, so filter on each boot's first
# entry ourselves - otherwise a long-lived Pi dumps its entire history here.
since_epoch=$(date -d "$SINCE" +%s 2>/dev/null || echo 0)
journalctl --list-boots --no-pager 2>/dev/null | awk -v since="$since_epoch" '
    /^ *IDX/ { next }                       # header on newer journalctl
    {
      cmd = "date -d \"" $4 " " $5 "\" +%s 2>/dev/null";
      cmd | getline epoch; close(cmd);
      # Unparseable timestamp: show the line rather than silently hiding a boot.
      if (epoch == "" || epoch + 0 >= since + 0) { print; n++ }
    }
    END {
      if (n > 1) printf "\n%d boots in this window - the Pi restarted %d time(s).\n", n, n - 1;
      else if (n == 1) print "\nSingle boot, no restarts in this window.";
    }' | body

rule "Kernel OOM kills"
journalctl -k --since "$SINCE" --no-pager 2>/dev/null \
  | grep -Ei 'out of memory|oom-kill|oom_reaper|killed process' | body

rule "Undervoltage / throttling (a common cause of unexplained lockups)"
journalctl -k --since "$SINCE" --no-pager 2>/dev/null \
  | grep -Ei 'voltage|throttl' | body

rule "Kernel oopses and driver warnings"
# A driver that dies in a workqueue takes its locks with it, and everything
# that later needs one parks in uninterruptible sleep forever. The board then
# freezes while the app keeps serving and every other probe here reads normal
# - which is exactly how a bcm2835_codec oops went unnoticed for five hours.
# Only the headline lines: a single oops is a hundred lines of register dump,
# and the pointer into the journal is what is wanted here.
# Case-sensitive on purpose: the kernel cases these consistently, and a
# case-insensitive "BUG:" also matches innocent lines like "mmc_debug:0".
# "blocked for more than" is the hung-task detector, which names the tasks
# stuck in uninterruptible sleep directly when it is enabled.
journalctl -k --since "$SINCE" --no-pager 2>/dev/null \
  | grep -E 'Internal error|Oops|BUG: |WARNING: CPU|driver bug|Unable to handle|blocked for more than' \
  | head -40 | body

rule "Reboots the app asked for"
# request_reboot() in app.py, i.e. the board recovering itself from something
# only a reboot clears. More than one or two in a window is the interesting
# case: it means whatever is wrong is coming straight back.
journalctl -u "$UNIT" --since "$SINCE" --no-pager 2>/dev/null \
  | grep -E 'rebooting (in [0-9]+s|now)|not rebooting' | body

rule "App warnings and errors"
journalctl -u "$UNIT" --since "$SINCE" -p warning --no-pager 2>/dev/null \
  | grep -v 'labtrack.health' | body

rule "Health warnings (memory, temperature, throttling, dead kiosk)"
journalctl -u "$UNIT" --since "$SINCE" -p warning --no-pager 2>/dev/null \
  | grep 'labtrack.health' | body

rule "Reported by the kiosk page itself"
journalctl -u "$UNIT" --since "$SINCE" --no-pager 2>/dev/null \
  | grep -E 'client [a-z-]+:' | body

rule "Chromium complaints"
journalctl -t "$CHROMIUM_TAG" --since "$SINCE" --no-pager 2>/dev/null \
  | grep -Ei 'error|fail|v4l2|decode|crash' | tail -40 | body

rule "Trend"
# The heartbeat line is a flat key=value list, so first/last/min/max per
# field is enough to show a leak: a steadily falling mem_avail or a rising
# chromium_mem over a multi-day window is the shape to look for.
#
# load1 and dstate are here for a different shape. Load counts tasks in
# uninterruptible sleep as well as running ones, so a load pinned at the core
# count (4 on a Pi 4) with a cold CPU and a non-zero dstate is a wedge, not
# work - nothing is running, things are stuck behind a dead kernel thread.
journalctl -u "$UNIT" --since "$SINCE" --no-pager 2>/dev/null \
  | grep -F 'health mem_avail=' \
  | awk '
      function val(line, key,   start, len) {
        if (!match(line, key "=[0-9.]+")) return "";
        start = RSTART + length(key) + 1;
        len   = RLENGTH - length(key) - 1;
        return substr(line, start, len) + 0;
      }
      BEGIN { nkeys = split("mem_avail chromium_mem app_mem load1 dstate", keys, " ") }
      {
        n++;
        for (i = 1; i <= nkeys; i++) {
          key = keys[i];
          v = val($0, key);
          if (v == "") continue;
          if (!(key in first)) { first[key] = v; min[key] = v; max[key] = v; }
          last[key] = v;
          if (v < min[key]) min[key] = v;
          if (v > max[key]) max[key] = v;
        }
      }
      END {
        if (!n) exit;
        printf "samples: %d (about %.1f hours at one per minute)\n", n, n / 60;
        for (i = 1; i <= nkeys; i++) {
          key = keys[i];
          if (!(key in first)) continue;
          # load1 and dstate are counts, not megabytes.
          unit = (key == "load1" || key == "dstate") ? "" : "M";
          printf "%-13s first %8.1f%s   last %8.1f%s   min %8.1f%s   max %8.1f%s   change %+.1f\n",
                 key, first[key], unit, last[key], unit, min[key], unit, max[key], unit,
                 last[key] - first[key];
        }
      }' | body

printf '\nFull logs:  journalctl -u %s --since "%s"\n' "$UNIT" "$SINCE"
printf 'Chromium:   journalctl -t %s --since "%s"\n' "$CHROMIUM_TAG" "$SINCE"
