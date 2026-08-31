# LabTrack — Operator's Guide

LabTrack is the attendance system running on the Raspberry Pi in the lab. Lab
members tap insert their CAC in the USB card reader; the Pi logs who came in and who
left, shows a live status board on its own screen, and serves a dashboard you
can open from any PC on the lab network.

This guide covers the two things you'll actually do — **reading the dashboard**
and **getting into the Pi when something needs fixing**.

---

## Fill these in first

Everything below refers to these by name. Replace them once and the rest of the
document is accurate.

| Placeholder | What it is | Value |
| --- | --- | --- |
| `<PI-IP>` | The Pi's address on the lab network | `10.10.149.51` |
| `<DASHBOARD-PASSWORD>` | Shared password for the dashboard | `fair-lab-2026` |
| `<PI-USER>` | Linux username on the Pi | `admin` |
| `<PI-PASSWORD>` | Login password for that user | `2RunEnv!` |
| `<PROJECT-DIR>` | Where the app lives on the Pi | `/home/admin/labtrack` |

> **Don't commit this file with the real values in it.** The LabTrack repository
> is public. Keep the filled-in copy somewhere private — a password manager,
> a shared drive, or a printout in the lab.

To find the Pi's IP, run `hostname -I` on the Pi itself (see *Getting into the
Pi* below).

---

## The two screens

**The kiosk board** is the Pi's own display, running full screen all day. It
shows who is currently in, a clock, the lab objectives cycling as slides, and a
green/red indicator telling you whether the card reader is alive. When someone
taps, it flashes a confirmation across the screen. Nobody logs into it — it just
runs.

**The dashboard** is the same data in a readable form, opened from your own PC.
That's the one you want.

---

## Opening the dashboard

From any computer on the same network as the Pi:

```
http://<PI-IP>:5000/dashboard
```

The browser will ask for a username and password. **Leave the username blank** —
only the password is checked. Enter `<DASHBOARD-PASSWORD>`.

The page updates itself every 5 seconds; you can leave it open on a second
monitor.

### What's on it

- **Current roster** — everyone on the team and whether they're in or out right
  now, with the time of their last change.
- **Hours this week** — total hours per person over the last 7 days, worked out
  by pairing each check-in with the check-out that follows it.
- **Recent activity** — the raw log: time, name, in/out, and any note the person
  left on their way out.
- **Admin** — a folded-away section at the bottom for correcting the log. See
  *Fixing mistakes* below.

### The `NO CARD` mark

Some entries are marked **`NO CARD`** in amber, on both the board and the
dashboard. That means the check-in was made by clicking a name on the kiosk
screen rather than by tapping a card — because the reader was down, someone
forgot their card, or they don't have one issued yet.

### Checking in without a card

If the reader dies or someone leaves their card at home, they can still be
logged. On the Pi's screen: move the mouse (the pointer is hidden after 8
seconds of stillness), click the name in the strip along the bottom, and confirm
in the dialog that appears. The dialog cancels itself after 20 seconds and on
any click outside it, so a stray click can't log anybody by accident.

The resulting entry is marked `NO CARD` as described above.

---

## Fixing mistakes

Open the **Admin** section at the bottom of the dashboard.

**A single wrong entry** — a double tap, or someone logged against the wrong
name — is removed with the `×` at the end of its row in Recent activity.

Be aware that hours are calculated by pairing check-ins with check-outs in
order, so deleting one half of a pair leaves the other unmatched and the hours
for that person will look wrong until you fix the other half too.

**Starting the log over** — the "Clear all events" button wipes the entire
attendance history. You have to type `DELETE` into the box beside it first.
Before it deletes anything it saves a timestamped backup of the database on the
Pi and tells you the filename, so a clear made in error is recoverable.

Clearing the log does **not** remove anybody from the roster. Everyone simply
reads as "out" afterwards, since there's nothing more recent saying otherwise.

**Both actions are permanent** and both are written to the Pi's system log, so
there's a record of what was removed and when.

---

## Getting into the Pi

Most of the time you won't need to. When you do — to restart the app, check
logs, or change the roster — connect over SSH from any machine on the network:

```bash
ssh <PI-USER>@<PI-IP>
```

Enter `<PI-PASSWORD>` when prompted.

On Windows this works from PowerShell or Windows Terminal as written. On Mac or
Linux, use Terminal.

Everything below assumes you're in the project directory once connected:

```bash
cd <PROJECT-DIR>
```

### The commands worth knowing

| What you want | Command |
| --- | --- |
| Restart the app | `sudo systemctl restart labtrack` |
| Is it running? | `sudo systemctl status labtrack` |
| Watch the logs live | `sudo journalctl -u labtrack -f` |
| Only things going wrong | `sudo journalctl -u labtrack -p warning` |
| Reboot the Pi | `sudo systemctl reboot` |
| Find the Pi's IP | `hostname -I` |
| Read the dashboard password | `cat <PROJECT-DIR>/config/dashboard.key` |

Press `Ctrl+C` to stop a command that's following the logs.

### Changing the dashboard password

Write the new password into `<PROJECT-DIR>/config/dashboard.key` and restart the
app. It's read once at startup, so the restart is required:

```bash
nano <PROJECT-DIR>/config/dashboard.key      # type the new password, Ctrl+O, Ctrl+X
sudo systemctl restart labtrack
```

There is one shared password for everybody; there are no individual accounts.

---

## Routine changes

### Adding or removing a lab member

The roster lives in `<PROJECT-DIR>/config/members.json`. Add someone with:

```bash
python3 scripts/add-member.py "Ada Vance"
sudo systemctl restart labtrack
```

It asks for the 10-digit number printed on the front of their CAC, twice,
without showing it on screen. That number is never written to disk — only a
one-way hash of it — so it can't be recovered from the Pi, the log, or the
config file, and a typo can't be checked afterwards (a wrong digit just means
the card is never recognised).

**If you don't have their number yet** — a new member, or a card that hasn't
been issued — put them on the board now and fill it in later:

```bash
python3 scripts/add-member.py --pending "Ada Vance"    # on the board today
python3 scripts/add-member.py --replace "Ada Vance"    # when the number arrives
```

Until the number is filled in, they check in by clicking their name on the
kiosk and every entry is marked `NO CARD`. Both commands need a restart.

**Removing someone** is a hand-edit of `config/members.json` — delete their
entry and restart. They come off the board and off the hours report, their card
stops being recognised, and their past attendance stays in the log. Adding the
same person back later reconnects them to their history.

> **One critical file.** `<PROJECT-DIR>/config/roster.key` is the secret that
> the roster hashes are built on. **Back it up somewhere off the Pi.** Without
> it, every card stops being recognised and the entire roster has to be added
> again from scratch. It is deliberately excluded from the code repository, so
> it is not backed up by anything else.

### Logging a shift after the fact

If the reader was down or someone forgot to tap, you can enter their hours at
the right time rather than the current time:

```bash
python3 scripts/add-event.py "Ada Vance" in  "2026-08-27 08:15"
python3 scripts/add-event.py "Ada Vance" out "2026-08-27 16:40" --note "left early"
```

Use `--list` to see the exact names it expects. The script shows you what it's
about to write alongside the entries either side of it, and warns you if the
result would break the in/out pairing that the hours report depends on. Enter
both halves of a shift — a lone check-in counts as time in the lab right up to
the present moment. No restart needed.

### Changing the slides on the board

The text cycling on the kiosk comes from `<PROJECT-DIR>/config/objectives.json`.
Edit it and the board picks the change up within a minute — no restart. Each
objective is either a line of text or a line of text with a picture beside it:

```json
"objectives": [
  "Finalize Q3 experiment protocol",
  { "text": "Calibrate sensor rig #2", "image": "rig2.jpg" }
]
```

Pictures go in `<PROJECT-DIR>/static/media/` and are about 800px wide. A missing
picture quietly falls back to text only.

---

## Things that happen on their own

**The Pi reboots itself every night at midnight.** This is routine maintenance,
not a symptom — a browser left running for weeks slowly leaks memory, and a
nightly restart clears it before anyone notices. The board is back up within a
minute. Nobody is checked out by it: whoever was checked in at midnight is still
checked in afterwards.

**The Pi logs a health line every minute** — memory, temperature, disk space,
power supply status, and whether the kiosk browser is still alive. This exists
so that if something fails during a long unattended run, there's a record of the
lead-up. You can see the current state at a glance:

```bash
curl -s http://localhost:5000/api/health         # run this on the Pi
scripts/soak-report.sh "3 days ago"              # summary of a longer window
```

---

## When something looks wrong

| Symptom | What it means | What to do |
| --- | --- | --- |
| Board says **"Reader offline"** in red | The card reader is unplugged, or its driver died | Reseat the USB reader. If that doesn't clear it: `sudo systemctl restart pcscd && sudo systemctl restart labtrack` |
| Board says **"Reader unknown"** in amber | Nothing is currently checking on the reader | Restart the app. If it persists, the reader software didn't install correctly — needs a look at the logs |
| A tap says **"Card not recognized"** | That EDIPI isn't on the roster, or was entered with a typo | Re-add the person with `add-member.py`. If it's happening to *everyone* at once, see the `roster.key` warning above and check the logs on startup |
| Screen is black or frozen | The kiosk browser died | `sudo systemctl reboot`. If it recurs, capture `sudo journalctl -u labtrack -p warning` before rebooting |
| Dashboard won't load from your PC | Wrong IP, app stopped, or you're on a different network | Check `sudo systemctl status labtrack` on the Pi and confirm `<PI-IP>` with `hostname -I` |
| Browser keeps asking for the password | Wrong password, or a username was typed in | Leave the username blank. Confirm the password with `cat <PROJECT-DIR>/config/dashboard.key` |
| Hours look wrong for one person | An unmatched check-in or check-out in the log | Check Recent activity for a missing half of a pair and add it with `add-event.py` |

In almost every case the first two things to try are `sudo systemctl restart
labtrack` and, failing that, `sudo systemctl reboot`. Neither loses any
attendance data — the log is on disk and is only ever added to.

---

## What this system does and doesn't guarantee

Worth being clear about, since it's an attendance record:

**The card read identifies, it doesn't authenticate.** LabTrack reads the ID
number from the card without asking for a PIN — which is what makes tapping fast
enough to actually get used. It proves that *the card* was presented, not that
its owner presented it. For a small lab's attendance board that's the right
trade; it isn't a security control and shouldn't be relied on as one.

**The dashboard password is a lock on the door, not an encrypted tunnel.** The
connection is plain HTTP, so the password and the page contents cross the lab
network unencrypted. It keeps out people wandering onto the network; it will not
stop anyone deliberately watching the traffic. If that's needed, there are
stronger options (an SSH tunnel, a VPN, or HTTPS) — worth a conversation rather
than a config change.

**ID numbers are protected; names are not.** The card numbers exist only on the
cards themselves — the Pi stores a one-way hash and never writes the number to
disk or to the logs. Members' names are stored in plain text.
