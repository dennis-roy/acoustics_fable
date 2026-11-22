"""The decimation filter: 1-bit microphone stream in, ordinary samples out.

The microphone sends one bit per clock period, 3.2 million a second. A 1 means "up",
a 0 means "down"; the sound is in the density of ones. This module turns that into
numbers at 200 000 (divide by 16, the default) or 100 000 (divide by 32) samples a
second, by low-pass filtering and keeping every 16th or 32nd result.

Units: 1.0 is the full scale of the 1-bit stream (a stream of all ones). Sound peaks can
go somewhat past 1.0 just before the microphone overloads; nothing is clipped here.

Timing: sample j stands for the moment of input bit j * decimation. The filter's own
delay has been removed, so a click at bit n shows at sample n / decimation.

The filters are fixed coefficient sets in filters.json, made by ../design_filters.py.
Each has a name that states clock, ratio and band edges, and a SHA-256 of its
coefficients. Record both with any result made from these samples (decision D2).

Needs numpy and scipy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy.signal import upfirdn

DEFAULT_DECIMATION = 16
_FILTERS_FILE = Path(__file__).with_name("filters.json")


@dataclass(frozen=True)
class FilterSpec:
    name: str
    sha256: str
    clock_hz: int
    decimation: int
    pass_hz: int
    stop_hz: int
    atten_db: float
    taps: np.ndarray  # float64, symmetric, sums to 1

    @property
    def rate_hz(self) -> float:
        return self.clock_hz / self.decimation

    @property
    def delay_samples(self) -> int:
        """The filter's delay, in output samples. Removed from the output."""
        return (len(self.taps) - 1) // (2 * self.decimation)

    @property
    def edge_s(self) -> float:
        """Length at each end of a capture where the filter ran past the data."""
        return (len(self.taps) - 1) / 2 / self.clock_hz


@dataclass(frozen=True)
class Samples:
    values: np.ndarray  # float32
    rate_hz: float
    filter: FilterSpec

    @property
    def seconds(self) -> float:
        return len(self.values) / self.rate_hz

    def inner(self) -> np.ndarray:
        """The samples without the edges, where the filter ran past the start or end."""
        n = self.filter.delay_samples
        return self.values[n : len(self.values) - n]


@lru_cache(maxsize=1)
def filters() -> tuple[FilterSpec, ...]:
    """Every filter in filters.json. Refuses a file whose coefficients do not match their SHA-256."""
    return load_filters(_FILTERS_FILE)


def load_filters(path: Path) -> tuple[FilterSpec, ...]:
    data = json.loads(Path(path).read_text())
    out = []
    for f in data["filters"]:
        taps = np.asarray(f["taps"], dtype="<f8")
        digest = hashlib.sha256(taps.tobytes()).hexdigest()
        if digest != f["sha256"]:
            raise ValueError(f"filters.json: coefficients of {f['name']} do not match their SHA-256")
        if len(taps) % (2 * f["decimation"]) != 1:
            raise ValueError(f"filters.json: {f['name']} has a length its delay cannot be removed from")
        out.append(FilterSpec(f["name"], digest, f["clock_hz"], f["decimation"], f["pass_hz"], f["stop_hz"],
                              f["atten_db"], taps))
    return tuple(out)


def get_filter(clock_hz: float = 3_200_000, decimation: int = DEFAULT_DECIMATION) -> FilterSpec:
    for f in filters():
        if f.clock_hz == round(clock_hz) and f.decimation == decimation:
            return f
    have = ", ".join(f"{f.clock_hz / 1e6:g} MHz / {f.decimation}" for f in filters())
    raise ValueError(f"no decimation filter for a {clock_hz / 1e6:g} MHz clock divided by {decimation} "
                     f"(available: {have})")


def decimate_signal(x: np.ndarray, spec: FilterSpec) -> Samples:
    """Filters and decimates a signal already at the clock rate (float values)."""
    x = np.asarray(x, dtype=np.float32)
    d, k = spec.decimation, spec.delay_samples
    n_out = len(x) // d
    if n_out == 0:
        return Samples(np.zeros(0, dtype=np.float32), spec.rate_hz, spec)
    # Full convolution, every d-th output; output m stands for input (m - k) * d.
    y = upfirdn(spec.taps.astype(np.float32), x, up=1, down=d)
    return Samples(np.ascontiguousarray(y[k : k + n_out]), spec.rate_hz, spec)


def decimate_bits(data: bytes, spec: FilterSpec) -> Samples:
    """Filters and decimates PDM payload bytes (bit 7 of byte 0 is the earliest sample)."""
    bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8))
    x = bits.astype(np.float32)
    x *= 2.0
    x -= 1.0
    return decimate_signal(x, spec)


def level_dbfs(values: np.ndarray) -> tuple[float, float]:
    """(peak, rms) in dBFS. Peak: 0 dBFS = full scale. RMS: 0 dBFS = a full-scale sine."""
    v = np.asarray(values, dtype=np.float64)
    if len(v) == 0:
        return float("-inf"), float("-inf")
    peak = float(np.max(np.abs(v)))
    rms = float(np.sqrt(np.mean(v * v)))
    floor = 1e-12
    return 20 * np.log10(max(peak, floor)), 20 * np.log10(max(rms * np.sqrt(2), floor))
