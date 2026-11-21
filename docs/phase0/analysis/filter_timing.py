#!/usr/bin/env python3
"""How long the decimation filter takes on THIS machine.

Run it once on the CM5 and once on the CM4 and paste both outputs back. The review
keeps the two results in separate rows (section 4.7); a laptop figure is not a CM5 or
CM4 figure.

    python3 docs/phase0/analysis/filter_timing.py

Needs numpy and scipy. It filters 10 s of random 1-bit data with both approved
settings (divide by 16 to 200 kHz, divide by 32 to 100 kHz), five times each.
"""
from __future__ import annotations

import os
import platform
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import scipy

from phase0_analysis import FS_PDM, OPTIONS, decimate, design

SECONDS = 10
REPEATS = 5


def machine() -> str:
    model = Path("/proc/device-tree/model")
    if model.exists():
        return model.read_text().rstrip("\0\n")
    return f"{platform.system()} {platform.machine()} (not a Raspberry Pi)"


def main() -> None:
    print(f"machine   {machine()}")
    print(f"cores     {os.cpu_count()}")
    print(f"software  Python {sys.version.split()[0]}, numpy {np.__version__}, scipy {scipy.__version__}")
    print(f"input     {SECONDS} s of 1-bit data at {FS_PDM / 1e6:.1f} MHz, {REPEATS} runs each, one core\n")

    packed = np.random.default_rng(1).integers(0, 256, size=int(FS_PDM * SECONDS / 8), dtype=np.uint8)
    for name, (dec, f_pass, f_stop, atten) in OPTIONS.items():
        h = design(FS_PDM, f_pass, f_stop, atten)
        times = []
        for _ in range(REPEATS):
            t0 = time.perf_counter()
            decimate(np.unpackbits(packed), h, dec)
            times.append(time.perf_counter() - t0)
        worst, median = max(times), statistics.median(times)
        print(f"divide by {dec:<2} ({len(h)} taps)   median {median:.2f} s   worst {worst:.2f} s"
              f"   = {SECONDS / worst:.0f}x faster than real time at worst")


if __name__ == "__main__":
    main()
