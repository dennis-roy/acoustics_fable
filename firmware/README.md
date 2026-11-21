# firmware: Pico 2 W front end

**State: build-plan step 2 (skeleton). It compiles for the Pico 2 W in CI. It has never run on a Pico.** The logic is tested on a PC. The hardware layer (`main.c`, `usb_descriptors.c`) has only ever been compiled, so expect to fix things at first power-up.

## What it does in this step

- Shows up on USB as one device with two serial ports: DATA and HB (heartbeat).
- Answers the version handshake and refuses to stream or switch outputs for a host with a different protocol major version.
- Sends a synthetic test stream at the real rate (430.6 kB/s) or as fast as the host takes it, so the link can be proven before any sensor is involved.
- Drives the six output pins, and opens all of them if the heartbeat stops for 500 ms, if the heartbeat says the application is not alive, or if the firmware itself hangs (hardware watchdog, 1 s).
- Keeps the radio off: GP23 is held low and no radio code is linked.
- Runs the system clock at 128 MHz and reports it, both as configured and as counted by the chip's frequency counter.

Not in this step: microphone clock and capture, tach, Start. Those are step 3 and step 7. Their pins are reserved in `src/board.h`.

The wire format is `../protocol/README.md`. The reasons behind the design are in `../docs/phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md`.

## Files

| File | What it is | Tested how |
|---|---|---|
| `src/core.c`, `core.h` | All behaviour: commands, stream, watchdog, outputs. No hardware calls | compiled for a PC and run through the system tests |
| `src/proto.c`, `proto.h` | Frame format and CRC-32 | same |
| `src/hal.h` | The five things the core needs from a board | |
| `src/main.c` | Pico 2 W layer: clock, pins, USB pump, hardware watchdog | compiles in CI; **never run** |
| `src/usb_descriptors.c`, `tusb_config.h` | Two-serial-port USB device | compiles in CI; **never run** |
| `src/board.h` | Pin map | |
| `host/core_host.c` | Runs `core.c` on a PC with two pseudo-terminals in place of USB | used by `scripts/selftest.sh` |
| `CMakeLists.txt` | Pico build | CI runs it at every push |

`core.c` is kept free of hardware so that the part with the logic in it can be tested without a Pico. `main.c` is deliberately thin.

## Existing firmware files changed

None. This repository had no firmware before this step: its only file was a one-line `README.md`. Every file above is new, so there was nothing to list for per-change approval. From step 3 on, any change to these files will be listed with its reason before it is made.

## Build

Easiest: every push builds it in CI (`.github/workflows/ci.yml`). Download the `eol_pico-uf2` artifact from the run and unzip it. First build: 2026-11-21, Pico SDK 2.1.1, arm-none-eabi-gcc 13.2.1, 67,584 bytes.

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

- One compiler warning: `main.c` line 16 defines `SYS_CLK_HZ`, a name the Pico SDK already defines. Our code uses our value (128 MHz) as intended. Fix proposed: rename ours to `EOL_SYS_CLK_HZ`. Not done yet, because changes to existing firmware files need the owner's approval first.
- USB identity is for development only: Raspberry Pi's vendor ID with the SDK's generic serial product ID. A shipped product needs its own.
- The on-board LED is not used. On a Pico 2 W it hangs off the radio chip, which stays off.
- There is no command yet to restart into BOOTSEL mode, so updating needs the button. Planned with the install scripts (step 11).
- If ModemManager is installed on the CM5 it may probe the two ports when they appear. The install script (step 11) will add a rule to stop that. **[NOT VERIFIED]** whether the Raspberry Pi OS image ships it.
