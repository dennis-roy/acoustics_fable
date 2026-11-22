"""Synthetic parts for the tests (spec: synthetic data only for software tests)."""

import numpy as np

FS = 200_000.0


def part(kind: str, seed: int, seconds: float = 1.0) -> np.ndarray:
    """A 'recording' at 200 kHz. kind: golden, squeal, rattle, novel."""
    rng = np.random.default_rng(seed)
    n = int(FS * seconds)
    t = np.arange(n) / FS
    level = 10 ** (rng.normal(0, 0.5) / 20)  # ±0.5 dB from part to part
    noise = rng.normal(0, 0.01, n)
    noise = np.convolve(noise, np.ones(4) / 4, mode="same")  # a little darker than white
    x = level * (noise + 0.004 * np.sin(2 * np.pi * 5_000 * t + rng.uniform(0, 6.3)))
    if kind == "squeal":
        x += 0.02 * level * 10 ** (rng.normal(0, 1) / 20) * np.sin(2 * np.pi * rng.normal(25_000, 100) * t)
    elif kind == "rattle":
        rate = rng.normal(15, 0.5)
        for t0 in np.arange(rng.uniform(0, 1 / rate), seconds, 1 / rate):
            i = int(t0 * FS)
            m = min(n - i, int(0.004 * FS))
            tt = np.arange(m) / FS
            x[i:i + m] += 0.08 * level * np.exp(-tt / 0.0008) * np.sin(2 * np.pi * 8_000 * tt)
    elif kind == "novel":
        x += 0.02 * level * (1 + np.sin(2 * np.pi * 120 * t)) * np.sin(2 * np.pi * 11_000 * t)
    return x


def tach(rpm: float, seconds: float, ppr: int = 1) -> np.ndarray:
    return np.arange(0, seconds, 60 / rpm / ppr)
