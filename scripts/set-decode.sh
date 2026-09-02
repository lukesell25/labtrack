#!/usr/bin/env bash
#
# Switch the kiosk between hardware and software video decode.
#
# Hardware is the default and what the Pi wants: 1080p H.264 on the V3D/V4L2
# path costs almost nothing, and software decode costs a core. Software is an
# escape hatch - every video failure this board has had came from the V4L2
# stack (the end-of-stream drain wedge, and the bcm2835_codec stop_streaming
# oops that freezes the display), and none of them reproduce without it. Use
# it to keep the board up while you work out what the hardware path is doing.
#
# Takes effect when Chromium next launches, i.e. at the next reboot - the
# kiosk autostart reads config/decode-mode on every start.
#
# Usage:   scripts/set-decode.sh [hardware|software]
#          scripts/set-decode.sh              # report the current setting
set -euo pipefail

cd "$(dirname "$0")/.."
FILE=config/decode-mode

current=hardware
if [ -r "$FILE" ]; then
  current=$(tr -d '[:space:]' < "$FILE")
  [ -n "$current" ] || current=hardware
fi

mode="${1:-}"
if [ -z "$mode" ]; then
  echo "video decode: $current"
  echo ""
  echo "Usage: scripts/set-decode.sh [hardware|software]"
  exit 0
fi

case "$mode" in
  hardware|software) ;;
  *)
    echo "Unknown mode '$mode' - expected 'hardware' or 'software'." >&2
    exit 1
    ;;
esac

if [ "$mode" = "$current" ]; then
  echo "video decode is already $mode - nothing to do."
  exit 0
fi

echo "$mode" > "$FILE"
echo "video decode: $current -> $mode"
echo ""
echo "Chromium reads this at launch, so reboot to apply:  sudo reboot"
echo "Confirm afterwards with:  journalctl -t labtrack-chromium | grep 'video decode'"
if [ "$mode" = software ]; then
  echo ""
  echo "Note: software decode costs roughly a full CPU core while the video"
  echo "plays. Expect load1 up by ~1.0 and a warmer Pi in the health line."
fi
