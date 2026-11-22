# firmware: Pico 2 W front end

**State: build-plan step 2 plus microphone capture (step 3, first part). It has never run on a Pico.** The logic is tested on a PC. The hardware layer (`main.c`, `mic.c`, `mic.pio`, `usb_descriptors.c`) has only ever been compiled, so expect to fix things at first power-up.

## What it does

- Shows up on USB as one device with two serial ports: DATA and HB (heartbeat).
- Answers the version handshake and refuses to stream or switch outputs for a host with a different protocol major version.
- Sends a synthetic test stream at the real rate (430.6 kB/s) or as fast as the host takes it, so the link can be proven before any sensor is involved.
- Drives the six output pins, and opens all of them if the heartbeat stops for 500 ms, if the heartbeat says the application is not alive, or if the firmware itself hangs (hardware watchdog, 1 s).
- Keeps the radio off: GP23 is held low and no radio code is linked.
- Runs the system clock at 128 MHz and reports it, both as configured and as counted by the chip's frequency counter.
- Drives the microphone: wakes it at 2.0 MHz, switches to 3.2 or 4.0 MHz, works out which half of the clock carries the data (the SELECT probe), and streams the 1-bit data. Between captures the clock is held low, so the microphone sleeps.
- Restarts into the USB bootloader on command, so new firmware can be loaded without pressing the button.

Not built yet: tach and Start (step 7), and the second-Pico logic analyser. Their pins are reserved in `src/board.h`.

The wire format is `../protocol/README.md`. The reasons behind the design are in `../docs/phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md`.

## Files

| File | What it is | Tested how |
|---|---|---|
| `src/core.c`, `core.h` | All behaviour: commands, streams, the SELECT probe, watchdog, outputs. No hardware calls | compiled for a PC and run through the system tests |
| `src/proto.c`, `proto.h` | Frame format and CRC-32 | same |
| `src/hal.h` | What the core needs from a board: outputs, identity, clock, microphone, restart | |
| `src/main.c` | Pico 2 W layer: clock, pins, USB pump, hardware watchdog | compiles in CI; **never run** |
| `src/mic.pio`, `mic.c`, `mic.h` | Microphone clock and capture: one PIO program, one DMA channel filling a ring | compiles in CI; **never run** |
| `src/usb_descriptors.c`, `tusb_config.h` | Two-serial-port USB device | compiles in CI; **never run** |
| `src/board.h` | Pin map | |
| `host/core_host.c` | Runs `core.c` on a PC with two pseudo-terminals in place of USB, and a simulated microphone | used by `scripts/selftest.sh` |
| `CMakeLists.txt` | Pico build | CI runs it at every push |

`core.c` is kept free of hardware so that the part with the logic in it can be tested without a Pico. `main.c` is deliberately thin.

## Existing firmware files changed

Step 2: none. The repository had no firmware before it.

Step 3 (2026-11-22) changed `CMakeLists.txt`, `src/board.h`, `src/hal.h`, `src/proto.h`, `src/core.h`, `src/core.c`, `src/main.c` and `host/core_host.c`. The reason for each is in section 13.3 of the review. The owner waived the separate approval that day (decision D10); changes are still listed there with their reasons.

## Build

Easiest: every push builds it in CI (`.github/workflows/ci.yml`). Download the `eol_pico-uf2` artifact from the run and unzip it. Built with Pico SDK 2.1.1 and arm-none-eabi-gcc 13.2.1. Firmware 0.3.0, the first with microphone capture, is 75,264 bytes (2026-11-22).

By hand, on Linux or a Raspberry Pi:

```
sudo apt install cmake build-essential gcc-arm-none-eabi libnewlib-arm-none-eabi libstdc++-arm-none-eabi-newlib
git clone --depth 1 --branch 2.1.1 https://github.com/raspberrypi/pico-sdk.git ~/pico-sdk
git -C ~/pico-sdk submodule update --init --depth 1 lib/tinyusb
export PICO_SDK_PATH=~/pico-sdk
cmake -S firmware -B build/firmware
cmake --build build/firmware
```

Expected: `build/firmware/eol_pico.uf2`. The first build also downloads and builds `picotool`, which takes a few minutes.

## Flash

1. Hold the BOOTSEL button on the Pico 2 W and plug in its USB cable. It appears as a USB drive.
2. Copy `eol_pico.uf2` onto that drive. The Pico restarts by itself.

## Load new firmware without the button

Once firmware 0.3.0 or later is on the Pico, from the CM5:

```
PYTHONPATH=link python3 -m eol_link bootloader
```

The Pico restarts as a USB drive. Copy `eol_pico.uf2` onto it as above. **[NOT VERIFIED]** on hardware.

## Check it (bench items 16 to 19)

On the CM5, with the Pico plugged in:

```
ls /dev/serial/by-id/
scripts/selftest.sh --hardware --long
```

Expected from `ls`: two names starting `usb-Zynith_EOL_Pico_front_end_`, one ending `-if00` (DATA) and one ending `-if02` (HB). If they are missing, the USB layer is not working; paste the output of `dmesg | tail -30`.

`selftest.sh` prints PASS, FAIL or NOT RUN per step. Paste its whole output back.

Debug text comes out of the UART on GP0 (pin 1) at 115200 baud, not over USB.

## Known limits

- USB identity is for development only: Raspberry Pi's vendor ID with the SDK's generic serial product ID. A shipped product needs its own.
- The on-board LED is not used. On a Pico 2 W it hangs off the radio chip, which stays off.
- The restart-to-bootloader command can be used by anyone who can open the DATA port. Decide before shipping whether it stays.
- The microphone clock pin floats for a moment between power-up and start-up, until the firmware drives it low.
- The capture ring holds 41 ms. If the main loop stalls for longer than about 30 ms during a capture, data is lost; the loss is counted and reported, never hidden.
- If ModemManager is installed on the CM5 it may probe the two ports when they appear. The install script (step 11) will add a rule to stop that. **[NOT VERIFIED]** whether the Raspberry Pi OS image ships it.
