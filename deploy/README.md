# deploy: installing the stand on the CM5 (or CM4)

**State: build-plan step 11. `install.sh --dry-run` was run on the bench CM5 (2026-11-22) and listed the right steps. The real install has not been run: it needs `sudo`, which needs the owner's password.** The kiosk and emergency-exit services have not run at all: no display is attached and there is no interface board yet.

## What gets installed

```
 /opt/eol/releases/<commit>/   one folder per installed version (the code)
 /opt/eol/current ──────────►  the active version (switched in one step; rollback switches it back)
 /opt/eol/venv/                Python environment: pinned packages from requirements.txt, numpy and scipy from Raspberry Pi OS
 /etc/eol/eol.env              settings: data folder, socket folder, microphone identity
 /var/lib/eol/                 database, recordings, models (owned by the eol user)
 /run/eol/                     sockets of the acquisition service and the heartbeat process
```

| Service | Does | Starts |
|---|---|---|
| `eol-heartbeat` | heartbeat to the Pico; real-time priority, locked memory (review 7.2) | always |
| `eol-acquire` | acquisition service: captures, INVALID checks, permanent recordings | always |
| `eol-app` | the application on http://127.0.0.1:8080 | always |
| `eol-kiosk` | full-screen browser on the touch screen (cage + Chromium); takes the screen's own resolution | with `--kiosk` |
| `eol-exit` | emergency-exit input (GPIO17) → stop the application → power off. **NOT a safety function** | with `--emergency-exit`, only once wired (D8) |

Also: a system user `eol` (groups dialout, gpio, video, render, input); a udev rule giving the Pico's serial ports to the dialout group and keeping ModemManager away; journal size limits (300 MB, 3 months); `/run/eol` made at every boot. Every service restarts by itself if it stops. Logs: `journalctl -u eol-app -u eol-acquire -u eol-heartbeat`.

## Install

On the CM5, in an up-to-date checkout of branch `rev-2`. Use `ssh -t` if over SSH, so `sudo` can ask for the password:

```
cd ~/acoustics_fable
git pull
deploy/install.sh --dry-run --mic-id "KAS-700-0164 #1"
sudo deploy/install.sh --mic-id "KAS-700-0164 #1"
```

Expected last lines:

```
   eol-heartbeat    active
   eol-acquire      active
   eol-app          active
   web application: HTTP 200
RESULT           PASS
```

Then log in at http://127.0.0.1:8080/ as `master` / `change-me-now` (change forced at first login).

- **Run it again** for every update: it only changes what differs, installs the new version beside the old one, migrates the database and restarts the services.
- **Rollback:** `sudo deploy/install.sh --rollback` switches back to the previous version. Database migrations only add, so the old version keeps working.
- **Uninstall:** `sudo deploy/install.sh --uninstall` removes the services and keeps the data; `--purge` deletes the data, the programs and the user too.

The bench data in `~/eol-data` (the first recordings and the demo database) is not moved. Copy it with `python3 -m eol_app backup` and `restore` if it is wanted.

## Backup and restore

```
sudo -u eol env $(cat /etc/eol/eol.env | xargs) /opt/eol/venv/bin/python -m eol_app backup /media/usb
sudo systemctl stop eol-app eol-acquire
sudo -u eol env $(cat /etc/eol/eol.env | xargs) /opt/eol/venv/bin/python -m eol_app restore /media/usb/eol-backup-<time> --force
sudo systemctl start eol-acquire eol-app
```

A backup holds the database (taken online), the recordings and the models, with a SHA-256 of every file. Restore checks them all before copying anything. **[NOT VERIFIED]** the `$(cat ... | xargs)` form with a microphone identity that contains spaces; if it fails, set `EOL_DATA_DIR=/var/lib/eol` by hand instead.

## Offline updates for a customer (signed)

The integrator, once, makes a signing key and puts its public half in `deploy/allowed_signers`:

```
ssh-keygen -t ed25519 -f ~/.ssh/eol_release_key -C eol-release
echo "eol-release@zynith $(cut -d' ' -f1,2 ~/.ssh/eol_release_key.pub)" > deploy/allowed_signers
```

Per release: `deploy/make-release.sh ~/.ssh/eol_release_key` → `build/release/eol-<commit>.tar.gz`, `.sha256`, `.tar.gz.sig`. At the customer, with no network: `deploy/verify-release.sh eol-<commit>.tar.gz`, then `cd eol-<commit> && sudo deploy/install.sh`. A package that was changed or signed by anyone else fails the check. `deploy/allowed_signers` is not in the repository yet: there is no release key yet.

## Not verified

- The real install (needs `sudo`), the services under systemd, and starting at boot.
- The kiosk: needs the Waveshare display attached, and probably its `config.txt` overlay. The service uses cage and Chromium on Wayland; whether that works unchanged on this image is not checked.
- The emergency-exit service: needs the interface board.
- Clock: the installer only reports whether the clock is synchronised (`timedatectl`). An RTC battery and an NTP server on the customer's network are the customer's to provide.
