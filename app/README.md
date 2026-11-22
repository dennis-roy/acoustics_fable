# app: the test stand application

Python package `eol_app`. The database, and the screens shown on the CM5's touch screen: login, Setup and Programming. Testing, analysis and reports are added in the next steps.

**State: build-plan step 5 (database and Setup). Tested on a macOS laptop against the Python Pico simulator, and on the CM5 with the real Pico and microphone (2026-11-22).** Testing and calibration screens are not built yet.

## How it fits

```
 touch screen (browser) ──HTTP──▶ eol_app (this) ──SQLite──▶ eol.sqlite3
                                     │
                                     └──local socket──▶ eol_acquire service ──USB──▶ Pico ──▶ microphone
```

- The database is SQLite in WAL mode, one file. Why: `../docs/decisions/2026-11-22-database-sqlite-wal.md`. Tables: `../docs/database/README.md`.
- Captures go through the acquisition service (`../acquire`), which must be running. The application never opens the Pico itself.

## Logins and roles

| Role | Can |
|---|---|
| programming | everything, plus users, programming variables and the audit log |
| setup | parts, failure modes, calibrated samples |
| testing | testing (step 7) |

- **The master login:** user `master`, default password `change-me-now`. It must be changed at the first login. There is exactly one master; it cannot be deactivated.
- New users get a temporary password, shown once, that must be changed at their first login.
- Passwords: at least 8 characters, a letter and a digit, not containing the user name. Stored as scrypt hashes (Python's standard library).
- Five wrong passwords in a row lock an account for 15 minutes (both are programming variables). Programming can unlock it.
- **Recovery** if the master password is lost: on the CM5 itself, `python3 -m eol_app reset-master` prints a new temporary password. It needs a shell on the machine, so physical or SSH access, and it is audited.
- Every change is written to the audit log with who, when and what.

## Setup, as built

1. **Parts:** part number (up to 20: letters, digits, space and `- _ . / ( )`), description (up to 60), capture duration 5 s up to the limit (+/- buttons), tach connected and pulses per revolution.
2. **Failure modes:** names up to 30, same characters.
3. **Samples:** first each failure mode, then Golden OK. For each capture: the unit's serial or barcode (it keeps one physical part's samples together for cross-validation), training or validation set (automatic by default: every 5th sample to validation), then **Capture**.
4. The result shows VALID or INVALID with the reasons and the levels. **Save** keeps it permanently as a calibrated sample (an INVALID one cannot be saved); **Retest** throws it away and logs it as aborted.
5. Deleting a sample or a failure mode is audited and soft (the row is kept with a deletion time; the raw recording stays). It marks the part's models invalidated until they are retrained and approved.

Capture is started by the Capture button for now. Starting it from the Start input is step 7.

## Programming variables

`capture_limit_s` (10), `samples_per_mode` (10, up to 25), `golden_samples` (10), `validation_every` (5), `peak_threshold` (0.7), `lockout_attempts` (5), `lockout_minutes` (15), `session_idle_minutes` (480). Each has a minimum, maximum and default; changes are audited with old and new value. More are added with the steps that use them.

## Install and run (bench)

On the CM5, once:

```
cd ~/acoustics_fable
python3 -m venv --system-site-packages ~/eol-venv
~/eol-venv/bin/pip install -r requirements.txt
```

Then, each time (two terminals, or add `&`):

```
cd ~/acoustics_fable
export PYTHONPATH=$PWD/link:$PWD/dsp:$PWD/acquire:$PWD/app
~/eol-venv/bin/python -m eol_acquire serve --mic-id "KAS-700-0164 bench 1"
~/eol-venv/bin/python -m eol_app serve --port 8080
```

Open `http://127.0.0.1:8080/` in a browser on the CM5. From another computer: start with `--host 0.0.0.0` and open `http://<CM5 address>:8080/`. That exposes the login page to the whole network, so only do it on a network you trust. The systemd services and the kiosk screen come with step 11.

Other commands: `python3 -m eol_app info` (database file, schema version, counts), `migrate`, `reset-master`.

## Tests

```
python3 -m unittest discover -s app/tests
```

Expected: 10 tests, `OK`. They cover: migrations create every table and can run twice; the schema refuses bad rows (too-long part number, tach without PPR, unknown role, a result for a part that does not exist, a second master); passwords; the forced change of the master password; lockout after five wrong passwords; forms without the session token are refused; roles limit what a user can open; programming variables are range-checked and audited; and Setup end to end: create a part and a failure mode, capture through the acquisition service and the simulated Pico, save, retest, a stale capture refused, delete a failure mode.

## Not verified

- On the touch screen itself: the CM5 has no desktop or browser installed yet (step 11). The screens were checked in tests, not by eye on the 1024×600 and 1280×800 screens.
- Many users at once: one station, one operator at a time is the design.
