# link: the CM5 side of the Pico link

Python package `eol_link`. Standard library only, with one exception: the band levels in the microphone test need numpy (`sudo apt install python3-numpy`), and are left out without it. Needs Python 3.11 or newer. Runs on a CM5, a CM4 or any Linux or macOS machine.

**State: step 2. Tested on a macOS laptop and on Ubuntu 24.04 in CI, against stand-ins for the Pico. Not yet run against a real Pico, or on a CM5 or CM4.**

## What is in it

| Module | Job |
|---|---|
| `protocol.py` | Builds and parses frames; the synthetic test pattern. The contract is `../protocol/README.md` |
| `transport.py` | Opens the Pico's serial ports and finds them under `/dev/serial/by-id/` |
| `client.py` | The application's side of the DATA port: version handshake, commands, incoming frames |
| `linktest.py` | The link proof: a synthetic stream checked for sequence, CRC and content |
| `wdtest.py` | Checks the Pico's heartbeat watchdog through the Pico's own reports |
| `heartbeat.py` | The heartbeat process, and the application's check-in with it |
| `capture.py` | One microphone capture: starts the stream, receives every frame, leaves out the pre-roll, counts any loss |
| `recording.py` | The recording file format (`.pdm` and `.json` with SHA-256), written safely, and its check |
| `mictest.py` | The microphone test: probe result, a complete stream, bit statistics, band levels |
| `pdmcheck.py` | Checks on the raw microphone bit stream that make a capture INVALID (stuck line, overload runs, lopsided bits) |
| `pdmspec.py` | Level per frequency band, straight from the 1-bit stream. The only module that needs numpy |

## The picture

```
  application ---------------- DATA port ----------------> Pico
      |   commands; receives stream, events, stats
      |
      | "alive", every 250 ms, over a local socket
      v
  heartbeat process ----------- HB port -----------------> Pico
      HEARTBEAT every 100 ms                               opens all outputs after
                                                           500 ms without one
```

The heartbeat is a separate process on purpose. If the application sent it, a long analysis could delay it and the Pico would open the outputs for no reason. The heartbeat process is tiny, asks Linux for real-time priority and locks its memory. The application only has to check in with it every so often; if it stops for 2 s, the heartbeat process tells the Pico and the outputs open.

## Use

From the repository root:

```
export PYTHONPATH=$PWD/link:$PWD/simulator

python3 -m eol_link info                       # handshake; what the Pico reports
python3 -m eol_link linktest --seconds 600     # bench item 17
python3 -m eol_link linktest --seconds 10 --max-rate   # bench item 16
python3 -m eol_link linktest --seconds 600 --busy      # bench item 18
python3 -m eol_link wdtest                     # watchdog logic
python3 -m eol_link mictest --seconds 5        # microphone: bench items 20 and 37
python3 -m eol_link mictest --seconds 5 --save captures   # the same, keeping the raw stream
python3 -m eol_link bootloader                 # restart the Pico as a USB drive, for new firmware
python3 -m eol_link heartbeat --socket /run/eol/heartbeat.sock
```

The Pico is found automatically. To use a simulator, give `--port` and `--hb-port` before the command (see `../simulator/README.md`).

`linktest` ends with `RESULT PASS` or `RESULT FAIL` and the reasons. It passes only with zero missing frames, zero CRC errors, zero wrong bytes, zero stray bytes, and the Pico agreeing that it dropped nothing.

`mictest` prints what the Pico's SELECT probe found, checks that every frame arrived, that about half the bits are 1 with no long runs, and gives the level in three bands (up to 20 kHz, 20 to 40 kHz, 40 to 80 kHz) in dBFS. The levels are for comparing one run with another; nothing here measures sound pressure. Options: `--clock 4.0` for the 4.0 MHz clock, `--read rising` or `--read falling` to override the probe.

## Tests

```
python3 -m unittest discover -s link/tests
```

Expected: 22 tests, `OK` (3 of them are skipped without numpy). The system tests that use the simulator are in `../tests/`; `../scripts/selftest.sh` runs everything.

## Not verified

- Against a real Pico, and on a CM5 or CM4.
- `mictest` against a real microphone. It has only seen simulated ones.
- Real-time priority and memory locking: they are Linux features, requested only by the `heartbeat` command, which no test starts. So they have never been exercised, on the laptop or in CI. The heartbeat process warns if it cannot get them. On the CM5 they need the service to run with the right privileges; the systemd unit comes with the install scripts (step 11).
- The limits in `pdmcheck.py`. They are starting values until bench item 39 measures the microphone's real overload behaviour.
