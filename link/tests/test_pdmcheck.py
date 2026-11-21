import random
import unittest

from eol_link.pdmcheck import check_pdm


def bits_to_bytes(bits: str) -> bytes:
    assert len(bits) % 8 == 0
    return int(bits, 2).to_bytes(len(bits) // 8, "big")


class PdmCheckTests(unittest.TestCase):
    def test_idle_like_stream_passes(self):
        result = check_pdm(bytes([0b10101010]) * 4000)
        self.assertTrue(result.valid, result.reasons)
        self.assertAlmostEqual(result.ones_fraction, 0.5)
        self.assertEqual(result.longest_run_bits, 0)

    def test_random_stream_passes(self):
        rng = random.Random(1)
        data = bytes(rng.choice((0x55, 0xAA, 0x5A, 0xA5, 0x69, 0x96, 0x33, 0xCC)) for _ in range(40000))
        self.assertTrue(check_pdm(data).valid)

    def test_stuck_low_and_stuck_high(self):
        for value, level in ((0x00, 0), (0xFF, 1)):
            result = check_pdm(bytes([value]) * 4000)
            self.assertFalse(result.valid)
            self.assertEqual(result.longest_run_bits, 32000)
            self.assertIn(f"stuck at {level}", result.reasons[0])

    def test_run_length_is_exact_across_byte_edges(self):
        # 3 bits + 4 whole bytes + 5 bits = a run of 40 ones, with zeros either side.
        bits = "10101010" + "01010111" + "1" * 32 + "11111010" + "10101010"
        for pad in (0, 1, 5):
            data = bytes([0xAA]) * pad + bits_to_bytes(bits) + bytes([0xAA]) * 50
            self.assertEqual(check_pdm(data, max_run_bits=64).longest_run_bits, 40)

    def test_run_over_the_limit_is_invalid(self):
        data = bytes([0xAA]) * 100 + bytes(5) + bytes([0xAA]) * 5000  # 40 zeros (+1 from the 0xAA before)
        result = check_pdm(data)
        self.assertFalse(result.valid)
        self.assertEqual(result.longest_run_bits, 41)
        self.assertIn("overload", result.reasons[0])
        self.assertTrue(check_pdm(data, max_run_bits=41).valid)

    def test_run_of_exactly_32_at_the_start_and_end(self):
        self.assertEqual(check_pdm(bytes(4) + bytes([0xAA]) * 100, max_run_bits=64).longest_run_bits, 32)
        self.assertEqual(check_pdm(bytes([0x55]) * 100 + b"\xff" * 4, max_run_bits=64).longest_run_bits, 33)

    def test_ones_fraction_out_of_range(self):
        result = check_pdm(bytes([0b11101110]) * 4000)  # 75 % ones, no long runs
        self.assertFalse(result.valid)
        self.assertIn("75.0%", result.reasons[0])

    def test_empty(self):
        self.assertFalse(check_pdm(b"").valid)


if __name__ == "__main__":
    unittest.main()
