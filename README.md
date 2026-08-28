# LabTrack

A small Flask app for your Raspberry Pi that:
- Identifies lab members by tapping their DoD CAC on a USB smart card reader (no PIN)
- Logs check-in/check-out events to SQLite
- Shows a live status board / screensaver on the Pi's own screen
- Serves a dashboard viewable from any other PC on the network

## Developing locally, away from the Pi

`scripts/setup.sh` is the **Pi deployment installer** - it installs
`pcscd`/`opensc` (talks to the physical card reader) and a kiosk-mode
Chromium, and installs systemd services. None of that is relevant on a
regular dev machine (Windows, WSL, Mac, whatever you're running Claude
Code on), and trying to run it there will just fail on hardware-only
packages like `pyscard` that need a real smart card reader driver stack to
even compile against.

For editing and testing the Flask app, dashboard, kiosk display, or
database logic without the Pi, use the lean dependency set instead:

```bash
python3 -m venv venv
source venv/bin/activate        # or venv\Scripts\activate on Windows
pip install -r requirements-dev.txt
python3 app.py
```

This runs the full app minus actual CAC hardware support -
`cac_reader.py` detects that `pyscard` isn't installed and logs a single
warning instead of crashing, so every route still works. Simulate a card
tap from another terminal (or curl/Postman) instead of tapping a real
card:

```bash
curl -X POST http://localhost:5000/api/manual-toggle \
     -H "Content-Type: application/json" \
     -d '{"member_id": 1}'
```

Then open `http://localhost:5000` (kiosk display) and
`http://localhost:5000/dashboard` in a browser to see your changes.

When you're ready to test against the real reader, deploy to the Pi as
usual - `requirements.txt` there includes `pyscard` and `python-pkcs11`
for the actual hardware path.

## Project layout

```
labtrack/
  app.py                  Flask app + routes
  database.py              SQLite schema + queries
  cac_reader.py             Background thread that watches the card reader
  config/members.json       EDIPI -> name roster (edit this!)
  config/objectives.json    Screensaver text content (edit any time)
  templates/                Kiosk + dashboard HTML
  static/                   CSS, JS, and a media/ folder for slide pictures
                            and the background video
  health.py                 Once-a-minute health heartbeat for long runs
  systemd/labtrack.service   Runs the app on boot
  autostart/*.desktop       Launches Chromium kiosk mode on desktop login
  scripts/setup.sh          Installs everything below in one go
  scripts/soak-report.sh    Summarises a long unattended run
```

## Step-by-step setup on the Pi

These steps assume a fresh Raspberry Pi OS (64-bit, Desktop) install, keyboard/mouse
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

This installs `pcscd`/`opensc`/`chromium-browser`, creates a Python virtual
environment, installs the pip requirements, installs and enables the
`labtrack` systemd service, installs the kiosk autostart entry, and enables
desktop auto-login via `raspi-config`.

You'll be prompted for your sudo password partway through.

### 3. Fill in your roster

Edit `config/members.json` and replace the placeholder EDIPIs with your 5 lab
members' real 10-digit EDIPIs (printed on the front of each CAC). The app
picks this up automatically the next time it starts (`systemctl restart labtrack`
if you edit it after setup).

### 4. Plug in the smart card reader and test it

```bash
pcsc_scan
```

Tap a CAC on the reader. You should see the tool print reader/card details
and an ATR (Answer To Reset) string change when the card is presented and
removed. If nothing happens here, stop and troubleshoot at this level first
(check `lsusb` sees the reader, check `systemctl status pcscd`) before
worrying about the app — everything else depends on this working.

### 5. Check where OpenSC's PKCS#11 module actually landed

```bash
find / -name "opensc-pkcs11.so" 2>/dev/null
```

`setup.sh` already tries this and tells you, but architectures/OS versions
vary. Open `cac_reader.py` and make sure `PKCS11_MODULE_PATH` at the top
matches what you found. Restart the service after any change:

```bash
sudo systemctl restart labtrack
```

### 6. Verify a real tap works end-to-end

```bash
sudo journalctl -u labtrack -f
```

Tap a registered CAC on the reader. You should see a log line like
`Member Name checked in at 2026-...`.

**If you see `EstablishContextException: ... Access denied. (0x8010006A)`:**
this is polkit, not the app. Since pcsc-lite 2.0.1, `pcscd` on
Debian/Ubuntu only authorizes clients with an active interactive login
session by default — a systemd service like `labtrack` doesn't have one,
so it gets rejected. Fix it with a polkit rule authorizing the service
user explicitly:

```bash
sudo cp systemd/40-labtrack-pcscd.rules /etc/polkit-1/rules.d/40-labtrack-pcscd.rules
sudo systemctl restart polkit
sudo systemctl restart labtrack
```

(`setup.sh` installs this automatically on a fresh run — this is only
needed if you set the project up before this rule existed.)

If you instead see "Unrecognized card" or "could not extract an EDIPI," the certificate on that particular card
layout may store the EDIPI slightly differently — run:

```bash
./venv/bin/python -c "
import pkcs11
lib = pkcs11.lib('/usr/lib/aarch64-linux-gnu/opensc-pkcs11.so')  # match cac_reader.py's path
for slot in lib.get_slots(token_present=True):
    print(slot.get_token())
"
```

or more simply `pkcs15-tool --list-certificates` / `pkcs15-tool --read-certificate 1`
(also installed by `opensc`) to inspect exactly what's on the card, and adjust
the extraction logic in `cac_reader.py` (`_extract_edipi_from_cert`) to match.
This is the one part of the project most likely to need a small tweak for
your specific card stock — I've implemented the standard PIV field plus a CN
fallback, but issuance details vary.

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
- **Background video** (optional): drop one `.mp4` into `static/media/`,
  then set `BACKGROUND_VIDEO` near the bottom of `static/js/main.js` to its
  filename. It loops continuously behind every slide, with a flat dim over
  it so the text stays readable. Leave `BACKGROUND_VIDEO = ""` for no
  background.

  Only `background.mp4` is tracked in git; `.gitignore` excludes every other
  video in `static/media/`. Keep your source footage and any encode
  experiments outside the repo or under those ignore rules — committed
  binaries are permanent, and this history already had to be rewritten once
  to remove 131MB of them. The Pi picks the video up through `git pull`
  along with everything else.

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

  The video also stays visible behind the check-in/check-out confirmation
  and the "reading card" overlay, dimmed to the same level as behind a
  slide, so the board never cuts to a flat panel mid-tap.

  H.264 at exactly 1920x1080 is both the panel's native resolution and
  inside the Pi 4's hardware decode ceiling (1920x1920); HEVC/VP9/AV1 or
  anything wider falls back to software decode and pegs the CPU. `-an`
  drops the audio track — the kiosk plays muted, so decoding audio is pure
  waste. Keep the clip short (20–60s) and make the last frame resemble the
  first, since it restarts on a hard cut. If the file is missing or won't
  decode, the slides fall back to the flat panel background.

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

Tapping a CAC should show a full-screen confirmation, then fade back to the
status board/screensaver.

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
| App events, errors, tracebacks | `app.py` | `journalctl -u labtrack` |
| Health heartbeat, once a minute | `health.py` | `journalctl -u labtrack \| grep health` |
| Errors the kiosk page saw | `report()` in `main.js` → `/api/client-log` | `journalctl -u labtrack \| grep client` |
| Chromium's own output | the `logger` pipe in `autostart/labwc-autostart` | `journalctl -t labtrack-chromium` |
| OOM kills, undervoltage, resets | the kernel | `journalctl -k` |

The heartbeat line looks like this, and is INFO normally, WARNING when
something on it looks wrong:

```
INFO labtrack.health: health mem_avail=1204M mem_total=3792M app_mem=48.2M
chromium_mem=612.4M chromium_procs=11 load1=0.42 temp=54.7C disk_free=21740M
throttled=0x0 kiosk_idle=1s uptime=486213s
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

A one-off sample without an ssh session: `curl -s http://<pi>:5000/api/health`.

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

That pulls out reboots, OOM kills, undervoltage events, app errors, anything
the kiosk reported about itself, Chromium complaints, and the memory trend
over the window — a steadily falling `mem_avail` or rising `chromium_mem`
across days is the shape of a leak. With no argument it covers the last week.

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

## Day-to-day maintenance

- **Restart the app:** `sudo systemctl restart labtrack`
- **View logs:** `sudo journalctl -u labtrack -f`
- **Check on a long run:** `scripts/soak-report.sh` (see "Watching a long
  run" above), or `curl -s http://localhost:5000/api/health` for one sample
- **Manually toggle someone in/out** (if the reader is down, or for testing)
  without touching the card reader:
  ```bash
  curl -X POST http://localhost:5000/api/manual-toggle \
       -H "Content-Type: application/json" \
       -d '{"member_id": 1}'
  ```
  (member IDs are assigned in the order they appear in `config/members.json`,
  starting at 1 — check `/api/state` to confirm which id maps to whom.)
- **Database** lives at `labtrack.db` in the project folder (plain SQLite —
  `sqlite3 labtrack.db` to poke at it directly if needed).

## A note on the CAC integration

Reading is limited to what's available *without* PIN entry — the PIV
Authentication certificate, which is a public object and readable by design.
This is enough to uniquely identify who tapped (via the EDIPI embedded in
the cert) but does **not** cryptographically prove the person physically
possesses the card's private key the way a PIN-backed challenge would. For a
5-person lab attendance log that's a reasonable tradeoff, but it's worth
being clear-eyed that this is identification, not full authentication.
