# firmware_la: the second Pico as a logic analyser

The bench has no oscilloscope (fact 6). A second Pico 2 watches the front-end Pico's wires and times them to about 7 to 13 ns. This is the firmware for it; `../bench/` is the CM5 tool that drives it. Why and what for: section 10.1 of `../docs/phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md`.

**State: runs on a real Pico 2 (self-test against its own generator, 2026-11-23, below). Not yet connected to the front-end Pico.**

## What it can and cannot see

It sees only 0 and 1, at its own input threshold, timed by its own crystal. So it can give: which edge comes first, delays and pulse widths to a few nanoseconds, duty cycle, and frequency **as a ratio to its own crystal**. It cannot give voltages, edge rise times, or an absolute frequency (review 10.4).

## Modes

| Command (one text line) | What it does | Resolution |
|---|---|---|
| `id` | firmware version and clock | |
| `levels` | the eight inputs right now | |
| `widths <ch> <periods> <timeout ms> [skip]` | times every high and low phase of one input, from a rising edge; reports shortest, longest, sums, and the first change of frequency (more than 1/8) with the time to it | 2 cycles = 13.3 ns per phase; averages to better |
| `burst <samples> <div> <trigger ch or -> <r or f> <timeout ms>` | all eight inputs, `150 MHz / div` samples per second, up to 262 144 samples, after a trigger edge | 6.7 ns at div 1 |
| `gen <pin 8-22> <hz> <duty %>`, `gen off` | square wave from the PWM, for example a stand-in tach | the crystal's |

Every answer is one JSON line. A burst is followed by base64 lines and `END`.

**Pulse widths without bias.** `la.pio` has two loops built the same way: every pass is 2 cycles and counts once, leaving either loop costs 2 cycles, and the push at the start of the next phase 3 cycles. So a phase lasts exactly `2n + 5` cycles whether high or low, and the duty cycle is not tilted towards either. Where an edge falls between two samples varies at random against the analyser's own clock, and averages out over many periods.

**Keeping up.** The phases go to a 32 kB ring by DMA, and the processor works them through. At 3.2 MHz that is 6.4 million numbers a second, close to what one core at 150 MHz can do. If it falls a ring behind, the result says `"overrun": true` and the tool marks it invalid; it never reports half a measurement as good. **[NOT VERIFIED]** that it keeps up at 3.2 MHz: if it does not, the fix is to run the analyser's clock higher, or measure every other period.

## Wiring (both Picos plugged into the CM5)

**Unplug both USB cables while wiring.** All signals are 3.3 V: do not connect anything above 3.3 V to the analyser.

| Front-end Pico 2 W (pin) | Signal | Analyser Pico 2 (pin) |
|---|---|---|
| GND (pin 38) | ground | GND (pin 38) |
| GP2 (pin 4) | microphone clock | GP0 (pin 1), channel 0 |
| GP3 (pin 5) | microphone data | GP1 (pin 2), channel 1 |
| GP10 to GP15 (pins 14 to 20) | the six outputs, when checking them | GP2 to GP7 (pins 4, 5, 6, 7, 9, 10), channels 2 to 7 |

The microphone stays connected to the front-end Pico as it is. The analyser's inputs only listen; they draw almost no current. **[NOT VERIFIED]** that an extra 10 to 20 cm of jumper wire on the clock line leaves the microphone working. If the microphone test fails with the analyser attached and passes without it, that is the cause; shorten the wires.

For the stand-in tach (bench item 30), the generator pin (default GP15, pin 20) drives at 3.3 V. The real tach input expects 5 to 24 V through its optocoupler (review 7.4), so it needs the interface board.

## Build and flash

Every push builds it in CI: download the `eol_la-uf2` artifact. By hand, as for the main firmware:

```
cmake -S firmware_la -B build/firmware_la -DPICO_BOARD=pico2 && cmake --build build/firmware_la
```

Flash the **second** Pico (hold BOOTSEL, plug in, copy `eol_la.uf2`). It then appears on the CM5 as `/dev/serial/by-id/usb-Zynith_EOL_logic_analyser_<serial>-if00`, separate from the front-end Pico.

## First run on a real Pico 2 (2026-11-23)

Flashed from the CM5 build (`eol_la.uf2`, commit 335b3ca), plugged into the development Mac, one jumper from GP15 (the generator) to GP0 (channel 0). The analyser measured its own generator:

| Generator | Measured | Duty |
|---|---|---|
| 100 kHz, 30 % (high 450 cycles) | 100 000.00 Hz, every one of 50 000 periods; high 449 cycles | 29.933 % |
| 100 kHz, 50 % (750) | high 749, low 751 | 49.933 % |
| 500 kHz, 10 % (30) | high 29 | 9.667 % |
| 1 MHz, 30 % (45) | 999 999.97 Hz; high 45, low 105 | 30.000 % |
| 1 MHz, 50 % (75) | high 75, low 75 | 50.000 % |

Burst at 150 MS/s of 1 MHz at 25 %: phases of 37 and 113 samples (37.5 and 112.5 expected).

Periods and frequencies are exact. Where the true phase is an even number of cycles, the reading is one cycle short. That is expected here and not a bias of the method: a phase is always read as `2n + 5`, an odd number, and this generator runs from the analyser's own clock, so the edges never move against it and the reading cannot average out. A signal from another crystal (the front-end Pico's microphone clock) does move against it and averages out. **[NOT VERIFIED]** until the microphone clock is measured.

The firmware caps the generator at 1 MHz (`la_core.c`), so 3.2 MHz could not be self-tested; it will be measured on the real microphone clock.

## Tests

```
cc -std=c11 -Wall -Wextra -Werror -Ifirmware_la/src firmware_la/host/la_core_test.c firmware_la/src/la_core.c -lm && ./a.out
```

Expected: `all la_core tests passed`. They check command parsing, the wake-up measurement (a 100 ms change from 2 to 3.2 MHz found at exactly the right period and cycle count), a glitch that keeps the period (caught as the shortest phase) and one that changes it (caught as the change), base64, and the generator's settings. `scripts/selftest.sh` runs them.
