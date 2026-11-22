"""Tests of the decimation filter. Need numpy and scipy; skipped, and reported as skipped, without them."""

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    import scipy  # noqa: F401

    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

if HAVE_DEPS:
    from eol_dsp import decimate as dm
    from eol_dsp.wavfile import read_float_wav, write_float_wav

DSP_DIR = Path(__file__).resolve().parents[1]
CLOCK = 3_200_000


def response_db(taps, freqs, fs):
    """Gain of an FIR filter at the given frequencies, in dB."""
    n = np.arange(len(taps))
    w = 2 * np.pi * np.asarray(freqs, dtype=float)[:, None] / fs
    h = (np.asarray(taps)[None, :] * np.exp(-1j * w * n[None, :])).sum(axis=1)
    return 20 * np.log10(np.maximum(np.abs(h), 1e-15))


def one_bit(x):
    """Second-order sigma-delta modulator: float signal (|x| < 0.7) to PDM bytes, bit 7 first."""
    v1 = v2 = 0.0
    bits = np.empty(len(x), dtype=np.uint8)
    for n, xn in enumerate(x.tolist()):
        y = 1.0 if v2 >= 0 else -1.0
        bits[n] = y > 0
        v1 += xn - y
        v2 += v1 - y
    return np.packbits(bits).tobytes()


def fit_amplitude(values, f, rate):
    """Amplitude of the sine at frequency f in values, by least squares."""
    t = np.arange(len(values)) / rate
    basis = np.column_stack([np.sin(2 * np.pi * f * t), np.cos(2 * np.pi * f * t)])
    coef, *_ = np.linalg.lstsq(basis, values.astype(np.float64), rcond=None)
    return float(np.hypot(*coef))


@unittest.skipUnless(HAVE_DEPS, "numpy or scipy is not installed")
class FilterFileTests(unittest.TestCase):
    def test_both_settings_exist_and_are_well_formed(self):
        self.assertEqual(sorted(f.decimation for f in dm.filters()), [16, 32])
        for f in dm.filters():
            with self.subTest(f.name):
                self.assertIn(f"pdm{f.clock_hz // 1000}k-d{f.decimation}-p{f.pass_hz // 1000}k-s{f.stop_hz // 1000}k",
                              f.name, "the name must state clock, ratio and band edges")
                self.assertTrue(np.array_equal(f.taps, f.taps[::-1]), "linear phase needs symmetric taps")
                self.assertAlmostEqual(f.taps.sum(), 1.0, places=12)
                self.assertEqual(len(f.taps) % (2 * f.decimation), 1)
                self.assertEqual(f.delay_samples * 2 * f.decimation + 1, len(f.taps))

    def test_the_default_is_divide_by_16_to_200_khz(self):
        f = dm.get_filter()
        self.assertEqual((f.decimation, f.rate_hz), (16, 200_000))

    def test_a_changed_coefficient_is_refused(self):
        data = json.loads((DSP_DIR / "eol_dsp" / "filters.json").read_text())
        data["filters"][0]["taps"][100] += 1e-9
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "filters.json"
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                dm.load_filters(path)

    def test_a_clock_without_a_filter_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no decimation filter"):
            dm.get_filter(4_000_000, 16)
        with self.assertRaises(ValueError):
            dm.get_filter(CLOCK, 8)

    def test_the_file_matches_a_fresh_design(self):
        sys.path.insert(0, str(DSP_DIR))
        try:
            import design_filters
        finally:
            sys.path.remove(str(DSP_DIR))
        fresh = design_filters.build()["filters"]
        for old, new in zip(dm.filters(), fresh):
            self.assertEqual(old.name, new["name"])
            np.testing.assert_allclose(old.taps, new["taps"], rtol=0, atol=1e-12)


@unittest.skipUnless(HAVE_DEPS, "numpy or scipy is not installed")
class ResponseTests(unittest.TestCase):
    def test_passband_is_flat_and_stopband_meets_the_design(self):
        for f in dm.filters():
            with self.subTest(f.name):
                passband = response_db(f.taps, np.linspace(0, f.pass_hz, 801), CLOCK)
                self.assertLess(np.abs(passband).max(), 0.001, "passband flat within 0.001 dB")
                stopband = response_db(f.taps, np.linspace(f.stop_hz, CLOCK / 2, 20001), CLOCK)
                self.assertLess(stopband.max(), -f.atten_db, f"stopband below -{f.atten_db:g} dB")

    def test_the_band_that_would_fold_onto_the_protected_range_is_blocked(self):
        # Divide by 16: 200 kHz out; anything from 120 kHz up would fold below 80 kHz.
        f = dm.get_filter(CLOCK, 16)
        self.assertLess(response_db(f.taps, [120_000, 160_000, 1_000_000], CLOCK).max(), -100)


@unittest.skipUnless(HAVE_DEPS, "numpy or scipy is not installed")
class DecimationTests(unittest.TestCase):
    def test_matches_the_textbook_calculation(self):
        x = np.random.default_rng(7).choice([-1.0, 1.0], size=40_000)
        for f in dm.filters():
            with self.subTest(f.name):
                full = np.convolve(x, f.taps)  # slow, obvious, float64
                k, d = f.delay_samples, f.decimation
                expected = full[k * d : k * d + (len(x) // d) * d : d]
                got = dm.decimate_signal(x, f).values
                self.assertEqual(len(got), len(x) // d)
                np.testing.assert_allclose(got, expected, rtol=0, atol=2e-6)

    def test_sample_j_is_the_moment_of_input_bit_j_times_the_ratio(self):
        # A pure tone at the clock rate: after the filter, every inner sample must equal the
        # tone at that moment. A leftover delay of one input bit would be an error of 4 %.
        for f in dm.filters():
            with self.subTest(f.name):
                n = np.arange(64_000)
                x = 0.5 * np.sin(2 * np.pi * 25_013 * n / CLOCK)
                s = dm.decimate_signal(x, f)
                j = np.arange(len(s.values))
                expected = 0.5 * np.sin(2 * np.pi * 25_013 * j * f.decimation / CLOCK)
                k = f.delay_samples
                np.testing.assert_allclose(s.inner(), expected[k : len(expected) - k], rtol=0, atol=1e-4)

    def test_a_tone_that_would_alias_is_removed(self):
        # 170 kHz would land on 30 kHz at 200 kHz out; 70 kHz would land on 30 kHz at 100 kHz out.
        for decimation, tone in ((16, 170_000), (32, 70_000)):
            f = dm.get_filter(CLOCK, decimation)
            n = np.arange(320_000)
            s = dm.decimate_signal(np.sin(2 * np.pi * tone * n / CLOCK), f)
            peak, rms = dm.level_dbfs(s.inner())
            self.assertLess(rms, -95, f"divide by {decimation}: {tone} Hz leaks at {rms:.1f} dBFS")

    def test_a_one_bit_stream_gives_back_its_tones(self):
        n = np.arange(320_000)  # 0.1 s
        x = 0.25 * np.sin(2 * np.pi * 1_000 * n / CLOCK) + 0.0625 * np.sin(2 * np.pi * 32_000 * n / CLOCK)
        s = dm.decimate_bits(one_bit(x), dm.get_filter())
        v = s.inner()
        self.assertAlmostEqual(fit_amplitude(v, 1_000, s.rate_hz), 0.25, delta=0.25 * 0.01)
        self.assertAlmostEqual(fit_amplitude(v, 32_000, s.rate_hz), 0.0625, delta=0.0625 * 0.01)

    def test_bits_and_signal_give_the_same_result(self):
        raw = np.random.default_rng(3).integers(0, 256, 5000, dtype=np.uint8).tobytes()
        x = np.unpackbits(np.frombuffer(raw, np.uint8)).astype(np.float32) * 2 - 1
        f = dm.get_filter()
        np.testing.assert_array_equal(dm.decimate_bits(raw, f).values, dm.decimate_signal(x, f).values)

    def test_too_little_input_gives_no_samples(self):
        f = dm.get_filter()
        self.assertEqual(len(dm.decimate_bits(b"", f).values), 0)
        self.assertEqual(len(dm.decimate_bits(b"\x55", f).values), 0)
        self.assertEqual(dm.level_dbfs(np.zeros(0)), (float("-inf"), float("-inf")))

    def test_levels(self):
        n = np.arange(200_000)
        peak, rms = dm.level_dbfs(0.5 * np.sin(2 * np.pi * 1000 * n / 200_000))
        self.assertAlmostEqual(peak, 20 * math.log10(0.5), delta=0.01)
        self.assertAlmostEqual(rms, 20 * math.log10(0.5), delta=0.01)


@unittest.skipUnless(HAVE_DEPS, "numpy or scipy is not installed")
class CommandLineTests(unittest.TestCase):
    def test_decimate_a_saved_capture_to_a_wav_file(self):
        n = np.arange(160_000)  # 50 ms
        raw = one_bit(0.25 * np.sin(2 * np.pi * 2_000 * n / CLOCK))
        with tempfile.TemporaryDirectory() as d:
            pdm, wav = Path(d) / "cap.pdm", Path(d) / "cap.wav"
            pdm.write_bytes(raw)
            pdm.with_suffix(".json").write_text(json.dumps({"clock_hz": CLOCK, "complete": True}))
            env = dict(os.environ, PYTHONPATH=str(DSP_DIR))
            out = subprocess.run([sys.executable, "-m", "eol_dsp", "decimate", str(pdm), "--wav", str(wav)],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertIn("eol-pdm3200k-d16", out.stdout)
            values, rate = read_float_wav(str(wav))
            self.assertEqual((rate, len(values)), (200_000, 10_000))
            self.assertAlmostEqual(fit_amplitude(values[100:-100], 2_000, rate), 0.25, delta=0.0025)
            info = json.loads(wav.with_suffix(".json").read_text())
            f = dm.get_filter()
            self.assertEqual((info["filter"], info["filter_sha256"]), (f.name, f.sha256))
            self.assertEqual(info["source_sha256"], hashlib.sha256(raw).hexdigest())

    def test_a_4_mhz_capture_is_refused_with_a_reason(self):
        with tempfile.TemporaryDirectory() as d:
            pdm = Path(d) / "cap.pdm"
            pdm.write_bytes(bytes(4000))
            pdm.with_suffix(".json").write_text(json.dumps({"clock_hz": 4_000_000}))
            env = dict(os.environ, PYTHONPATH=str(DSP_DIR))
            out = subprocess.run([sys.executable, "-m", "eol_dsp", "decimate", str(pdm)],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(out.returncode, 1)
            self.assertIn("no decimation filter for a 4 MHz clock", out.stdout)

    def test_wav_round_trip(self):
        v = np.array([0.0, 0.5, -1.25, 1e-6], dtype=np.float32)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.wav")
            write_float_wav(path, v, 200_000)
            got, rate = read_float_wav(path)
        np.testing.assert_array_equal(got, v)
        self.assertEqual(rate, 200_000)


if __name__ == "__main__":
    unittest.main()
