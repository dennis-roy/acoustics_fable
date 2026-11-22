"""Band levels from a 1-bit stream. Needs numpy; skipped, and reported as skipped, without it."""

import math
import unittest

from eol_link.pdmspec import NFFT, band_levels, have_numpy

CLOCK_HZ = 3_200_000


def one_bit_stream(tones, samples: int) -> bytes:
    """A second-order sigma-delta modulator fed with sine waves: [(hertz, amplitude), ...]."""
    v1 = v2 = 0.0
    out = bytearray(samples // 8)
    byte = 0
    for n in range(samples):
        x = sum(a * math.sin(2 * math.pi * f * n / CLOCK_HZ) for f, a in tones)
        y = 1.0 if v2 >= 0 else -1.0
        v1 += x - y
        v2 += v1 - y
        byte = (byte << 1) | (y > 0)
        if n & 7 == 7:
            out[n >> 3] = byte
            byte = 0
    return bytes(out)


@unittest.skipUnless(have_numpy(), "numpy is not installed")
class BandLevelTests(unittest.TestCase):
    def test_tones_are_reported_at_their_level_and_frequency(self):
        data = one_bit_stream([(10_000, 0.5), (30_000, 0.1)], 4 * NFFT)
        audible, ultrasonic, top = band_levels(data, CLOCK_HZ)
        self.assertAlmostEqual(audible.dbfs, 20 * math.log10(0.5), delta=0.5)  # -6.0 dBFS
        self.assertAlmostEqual(audible.peak_hz, 10_000, delta=50)
        self.assertAlmostEqual(ultrasonic.dbfs, 20 * math.log10(0.1), delta=0.5)  # -20.0 dBFS
        self.assertAlmostEqual(ultrasonic.peak_hz, 30_000, delta=50)
        self.assertLess(top.dbfs, -40, "nothing was put in the 40 to 80 kHz band")

    def test_silence_is_far_below_a_tone(self):
        quiet = band_levels(one_bit_stream([], 2 * NFFT), CLOCK_HZ)
        self.assertLess(quiet[0].dbfs, -60)

    def test_too_little_data_is_refused(self):
        with self.assertRaises(ValueError):
            band_levels(bytes(100), CLOCK_HZ)


if __name__ == "__main__":
    unittest.main()
