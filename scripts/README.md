# scripts

## selftest.sh

One command that says what works. Run it from anywhere:

```
scripts/selftest.sh              # no hardware needed, about 2 minutes
scripts/selftest.sh --long       # the same, with 10-minute link runs
scripts/selftest.sh --hardware   # plus the tests on a real Pico plugged into this machine
scripts/selftest.sh --hardware --long   # bench items 16 to 19 in full
scripts/selftest.sh --hardware --mic    # plus the microphone test, once the microphone is wired
```

Needs Python 3.11 or newer. A C compiler (`cc`), numpy, scipy and the packages in `../requirements.txt` are used if present. On the CM5 run it with the application's virtual environment so the application tests run too: `PYTHON=~/eol-venv/bin/python scripts/selftest.sh`.

Every step ends as one of three:

| Result | Meaning |
|---|---|
| PASS | ran and met its criterion |
| FAIL | ran and did not. The last lines of its output are shown; all of it is in `build/selftest.log` |
| NOT RUN | could not run here, with the reason. **Never counted as a pass** |

The exit code is 0 only if nothing failed.

Without `--hardware` it checks, against the Python simulator and against the firmware's logic compiled for this computer:

1. the frame format, synthetic data and microphone bit-stream checks (unit tests);
2. handshake, stream, loss reporting, outputs and watchdog (system tests in `../tests/`);
3. a real-rate link run with zero loss, a maximum-rate run with zero loss, the watchdog check and a microphone test on a simulated microphone, through the command-line tools;
4. that firmware claiming another protocol version is refused;
5. the decimation filter (`../dsp/tests`);
6. the acquisition service and recording store against the simulator (`../acquire/tests`).

With `--hardware` it also runs bench items 16 to 19 from the Phase 0 review on the Pico it finds under `/dev/serial/by-id/`. Stop the heartbeat service and the acquisition service first, because these checks need the Pico to themselves.

Expected last line on a machine with a C compiler and no Pico:

```
PASS 17   FAIL 0   NOT RUN 2   (NOT RUN is not a pass)
```

With `--hardware --mic` on the CM5 with the Pico and microphone, it was `PASS 26   FAIL 0   NOT RUN 2` on 2026-11-22. That run also captures through the acquisition service, saves the capture in `build/acquire-selftest/` and verifies it by its SHA-256.

Without numpy or scipy there are extra NOT RUN lines, for the band-level and decimation filter tests.

Later steps add to it (storage, database, outputs).
