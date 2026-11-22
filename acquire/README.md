# acquire: the acquisition service and the recording store

Python package `eol_acquire`. It runs captures from the Pico, decides whether each one is usable (the INVALID checks), and keeps the permanent recordings.

**State: build-plan step 4. Tested on a macOS laptop against the Python Pico simulator, and on the CM5 with the real Pico 2 W and microphone (2026-11-22).** Needs numpy and scipy (the INVALID checks use the decimation filter in `../dsp`).

## The idea in one picture

```
 application ──capture──▶ service ──Pico──▶ capture ──▶ INVALID checks 1 to 5 ──▶ held in memory
                                                                                      │
 application ──save─────▶ service ──▶ permanent recording + SHA-256   (golden / failure_mode / bench only)
 application ──discard──▶ service ──▶ dropped, never written to disk
```

- **One capture is held at a time.** Setup captures a sample, the operator looks at it, then presses Save or Retest. A new capture replaces an unsaved one.
- **Test and production parts are never stored raw** (the spec's data-retention rule, decision D5). Saving needs a purpose, and there is no purpose for a production part. Analysed results of production parts go to the database (step 5 on).
- **The application never touches the Pico directly.** Only one program at a time can use its data port, so the service owns it. It opens the port only during a capture, so other tools such as `eol_link mictest` can use the Pico in between.

## What makes a capture INVALID

From section 4.8 of `../docs/phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md`. Each failed check gives a reason in plain words, for the screen (decision D7).

| # | Check | Limit (starting value, NOT VERIFIED) |
|---|---|---|
| 1 | data line stuck for the whole capture | exact |
| 2 | longest run of equal bits | 32 bits. Bench item 39 found it fires exactly when peaks reach full scale |
| 3 | share of 1 bits | 45 to 55 % |
| 4 | peak of the filtered signal | above -3 dBFS |
| 5 | any lost, corrupt or dropped frame, or the probe found no microphone | any |

The limits used are written into every recording, so a later change stays traceable.

**Pre-roll:** the first 0.5 s of every capture is received and checked for loss, but left out of the recording. On the bench the microphone's output below 200 Hz settles for about 0.4 s after it wakes; above 200 Hz it is steady from the start (review 13.5).

## Permanent recordings

Kept under `<data dir>/recordings/<year>/<month>/`:

- `rec-<time>-<random>.pdm`: the raw 1-bit stream after the pre-roll, exactly as the Pico sent it.
- `rec-<time>-<random>.json`: what it is. Purpose, part, failure mode, note, **microphone identity**, **Pico firmware**, **filter name and SHA-256**, software version, capture time, probe result, link counts, the INVALID checks with their limits and levels, and the **SHA-256 of the .pdm**.

That is decision D5: raw stream, SHA-256, microphone identity, firmware and filter version.

| Purpose | Use | INVALID capture allowed? |
|---|---|---|
| `golden` | a Golden OK sample | no |
| `failure_mode` | a sample of a named failure mode (`--failure-mode`, required) | no |
| `bench` | an engineering recording | yes: storing a bad capture can be the point |

Names follow the spec's Setup rules: letters, digits, space and `- _ . / ( )`; part number up to 20 characters, failure mode up to 30.

**Written safely** (`../link/eol_link/recording.py`). Each file is written under a temporary name, flushed to the disk and renamed. The data goes first and the description last, so a power cut or an emergency exit leaves at most a `.pdm` without its `.json`. `verify` reports that as "unfinished", never as a recording. Both files are made read-only. Before writing, at least 500 MB must stay free on the disk.

Deleting a recording is not offered here: in the spec it is a Setup action, audited, that invalidates the model (step 5).

**Where:** `$EOL_DATA_DIR` if set; otherwise `/var/lib/eol` if it exists and is writable (production, made by the install scripts of step 11); otherwise `~/eol-data` (bench). The service's socket goes in `$EOL_RUN_DIR`, `/run/eol` or `~/eol-data/run`, by the same rule.

## Use

From the repository root:

```
export PYTHONPATH=$PWD/link:$PWD/dsp:$PWD/acquire

python3 -m eol_acquire serve --mic-id "KAS-700-0164 bench 1" &    # the service
python3 -m eol_acquire status
python3 -m eol_acquire capture --seconds 5                         # prints a capture id and VALID or INVALID
python3 -m eol_acquire save <capture id> --purpose bench --note "quiet room"
python3 -m eol_acquire save <capture id> --purpose failure_mode --failure-mode squeal --part BRK-1234
python3 -m eol_acquire discard <capture id>
python3 -m eol_acquire list                                        # does not need the service
python3 -m eol_acquire verify                                      # every recording against its SHA-256
```

Stop the service with Ctrl-C, or `kill` its process. Stop it before `scripts/selftest.sh --hardware`, which needs the Pico to itself. A systemd unit for it comes with the install scripts (step 11).

Expected from `capture`, in a quiet room on the bench:

```
probe            SELECT = GND (data read at the rising edge)
frames           550 received, 0 missing
bits that are 1  49.99%
longest run      under 32 bits
level            peak -46.4 dBFS, rms -58.4 dBFS (filter eol-pdm3200k-d16-p40k-s120k-a100-r1)
RESULT           VALID
```

`verify` ends with `RESULT PASS` when every recording matches its SHA-256.

### The socket protocol

One JSON object per line each way. Every reply has `"ok"`; a refused request has `"ok": false` and `"error"` with the reason.

| Request | Reply |
|---|---|
| `{"cmd": "status"}` | pending capture id, data folder, microphone, decimation, limits, software version, number of recordings |
| `{"cmd": "capture", "seconds": 5, "clock_mhz": 3.2}` | `"capture"`: id, valid, reasons, probe, frames, missing, levels, bands, filter |
| `{"cmd": "save", "capture_id": "...", "purpose": "golden", "part": "...", "failure_mode": "...", "note": "..."}` | `"recording"`: id, path, SHA-256, bytes, seconds |
| `{"cmd": "discard", "capture_id": "..."}` | `"discarded"` |

`seconds` is from above 0 up to 60. A 4.0 MHz capture works but is always INVALID: there is no filter for that clock, so check 4 cannot run.

## Tests

```
python3 -m unittest discover -s acquire/tests
```

Expected: 17 tests, `OK`. They check each INVALID reason; that a saved recording is complete, read-only and verifiable, and equals the simulated signal after the pre-roll bit for bit; that test and production parts can never be stored; that an INVALID capture cannot become a calibrated sample; the name rules; that a full disk is refused before anything is written; that `verify` finds changed, missing and unfinished recordings; that a save interrupted after the data leaves nothing that looks complete; and the service over its socket. I broke the code nine ways on purpose; all nine were caught.

`scripts/selftest.sh --hardware --mic` also runs a capture, save and verify on the real Pico, in a separate folder under `build/`.

## Not verified

- The INVALID limits (review 4.8): starting values.
- What happens to a write in progress when power is really cut. The design follows the usual safe-write steps, and an interrupted write is tested by simulation; bench item 34 (emergency exit while writing) has not been done.
- Long-running behaviour: the service has run for minutes, not days.
