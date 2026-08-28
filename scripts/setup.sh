#!/usr/bin/env bash
# Run this FROM the labtrack/ directory on the Pi itself, after cloning/copying
# the project to /home/admin/labtrack. See README.md for the full walkthrough.
set -euo pipefail

if [ "$(basename "$PWD")" != "labtrack" ]; then
  echo "Run this from inside the labtrack/ directory (e.g. cd ~/labtrack && ./scripts/setup.sh)"
  exit 1
fi

echo "==> Installing system packages (smart card + browser + python venv)"
sudo apt update
sudo apt install -y \
  pcscd pcsc-tools opensc libpcsclite-dev swig \
  python3-venv python3-dev \
  chromium-browser

echo "==> Enabling pcscd (smart card daemon)"
sudo systemctl enable --now pcscd

echo "==> Installing polkit rule so pcscd allows the service user (no interactive login session)"
sudo cp systemd/40-labtrack-pcscd.rules /etc/polkit-1/rules.d/40-labtrack-pcscd.rules
sudo systemctl restart polkit

echo "==> Creating Python virtual environment"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "==> Locating opensc-pkcs11.so so cac_reader.py points at the right path"
FOUND_MODULE=$(find / -name "opensc-pkcs11.so" 2>/dev/null | head -n 1 || true)
if [ -n "$FOUND_MODULE" ]; then
  echo "    Found: $FOUND_MODULE"
  echo "    Update PKCS11_MODULE_PATH in cac_reader.py to this path if it differs."
else
  echo "    WARNING: opensc-pkcs11.so not found. Check the opensc package installed correctly."
fi

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
echo "  1. Edit config/members.json with each lab member's real EDIPI."
echo "  2. Plug in the USB smart card reader, then run: pcsc_scan"
echo "     (tap a CAC and confirm the reader + card are detected before trusting the app)"
echo "  3. Reboot: sudo reboot"
echo "     The Pi should boot to desktop and Chromium should launch in kiosk mode automatically."
echo ""
echo "  To watch a long unattended run:"
echo "    sudo journalctl -u labtrack -t labtrack-chromium -f   # app + browser"
echo "    ./scripts/soak-report.sh \"2 days ago\"                # summary of a run"
