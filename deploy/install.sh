#!/usr/bin/env bash
# Installs the EOL test stand on a Raspberry Pi CM5 or CM4 (Raspberry Pi OS 64-bit).
# Safe to run again: every step checks first and only changes what is missing or different.
#
#   sudo deploy/install.sh --mic-id "KAS-700-0164 #1"            install or update from this checkout
#   deploy/install.sh --dry-run --mic-id "..."                     show what would be done; changes nothing
#   sudo deploy/install.sh --mic-id "..." --kiosk                  also start the touch-screen browser at boot
#   sudo deploy/install.sh --mic-id "..." --emergency-exit         also enable the emergency-exit input (D8: only once wired)
#   sudo deploy/install.sh --rollback                              go back to the previous installed version
#   sudo deploy/install.sh --uninstall [--purge]                   remove services; --purge also deletes data and the user
#
# Layout: /opt/eol/releases/<commit>, /opt/eol/current -> the active one, /opt/eol/venv,
# /etc/eol/eol.env (settings), /var/lib/eol (database, recordings, models), /run/eol (sockets).
# The emergency-exit input is NOT a safety function.
set -euo pipefail

SRC=$(cd "$(dirname "$0")/.." && pwd)
PREFIX=/opt/eol
DATA=/var/lib/eol
ETC=/etc/eol
USER_NAME=eol
UNITS=(eol-heartbeat eol-acquire eol-app)
DRY=0 KIOSK=0 EXIT=0 MODE=install PURGE=0 MIC_ID=""

while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) DRY=1 ;;
    --kiosk) KIOSK=1 ;;
    --emergency-exit) EXIT=1 ;;
    --mic-id) MIC_ID=${2:?--mic-id needs a value}; shift ;;
    --rollback) MODE=rollback ;;
    --uninstall) MODE=uninstall ;;
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\n== %s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then printf '   would run: %s\n' "$*"; else "$@"; fi; }
write() { # write <path> <mode>: content from stdin; only if different
  local path=$1 mode=$2 tmp
  tmp=$(mktemp)
  cat > "$tmp"
  if [ -f "$path" ] && cmp -s "$tmp" "$path"; then rm -f "$tmp"; echo "   unchanged: $path"; return 1; fi
  if [ "$DRY" = 1 ]; then echo "   would write: $path"; rm -f "$tmp"; return 0; fi
  install -D -m "$mode" "$tmp" "$path"; rm -f "$tmp"; echo "   wrote: $path"; return 0
}

if [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then echo "run with sudo (or add --dry-run)" >&2; exit 1; fi

# ---------- uninstall ----------
if [ "$MODE" = uninstall ]; then
  say "Stopping and removing services"
  for u in "${UNITS[@]}" eol-kiosk eol-exit; do
    if [ -f "/etc/systemd/system/$u.service" ]; then
      run systemctl disable --now "$u.service" || true
      run rm -f "/etc/systemd/system/$u.service"
    fi
  done
  run rm -f /etc/udev/rules.d/60-eol-pico.rules /etc/tmpfiles.d/eol.conf /etc/systemd/journald.conf.d/eol.conf
  run systemctl daemon-reload
  if [ "$PURGE" = 1 ]; then
    say "Purging data, programs and the $USER_NAME user"
    run rm -rf "$PREFIX" "$DATA" "$ETC" /run/eol
    if id "$USER_NAME" > /dev/null 2>&1; then run userdel "$USER_NAME"; fi
  else
    echo "   kept: $DATA (database, recordings, models), $PREFIX, $ETC. Add --purge to delete them."
  fi
  exit 0
fi

# ---------- rollback ----------
if [ "$MODE" = rollback ]; then
  cur=$(readlink -f "$PREFIX/current" 2>/dev/null || true)
  prev=$(ls -1dt "$PREFIX"/releases/* 2>/dev/null | grep -vx "$cur" | head -1 || true)
  [ -n "$prev" ] || { echo "no earlier release to go back to" >&2; exit 1; }
  say "Rolling back: $(basename "$cur") -> $(basename "$prev")"
  run ln -sfn "$prev" "$PREFIX/current.new"
  run mv -T "$PREFIX/current.new" "$PREFIX/current"
  run systemctl restart "${UNITS[@]}"
  echo "   The database is not rolled back: migrations only add. A newer schema is read by older code only if it did not depend on the new parts."
  exit 0
fi

# ---------- install / update ----------
[ -n "$MIC_ID" ] || [ -f "$ETC/eol.env" ] || { echo "--mic-id is required on the first install (decision D5)" >&2; exit 2; }

say "Checking this machine"
. /etc/os-release
echo "   OS: $PRETTY_NAME"
model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)
echo "   board: $model"
case $model in *"Compute Module 5"*|*"Compute Module 4"*) ;; *) echo "   note: not a CM5 or CM4; continuing";; esac
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Python 3.11 or newer is needed" >&2; exit 1; }
commit=$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo "nogit-$(date +%Y%m%d%H%M%S)")
dirty=$(git -C "$SRC" status --porcelain --untracked-files=no 2>/dev/null | head -1 || true)
[ -z "$dirty" ] || echo "   note: the checkout has uncommitted changes; they are installed too (release $commit+changes)"
echo "   release: $commit"

say "Packages"
pkgs=(python3-venv python3-numpy python3-scipy git)
[ "$KIOSK" = 1 ] && pkgs+=(cage chromium)
[ "$EXIT" = 1 ] && pkgs+=(python3-libgpiod)
missing=()
for p in "${pkgs[@]}"; do dpkg -s "$p" > /dev/null 2>&1 || missing+=("$p"); done
if [ ${#missing[@]} -gt 0 ]; then run apt-get update; run apt-get install -y "${missing[@]}"; else echo "   all present: ${pkgs[*]}"; fi

say "User $USER_NAME"
if id "$USER_NAME" > /dev/null 2>&1; then echo "   exists"; else run useradd --system --home "$DATA" --shell /usr/sbin/nologin "$USER_NAME"; fi
for grp in dialout gpio video render input; do
  if getent group "$grp" > /dev/null && ! id -nG "$USER_NAME" 2>/dev/null | tr ' ' '\n' | grep -qx "$grp"; then run usermod -aG "$grp" "$USER_NAME"; fi
done

say "Program files"
rel=$PREFIX/releases/$commit${dirty:++changes}
if [ -d "$rel" ] && [ -z "$dirty" ]; then echo "   $rel already there"
else
  run mkdir -p "$rel"
  if [ "$DRY" = 1 ]; then echo "   would copy $SRC to $rel"
  else (cd "$SRC" && tar --exclude=.git --exclude=build --exclude='__pycache__' -cf - .) | tar -C "$rel" -xf -; fi
fi
if [ ! -x "$PREFIX/venv/bin/python" ]; then run python3 -m venv --system-site-packages "$PREFIX/venv"; fi
run "$PREFIX/venv/bin/pip" install --quiet -r "$rel/requirements.txt"

say "Settings and folders"
if [ -n "$MIC_ID" ]; then
  printf 'EOL_DATA_DIR=%s\nEOL_RUN_DIR=/run/eol\nEOL_MIC_ID="%s"\nPYTHONPATH=%s/current/link:%s/current/dsp:%s/current/acquire:%s/current/app:%s/current/analysis\n' \
    "$DATA" "$MIC_ID" "$PREFIX" "$PREFIX" "$PREFIX" "$PREFIX" "$PREFIX" | write "$ETC/eol.env" 0644 || true
fi
run install -d -o "$USER_NAME" -g "$USER_NAME" -m 0750 "$DATA"
echo "d /run/eol 0770 $USER_NAME $USER_NAME -" | write /etc/tmpfiles.d/eol.conf 0644 && run systemd-tmpfiles --create /etc/tmpfiles.d/eol.conf || true

say "Pico USB rule (group dialout; ModemManager keeps away)"
cat <<'RULE' | write /etc/udev/rules.d/60-eol-pico.rules 0644 && run udevadm control --reload-rules && run udevadm trigger --subsystem-match=tty || true
# Zynith EOL Pico front end (firmware/src/usb_descriptors.c): Raspberry Pi vendor ID, development product ID.
SUBSYSTEM=="tty", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="000a", MODE="0660", GROUP="dialout", ENV{ID_MM_DEVICE_IGNORE}="1"
RULE

say "Log limits"
printf '[Journal]\nSystemMaxUse=300M\nMaxRetentionSec=3month\n' | write /etc/systemd/journald.conf.d/eol.conf 0644 && run systemctl restart systemd-journald || true

say "Switching to release $(basename "$rel")"
run ln -sfn "$rel" "$PREFIX/current.new"
run mv -T "$PREFIX/current.new" "$PREFIX/current"

say "Database"
if [ "$DRY" = 1 ]; then echo "   would run: python -m eol_app migrate (as $USER_NAME)"
else (set -a; . "$ETC/eol.env"; set +a; runuser -u "$USER_NAME" -- env PYTHONPATH="$PYTHONPATH" EOL_DATA_DIR="$EOL_DATA_DIR" "$PREFIX/venv/bin/python" -m eol_app migrate); fi

say "Services"
changed=0
for u in "${UNITS[@]}"; do
  sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@USER@|$USER_NAME|g" "$SRC/deploy/systemd/$u.service" | write "/etc/systemd/system/$u.service" 0644 && changed=1 || true
done
if [ "$KIOSK" = 1 ]; then
  sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@USER@|$USER_NAME|g" "$SRC/deploy/systemd/eol-kiosk.service" | write /etc/systemd/system/eol-kiosk.service 0644 && changed=1 || true
  UNITS+=(eol-kiosk)
fi
if [ "$EXIT" = 1 ]; then
  sed -e "s|@PREFIX@|$PREFIX|g" "$SRC/deploy/systemd/eol-exit.service" | write /etc/systemd/system/eol-exit.service 0644 && changed=1 || true
  UNITS+=(eol-exit)
fi
run systemctl daemon-reload
run systemctl enable "${UNITS[@]/%/.service}"
run systemctl restart "${UNITS[@]/%/.service}"

say "Checks"
if [ "$DRY" = 1 ]; then echo "   (dry run: nothing to check)"; exit 0; fi
sleep 3
ok=1
for u in "${UNITS[@]}"; do
  s=$(systemctl is-active "$u.service" || true); printf '   %-16s %s\n' "$u" "$s"; [ "$s" = active ] || ok=0
done
code=$(python3 -c 'import urllib.request as u; print(u.urlopen("http://127.0.0.1:8080/login", timeout=5).status)' 2>/dev/null || echo none)
echo "   web application: HTTP $code"; [ "$code" = 200 ] || ok=0
timedatectl show -p NTPSynchronized --value 2>/dev/null | sed 's/^/   clock synchronised: /'
[ "$ok" = 1 ] && echo "RESULT           PASS" || { echo "RESULT           FAIL (see: journalctl -u eol-app -u eol-acquire -u eol-heartbeat -n 50)"; exit 1; }
