"""Sound level per frequency band, straight from the 1-bit microphone stream.

Needs numpy (on the CM5: sudo apt install python3-numpy). Everything else in eol_link
works without it.

The 1-bit stream is treated as a signal of +1 and -1 and its spectrum is averaged over
many short pieces. No decimation filter is involved, so this does not depend on the
filter version. Levels are in dBFS: 0 dBFS is a sine wave that uses the microphone's
whole digital range. They are relative figures. Without a calibrated sound source they
say nothing about sound pressure.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_BANDS = ((100.0, 20_000.0), (20_000.0, 40_000.0), (40_000.0, 80_000.0))
NFFT = 1 << 16


@dataclass(frozen=True)
class BandLevel:
    low_hz: float
    high_hz: float
    dbfs: float
    peak_hz: float  # strongest single frequency inside the band


def have_numpy() -> bool:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def band_levels(data: bytes, clock_hz: float, bands=DEFAULT_BANDS, max_pieces: int = 64) -> list[BandLevel]:
    """Level in each band. `data` is PDM payload bytes, bit 7 of byte 0 first."""
    import numpy as np

    bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8))
    pieces = len(bits) // NFFT
    if pieces == 0:
        raise ValueError(f"need at least {NFFT // 8} bytes")
    step = max(1, pieces // max_pieces)
    window = np.hanning(NFFT).astype(np.float32)
    power = np.zeros(NFFT // 2 + 1)
    used = 0
    for i in range(0, pieces, step):
        piece = bits[i * NFFT : (i + 1) * NFFT].astype(np.float32) * 2.0 - 1.0
        power += np.abs(np.fft.rfft(piece * window)) ** 2
        used += 1
    # Scale so that a sine of amplitude A adds up to A*A/2 across the bins it spreads over.
    power *= 2.0 / (used * window.sum() ** 2)
    noise_bandwidth_bins = NFFT * float((window**2).sum()) / float(window.sum()) ** 2

    bin_hz = clock_hz / NFFT
    levels = []
    for low, high in bands:
        a, b = int(round(low / bin_hz)), int(round(high / bin_hz))
        in_band = power[a:b]
        band_power = float(in_band.sum()) / noise_bandwidth_bins
        dbfs = 10.0 * np.log10(max(band_power, 1e-20) / 0.5)
        levels.append(BandLevel(low, high, float(dbfs), float((a + int(in_band.argmax())) * bin_hz)))
    return levels
