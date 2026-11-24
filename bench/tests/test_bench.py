"""Tests of the bench tool against a fake analyser (no hardware)."""

import base64
import json
import unittest
from unittest.mock import patch

from eol_bench.la import Analyser, AnalyserError, counts_report, data_delay, edges, widths_report

CLK = 150_000_000


class CountsTests(unittest.TestCase):
    def test_clock_cli_requires_ten_settled_seconds_and_bounded_counts(self):
        from eol_bench.__main__ import main
        import io
        gates = 1020000
        report = dict(mode="counts", clk_hz=CLK, gate_cycles=1500, gates=gates,
                      sum=32 * gates, min=32, max=32, zero_gates=0, ref_n=64, ref_sum=2048,
                      change_at=-1, after_n=gates - 64, after_sum=(gates - 64) * 32,
                      after_min=32, after_max=32, overrun=False, timed_out=False)
        cases = [(report, 0), ({**report, "min": 15, "max": 49, "after_min": 15, "after_max": 49}, 1),
                 ({**report, "gates": 1000000, "after_n": 999936}, 1)]
        for reply, expected in cases:
            with patch("sys.argv", ["eol_bench", "--port", "fake", "clock"]), \
                 patch("eol_bench.__main__.Port"), \
                 patch("eol_bench.__main__._measure_during_stream", return_value=reply), \
                 patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(main(), expected)

    def test_frequency_and_switch(self):
        j = dict(mode="counts", clk_hz=CLK, gate_cycles=1500, gates=15000,
                 sum=360000, min=20, max=32, zero_gates=0, ref_n=64, ref_sum=1280,
                 change_at=10000, after_n=4984, after_sum=4984 * 32, after_min=32, after_max=32,
                 overrun=False, timed_out=False)
        r = counts_report(j, expected_gates=15000)
        self.assertEqual(r.problems, [])
        self.assertEqual(r.ref_freq_hz, 2e6)
        self.assertEqual(r.after_freq_hz, 3.2e6)
        self.assertEqual(r.change_ms, 100)
        self.assertTrue(counts_report({**j, "overrun": True}, 15000).problems)
        self.assertTrue(counts_report({**j, "gates": 14999}, 15000).problems)
        self.assertTrue(counts_report({**j, "zero_gates": 1}, 15000).problems)
        self.assertTrue(counts_report({**j, "timed_out": True}, 15000).problems)

    def test_old_firmware_and_empty_capture(self):
        with self.assertRaisesRegex(AnalyserError, "counting"):
            counts_report({"mode": "widths"}, 10)
        j = dict(mode="counts", clk_hz=CLK, gate_cycles=1500, gates=0, sum=0,
                 min=0, max=0, zero_gates=0, ref_n=0, ref_sum=0,
                 change_at=-1, after_n=0, after_sum=0, after_min=0, after_max=0, overrun=False, timed_out=True)
        self.assertTrue(counts_report(j, 10).problems)


class FakePort:
    """Answers commands the way the firmware does."""

    def __init__(self, answers):
        self.answers, self.sent, self._out = answers, [], b""

    def write(self, data):
        self.sent.append(data.decode().strip())
        self._out += self.answers.pop(0).encode()

    def read(self, timeout):
        out, self._out = self._out, b""
        return out


def wakeup_json(change_ms=100.0, min_ns=153.0, overrun=False, timed_out=False):
    """What the firmware reports for the microphone wake-up (2 MHz, then 3.2 MHz)."""
    slow = int(change_ms * 2000)
    return {"mode": "widths", "clk_hz": CLK, "periods": slow + 100000, "total_cycles": slow * 75 + 100000 * 47,
            "high": {"min": round(min_ns * CLK / 1e9), "max": 39, "sum": slow * 37 + 100000 * 23},
            "low": {"min": 23, "max": 39, "sum": slow * 38 + 100000 * 24}, "ref_periods": 64, "ref_sum": 64 * 75,
            "change_at": slow, "cycles_to_change": slow * 75, "after_periods": 4096, "after_sum": 4096 * 46.875,
            "min_high_near_change": 23, "min_low_near_change": 23, "overrun": overrun, "timed_out": timed_out, "ended": True}


class WidthsTests(unittest.TestCase):
    def test_wakeup_numbers(self):
        r = widths_report(wakeup_json())
        self.assertAlmostEqual(r.change_ms, 100.0, places=6)
        self.assertAlmostEqual(r.ref_freq_hz, 2e6, delta=1)
        self.assertAlmostEqual(r.after_freq_hz, 3.2e6, delta=1)
        self.assertAlmostEqual(r.min_high_ns, 153.3, delta=0.5)
        self.assertEqual(r.problems, [])

    def test_overrun_and_no_signal_are_problems(self):
        self.assertTrue(widths_report(wakeup_json(overrun=True)).problems)
        r = widths_report({**wakeup_json(timed_out=True), "periods": 0})
        self.assertIn("no signal", " ".join(r.problems))

    def test_duty(self):
        r = widths_report(wakeup_json())
        self.assertAlmostEqual(r.duty, (200000 * 37 + 100000 * 23) / (200000 * 75 + 100000 * 47), places=6)


def square(n, period, high, offset=0):
    return [1 if (i + offset) % period < high else 0 for i in range(n)]


class BurstTests(unittest.TestCase):
    def test_edges(self):
        data = bytes(square(20, 10, 5))
        self.assertEqual(edges(data, 0), [(5, 0), (10, 1), (15, 0)])

    def test_data_follows_the_falling_edge_by_its_delay(self):
        # clock: period 48 samples (6.67 ns each = 320 ns, about 3.1 MHz), high 24; data changes 4 samples
        # (26.7 ns) after every falling edge, as a SELECT = GND microphone would.
        n, period = 4800, 48
        clk = square(n, period, 24)
        data_bits, level = [], 0
        for i in range(n):
            if i >= 4 and clk[i - 4] == 0 and clk[i - 5] == 1:
                level ^= 1
            data_bits.append(level)
        data = bytes(c | (d << 1) for c, d in zip(clk, data_bits))
        d = data_delay(data, 1e9 / CLK, 0, 1)
        self.assertEqual(d["data_follows"], "falling")
        self.assertAlmostEqual(d["after_falling"]["median_ns"], 4 * 1e9 / CLK, delta=0.1)


class ProtocolTests(unittest.TestCase):
    def test_counting_armed_response_is_not_the_result(self):
        from eol_bench.__main__ import _measure_during_stream
        la = Analyser(FakePort(['{"armed":"counts"}\n{"mode":"counts","gates":100}\n']))
        with patch("eol_bench.__main__._start_mic_stream") as start:
            result = _measure_during_stream(la, "counts 0 100 1000", False, 1)
        self.assertEqual(result["gates"], 100)
        start.assert_called_once_with(1)

    def test_rejected_count_command_does_not_start_microphone(self):
        from eol_bench.__main__ import _measure_during_stream
        for exception in (AnalyserError("unknown command"), OSError("USB disconnected")):
            with patch.object(Analyser, "command", side_effect=exception):
                with patch("eol_bench.__main__._start_mic_stream") as start:
                    with self.assertRaises(AnalyserError):
                        _measure_during_stream(Analyser(FakePort([])), "counts 0 100 1000", False, 1)
                    start.assert_not_called()

    def test_missing_armed_ack_does_not_start_microphone(self):
        from eol_bench.__main__ import _measure_during_stream
        with patch("eol_bench.__main__._start_mic_stream") as start:
            with self.assertRaisesRegex(AnalyserError, "without confirming armed"):
                _measure_during_stream(Analyser(FakePort(['{"mode":"counts"}\n'])), "counts 0 100 1000", False, 1)
            start.assert_not_called()

    def test_command_and_errors(self):
        port = FakePort(['noise from boot\n{"fw":"eol-la 0.1.0","clk_hz":150000000}\n', '{"error":"unknown command"}\n'])
        la = Analyser(port)
        self.assertEqual(la.command("id")["fw"], "eol-la 0.1.0")
        with self.assertRaisesRegex(AnalyserError, "unknown"):
            la.command("explode")
        self.assertEqual(port.sent, ["id", "explode"])

    def test_burst_is_decoded_and_checked(self):
        samples = bytes(range(256)) * 4
        lines = [base64.b64encode(samples[i:i + 57]).decode() for i in range(0, len(samples), 57)]
        head = {"mode": "burst", "clk_hz": CLK, "div": 1, "samples": len(samples), "trigger": 0, "rising": True}
        la = Analyser(FakePort([json.dumps(head) + "\n" + "\n".join(lines) + "\nEND\n"]))
        h, data = la.burst(len(samples), 1, 0)
        self.assertEqual((h["samples"], data), (1024, samples))
        short = Analyser(FakePort([json.dumps(head) + "\n" + lines[0] + "\nEND\n"]))
        with self.assertRaisesRegex(AnalyserError, "incomplete"):
            short.burst(len(samples), 1, 0)

    def test_no_answer_times_out(self):
        la = Analyser(FakePort([""]))
        with self.assertRaisesRegex(AnalyserError, "no answer"):
            la.command("id", timeout=0.05)


if __name__ == "__main__":
    unittest.main()
