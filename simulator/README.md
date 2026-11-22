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

## The simulated microphone

`eol_link mictest` works against the simulator. The "microphone" sends square waves at 1 kHz and 32 kHz, made by the rule in `../protocol/README.md` ("Simulated microphone signal"), so the expected band levels are known: about -9 dBFS below 20 kHz and -21 dBFS between 20 and 40 kHz.

| Option | Meaning |
|---|---|
| `--mic gnd` (default), `--mic vdd` | how the microphone's SELECT pin is tied |
| `--mic stuck` | a dead data line: the probe must report it and nothing is streamed |
| `--mic none` | firmware built without microphone capture |
| `--mic-release low` (default), `hold`, `high` | what the data line does while the microphone is not driving it |

## Deliberate faults

In tests, `PicoSim(faults=Faults(...))` makes it misbehave so the receiver can be shown to notice:

| Fault | What the simulator does | What the receiver must report |
|---|---|---|
| `drop_pdm={20}` | never sends PDM frame 20 | one missing frame |
| `corrupt_pdm={20}` | flips a bit in frame 20 after its CRC was computed | one CRC error, one missing frame |
| `wrong_data_pdm={20}` | sends wrong contents with a correct CRC | one frame with wrong data |

## What it does not simulate

USB itself, the clocks, the pins, timing to better than about a millisecond, the tach and Start. The synthetic stream is a test pattern, not sound, and the simulated microphone is two square waves, not a real acoustic signal.
