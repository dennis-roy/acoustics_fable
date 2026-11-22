#!/usr/bin/env python3
"""Designs the decimation filters and writes them to eol_dsp/filters.json.

Run only to change a filter. The application never designs filters: it loads the
committed coefficients, so a filter's identity (name and SHA-256) cannot drift with
the scipy version. Needs numpy and scipy.

    python3 dsp/design_filters.py            # rewrite eol_dsp/filters.json
    python3 dsp/design_filters.py --check    # exit 1 if the file differs from a fresh design

The design rule is the one in section 4.4 of docs/phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md:
a linear-phase Kaiser-window low-pass, 100 dB stopband, passband 0 to 40 kHz, stopband
starting where the first alias band would reach the protected range. One change: the
length is rounded UP to 2*K*D + 1 taps (D = decimation), so the filter's delay is exactly
K output samples and can be removed without interpolation.

If a filter's coefficients change for any reason, its name must change too: raise the
"r" number in REVISION.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from scipy import signal

OUT = Path(__file__).resolve().parent / "eol_dsp" / "filters.json"
REVISION = 1

CLOCK_HZ = 3_200_000
PASS_HZ = 40_000
ATTEN_DB = 100.0
# decimation: highest frequency that must stay free of aliases
SETTINGS = {16: 80_000, 32: 40_000}


def design(clock_hz: int, decimation: int, alias_free_hz: int) -> dict:
    out_rate = clock_hz // decimation
    stop_hz = out_rate - alias_free_hz
    width = (stop_hz - PASS_HZ) / (clock_hz / 2)
    numtaps, beta = signal.kaiserord(ATTEN_DB, width)
    k = -(-(numtaps - 1) // (2 * decimation))  # ceiling
    numtaps = 2 * k * decimation + 1
    h = signal.firwin(numtaps, (PASS_HZ + stop_hz) / 2, window=("kaiser", beta), fs=clock_hz)
    h = h / h.sum()
    name = (f"eol-pdm{clock_hz // 1000}k-d{decimation}-p{PASS_HZ // 1000}k-s{stop_hz // 1000}k"
            f"-a{ATTEN_DB:.0f}-r{REVISION}")
    return {
        "name": name,
        "clock_hz": clock_hz,
        "decimation": decimation,
        "pass_hz": PASS_HZ,
        "stop_hz": stop_hz,
        "atten_db": ATTEN_DB,
        "window": f"kaiser, beta {beta!r}",
        "sha256": hashlib.sha256(np.asarray(h, "<f8").tobytes()).hexdigest(),
        "taps": [float(t) for t in h],
    }


def build() -> dict:
    return {
        "about": "Decimation filters for the 1-bit microphone stream. Written by dsp/design_filters.py; "
                 "do not edit by hand. sha256 is over the taps as little-endian float64.",
        "filters": [design(CLOCK_HZ, d, a) for d, a in SETTINGS.items()],
    }


def main() -> int:
    fresh = build()
    if "--check" in sys.argv:
        old = json.loads(OUT.read_text())
        same = [f["name"] for f in old["filters"]] == [f["name"] for f in fresh["filters"]] and all(
            np.allclose(a["taps"], b["taps"], rtol=0, atol=1e-12) for a, b in zip(old["filters"], fresh["filters"]))
        print("filters.json matches a fresh design" if same else "filters.json DIFFERS from a fresh design")
        return 0 if same else 1
    OUT.write_text(json.dumps(fresh, indent=1) + "\n")
    for f in fresh["filters"]:
        print(f"{f['name']}: {len(f['taps'])} taps, sha256 {f['sha256'][:16]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
