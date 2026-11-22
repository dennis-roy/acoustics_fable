# Bench bring-up: CM5, Pico 2 W and the microphone

**State: written 2026-11-22. Nothing in this guide has been run on hardware yet.** Commands for the CM5 were written on a Mac and are untested on a Raspberry Pi. Reasons and datasheet citations are in `../phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md` (sections 3.4, 6.1, 6.5, 7.4, 10).

## What connects to what

```
 mic flex --> Sunrom breakout --5 jumper wires--> Pico 2 W --USB cable--> CM5 IO Board
```

- The microphone connects to the **Pico**, not to the CM5.
- The Pico connects to the CM5 with **one USB cable** and nothing else.
- Start, tach and the six PLC outputs are not part of this bench. They need the isolated interface board (review section 7).
- The second board, the Pico 2, is kept for later as the bench logic analyser (review 10.1). Leave it unplugged.

## Order of work

| Part | What | Needs |
|---|---|---|
| 1 | Set up the CM5 | CM5 with Raspberry Pi OS and network |
| 2 | Flash the Pico 2 W | a computer and a micro-USB data cable |
| 3 | Link test: Pico on the CM5, no microphone | parts 1 and 2 |
| 4 | Wire the microphone | part 3 passed; five jumper wires |
| 5 | Microphone test | part 4 |

Do them in this order. Part 3 proves the USB link before the microphone adds anything that can go wrong.

## Part 1: set up the CM5

On the CM5, in a terminal:

```
python3 --version
sudo apt update
sudo apt install -y git python3-numpy python3-scipy
git clone https://github.com/dennis-roy/acoustics_fable.git
cd acoustics_fable
git checkout rev-2
groups
scripts/selftest.sh
```

- `python3 --version` must say 3.11 or newer.
- `groups` must include `dialout`. That group may open serial ports. If it is missing: `sudo usermod -aG dialout $USER`, then log out and in again. **[NOT VERIFIED]** whether the Raspberry Pi OS default user is in it.
- `scripts/selftest.sh` needs no Pico and takes about 2 minutes. Expected last line: `PASS 15   FAIL 0   NOT RUN 2   (NOT RUN is not a pass)`.

numpy is for the band levels in the microphone test; scipy only for the filter timing in part 3. The link itself needs nothing beyond Python.

## Part 2: flash the Pico 2 W

The firmware file is `eol_pico.uf2`, built by CI. Get it from the `eol_pico-uf2` artifact of the latest green `ci` run on GitHub for branch `rev-2`. It must be firmware 0.3.0 or later for the microphone.

1. Unplug the Pico 2 W.
2. Hold its BOOTSEL button, plug its USB cable into a computer, then let go. A new USB drive appears.
3. Copy `eol_pico.uf2` onto that drive. The drive disappears and the Pico restarts.

Nothing visible happens after that: the on-board LED is not used (it hangs off the radio chip, which stays off).

After this first time the button is not needed: `PYTHONPATH=link python3 -m eol_link bootloader` on the CM5 puts the Pico back into this mode. **[NOT VERIFIED]** on hardware.

## Part 3: link test (bench items 16 to 19)

1. Plug the Pico 2 W's USB cable into a USB port of the CM5 IO Board. Nothing else is connected to the Pico.
2. On the CM5, in the `acoustics_fable` folder:

```
ls /dev/serial/by-id/
scripts/selftest.sh --hardware
```

- `ls` must show two names starting `usb-Zynith_EOL_Pico_front_end_`, one ending `-if00` and one ending `-if02`. If they are missing, stop and paste the output of `dmesg | tail -30`.
- The self-test takes a few minutes. Paste all of its output back, whatever it says.

If that passes, run the long version and the filter timing:

```
scripts/selftest.sh --hardware --long
python3 docs/phase0/analysis/filter_timing.py
```

The long run takes about 25 minutes.

## Part 4: wire the microphone

### Before any wire goes on

1. **Unplug the Pico's USB cable.** Wire with the power off. Plug it in again only when every wire has been checked twice.
2. **Check which way up the flex goes.** The flex has gold fingers on one side only, the side with the metal can. Look into the breakout's connector: its row of tiny contacts is on the floor or on the roof of the slot. The gold side must face those contacts. The breakout's product page does not say which it is [SUN], so it has to be looked at.
3. **Check the two end pads.** With the flex pushed home, the fingers must sit on pads 2 to 7, leaving pads 1 and 8 with no finger (review 6.5). One position off puts power on the wrong pin.

Hold the flex by its edges. The sound inlet is the small hole on the back, the side without the metal can. Keep it uncovered and clean.

### Five wires

Pad numbers are the ones printed on the breakout, as you read them on 2026-11-22. Pico pin numbers are the physical pins, counted as in its pinout drawing [P2W Figure 2].

| Breakout pad | Flex letter | Signal | Wire to |
|---|---|---|---|
| 1 | | not used | nothing |
| 2 | G | ground | Pico pin 3 (GND) |
| 3 | P | power | Pico pin 36 (3V3 OUT) |
| 4 | D | data | Pico pin 5 (GP3) |
| 5 | K | clock | Pico pin 4 (GP2) |
| 6 | S | select | nothing for now |
| 7 | G | ground | Pico pin 8 (GND) |
| 8 | | not used | nothing |

```
 Pico pin 36 (3V3 OUT) ------------------ pad 3 (power)
 Pico pin 3  (GND) ---------------------- pad 2 (ground)
 Pico pin 8  (GND) ---------------------- pad 7 (ground)
 Pico pin 4  (GP2) ---------------------- pad 5 (clock)
 Pico pin 5  (GP3) ---------------------- pad 4 (data)
                                          pad 6 (select): not connected
```

Use the shortest jumper wires you have. The clock is 3.2 MHz with fast edges, and long loose wires distort it.

### What this bench wiring leaves out

The design in review 7.4 has four small parts that are not on the bench: a 100 Ω resistor and two capacitors that clean the microphone's supply, and an 8.2 kΩ resistor on the data line. With only jumper wires:

- The microphone is fed straight from the Pico's 3.3 V pin. That is inside its 1.62 to 3.6 V supply range [MIC s2], so it is allowed. The supply will be noisier, so **noise-floor figures from this bench do not count**.
- The data line has no pull-down. The firmware's SELECT probe was changed so it does not need one (review 3.4).
- SELECT is left open for the first power-up. The datasheet says SELECT "should not be left floating" [MIC s9], and also that the pin is pulled low inside the microphone. Whether the flex ties it is not known (review 6.5). The probe result will show how it sits; then it is wired to that same side (pad 6 to GND or to 3.3 V) and never to the opposite one.

## Part 5: microphone test (bench items 20, 37 and 46c)

With the microphone wired and the Pico plugged into the CM5:

```
cd acoustics_fable
export PYTHONPATH=$PWD/link
python3 -m eol_link info
python3 -m eol_link mictest --seconds 5
```

`info` must say `microphone       capture built in`. If it says `not in this firmware`, the Pico still has the old firmware: do part 2 again.

What `mictest` prints, and what to look for. Paste all of it back.

| Line | A working microphone | A problem |
|---|---|---|
| `probe` | `SELECT = GND` or `SELECT = VDD` | `line stuck`: no power, a wire on the wrong pad, the flex in upside down, or no clock. `ambiguous`: a loose wire or the clock too distorted |
| `missing frames`, `CRC errors` | 0 | anything else is a link problem, not a microphone problem |
| `bits that are 1` | 45 to 55 % in a quiet room | far off: a fault |
| `band` lines | change when you make a noise | never change: the data is not sound |

Then three listening checks. Each is a comparison between two runs, since there is no reference instrument:

1. **Quiet, twice.** Two runs in a quiet room give band levels within about 1 dB of each other.
2. **Clap or talk during a run.** The `0.1 to 20 kHz` band rises.
3. **Shake a bunch of keys near the inlet during a run.** The `20 to 40 kHz` and `40 to 80 kHz` bands rise. Keys are a handy broadband ultrasonic source; that is general knowledge, not a calibrated level.

To keep the raw recording of a run, add `--save captures`. It writes the 1-bit stream and a small description with its SHA-256.

If the probe says `line stuck`, unplug the USB cable before touching any wire.
