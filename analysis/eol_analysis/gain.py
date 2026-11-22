"""Golden-relative gain (spec, Testing: "the golden also defines the reference gain").

Method: the level of the golden part in a fixed band (1 to 40 kHz, where the microphone
and the squeal and rattle content are) is measured when the Golden OK samples are taken
in Setup: that is the reference. At every calibration check the golden part is measured
again, and all captures until the next calibration are multiplied by

    gain = reference level / golden level now

So a drop in sensitivity of the microphone chain (ageing, a moved microphone, dust on
the inlet) is cancelled, while real loudness differences between parts stay visible:
every part of the session is scaled by the same number. The gain is recorded with every
calibration and every result. A gain outside ±6 dB (a programming variable) fails the
calibration: something changed too much to be corrected by scaling.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import signal

REF_BAND_HZ = (1_000.0, 40_000.0)


def band_level(x: np.ndarray, fs: float, band: tuple[float, float] = REF_BAND_HZ) -> float:
    """RMS in the band, linear (full scale of the 1-bit stream = 1)."""
    f, p = signal.welch(np.asarray(x, dtype=np.float64), fs=fs, nperseg=min(8192, len(x)))
    m = (f >= band[0]) & (f < band[1])
    return math.sqrt(float(p[m].sum() * (f[1] - f[0])))


def reference_level(golden_levels: list[float]) -> float:
    """The reference: median band level of the Golden OK training samples."""
    if not golden_levels:
        raise ValueError("no Golden OK samples to make a reference from")
    return float(np.median(golden_levels))


def calibration_gain(reference: float, golden_now: float, limit_db: float = 6.0) -> tuple[float, str | None]:
    """(gain, None) or (gain, why it is refused)."""
    if golden_now <= 0:
        return math.nan, "the golden part gave no signal in the reference band"
    gain = reference / golden_now
    g_db = 20 * math.log10(gain)
    if abs(g_db) > limit_db:
        return gain, f"gain {g_db:+.1f} dB is outside ±{limit_db:g} dB: check the microphone and its position"
    return gain, None
