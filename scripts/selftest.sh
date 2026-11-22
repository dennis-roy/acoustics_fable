#!/usr/bin/env bash
# Self-test for build-plan step 2 (firmware skeleton, receiver, link proof).
#
#   scripts/selftest.sh              everything that needs no Pico (about 2 minutes)
#   scripts/selftest.sh --long       the same, with 10-minute link runs instead of 10 s
#   scripts/selftest.sh --hardware   also the tests on a real Pico plugged into this machine
#                                    (bench items 16 to 19; add --long for the 10-minute runs)
#   scripts/selftest.sh --hardware --mic   also the microphone test, once the microphone is wired
#
# Every step ends as PASS, FAIL or NOT RUN. NOT RUN is never counted as a pass.
# Exit code: 0 only if nothing failed.

set -u
cd "$(dirname "$0")/.."
ROOT=$PWD
export PYTHONPATH="$ROOT/link:$ROOT/simulator"
PY=${PYTHON:-python3}
BUILD=$ROOT/build
LOG=$BUILD/selftest.log
mkdir -p "$BUILD"
: > "$LOG"

HARDWARE=0
MIC=0
SOAK=10
for arg in "$@"; do
  case $arg in
    --hardware) HARDWARE=1 ;;
    --mic) MIC=1 ;;
    --long) SOAK=600 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

n_pass=0 n_fail=0 n_notrun=0
summary=()

run() { # run "name" command...
  local name=$1; shift
  printf '%-58s ' "$name"
  { echo; echo "=== $name"; echo "\$ $*"; } >> "$LOG"
  if "$@" >> "$LOG" 2>&1; then
    echo PASS; n_pass=$((n_pass + 1)); summary+=("PASS     $name")
  else
    echo FAIL; n_fail=$((n_fail + 1)); summary+=("FAIL     $name")
    tail -n 15 "$LOG" | sed 's/^/    | /'
  fi
}

notrun() { # notrun "name" "reason"
  printf '%-58s %s\n' "$1" "NOT RUN ($2)"
  n_notrun=$((n_notrun + 1)); summary+=("NOT RUN  $1: $2")
}

SIM_PID=
start_device() { # start_device command... ; sets DATA_PORT and HB_PORT
  local out=$BUILD/device.out
  : > "$out"
  "$@" > "$out" 2>> "$LOG" &
  SIM_PID=$!
  for _ in $(seq 50); do
    grep -q '^PORTS ' "$out" && break
    sleep 0.1
  done
  read -r _ DATA_PORT HB_PORT < <(grep '^PORTS ' "$out")
}
stop_device() {
  [ -n "$SIM_PID" ] && kill "$SIM_PID" 2> /dev/null && wait "$SIM_PID" 2> /dev/null
  SIM_PID=
}
trap stop_device EXIT

link() { "$PY" -m eol_link --port "$DATA_PORT" --hb-port "$HB_PORT" "$@"; }
refused() { ! link info && grep -q 'REFUSED' <(link info); }

device_checks() { # device_checks "label"
  run "$1: version handshake" link info
  run "$1: link, real rate, ${SOAK} s, zero loss" link linktest --seconds "$SOAK"
  # --min-rate 0: a stand-in's speed says nothing about USB, so only loss is judged here.
  run "$1: link, maximum rate, 5 s, zero loss" link linktest --seconds 5 --max-rate --min-rate 0
  run "$1: heartbeat watchdog logic" link wdtest
  run "$1: microphone test, simulated microphone, 3 s" link mictest --seconds 3
}

echo "Self-test, step 2. Log: $LOG"
echo

# ---- 1. No hardware needed ----

run "Python is 3.11 or newer" "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))'
run "unit tests: frame format, synthetic data, mic checks" "$PY" -m unittest discover -s link/tests
if ! "$PY" -c 'import numpy' 2> /dev/null; then
  notrun "band-level tests" "numpy is not installed here (on a Raspberry Pi: sudo apt install python3-numpy)"
fi

CORE_HOST=
if command -v cc > /dev/null; then
  run "firmware core compiles for this computer, no warnings" \
    cc -std=c11 -O2 -Wall -Wextra -Werror -Ifirmware/src -o "$BUILD/core_host" \
    firmware/host/core_host.c firmware/src/core.c firmware/src/proto.c
  [ -x "$BUILD/core_host" ] && CORE_HOST=$BUILD/core_host
else
  notrun "firmware core compiles for this computer" "no C compiler (cc) here"
fi

if [ -n "$CORE_HOST" ]; then
  run "system tests: simulator and firmware core" env EOL_CORE_HOST="$CORE_HOST" "$PY" -m unittest discover -s tests
else
  run "system tests: simulator only" "$PY" -m unittest discover -s tests
  notrun "system tests: firmware core" "the core was not built"
fi

start_device "$PY" -m pico_sim
device_checks "simulator"
stop_device

start_device "$PY" -m pico_sim --proto-major 2
run "simulator with protocol 2: receiver refuses it" refused
stop_device

if [ -n "$CORE_HOST" ]; then
  start_device "$CORE_HOST"
  device_checks "firmware core on this computer"
  stop_device
else
  notrun "firmware core on this computer: link and watchdog" "the core was not built"
fi

# ---- 2. With a real Pico ----

if [ "$HARDWARE" = 1 ]; then
  if PORTS=$("$PY" -c 'from eol_link.transport import find_ports as f; p = f(); print(*p) if p else exit(1)'); then
    read -r DATA_PORT HB_PORT <<< "$PORTS"
    run "Pico: version handshake (bench item 19)" link info
    link info | sed 's/^/    | /'
    run "Pico: maximum rate at least 650 kB/s, 10 s (bench item 16)" link linktest --seconds 10 --max-rate
    run "Pico: real rate, ${SOAK} s, zero loss (bench item 17)" link linktest --seconds "$SOAK"
    run "Pico: same with every CPU core busy (bench item 18)" link linktest --seconds "$SOAK" --busy
    run "Pico: heartbeat watchdog logic" link wdtest
    if [ "$MIC" = 1 ]; then
      run "Pico: microphone test, 5 s (bench items 20 and 37)" link mictest --seconds 5
      link mictest --seconds 5 | sed 's/^/    | /'
    else
      notrun "Pico: microphone test" "give --mic once the microphone is wired"
    fi
    [ "$SOAK" -lt 600 ] && notrun "Pico: 10-minute runs for bench items 17 and 18" "give --long"
  else
    notrun "Pico: bench items 16 to 19" "no Pico found under /dev/serial/by-id/"
  fi
else
  notrun "Pico: bench items 16 to 19" "no --hardware option"
fi
notrun "Pico firmware build" "not part of this script: CI builds it, or see firmware/README.md"

echo
echo "Summary"
printf '  %s\n' "${summary[@]}"
echo
echo "PASS $n_pass   FAIL $n_fail   NOT RUN $n_notrun   (NOT RUN is not a pass)"
[ "$n_fail" = 0 ]
