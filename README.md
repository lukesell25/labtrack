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
warning instead of crashing, so every route still works. Click a name on
the kiosk page to check that person in or out (see "Checking in without a
card" below), or drive the same thing from another terminal (or
curl/Postman):

```bash
curl -X POST http://localhost:5000/api/manual-toggle \
     -H "Content-Type: application/json" \
     -d '{"member_id": 1}'
```

Each call toggles that member, so run it twice to exercise both the
check-in toast and the checkout note prompt. Note that events written this
way are flagged as manual and show a "No card" mark on the board - that is
the real behaviour, not a dev-mode artifact, so it is not a pixel-perfect
stand-in for a tap.

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
  config/members.json       Hashed EDIPI -> name roster (scripts/add-member.py)
  config/roster.key         Secret salt for those hashes - back this up, never commit it
  config/objectives.json    Screensaver text content (edit any time)
  templates/                Kiosk + dashboard HTML
  static/                   CSS, JS, and a media/ folder for slide pictures
                            and the background video
  health.py                 Once-a-minute health heartbeat for long runs
  systemd/labtrack.service   Runs the app on boot
  systemd/labtrack-reboot.*  Timer + unit for the nightly 00:00 reboot
  autostart/*.desktop       Launches Chromium kiosk mode on desktop login
  identity.py               One-way hashing of EDIPIs for the roster
  scripts/setup.sh          Installs everything below in one go
  scripts/add-member.py     Adds a member without their EDIPI hitting disk
                            (--pending for one you don't have an EDIPI for yet)
  scripts/add-event.py      Logs a check-in/out at a time you name
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

Add each lab member with their name and the 10-digit EDIPI printed on the
front of their CAC:

```bash
python3 scripts/add-member.py "Ada Vance"
```

It asks for the EDIPI twice, without echoing it, and writes only a hash of it
to `config/members.json` — the number itself is never stored, not in the file,
not in the database, not in the journal. Because it asks rather than taking an
argument, the EDIPI also stays out of your shell history. Restart to pick it
up:

```bash
sudo systemctl restart labtrack
```

Since the hash is one-way there is no way to check a typo afterwards — a wrong
digit just means the card is never recognised — which is why it prompts twice.
If that happens, delete the entry and add the person again.

**The hashing key.** The hashes are salted with `config/roster.key`, generated
automatically the first time the app or `add-member.py` runs and readable only
by its owner. **Back it up somewhere off the Pi.** Hashes are meaningless
without the key that made them: lose it and every card stops matching and the
whole roster has to be re-added. The app says so on startup rather than
quietly failing:

```
ERROR labtrack.db: 5 member(s) were hashed with a different roster key than the one at ...
```

The same applies to a **fresh clone**: `config/members.json` comes down from
git already full of hashes, but the key that made them is gitignored and does
not. Copy the key into `config/` *before* the app first starts, or you get the
quietest failure this system has — the roster syncs, the board shows everyone,
and every tap comes back "Card not recognized". Startup says so:

```
ERROR labtrack.db: config/members.json lists 5 member(s) already hashed, but the roster key at ... was just generated here
```

**Which machine owns the key.** Whichever one the roster was built on holds
the key those hashes belong to, and the Pi needs that same file to recognise
those cards — copying it across once, as part of deploying, is the intended
path. Keeping the production key on a laptop afterwards is not: a dev machine
away from the Pi should generate its own key and hash a throwaway roster of
made-up numbers. Hashes are not portable between the two.

**Adding and removing people.** The file is the source of truth: anyone added
appears on the board after a restart, and anyone removed disappears from the
status board and from "Hours this week". Their past check-ins stay in the
database and keep showing in "Recent activity" — removing someone retires
them, it doesn't erase the attendance log, and their card stops being
recognised. Add the same EDIPI back and they return with their history
intact. The restart logs what changed:

```
INFO labtrack.db: Roster sync deactivated 1 member(s): Ada Vance
```

Note the roster is keyed on the **hashed EDIPI**, not on name or position in
the file — so reordering the list or fixing a spelling is safe, but re-adding
someone with a different EDIPI reads as "one person left, a different one
joined".

**Someone whose EDIPI you don't have yet.** A new member, a card that hasn't
been issued, or the stretch before the reader is installed at all - put them
on the board now and fill the number in later:

```bash
python3 scripts/add-member.py --pending "Ada Vance"
```

That asks for nothing and writes a placeholder where the hash goes
(`pending-ada-vance`). After a restart Ada is a full member: she appears on
the kiosk strip and the dashboard, she can be checked in and out by clicking
her name (see "Checking in without a card" below), and those events are
flagged `manual` and marked `NO CARD` exactly like anyone else's click-in. No
card can ever match her, because a real hash is 32 hex characters and the
placeholder deliberately isn't one.

Adding the name by hand with no `edipi_hash` field at all does the same thing
rather than failing - the roster sync runs at startup, so a half-finished edit
that raised would take the whole board down. Every restart names the person
until it's finished:

```
WARNING labtrack.db: config/members.json lists Ada Vance with no edipi_hash, so they are on the board as a pending member ...
```

**Filling in a pending member's EDIPI.** When the number turns up:

```bash
python3 scripts/add-member.py --replace "Ada Vance"
```

It prompts for the EDIPI the same way (twice, not echoed), replaces the
placeholder in `config/members.json`, and - the part that matters - rewrites
that same row in `labtrack.db` instead of adding a second one.

**Don't do this swap by editing the file alone.** The roster is keyed on the
hashed EDIPI, so changing the hash reads as *one person leaving and a
different one joining*: the restart adds a new row and deactivates the old
one, which still owns every check-in Ada logged while she was pending. Those
vanish from the board and from "Hours this week", and if she was checked in at
the time, that `in` is left with no `out` after it and counts as time in the
lab up to now.

```
id 1  pending-ada-vance   Ada Vance  active 0   <- owns her events
id 2  aaaabbbb...         Ada Vance  active 1   <- fresh, empty
```

**When the key and the database are on different machines.** The hash has to
be made where `config/roster.key` lives, which often isn't the Pi holding
`labtrack.db`. `--replace` says so and prints the one command that finishes
the job, to be run on the Pi *before* restarting - the restart is what would
create that second row:

```bash
sqlite3 labtrack.db "UPDATE members SET edipi_hash = '<the new hash>' WHERE edipi_hash = 'pending-ada-vance';"
```

Copy the updated `config/members.json` across as well, then restart. Ada's
next tap is recognised and the `NO CARD` mark clears with it.

A hand-edited entry with a plaintext `"edipi": "1234567890"` still works, so a
half-finished edit can't silently drop somebody off the board, but it defeats
the point and every startup will say so until it's converted:

```
WARNING labtrack.db: config/members.json lists a plaintext EDIPI for Ada Vance ...
```

### 4. Plug in the smart card reader and test it

```bash
pcsc_scan
```

Tap a CAC on the reader. You should see the tool print reader/card details
and an ATR (Answer To Reset) string change when the card is presented and
removed. If nothing happens here, stop and troubleshoot at this level first
(check `lsusb` sees the reader, check `systemctl status pcscd`) before
worrying about the app — everything else depends on this working.

Once the app is running, the same check is on the board itself: the kiosk
header shows a green "Reader ready" beside the clock while a reader is
attached, and a red "Reader offline" if it is unplugged or `pcscd` has
died. That state is also in the journal — but only when it changes:

```bash
journalctl -u labtrack | grep -i "card reader"
```

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
the password, including `/api/manual-toggle` - without that, anyone on the
lab network could check people in and out.

**This is a lock on the door, not an encrypted tunnel.** There is no HTTPS
on this hop, so the password and the page contents cross the network in the
clear and anyone able to sniff the lab network can read both. That's an
accepted trade for an attendance board on a trusted LAN. If you need more
than that, the options in rough order of effort are: an ssh tunnel from the
viewing PC (`ssh -L 5000:localhost:5000 admin@<pi-ip>`, then browse
`http://localhost:5000/dashboard` - works today with no server change, but
one person at a time), Tailscale or another WireGuard mesh on the Pi and
each viewing PC, or a TLS-terminating reverse proxy. If you ever do put a
proxy in front, note that it breaks the loopback exemption: every request
would then appear to come from the Pi itself and skip the password entirely
(see the comment in `webauth.py`).

## Checking in without a card

Tapping a CAC is the normal path. When that isn't possible - the reader is
down, someone left their card at home, or the reader hasn't been installed
yet - a member can check themselves in or out from the kiosk itself:

1. Move the mouse. The kiosk hides the pointer after 8 seconds of stillness
   (an always-on board shouldn't have a cursor parked on it for a week), so
   it reappears as soon as the mouse does.
2. Click your name in the roster strip along the bottom of the screen.
3. The board asks "Check in?" / "Check out?" - click Confirm. The dialog
   cancels itself after 20 seconds, and clicking anywhere outside the two
   buttons cancels it too, so a stray click never logs anything by itself.

From there it behaves exactly like a tap: the same confirmation, and the
same optional "why are you out" note prompt on a checkout.

**Anything logged this way is marked.** The event carries a `manual` flag in
the database, the kiosk shows an amber `NO CARD` under that person's name
until their next tap, and the dashboard shows it in both the roster and the
Note column of the activity log. Nothing verified a card, so nothing
pretends one was read - the whole point of tapping a CAC is that the entry
means something, and an entry anybody could have clicked has to be legible
as such.

For a **pending member** - someone on the roster with no EDIPI yet, added
with `add-member.py --pending` (see "3. Fill in your roster") - this isn't a
fallback, it's the only way in until their number arrives. Every event they
log stays marked `NO CARD`; the mark clears on their first real tap once the
EDIPI is filled in with `--replace`.

A mouse has to be plugged into the Pi for any of this, which is also the
thing that makes it a fallback rather than the front door: no mouse, no
click-in. The `/api/manual-toggle` endpoint underneath it is the same one
described under "Day-to-day maintenance" below, so a member can also be
toggled from another machine on the network.

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
| Card reader plugged/unplugged | `start_reader_watch()` in `cac_reader.py` | `journalctl -u labtrack \| grep -i "card reader"` |
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

A reboot doesn't check anybody out — `events` is append-only, so whoever was
checked in at midnight is still checked in afterwards and their hours keep
accruing. That's unchanged by this timer; it's the same as any other restart.

## Day-to-day maintenance

- **Restart the app:** `sudo systemctl restart labtrack`
- **Reboot the Pi now:** `sudo systemctl reboot` (it also does this nightly on
  its own — see "Nightly reboot" above)
- **View logs:** `sudo journalctl -u labtrack -f`
- **Check on a long run:** `scripts/soak-report.sh` (see "Watching a long
  run" above), or `curl -s http://localhost:5000/api/health` for one sample
- **Manually toggle someone in/out** (if the reader is down, or for testing)
  without touching the card reader. From the kiosk itself this is a click on
  the person's name - see "Checking in without a card" above - and over the
  network it's the same endpoint that click posts to:
  ```bash
  curl -X POST http://localhost:5000/api/manual-toggle \
       -H "Content-Type: application/json" \
       -d '{"member_id": 1}'
  ```
  Either way the event is flagged as manual and shows a "No card" mark on
  the board and the dashboard until that person's next tap.
  (member IDs are assigned in the order they appear in `config/members.json`,
  starting at 1 — check `/api/state` to confirm which id maps to whom.)
- **Add someone before you have their EDIPI**, then fill it in later without
  losing the attendance they built up in the meantime:
  ```bash
  python3 scripts/add-member.py --pending "Ada Vance"   # on the board now
  python3 scripts/add-member.py --replace "Ada Vance"   # when the number arrives
  ```
  Until `--replace`, they check in by clicking their name on the kiosk and
  every event is marked `NO CARD`. `--replace` rewrites their existing member
  row rather than adding a second one - see "3. Fill in your roster" above for
  why hand-editing the hash in `config/members.json` instead splits their
  history in two. Both need a restart to take effect.
- **Log someone in/out at a past time** (the reader was down, or they forgot
  to tap) — `manual-toggle` above always stamps the current time, so use this
  instead when the time matters:
  ```bash
  python3 scripts/add-event.py "Ada Vance" in  "2026-08-27 08:15"
  python3 scripts/add-event.py "Ada Vance" out "2026-08-27 16:40" --note "left early"
  ```
  It takes a name rather than an id (`--list` prints the roster), previews the
  event against the ones either side of it, and warns before writing if the
  result would break the in/out pairing the hours report depends on. Add both
  halves of a shift: a lone `in` counts as time in the lab up to now. No
  restart needed — the kiosk and dashboard pick it up on their next poll.
- **Database** lives at `labtrack.db` in the project folder (plain SQLite —
  `sqlite3 labtrack.db` to poke at it directly if needed).

## A note on the CAC integration

The roster stores `scrypt(EDIPI, salt=config/roster.key)` rather than the
EDIPI, so a tap is identified by hashing the card's EDIPI and matching. Ten
digits is a small enough keyspace that a plain SHA-256 would be brute-forced
in minutes, and an HMAC would be too as soon as the key file leaked next to
the roster it protects; scrypt's work factor puts a full sweep at decades of
CPU time even for someone holding the key. It costs ~100-200ms per tap, on the
reader thread, inside the 1-3s the certificate read already takes. Note this
protects the ID numbers, not the names, which are still in the file.

Reading is limited to what's available *without* PIN entry — the PIV
Authentication certificate, which is a public object and readable by design.
This is enough to uniquely identify who tapped (via the EDIPI embedded in
the cert) but does **not** cryptographically prove the person physically
possesses the card's private key the way a PIN-backed challenge would. For a
5-person lab attendance log that's a reasonable tradeoff, but it's worth
being clear-eyed that this is identification, not full authentication.
