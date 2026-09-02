#!/usr/bin/env bash
#
# Build the long-playing background video the kiosk actually uses.
#
# Why this exists: main.js loops the background by seeking back to 0 before
# end-of-stream (see LOOP_TAIL_S), because letting playback drain the Pi's
# hardware decoder wedges it permanently. That seek is itself a
# VIDIOC_STREAMOFF on the V4L2 m2m decoder, and bcm2835_codec has a race in
# stop_streaming that oopses the kernel roughly once in every couple of
# thousand seeks - which on a 12s loop is about once every seven hours. It
# leaves tasks stuck in uninterruptible sleep and the display frozen; only a
# reboot clears it. See "Background video freezes" in README.md for the
# journal trace.
#
# We cannot fix the driver, so we seek less often: concatenate the 12s master
# loop N times into a 10-minute file, and the wrap happens 50x less often.
# This is a stream copy, not a re-encode - identical bytes, no quality loss,
# a few seconds to run.
#
# The master (static/media/background.mp4) is the only video tracked in git,
# because a 900MB generated file has no business in a public repo whose
# history already had to be rewritten once to purge 131MB of video. So the
# short file ships and the long one is built here, on the Pi, after a clone.
#
# Usage:   scripts/build-loop.sh [minutes]      (default 10)
# Re-run it after replacing background.mp4.
set -euo pipefail

cd "$(dirname "$0")/.."

SRC=static/media/background.mp4
OUT=static/media/background-long.mp4

# Must match LOOP_TAIL_S in static/js/main.js. The master is built as
# "visible loop + a repeat of its own first TAIL_S seconds" (README step 7),
# so the part that actually plays is everything before this tail.
TAIL_S=5

TARGET_MIN="${1:-10}"

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "ffmpeg and ffprobe are required (sudo apt install -y ffmpeg)" >&2
  exit 1
fi
if [ ! -f "$SRC" ]; then
  echo "$SRC not found - nothing to build from." >&2
  exit 1
fi

duration=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$SRC")
loop_s=$(awk -v d="$duration" -v t="$TAIL_S" 'BEGIN { printf "%.6f", d - t }')

# A master at or under the tail length has no visible portion at all, and the
# arithmetic below would divide by zero or go negative.
if awk -v l="$loop_s" 'BEGIN { exit !(l <= 0.1) }'; then
  echo "$SRC is ${duration}s, which leaves no visible loop once the ${TAIL_S}s tail" >&2
  echo "is removed. Re-encode it per README step 7." >&2
  exit 1
fi

# Cut the loop by frame count, never by -t. A stream copy keeps whole
# packets, and `-t "$loop_s"` rounds outward: on the shipped 17s master it
# hands back 362 frames instead of 360, so every one of the 50 joins would
# replay two frames. That is a visible hitch at each wrap - precisely the
# seam the master's encode (README step 7) exists to avoid.
fps=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of csv=p=0 "$SRC")
loop_frames=$(awk -v l="$loop_s" -v f="$fps" 'BEGIN { split(f, r, "/"); printf "%d", l * r[1] / r[2] + 0.5 }')

repeats=$(awk -v m="$TARGET_MIN" -v l="$loop_s" 'BEGIN { n = int((m * 60) / l); print (n < 1 ? 1 : n) }')
visible=$(awk -v n="$repeats" -v l="$loop_s" 'BEGIN { printf "%.1f", n * l }')
total=$(awk -v v="$visible" -v t="$TAIL_S" 'BEGIN { printf "%.1f", v + t }')

echo "==> master ${duration}s = ${loop_s}s visible loop (${loop_frames} frames at ${fps}fps) + ${TAIL_S}s tail"
echo "==> building ${repeats} x ${loop_s}s = ${visible}s visible (+${TAIL_S}s tail = ${total}s)"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Split the master back into its two parts. Both cuts land on the keyframe at
# loop_s - the master is encoded with -g 60 at 30fps, so there is one every
# 2s - which is what lets these be stream copies rather than re-encodes.
ffmpeg -v error -y -i "$SRC" -frames:v "$loop_frames" -c copy "$work/loop.mp4"
ffmpeg -v error -y -ss "$loop_s" -i "$SRC" -c copy "$work/tail.mp4"

for _ in $(seq "$repeats"); do echo "file '$work/loop.mp4'"; done > "$work/list.txt"
echo "file '$work/tail.mp4'" >> "$work/list.txt"

# +faststart is not optional: with the moov index at the end (ffmpeg's
# default) Chromium range-requests the tail before it can play, and on the Pi
# that fetch pattern stalls partway through with no error code. See
# Performance in CLAUDE.md.
ffmpeg -v error -y -f concat -safe 0 -i "$work/list.txt" \
       -c copy -movflags +faststart "$OUT"

built=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT")
moov=$(grep -abo moov "$OUT" | head -1 | cut -d: -f1)
echo "==> wrote $OUT"
echo "    duration ${built}s, $(du -h "$OUT" | cut -f1), moov at byte ${moov:-?}"
echo ""
echo "The kiosk prefers this file over background.mp4 automatically"
echo "(BACKGROUND_VIDEO_CANDIDATES in app.py). Reload the kiosk page to pick it up."
