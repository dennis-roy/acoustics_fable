# simulator: a Pico front end in software

Python package `pico_sim`. It offers the same two serial ports as the real firmware, as pseudo-terminals, and behaves as `../protocol/README.md` describes. Use it to develop and test the CM5 software with no hardware.

It was written from the protocol document, not translated from the firmware's C code. The test suite runs the same checks against this simulator and against the firmware's own logic compiled for a PC (`../firmware/host/core_host.c`), so each catches mistakes in the other.

## Run it

```
export PYTHONPATH=$PWD/link:$PWD/simulator
python3 -m pico_sim
```

Expected output, one line, then it keeps running:

```
PORTS /dev/pts/3 /dev/pts/4
```

The first path is the DATA port, the second the HB port (on macOS they look like `/dev/ttys012`). In another terminal:

```
export PYTHONPATH=$PWD/link:$PWD/simulator
python3 -m eol_link --port /dev/pts/3 --hb-port /dev/pts/4 info
python3 -m eol_link --port /dev/pts/3 --hb-port /dev/pts/4 linktest --seconds 10
```

`python3 -m pico_sim --proto-major 2` pretends to be firmware with another protocol version. `eol_link info` must then print `REFUSED`.

## Deliberate faults

In tests, `PicoSim(faults=Faults(...))` makes it misbehave so the receiver can be shown to notice:

| Fault | What the simulator does | What the receiver must report |
|---|---|---|
| `drop_pdm={20}` | never sends PDM frame 20 | one missing frame |
| `corrupt_pdm={20}` | flips a bit in frame 20 after its CRC was computed | one CRC error, one missing frame |
| `wrong_data_pdm={20}` | sends wrong contents with a correct CRC | one frame with wrong data |

## What it does not simulate

USB itself, the clocks, the pins, timing to better than about a millisecond, the microphone, the tach and Start. The synthetic stream is a test pattern, not sound. A simulated microphone signal comes with the acquisition work (step 4).
