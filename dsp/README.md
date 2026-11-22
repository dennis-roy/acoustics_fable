# dsp: the decimation filter

Python package `eol_dsp`. It turns the microphone's 1-bit stream into ordinary samples.

**State: build-plan step 3. Tested on a macOS laptop and on the CM5, and run on real microphone captures from the bench (2026-11-22).** Needs numpy and scipy (on a Raspberry Pi: `sudo apt install python3-numpy python3-scipy`; both were already on the bench CM5).

## What it does

The microphone sends 3.2 million bits a second. A 1 means "up" and a 0 means "down"; the sound is in how dense the ones are. This package low-pass filters that stream and keeps every 16th result (200 000 samples a second, the default) or every 32nd (100 000).

```
 1-bit stream, 3.2 MHz  -->  low-pass filter  -->  keep every 16th  -->  samples, 200 kHz
 (from the Pico)            (289 taps, 0-40 kHz        (or 32nd: 100 kHz)   1.0 = full scale
                             flat, -100 dB from
                             120 kHz up)
```

- **Units:** 1.0 is the full scale of the 1-bit stream. Peaks can go a little past 1.0 just before the microphone overloads. Nothing is clipped.
- **Timing:** sample `j` is the moment of input bit `j × 16`. The filter's own delay is removed, so the samples line up with the capture's time stamps.
- **Edges:** the first and last 45 µs (divide by 16) or 170 µs (divide by 32) are affected by the start and end of the data. `Samples.inner()` leaves them out.

## The two filters

| Name | Ratio | Out | Taps | Passband | Stopband | Free of aliases up to |
|---|---|---|---|---|---|---|
| `eol-pdm3200k-d16-p40k-s120k-a100-r1` | 16 (default) | 200 kHz | 289 | 0 to 40 kHz, flat within 0.001 dB | from 120 kHz, below -100 dB | 80 kHz |
| `eol-pdm3200k-d32-p40k-s60k-a100-r1` | 32 | 100 kHz | 1089 | same | from 60 kHz, below -100 dB | 40 kHz |

The design is section 4.4 of `../docs/phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md`, with one change: the length is rounded up to `2 × K × ratio + 1` taps (289 instead of 259, 1089 instead of 1027), so the delay is a whole number of output samples and can be removed exactly.

**Filter version (decision D2).** The coefficients are stored in `eol_dsp/filters.json`, each with a name that states clock, ratio and band edges, and a SHA-256 of the coefficients. The program checks the SHA-256 every time it loads them and refuses a changed file. Anything made from these samples must record the name and the SHA-256: `python3 -m eol_dsp decimate --wav` writes them into the `.json` beside the WAV file.

The application never designs a filter. `design_filters.py` does, with scipy, and only when a filter is to change. Stored coefficients mean a filter's identity cannot drift with the scipy version. If a filter ever changes, its name changes too (the `r1` at the end).

There is no filter for the 4.0 MHz bench clock. Captures at 4.0 MHz can still be compared with the band levels of `eol_link mictest`, which work on the raw bits.

## Use

From the repository root:

```
export PYTHONPATH=$PWD/dsp
python3 -m eol_dsp filters                         # the two filters, names and SHA-256
python3 -m eol_dsp decimate CAPTURE.pdm            # levels of a capture saved by 'eol_link mictest --save'
python3 -m eol_dsp decimate CAPTURE.pdm --wav out.wav    # also write the samples as a WAV file
python3 -m eol_dsp decimate CAPTURE.pdm --ratio 32       # 100 kHz instead of 200 kHz
python3 -m eol_dsp timing                          # how long the filters take on this machine
```

The WAV file is 32-bit floating point, mono, at 200 kHz, so nothing is rounded or clipped. Audacity opens it; its spectrogram view shows the ultrasound. Most sound cards cannot play 200 kHz, and nobody can hear above 20 kHz.

In Python:

```python
from eol_dsp import decimate_bits, get_filter
samples = decimate_bits(pdm_bytes, get_filter())     # divide by 16
samples.values, samples.rate_hz, samples.filter.name, samples.filter.sha256
```

## Speed

Time to filter 10 s of capture, one core:

| Machine | Divide by 16 | Divide by 32 | Measured |
|---|---|---|---|
| CM5 (Rev 1.0, Raspberry Pi OS trixie, Python 3.13.5, numpy 2.2.4, scipy 1.15.3) | 0.73 s | 1.27 s | 2026-11-22 |
| CM4 | not measured | not measured | **[NOT VERIFIED]**: run `python3 -m eol_dsp timing` on one |
| Development laptop (Apple arm64) | 0.34 s worst 0.38 s | 0.92 s | 2026-11-22. Not a target figure |

Target: under 1 s for the default. The CM5 meets it; divide by 32 does not, which is acceptable because it is not the default.

The whole capture is held in memory: about 17 MB of RAM per second of capture. A 10 s capture needs about 170 MB.

## Tests

```
python3 -m unittest discover -s dsp/tests
```

Expected: 17 tests, `OK`. They check that the stored coefficients are intact and match a fresh design; the passband is flat within 0.001 dB and the stopband below -100 dB; the output equals a slow, obvious textbook calculation; sample `j` lines up with input bit `j × ratio`; tones that would fold back are removed; a 1-bit stream gives its tones back within 1 %; and the command line writes a WAV file with the filter's name and SHA-256 beside it. I broke the code six ways on purpose (delay left in, delay off by one, bits in the wrong order, SHA-256 not checked, ones and zeros swapped, one sample too few); the tests caught all six.

## Not verified

- The test signals come from a textbook 1-bit modulator, not the microphone's own, which the datasheet does not describe. On real captures the output agrees with the band levels of `mictest` second by second, but that is a consistency check, not a proof against a reference.
- Absolute sound pressure: not measured anywhere in this project (no calibrator).
- Speed on a CM4.
