# scripts

## selftest.sh

One command that says what works. Run it from anywhere:

```
scripts/selftest.sh              # no hardware needed, about 1.5 minutes
scripts/selftest.sh --long       # the same, with 10-minute link runs
scripts/selftest.sh --hardware   # plus the tests on a real Pico plugged into this machine
scripts/selftest.sh --hardware --long   # bench items 16 to 19 in full
```

Needs Python 3.11 or newer and nothing else. A C compiler (`cc`) is used if present.

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
3. a real-rate link run with zero loss, a maximum-rate run with zero loss, and the watchdog check, through the command-line tools;
4. that firmware claiming another protocol version is refused.

With `--hardware` it also runs bench items 16 to 19 from the Phase 0 review on the Pico it finds under `/dev/serial/by-id/`. Stop the heartbeat service first, because the watchdog check needs the heartbeat port to itself.

Expected last line on a machine with a C compiler and no Pico:

```
PASS 13   FAIL 0   NOT RUN 2   (NOT RUN is not a pass)
```

This is the first self-test, for step 2. Later steps add to it (microphone, storage, database, outputs).
