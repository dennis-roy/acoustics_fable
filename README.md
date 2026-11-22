# acoustics_fable

End-of-line vibro-acoustic test stand (Zynith IT Solutions Pvt Ltd). A Raspberry Pi Pico 2 W captures an ultrasonic microphone and streams it over USB to a Raspberry Pi CM5, which analyses each part and reports OK / NOK to a PLC.

Work happens on branch `rev-2`.

## Status

| Step | Work | State |
|---|---|---|
| 1 | Hardware design review (Phase 0) | approved 2026-11-21, revision B |
| 2 | Pico firmware skeleton, CM5 receiver, link proof with synthetic data, simulator, first self-test | **done 2026-11-22**: bench items 16 to 19 passed on the Pico 2 W plugged into the CM5 |
| 3 | Microphone clock and capture, SELECT probe, microphone test | **capture, probe, test and decimation filter written; a real microphone was read on 2026-11-22 and responds to sound.** Logic analyser firmware and tach sampler not started |
| 4 | Acquisition service and recording storage | **done 2026-11-22**: a real capture stored and verified by hash on the CM5 |
| 5 | Database and Setup screens | **done 2026-11-22**: Setup captures and saves samples from the real microphone through the web screen |
| 6 | Analysis catalogue and models | **software done**: tested on synthetic parts; no model trained on real parts yet |
| 7 | Testing and calibration flow | **software done**: calibration gate, results without raw data, PLC outputs through the Pico; interface board not built |
| 8 | Users, login, reports | **done in software** |
| 9, 10 | Mando and Ashok Leyland applications | **templates**; details to be confirmed with the customers |
| 11 | Install scripts, services, backup, signed updates | **written; dry run on the CM5**; the real install needs your `sudo` (`deploy/README.md`) |

**Verified on hardware so far** (Pico 2 W, CM5 and the KAS-700-0164 microphone, 2026-11-22): the USB link, the watchdog logic, and microphone capture with relative sound checks. Absolute levels are not measured. Each module's README says exactly what was run and where.

## How the pieces fit

```
 microphone --> Pico 2 W --USB, two serial ports--> CM5
                  |                                   |
   firmware/  <---+                                   +--->  link/      receiver, link test,
   makes the stream, drives the outputs,                                heartbeat process
   opens them if the heartbeat stops

 protocol/    the wire format both sides follow
 simulator/   a Pico in software, for working without hardware
 dsp/         the decimation filter: 1-bit stream to 200 kHz samples
 acquire/     the acquisition service: captures, INVALID checks, permanent recordings
 app/         the application: database, login, Setup, models, Testing, reports
 analysis/    the analysis catalogue, golden gain and the models that decide OK / NOK
 deploy/      install.sh, systemd services, backup, signed releases
 firmware_la/ the second Pico as a logic analyser (bench, no oscilloscope)
 bench/       CM5 tool that drives it, with the bench-item pass limits
 scripts/     selftest.sh
 tests/       system tests: receiver against the simulator and against the firmware's logic
 docs/        the design review, the calculations behind it, datasheet links
```

| Folder | Read first |
|---|---|
| `docs/phase0/` | `PHASE0_HARDWARE_DESIGN_REVIEW.md`: why everything is the way it is, with a datasheet citation for every figure |
| `protocol/` | `README.md`: the frame format, commands, version handshake, watchdog rules |
| `firmware/` | `README.md`: build, flash, check |
| `link/` | `README.md`: the CM5-side tools |
| `simulator/` | `README.md`: running without a Pico |
| `dsp/` | `README.md`: the decimation filter, its versions, how to make a WAV file from a capture |
| `acquire/` | `README.md`: the acquisition service and the recording store |
| `app/` | `README.md`: logins, Setup, models, Testing, reports, how to run it |
| `analysis/` | `README.md`: every analysis, the gain, the models and the decision rule |
| `docs/operator/` | `README.md`: operator work instructions |
| `deploy/` | `README.md`: installing on the CM5, services, backup, updates |
| `firmware_la/` | `README.md`: the logic analyser, its modes and the wiring |
| `bench/` | `README.md`: bench checks with the analyser |
| `docs/database/` | `README.md`: every table and column |
| `docs/decisions/` | why the bigger choices were made |
| `scripts/` | `README.md`: the self-test |
| `docs/bench/` | `README.md`: setting up the CM5, flashing the Pico, wiring the microphone on the bench |

## Quick check, no hardware

```
git clone https://github.com/dennis-roy/acoustics_fable.git
cd acoustics_fable
git checkout rev-2
scripts/selftest.sh
```

Needs Python 3.11 or newer, with numpy and scipy for all steps to run. Takes about 2 minutes. Expected last line:

```
PASS 19   FAIL 0   NOT RUN 2   (NOT RUN is not a pass)
```

## First run on the real hardware

This is what closes step 2 (bench items 16 to 19 of the review).

1. Get the firmware: open the latest `ci` run on GitHub for branch `rev-2`, download the `eol_pico-uf2` artifact and unzip it. Or build it (`firmware/README.md`). It compiles, but it has never run on a Pico, so the first power-up may not work; step 4 says what to paste back.
2. Flash it: hold BOOTSEL on the Pico 2 W, plug it into a computer, copy `eol_pico.uf2` onto the drive that appears.
3. Plug the Pico straight into the CM5 IO Board.
4. On the CM5:

   ```
   git clone https://github.com/dennis-roy/acoustics_fable.git && cd acoustics_fable && git checkout rev-2
   ls /dev/serial/by-id/
   scripts/selftest.sh --hardware --long
   python3 docs/phase0/analysis/filter_timing.py
   ```

   `ls` should show two names starting `usb-Zynith_EOL_Pico_front_end_`. The self-test takes about 25 minutes with `--long`. The last command needs `sudo apt install python3-numpy python3-scipy`; it reports the filter time for this board.
5. Paste all the output back. Anything that says FAIL gets fixed before step 3 starts.

On a CM4, do the same; the results are recorded separately.

## Notes for whoever (or whatever) picks this up next

- Start with section 0 and section 12 of the review. Section 12.3 lists what is still needed from the owner.
- **[NOT VERIFIED]** in any document here means: not proven from a datasheet that was opened, or not measured. Do not treat such items as facts. Never claim hardware verification that did not happen; section 13 of the review is the record of what was run.
- Never guess a hardware value. Cite the document and page, as the review does.
- The emergency-exit input is an orderly software shutdown. It is **not a safety function** and must be labelled so wherever it appears.
- The owner has no oscilloscope, frequency counter or sound calibrator. Section 10 of the review is the bench plan for that situation.
- Before changing an existing firmware file, list the file and the reason and get the owner's approval.
- The ADXL383 accelerometer is optional and must never block the rest (review section 8).
- Datasheet PDFs are not committed. `docs/datasheets/README.md` has the links and versions.
