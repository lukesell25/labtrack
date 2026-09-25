#!/usr/bin/env bash
# Run this FROM the labtrack/ directory on the Pi itself, after cloning/copying
# the project to /home/admin/labtrack. See README.md for the full walkthrough.
set -euo pipefail

if [ "$(basename "$PWD")" != "labtrack" ]; then
  echo "Run this from inside the labtrack/ directory (e.g. cd ~/labtrack && ./scripts/setup.sh)"
  exit 1
fi

echo "==> Installing system packages (browser + python venv)"
sudo apt update
# ffmpeg is for scripts/build-loop.sh, which builds the long-playing
# background video from the short master tracked in git (see below).
sudo apt install -y \
  python3-venv \
  chromium-browser ffmpeg

echo "==> Installing the polkit rule for the service user (no interactive login session)"
# Lets app.py reboot the Pi to recover the board from a wedged display, which
# nothing in userspace can otherwise undo - see request_reboot() in app.py.
sudo cp systemd/40-labtrack-reboot.rules /etc/polkit-1/rules.d/40-labtrack-reboot.rules
sudo systemctl restart polkit

echo "==> Creating Python virtual environment"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "==> Making the systemd journal persistent across reboots"
# Raspberry Pi OS defaults to Storage=auto with no /var/log/journal, which
# puts the journal in RAM - so it is wiped on every boot, destroying the log
# that explains a crash at exactly the moment you need it. The size cap keeps
# it off the SD card's throat. See "Watching a long run" in README.md.
sudo mkdir -p /var/log/journal
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/labtrack.conf >/dev/null <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=500M
MaxRetentionSec=1month
EOF
sudo systemctl restart systemd-journald

echo "==> Installing systemd service"
sudo cp systemd/labtrack.service /etc/systemd/system/labtrack.service
sudo systemctl daemon-reload
sudo systemctl enable --now labtrack

echo "==> Installing the nightly reboot timer (00:00 daily)"
# The kiosk runs unattended for weeks; a scheduled reboot is what clears the
# slow failures nothing else recovers from (Chromium's memory creep, a wedged
# video decoder). Enabling the timer does not reboot now - it only schedules.
sudo cp systemd/labtrack-reboot.service /etc/systemd/system/labtrack-reboot.service
sudo cp systemd/labtrack-reboot.timer /etc/systemd/system/labtrack-reboot.timer
sudo systemctl daemon-reload
sudo systemctl enable --now labtrack-reboot.timer

echo "==> Building the long-playing background video"
# The kiosk loops its background by seeking back to the start, and each seek
# is a chance to hit the bcm2835_codec stop_streaming bug that freezes the
# display. Concatenating the 12s master into a 10-minute file makes that seek
# happen 50x less often. Stream copy, so it is lossless and takes seconds.
# Not in git: the result is ~900MB. Skipped rather than fatal - the board
# still runs on the short master, just with a shorter mean time to freeze.
if ! ./scripts/build-loop.sh 10; then
  echo "    WARNING: could not build the long loop; the kiosk will fall back to"
  echo "    the short background.mp4. Re-run scripts/build-loop.sh to retry."
fi

echo "==> Installing kiosk autostart (labwc - the default Wayland desktop on current Raspberry Pi OS)"
mkdir -p ~/.config/labwc
cp autostart/labwc-autostart ~/.config/labwc/autostart
chmod +x ~/.config/labwc/autostart

echo "==> Installing persistent Chromium flag (skips the login-keyring prompt on every launch, not just the kiosk one)"
sudo mkdir -p /etc/chromium.d
sudo cp autostart/99-labtrack-password-store /etc/chromium.d/99-labtrack-password-store

echo "==> Setting desktop auto-login (boot straight to desktop, no login prompt)"
sudo raspi-config nonint do_boot_behavior B4

echo ""
echo "Setup complete. Next steps:"
echo "  1. List each lab member's name in config/members.json, then restart:"
echo "     sudo systemctl restart labtrack"
echo "  2. Plug a USB keyboard into the Pi - it is how people check in and out"
echo "     (arrow keys to pick a name, Enter to check in/out)."
echo "  3. To view the dashboard from another PC, note the password:"
echo "     cat config/dashboard.key    (generated on first start; blank username)"
echo "     Replace it with a passphrase of your own if you prefer, then restart."
echo "  4. Reboot: sudo reboot"
echo "     The Pi should boot to desktop and Chromium should launch in kiosk mode automatically."
echo ""
echo "  If the background video misbehaves, fall back to software decode:"
echo "    ./scripts/set-decode.sh software && sudo reboot"
echo ""
echo "  To watch a long unattended run:"
echo "    sudo journalctl -u labtrack -t labtrack-chromium -f   # app + browser"
echo "    ./scripts/soak-report.sh \"2 days ago\"                # summary of a run"
