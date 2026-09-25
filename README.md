# LabTrack

A small Flask app for your Raspberry Pi that:
- Shows who's in the lab, who's away on campus (the server room, a lecture
  hall), and who's out (lunch, gone home) - current status only. It is not a
  timesheet: no check-in times are recorded or shown anywhere, and everyone
  is reset to out once a day
- Lets people check themselves in and out from a keyboard in front of the
  board: arrow keys to pick a name, Enter to check in or out (a mouse works
  too)
- Shows a live status board / screensaver on the Pi's own screen
- Serves a read-only "who's here" dashboard viewable from any other PC on
  the network

There's no card reader and no login: anyone at the board can set anyone's
status, like a whiteboard. (It used to identify people by CAC tap; that was
removed.)

## Developing locally, away from the Pi

`scripts/setup.sh` is the **Pi deployment installer** - it installs a
kiosk-mode Chromium, ffmpeg and systemd services, and none of that belongs on
a regular dev machine. For editing and testing the app on Windows, WSL, a Mac
or anything else:

```bash
python3 -m venv venv
source venv/bin/activate        # or venv\Scripts\activate on Windows
pip install -r requirements.txt
python3 app.py
```

Then open `http://localhost:5000` (kiosk display) and
`http://localhost:5000/dashboard` in a browser. Drive the kiosk exactly as
people will on the Pi - arrow keys and Enter, see "Checking in" below - or
change a status from another terminal (or curl/Postman):

```bash
curl -X POST http://localhost:5000/api/set-status \
     -H "Content-Type: application/json" \
     -d '{"member_id": 1}'
```

Each call toggles that member, so run it twice to exercise both the
check-in toast and the checkout note prompt. Add `"action"` (`in`, `away` or
`out`) and, for away, `"location"` to record something specific rather than a
toggle - `{"member_id": 1, "action": "away", "location": "Server room"}`
puts them on the board as away.

## Project layout

```
labtrack/
  app.py                  Flask app + routes
  database.py              SQLite schema + queries
  config/members.json       The roster: a list of names (edit, then restart)
  config/objectives.json    Screensaver text content (edit any time)
  config/locations.json     Preset "away" places for the leaving dialog (edit any time)
  config/decode-mode        hardware|software video decode (scripts/set-decode.sh)
  templates/                Kiosk + dashboard HTML
  static/                   CSS, JS, img/ (the FAIR logo, plus the dark-background
                            version the kiosk shows), and a media/ folder for
                            slide pictures and the background video
  health.py                 Once-a-minute health heartbeat for long runs
  webauth.py                The shared password for the dashboard from other PCs
  systemd/labtrack.service   Runs the app on boot
  systemd/labtrack-reboot.*  Timer + unit for the nightly 00:00 reboot
  systemd/*.rules            polkit rule letting the app reboot the Pi
  autostart/*.desktop       Launches Chromium kiosk mode on desktop login
  scripts/setup.sh          Installs everything below in one go
  scripts/soak-report.sh    Summarises a long unattended run
  scripts/build-loop.sh     Builds the long-playing background video (run on the Pi)
  scripts/set-decode.sh     Switches the kiosk between hardware/software decode
```

## Step-by-step setup on the Pi

These steps assume a fresh Raspberry Pi OS (64-bit, Desktop) install, keyboard
attached, connected to your network.

### 1. Get the project onto the Pi

Copy the whole `labtrack/` folder to the Pi, e.g. via `scp`, a USB drive, or
`git clone` if you push it to a repo. Put it at `/home/admin/labtrack` (the
systemd service file assumes this path and the `admin` username — edit
`systemd/labtrack.service` if you use a different location or username).

### 2. Run the setup script

```bash
cd /home/admin/labtrack
chmod +x scripts/setup.sh
./scripts/setup.sh
```

This installs `chromium-browser` and `ffmpeg`, creates a Python virtual
environment, installs the pip requirements, installs and enables the
`labtrack` systemd service, installs the kiosk autostart entry, and enables
desktop auto-login via `raspi-config`.

You'll be prompted for your sudo password partway through.

### 3. Fill in your roster

`config/members.json` is a list of names, exactly as they should appear on
the board:

```json
{
  "members": ["Ada Vance", "Grace Hopper"]
}
```

Restart to pick up a change:

```bash
sudo systemctl restart labtrack
```

The strip along the bottom of the kiosk is alphabetical and doesn't move
around, so people quickly learn where their name is.

**Adding and removing people.** The file is the source of truth: anyone added
appears on the board after a restart, and anyone removed disappears from the
kiosk and the dashboard. Their row is retired rather than deleted, so adding
the same name back returns them on the same row. The restart logs what
changed:

```
INFO labtrack.db: Roster sync deactivated 1 member(s): Ada Vance
```

The roster is keyed on the **name**, so fixing a spelling reads as "one
person left, a different one joined" - harmless, since the only thing lost is
their current status, which starts over as "out".

### 4. Plug in a keyboard

Any USB keyboard in front of the board. It is how people check in and out,
so leave it there. A mouse is optional - clicking a name does the same thing
as selecting it with the keys - and the pointer hides itself after a few
seconds of stillness so it doesn't sit on the display all day.

### 5. Try a check-in end to end

On the kiosk: press → to highlight the first name, Enter to open it, Enter
again to check in. The board shows the confirmation and the name turns
green. Do it again for the same person to see the leaving dialog - Check
out, or one of the away places - and the optional note prompt after a
checkout. The header of the board spells the keys out for anyone new
("← → choose your name · Enter check in / out"); see "Checking in" below for
the whole flow.

### 6. Pick your away places

The leaving dialog offers preset places for "still at work, elsewhere",
from `config/locations.json`:

```json
{
  "locations": ["Server room", "Lecture hall"]
}
```

Edit the list any time; the kiosk re-reads it within a minute, no restart.
"Other…" is always offered as well, for anything not on the list.

### 7. Add screensaver content

The kiosk cycles the objectives, one full-panel slide at a time, over an
optional looping background video.

- Edit `config/objectives.json` any time — no restart needed, it's re-read
  every 60 seconds by the kiosk page. Each objective is either a plain
  string, or an object with a picture beside the text:

  ```json
  "objectives": [
    "Finalize Q3 experiment protocol",
    { "text": "Calibrate sensor rig #2", "image": "rig2.jpg" }
  ]
  ```

  Picture files go in `static/media/` and are named here by filename only.
  Around 800px wide is the right size — that's how large they actually
  render on the 1080p panel, and anything bigger just costs the Pi decode
  time without looking better. If a picture is missing or won't load, that
  slide quietly falls back to text only.
- **Background video** (optional): drop one `.mp4` into `static/media/` named
  `background.mp4`. It loops continuously behind every slide, with a flat dim
  over it so the text stays readable. Delete it for no background.

  The filename is chosen server-side by `BACKGROUND_VIDEO_CANDIDATES` in
  `app.py`, which prefers `background-long.mp4` and falls back to
  `background.mp4`. Edit that tuple to use different footage; set it to `()`
  for no background at all. Nothing in `main.js` names a file any more — it
  reads what the server rendered into `data-video`.

  **After changing the video, rebuild the long loop:**

  ```bash
  scripts/build-loop.sh 10        # 10-minute loop; re-run after any change
  ```

  **If the video stutters on the Pi**, build the loop at 720p instead:

  ```bash
  scripts/build-loop.sh 10 --height 720
  ```

  That re-encodes the master at 720p on the Pi (a few minutes) before
  building the loop - 44% of the pixels for the Pi to decode and composite
  every frame, at the cost of a softer picture behind the dim. The master in
  git stays 1080p; run the script without `--height` to go back. Compare
  `video_fps` in the health heartbeat (below) before and after rather than
  judging by eye.

  This is not optional polish. The kiosk loops by seeking back to the start
  before end-of-stream, and each of those seeks is a chance to hit a
  `bcm2835_codec` kernel bug that freezes the whole display — see "Background
  video freezes" below. `build-loop.sh` stream-copies the 12s master into a
  10-minute file so the seek happens 50x less often. It is lossless (no
  re-encode) and takes about two seconds. `scripts/setup.sh` runs it for you
  on first install.

  Only `background.mp4` is tracked in git; `.gitignore` excludes every other
  video in `static/media/`, which includes the ~900MB `background-long.mp4`
  this produces. Keep your source footage and any encode experiments outside
  the repo or under those ignore rules — committed binaries are permanent,
  and this history already had to be rewritten once to remove 131MB of them.
  The Pi picks the master up through `git pull` and builds the long file
  locally.

  **The last 5 seconds of the file never play.** `main.js` wraps playback
  back to the start early, because letting it reach the end of the file
  permanently wedges the Pi's hardware decoder (see below).

  The trick that makes this free rather than lossy: **encode your loop, then
  append a repeat of its own first 5 seconds.** Playback then shows exactly
  your whole loop and wraps at its true loop point, and the appended 5s is
  never seen — it exists only to keep the decoder away from end-of-stream.
  `-stream_loop 1` plays the source twice and `-frames:v` cuts it to length:

  ```bash
  # source is a seamless 12s 1080p60 loop -> 17s output (12s visible + 5s tail)
  # frames = (visible_seconds + 5) * 30    e.g. (12 + 5) * 30 = 510
  ffmpeg -stream_loop 1 -i source.mp4 -an \
         -vf "select='not(mod(n\,2))',setpts=N/30/TB" -frames:v 510 \
         -c:v libx264 -preset slow -profile:v high -level:v 4.0 -crf 16 \
         -maxrate 12M -bufsize 24M -pix_fmt yuv420p -r 30 -g 60 \
         -movflags +faststart background.mp4
  ```

  `select='not(mod(n,2))'` takes every second frame to go 60→30fps. Use it
  rather than plain `-r 30`, which picks frames with a drifting phase — that
  makes the appended tail no longer line up with the start and puts a visible
  jump at the wrap point. If your source is already 30fps, drop the `select`
  and `setpts` filter entirely and just use `-frames:v`. If it needs
  rescaling, add
  `-vf "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080"`.

  **Bitrate is the main quality lever, and 4 Mb/s is too low** for detailed
  footage. Measured on the current clip against the source, one aligned
  frame: 4 Mb/s scored 31.7 dB PSNR, 8 Mb/s 36.9 dB, 12 Mb/s 39.1 dB, 16 Mb/s
  41.0 dB, 20 Mb/s 42.7 dB. There is no knee — it is a straight size/quality
  trade, so pick by how much space you want to spend. 12 Mb/s (~25 MB for
  17s) is the shipped setting. **Stay at `-level:v 4.0`**, which is what the
  Pi is known to decode here; above ~20 Mb/s x264 will need level 4.2, which
  is untested on this hardware.

  To check a loop is seamless before encoding, compare its last frame to its
  first and to a neighbour — if last-vs-first is about the same as
  one-frame-vs-next, it loops cleanly:

  ```bash
  ffmpeg -i src.mp4 -vf "select=eq(n\,0)"   -vframes 1 f0.png
  ffmpeg -i src.mp4 -vf "select=eq(n\,1)"   -vframes 1 f1.png
  ffmpeg -i src.mp4 -vf "select=eq(n\,719)" -vframes 1 flast.png   # last frame
  ffmpeg -i flast.png -i f0.png -lavfi psnr -f null -   # the loop seam
  ffmpeg -i f1.png    -i f0.png -lavfi psnr -f null -   # one frame of motion
  ```

  **`-movflags +faststart` is not optional.** Without it ffmpeg writes the
  `moov` index at the *end* of the file, forcing Chromium to fetch the tail
  with a separate range request before it can play anything — so the board
  shows nothing until the whole tail arrives. Fix an existing file without
  re-encoding it:

  ```bash
  ffmpeg -i background.mp4 -c copy -movflags +faststart fixed.mp4
  ```

  To check a file, confirm `moov` sits near the start rather than the end:

  ```bash
  grep -abo moov background.mp4 | head -1
  ```

  **If the video freezes a few seconds in and never recovers**, with the
  clock and check-ins still working, the cause is almost certainly *not* the
  file. Chromium drains the hardware decoder as playback approaches the end
  of the stream, and the Pi's `bcm2835-codec` V4L2 drain never completes —
  the picture stops with no error code and `readyState` drops from 4 to 2.
  It reproduces on any clip, at any resolution, bitrate or profile, always
  at `duration` minus ~3.2s. That is exactly what the early wrap-around in
  `main.js` exists to avoid, so before re-encoding anything, check that the
  `<video>` in `templates/index.html` still has **no `loop` attribute** and
  that `LOOP_TAIL_S` is still comfortably larger than 3.3. To confirm the
  decoder is the culprit rather than the file, launch Chromium by hand with
  `--disable-accelerated-video-decode` — the same file will then loop
  forever, at the cost of far more CPU than the kiosk can spare in
  production.

  The video also stays visible behind the check-in dialog and
  confirmation, dimmed to the same level as behind a slide, so the board
  never cuts to a flat panel mid-check-in.

  H.264 at exactly 1920x1080 is both the panel's native resolution and
  inside the Pi 4's hardware decode ceiling (1920x1920); HEVC/VP9/AV1 or
  anything wider falls back to software decode and pegs the CPU. `-an`
  drops the audio track — the kiosk plays muted, so decoding audio is pure
  waste. Make the last frame resemble the first, since it restarts on a hard
  cut. If the file is missing or won't decode, the slides fall back to the
  flat panel background.

  **Do not try to keep the clip short.** Duration costs nothing per frame,
  and a longer loop is actively safer here — it is the whole point of
  `build-loop.sh`. What matters is the encode settings above, not the length.

### 8. Reboot and confirm the kiosk comes up unattended

```bash
sudo reboot
```

The Pi should boot straight to the desktop (auto-login) and Chromium should
launch full-screen against `http://localhost:5000` automatically.

**If you already ran `setup.sh` before this was fixed** (autostart silently
did nothing, or Chromium prompted to unlock a keyring before it would load
anything): current Raspberry Pi OS (Bookworm/trixie) uses `labwc`, a
Wayland compositor, as its default desktop - it does not read
`~/.config/autostart/*.desktop` files the way the older X11/LXDE desktop
did, so that autostart entry silently does nothing.

Two things to know if you're debugging this by hand:

- **`labwc` runs the autostart file with `sh` (dash), ignoring its
  shebang line.** Bash-only syntax like `/dev/tcp` will silently fail
  under dash and hang the script forever - stick to POSIX sh and external
  tools like `curl` in this file. (`ps aux | grep autostart` will show you
  exactly which interpreter labwc actually used, which is the fastest way
  to catch this class of bug.)
- **Chromium's login-keyring prompt has two separate fixes depending on
  how Chromium is launched.** `--password-store=basic` passed at launch
  time (as the kiosk autostart script does) only covers *that* launch.
  For it to apply when Chromium is opened manually too, it needs to go in
  `/etc/chromium.d/` - Debian's mechanism for flags that apply to every
  invocation of the `chromium` wrapper, for any user. `setup.sh`
  installs this automatically (`autostart/99-labtrack-password-store`).
- **The `chromium-browser` apt package doesn't necessarily install a
  `chromium-browser` binary.** On current Raspberry Pi OS (Debian
  trixie), the package pulls in Debian's `chromium`, and the actual
  command on your `PATH` is just `chromium` - no `chromium-browser`
  compatibility symlink. If the autostart script silently does nothing
  even though the LabTrack service is confirmed running, try the exact
  launch command by hand (`chromium --kiosk ... http://localhost:5000`)
  and see if you get `command not found` instead of a browser window.
  This repo's scripts already call `chromium`, not `chromium-browser` -
  this note is here in case a future Raspberry Pi OS release renames it
  again.

Fix an existing install by installing both files directly:

```bash
mkdir -p ~/.config/labwc
cp autostart/labwc-autostart ~/.config/labwc/autostart
chmod +x ~/.config/labwc/autostart

sudo mkdir -p /etc/chromium.d
sudo cp autostart/99-labtrack-password-store /etc/chromium.d/99-labtrack-password-store

sudo reboot
```

Checking someone in from the keyboard (→, Enter, Enter) should show a
full-screen confirmation, then fade back to the status board/screensaver.

**If Chromium still can't reach the internet / still prompts for a
keyring even after the fix above:** the real cause is almost certainly
your Wi-Fi connection itself, not Chromium. If your connection was set up
as a per-user connection, its saved password lives in your login keyring -
and since desktop auto-login never enters a password, that keyring never
unlocks on boot, so NetworkManager can't retrieve the Wi-Fi password each
fresh boot. `--password-store=basic` only stops *Chromium's own* password
manager from touching the keyring; it does nothing for NetworkManager's
separate dependency on it.

The permanent fix is to make NetworkManager store the Wi-Fi password
itself, system-wide, so it never needs to ask a keyring for it:

```bash
nmcli connection show                       # find your Wi-Fi connection's name
sudo nmcli connection modify "<name>" 802-11-wireless-security.psk-flags 0
sudo nmcli connection modify "<name>" connection.permissions ""
sudo nmcli connection up "<name>"
sudo reboot
```

`psk-flags 0` tells NetworkManager to store the password directly in the
connection file (root-readable only, under `/etc/NetworkManager/system-connections/`)
instead of asking a per-user secret agent/keyring for it every time.
`connection.permissions ""` makes it a system-wide connection rather than
tied to your specific user session. After this, Wi-Fi should come up fully
on boot with no keyring involved at all, regardless of desktop session
state.

### 9. View the dashboard from another PC

Find the Pi's IP address (`hostname -I` on the Pi), then from any other
machine on the same network:

```
http://<pi-ip-address>:5000/dashboard
```

The browser will ask for a password. Leave the username blank - only the
password is checked. The Pi generates one the first time the app starts;
read it on the Pi with:

```bash
cat ~/labtrack/config/dashboard.key
```

To set one people can remember instead, write it into that file and restart
(`sudo systemctl restart labtrack`). It is read once at startup, so a change
needs the restart. Keep the file mode at 0600, and don't commit it - it's
gitignored, and it is per-Pi.

The kiosk itself is never prompted: requests from the Pi are exempt, so the
board keeps working through all of this. Everything from off the Pi needs
the password, including `/api/set-status` - without that, anyone on the
lab network could check people in and out.

**This is a lock on the door, not an encrypted tunnel.** There is no HTTPS
on this hop, so the password and the page contents cross the network in the
clear and anyone able to sniff the lab network can read both. That's an
accepted trade for a status board on a trusted LAN. If you need more
than that, the options in rough order of effort are: an ssh tunnel from the
viewing PC (`ssh -L 5000:localhost:5000 admin@<pi-ip>`, then browse
`http://localhost:5000/dashboard` - works today with no server change, but
one person at a time), Tailscale or another WireGuard mesh on the Pi and
each viewing PC, or a TLS-terminating reverse proxy. If you ever do put a
proxy in front, note that it breaks the loopback exemption: every request
would then appear to come from the Pi itself and skip the password entirely
(see the comment in `webauth.py`).

## Stepping away (still at work)

A member has three states, not two: **in** the lab, **out** (gone home, at
lunch), and **away** - at work but somewhere else, like the server room or
a lecture hall. A typical day reads in → away (server room) → in → out
(lunch) → in → away (lecture hall) → in → out. The board just says where
the person is now - never since when.

Leaving asks where to: the dialog for someone who is in offers **Check
out**, or one of the preset places under "Still at work, elsewhere"
(**Server room**, **Lecture hall**, ... - see "6. Pick your away places"),
or **Other...** to type one. Wherever they went is shown under the person's
name on the board and the dashboard, in blue with a hollow ring so "away" is
never mistaken for "in" from across the room. Selecting someone who is away
offers **Back in lab** or **Check out**.

## Checking in

Everything is done from the keyboard in front of the board (a mouse works
too). The top of the board says how: **← → choose your name · Enter check
in / out**.

1. Press ← or → to highlight your name on the strip along the bottom. The
   first press lands on the first (→) or last (←) name; Home and End jump to
   the ends. The highlight clears itself after 30 seconds, or on Escape.
2. Press **Enter**. The board asks, with the likeliest answer already
   selected: someone out gets **Check in**; someone in gets **Check out**
   plus the away places; someone away gets **Back in lab**.
3. Press **Enter** again to confirm - or use the arrow keys to pick a
   different button first. **Escape** cancels, and so does walking away:
   the dialog closes itself after 20 seconds, so a stray keypress never
   changes anything by itself.

So the everyday case is three keys: arrow to your name, Enter, Enter.

After a checkout the board offers an optional one-line note ("at lunch,
back at 14:00") - type it and press Enter twice to save, or Escape to skip.
The note shows under your name until you're back.

With a mouse, click your name instead of steps 1-2, then click a button.
The pointer hides itself after 8 seconds of stillness, so it doesn't sit on
the display all day. Status can also be changed from another machine on the
network - see "Day-to-day maintenance" below.

## Watching a long run

The board is meant to sit powered on for weeks, and the failure modes that
matter over that timescale are quiet ones: the video decoder wedging,
Chromium leaking memory until the kernel kills it, a marginal power supply
browning out the Pi at 3am. None of those announce themselves. This section
is the setup that makes them visible after the fact.

### One-time: make the journal survive a reboot

**Do this before any long test.** Raspberry Pi OS ships journald with
`Storage=auto` and no `/var/log/journal` directory, which means the journal
lives in RAM and is **wiped on every boot** — so if the Pi crashes or reboots,
the log explaining why is destroyed at exactly the moment you need it. Check
which mode you are in:

```bash
journalctl --disk-usage
```

If that says anything about `/run/log/journal`, the logs are volatile. Fix it:

```bash
sudo mkdir -p /var/log/journal
sudo tee /etc/systemd/journald.conf.d/labtrack.conf >/dev/null <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=500M
MaxRetentionSec=1month
EOF
sudo systemctl restart systemd-journald
journalctl --disk-usage      # should now say /var/log/journal
```

The 500M cap keeps the SD card from filling up; a month of heartbeats and
Chromium output fits comfortably inside it.

### What gets logged

Everything lands in the one journal, on a single timeline with the kernel's
own messages, so an app error and an OOM kill three seconds later are
obviously related:

| Source | Where it comes from | Read it with |
| --- | --- | --- |
| App errors, tracebacks (never who checked in - see "No time tracking" in CLAUDE.md) | `app.py` | `journalctl -u labtrack` |
| Health heartbeat, once a minute | `health.py` | `journalctl -u labtrack \| grep health` |
| Errors the kiosk page saw | `report()` in `main.js` → `/api/client-log` | `journalctl -u labtrack \| grep client` |
| Chromium's own output, and which decode path it launched with | the `logger` pipe in `autostart/labwc-autostart` | `journalctl -t labtrack-chromium` |
| OOM kills, undervoltage, resets | the kernel | `journalctl -k` |

The heartbeat line looks like this, and is INFO normally, WARNING when
something on it looks wrong:

```
INFO labtrack.health: health mem_avail=1204M mem_total=3792M app_mem=48.2M
chromium_mem=612.4M chromium_procs=11 load1=0.42 temp=54.7C disk_free=21740M
throttled=0x0 kiosk_idle=1s video_fps=30.0 video_dropped=0 uptime=486213s
```

Two fields are worth knowing about specifically:

- **`throttled`** is `vcgencmd get_throttled`. Anything other than `0x0` means
  the power supply is sagging or the Pi is overheating; undervoltage is the
  most common cause of a Pi that locks up or reboots with nothing in the logs,
  and it is invisible any other way. It is decoded into plain words on the
  same line when set.
- **`kiosk_idle`** is how long since the kiosk page last polled. The browser
  is the one part of the system that can die without anything erroring on the
  server, so a growing number here means Chromium crashed or its renderer was
  OOM-killed even though the app itself is fine. Over 120s and the heartbeat
  becomes a WARNING.
- **`video_fps`** is how many background-video frames actually reached the
  screen per second over the last minute, as the kiosk page measured it, and
  **`video_dropped`** how many Chromium dropped for arriving too late. The
  file is 30fps, so anything well under 30 is the video lagging. `-` means no
  video playing (or its first two minutes). This is the number to compare
  when trying to make the video smoother - e.g. before and after
  `build-loop.sh --height 720`, or hardware vs software decode.

A one-off sample without an ssh session:
`curl -s -u :"$(cat dashboard.key)" http://<pi>:5000/api/health` - requests from
off the Pi need the dashboard password (step 9); on the Pi itself the bare
`curl -s http://localhost:5000/api/health` still works.

**If a field reads `?`** it means that probe could not be taken, not that the
value was zero — and the heartbeat says why once per boot, at WARNING:

```bash
journalctl -u labtrack -p warning | grep 'health line'
```

For `throttled=?` specifically there are two causes. The common one is PATH:
the systemd unit sets `PATH` explicitly, and if it lists only the venv's `bin`
directory then `vcgencmd` is unfindable from the service even though it works
in your shell. `systemd/labtrack.service` now appends the system directories,
and `health.py` resolves `vcgencmd` by absolute path anyway. The other is
permissions — `vcgencmd` needs `/dev/vcio`, which is group `video`; if the
warning mentions VCHI or vchiq, run `sudo usermod -aG video admin` and reboot.

### Reviewing the run

```bash
scripts/soak-report.sh "3 days ago"
```

That pulls out reboots, OOM kills, undervoltage events, kernel oopses and
driver warnings, reboots the app asked for, app errors, anything the kiosk
reported about itself, Chromium complaints, and the trend over the window. A
steadily falling `mem_avail` or rising `chromium_mem` across days is the shape
of a leak; a `load1` that climbs to the core count and stays there while
`dstate` is non-zero is the shape of a wedged driver (see "Background video
freezes"). With no argument it covers the last week.

To watch live while you set things up:

```bash
sudo journalctl -u labtrack -t labtrack-chromium -f     # app + browser together
sudo journalctl -u labtrack -p warning -f               # only things going wrong
```

### On the background video specifically

The decoder wedge described in step 7 produces *no* error event — the picture
just stops while the page still believes it is playing. `main.js` therefore
watches `currentTime` on a 5s timer and reports `video-stall` if it hasn't
moved for 15s, along with the `readyState` and where in the clip it died.
When that fires it pauses the video and drops back to the flat background, so
the board stays readable for the rest of the run instead of sitting on a dead
frame. It deliberately does not retry: once that decoder has wedged it does
not come back, so a retry loop would only report the same stall forever.

Two related reports come from the same watchdog: `video-never-started` (no
first frame within 30s — what the non-faststart range-request stall looks
like) and `video-too-short` (a replacement clip shorter than `LOOP_TAIL_S`).

**A `video-stall` report now also reboots the Pi** — see "Background video
freezes" below for why, and "Automatic recovery" for how.

## Background video freezes

There are two distinct video failures on this hardware, and they are easy to
confuse because both end with a picture that has stopped moving. Step 7
covers the first. This is the second, and it is much worse: it takes the
whole display with it, not just the video.

**Symptom.** The clock stops. The background video is not merely frozen — it
is gone entirely, replaced by the flat panel background. Moving the mouse
produces no cursor. Meanwhile the app is perfectly healthy: `systemctl status
labtrack` is active with zero restarts, the dashboard works from another PC,
and `/api/health` reports `"ok": true` with no concerns. Only a reboot clears
it.

**What is actually happening.** Wrapping the video back to the start makes
Chromium issue `VIDIOC_STREAMOFF` on the V4L2 m2m decoder. There is a race in
`bcm2835_codec`'s `stop_streaming` that leaves buffers in an active state; vb2
then warns, and a kernel workqueue thread dereferences NULL freeing the
dma-buf:

```
videobuf2_common: driver bug: stop_streaming operation is leaving buffer 0 in active state
Unable to handle kernel NULL pointer dereference at virtual address 0000000000000248
Internal error: Oops: 0000000096000005 [#1] SMP
Workqueue: events delayed_fput
pc : dma_release_from_dev_coherent+0x1c/0xd0
       dma_free_attrs / vb2_dc_put / dma_buf_release / __fput / delayed_fput
```

The kworker dies holding locks nothing will ever release. Tasks then pile up
behind it one at a time, and when the compositor is one of them the screen
stops updating — about 45 minutes after the oops in the case we measured.

**How to recognise it in the logs.** The giveaway is in the health heartbeat,
and it is counter-intuitive: **load average pinned at exactly the core count
while the CPU runs cold.**

```
Sep 02 06:50 load1=1.77 temp=45.8    <- video decoding normally
Sep 02 06:55 ... kernel oops ...     <- and the client reports video-stall
Sep 02 07:01 load1=1.00 temp=36.5    <- decode stopped, Pi cooled 9C
Sep 02 07:28 load1=1.76 temp=38.0    <- tasks begin blocking
Sep 02 07:43 load1=4.03 temp=37.5    <- four stuck; display froze at 07:41
Sep 02 09:01 load1=4.01 temp=37.0    <- flat at 4.0 for hours
```

Four busy cores on a Pi 4 mean 65–80°C. At 37°C nothing is running: Linux
counts tasks in uninterruptible ("D") sleep toward load average, and these are
parked forever. `health.py` now reports a `dstate=` count alongside `load1=`
and warns when a process has been stuck for over five minutes, so this shows
up as a WARNING within minutes instead of going unnoticed for hours.
`scripts/soak-report.sh` has a "Kernel oopses and driver warnings" section for
the same reason — it previously grepped the kernel only for OOM kills and
undervoltage, and reported "(none)" straight through this.

**What has been done about it.** It is a kernel driver bug, so nothing in this
repo can fix it — only make it rarer and recover from it faster:

- `scripts/build-loop.sh` makes the loop 10 minutes instead of 12 seconds, so
  the risky seek happens 50x less often. Observed rate is roughly one failure
  per 2,000 seeks, which moves the expected interval from about seven hours to
  about two weeks.
- A `video-stall` report now triggers an automatic reboot (below).
- `scripts/set-decode.sh software` avoids the V4L2 path entirely, at the cost
  of a CPU core.

If you want to escalate it upstream, `raspberrypi/linux` is the place, and the
trace above plus a clip looping on hardware decode for a few hours is the
report.

## Automatic recovery

When the kiosk reports `video-stall`, the server schedules a reboot 30 seconds
out and the board says so on screen ("Display fault — restarting in 30s")
before it goes. The reasoning is that the alternative is worse: the display is
already dead at that point, and without this it stays dead until somebody
walks past and notices, or until the nightly 00:00 reboot.

Two guards keep this from becoming its own problem:

- **Nothing reboots a Pi that has been up less than 30 minutes.** A fault that
  reasserts itself every boot would otherwise cycle the board forever, and a
  kiosk stuck in a reboot loop is far worse than one showing a flat
  background — nobody can check in during a loop. When the guard blocks a
  reboot it says so in the journal and the board degrades to the flat
  background instead.
- **Only an explicit set of client-log keys can trigger it** (`video-stall`,
  currently). Ordinary JS errors and failed polls never reboot anything.

It needs the polkit rule in `systemd/40-labtrack-reboot.rules`, which
`scripts/setup.sh` installs — the service runs as `admin`, not root, and
logind refuses without it. An install predating that file needs:

```bash
sudo cp systemd/40-labtrack-reboot.rules /etc/polkit-1/rules.d/
sudo systemctl restart polkit
```

If it is missing, the reboot fails and the journal says so explicitly rather
than leaving the board sitting under a notice for a reboot that never comes.

**What if the reboot itself hangs?** It is a fair worry — by the time this
fires the kernel has already oopsed, and a shutdown that has to wait on tasks
stuck in uninterruptible sleep can stall. Two things bound it. The reboot is
requested about 50 seconds after the oops, when only one task is typically
stuck and shutdown still proceeds normally (the display does not freeze until
~45 minutes in). And systemd is already using the Pi's hardware watchdog —
`Using hardware watchdog 'Broadcom BCM2835 Watchdog timer'` appears in every
boot log — which it arms across the reboot path, so a shutdown that genuinely
wedges gets force-reset rather than leaving the Pi off. Nothing needs
configuring for that; it is the default.

Check what has happened:

```bash
journalctl -u labtrack | grep -E 'rebooting|not rebooting'
```

## Switching video decode

Hardware decode is the default and what the Pi wants — 1080p H.264 on the
V3D/V4L2 path is nearly free, while software decode costs about a full core
permanently. But every video failure this board has had came from the V4L2
stack, and neither reproduces under software decode, so it is the escape hatch
when you need the board up while you work out what the hardware path is doing.

```bash
scripts/set-decode.sh              # report the current setting
scripts/set-decode.sh software     # then: sudo reboot
scripts/set-decode.sh hardware     # back again
```

It writes one word to `config/decode-mode`, which the kiosk autostart reads at
every Chromium launch, so a reboot is what applies it. The setting is
per-Pi and gitignored, so `git pull` will not fight a local change. Confirm
which path is live with:

```bash
journalctl -t labtrack-chromium | grep 'video decode'
```

Expect `load1` up by roughly 1.0 and a warmer Pi in the health line while
software decode is active.

## Nightly reboot

`scripts/setup.sh` installs a systemd timer that reboots the Pi every night at
**00:00**. The board is back up within a minute or so — the app starts on boot
and labwc relaunches Chromium in kiosk mode.

This is maintenance, not a fix for anything specific. The failure modes that
matter on a machine left running for weeks are the ones that leave everything
*looking* fine: Chromium's memory creeping up until the OOM killer picks
something, or the Pi's video decoder wedging on a dead frame. A daily reboot
clears both before they get far enough to notice.

Check the schedule:

```bash
systemctl list-timers labtrack-reboot.timer
```

```
NEXT                        LEFT     LAST  PASSED  UNIT                   ACTIVATES
Sat 2026-08-29 00:00:00 MDT 8h left  -     -       labtrack-reboot.timer  labtrack-reboot.service
```

Change the time by editing `OnCalendar` in
`/etc/systemd/system/labtrack-reboot.timer` (`systemd-analyze calendar "*-*-* 03:00:00"`
checks an expression before you commit to it), then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart labtrack-reboot.timer
```

Turn it off with `sudo systemctl disable --now labtrack-reboot.timer`.

Separately from the reboot, the app resets everyone to **out** once a day,
clearing notes too: with no time on anybody's card, a
forgotten checkout would otherwise say "In lab" forever. It keys on the date
rather than on the reboot, so it happens just after midnight whether or not
the timer is enabled, a Pi that was off overnight still starts the day
clean, and a restart or self-reboot during the day changes nothing.

## Day-to-day maintenance

- **Restart the app:** `sudo systemctl restart labtrack`
- **Reboot the Pi now:** `sudo systemctl reboot` (it also does this nightly on
  its own — see "Nightly reboot" above)
- **View logs:** `sudo journalctl -u labtrack -f`
- **Check on a long run:** `scripts/soak-report.sh` (see "Watching a long
  run" above), or `curl -s http://localhost:5000/api/health` for one sample
- **After changing the background video:** `scripts/build-loop.sh 10` (see
  "Background video freezes" — the long loop is what keeps the display from
  wedging, and it is not tracked in git so it must be rebuilt on the Pi)
- **If the video misbehaves:** `scripts/set-decode.sh software && sudo reboot`
  (see "Switching video decode")
- **Change someone's status from another machine** (or for testing) -
  the same endpoint the kiosk dialog posts to:
  ```bash
  curl -X POST http://<pi-ip>:5000/api/set-status -u :<dashboard password> \
       -H "Content-Type: application/json" \
       -d '{"member_id": 1}'
  ```
  That toggles (in → out, out or away → in). To record something specific,
  add `"action"` - `in`, `away` or `out` - and for away a `"location"`:
  `'{"member_id": 1, "action": "away", "location": "Server room"}'`. An
  action that changes nothing (`in` while already in) is refused with a 409.
  Check `/api/state` to see which id maps to whom.
- **Add or remove someone:** edit the list of names in
  `config/members.json`, then restart the app (see "3. Fill in your
  roster").
- **Database** lives at `labtrack.db` in the project folder (plain SQLite —
  `sqlite3 labtrack.db` to poke at it directly if needed).
