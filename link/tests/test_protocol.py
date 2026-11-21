import struct
import unittest
import zlib

from eol_link import protocol as p


class FrameTests(unittest.TestCase):
    def test_layout_matches_the_protocol_document(self):
        frame = p.encode_frame(p.T_EVENT, b"\x01\x00\x00\x00\x2a\x00\x00\x00", flags=3, seq=7, t_us=0x0102030405060708)
        self.assertEqual(frame[:4], b"EOL1")
        self.assertEqual(frame[4:8], bytes([p.T_EVENT, 3, 8, 0]))
        self.assertEqual(struct.unpack_from("<IQ", frame, 8), (7, 0x0102030405060708))
        self.assertEqual(struct.unpack_from("<I", frame, 20)[0], zlib.crc32(frame[:20]))
        self.assertEqual(struct.unpack_from("<I", frame, 32)[0], zlib.crc32(frame[:32]))
        self.assertEqual(len(frame), p.HDR_LEN + 8 + p.CRC_LEN)

    def test_round_trip_in_any_chunking(self):
        frames = [p.encode_frame(p.T_PDM, bytes([i]) * 4000, seq=i) for i in range(5)]
        stream = b"".join(frames)
        for chunk in (1, 7, 64, 4096, len(stream)):
            parser = p.FrameParser()
            out = []
            for i in range(0, len(stream), chunk):
                out += parser.feed(stream[i : i + chunk])
            self.assertEqual([f.seq for f in out], [0, 1, 2, 3, 4], chunk)
            self.assertEqual(out[3].payload, bytes([3]) * 4000)
            self.assertEqual((parser.junk_bytes, parser.crc_errors, parser.header_errors), (0, 0, 0))

    def test_resyncs_after_junk(self):
        good = p.encode_frame(p.T_ACK, p.pack_ack(p.T_HELLO, 0, 9), seq=1)
        parser = p.FrameParser()
        out = parser.feed(b"noise EOL not a frame" + good + b"EO" + good)
        self.assertEqual(len(out), 2)
        self.assertEqual(parser.junk_bytes, len(b"noise EOL not a frame") + 2)

    def test_corrupt_payload_is_rejected_and_the_next_frame_survives(self):
        bad = bytearray(p.encode_frame(p.T_PDM, bytes(4000), seq=0))
        bad[100] ^= 0x40
        good = p.encode_frame(p.T_PDM, bytes(4000), seq=1)
        parser = p.FrameParser()
        out = parser.feed(bytes(bad) + good)
        self.assertEqual([f.seq for f in out], [1])
        self.assertEqual(parser.crc_errors, 1)

    def test_corrupt_length_is_caught_by_the_header_crc(self):
        bad = bytearray(p.encode_frame(p.T_TACH, bytes(250), seq=0))
        bad[6] ^= 0xFF  # the length field
        good = p.encode_frame(p.T_TACH, bytes(250), seq=1)
        parser = p.FrameParser()
        out = parser.feed(bytes(bad) + good)
        self.assertEqual([f.seq for f in out], [1])
        self.assertEqual(parser.header_errors, 1)

    def test_info_and_stats_sizes(self):
        info = p.pack_info(proto_major=1, proto_minor=0, fw=(0, 2, 0), board=1, hb_timeout_ms=500,
                           sys_clk_hz=128_000_000, fc_sys_khz=128_000, unique_id=b"12345678", features=9,
                           build="abc1234")
        self.assertEqual(len(info), 48)
        got = p.unpack_info(info)
        self.assertEqual((got.fw_version, got.sys_clk_hz, got.build, got.pdm_payload), ("0.2.0", 128_000_000, "abc1234", 4000))
        stats = p.Stats(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
        self.assertEqual(len(p.pack_stats(stats)), 40)
        self.assertEqual(p.unpack_stats(p.pack_stats(stats)), stats)


class SynthTests(unittest.TestCase):
    def test_pattern_follows_the_documented_recipe(self):
        pattern = p.synth_pattern(1)
        self.assertEqual(len(pattern), 65536)
        # First xorshift32 output for x = 1: 1 ^ (1 << 13) = 0x2001; ^ >> 17 = 0x2001;
        # ^ << 5 = 0x2001 ^ 0x40020 = 0x42021.
        self.assertEqual(pattern[:4], (0x42021).to_bytes(4, "little"))
        self.assertEqual(p.synth_pattern(0), pattern)  # seed 0 is replaced by 1

    def test_frames_walk_through_the_pattern_and_wrap(self):
        pattern = p.synth_pattern(0xABCD)
        src = p.SynthSource(0xABCD)
        self.assertEqual(src.payload(p.T_PDM, 0), pattern[:4000])
        n = 16  # 16 * 4000 = 64000: this frame crosses the end of the pattern
        self.assertEqual(src.payload(p.T_PDM, n), pattern[64000:] + pattern[: 4000 - 1536])
        self.assertEqual(src.payload(p.T_TACH, 0), pattern[32768 : 32768 + 250])
        self.assertEqual(len({src.payload(p.T_PDM, i) for i in range(100)}), 100)


if __name__ == "__main__":
    unittest.main()
