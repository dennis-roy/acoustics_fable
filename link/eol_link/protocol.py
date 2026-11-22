"""Frame encoding and decoding. The contract is protocol/README.md.

The firmware has its own, separately written copy of this in firmware/src/proto.c.
"""

from __future__ import annotations

import struct
import sys
import zlib
from array import array
from dataclasses import dataclass

PROTO_MAJOR = 1
PROTO_MINOR = 1

MAGIC = b"EOL1"
HDR_LEN = 24
CRC_LEN = 4
MAX_PAYLOAD = 4096
PDM_PAYLOAD = 4000
TACH_PAYLOAD = 250
PATTERN_SIZE = 65536
FRAME_PERIOD_S = 0.010
# One PDM and one TACH frame every 10 ms, with headers and CRCs.
REAL_RATE_BYTES_PER_S = (PDM_PAYLOAD + TACH_PAYLOAD + 2 * (HDR_LEN + CRC_LEN)) / FRAME_PERIOD_S

T_INFO, T_PDM, T_TACH, T_EVENT, T_STATS, T_ACK, T_MIC_STATUS, T_HB_STATUS = (
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x10,
)
T_HELLO, T_STREAM_START, T_STREAM_STOP, T_SET_OUTPUTS, T_GET_STATS, T_REBOOT, T_HEARTBEAT = (
    0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x90,
)

F_GAP, F_SYNTH = 0x01, 0x02

ST_OK, ST_BAD_VERSION, ST_BAD_ARG, ST_NOT_IMPLEMENTED, ST_BUSY, ST_SAFE_STATE = range(6)
STATUS_NAMES = ("OK", "BAD_VERSION", "BAD_ARG", "NOT_IMPLEMENTED", "BUSY", "SAFE_STATE")

EV_STREAM_STARTED, EV_STREAM_STOPPED, EV_TX_OVERFLOW, EV_WATCHDOG_TRIP, EV_WATCHDOG_OK, EV_MIC_OVERRUN = range(1, 7)

SRC_SYNTH, SRC_MIC = 0, 1
MODE_REAL, MODE_MAX = 0, 1

FEATURE_SYNTH, FEATURE_MIC, FEATURE_TACH, FEATURE_OUTPUTS = 1, 2, 4, 8

# Microphone (protocol/README.md, "Microphone stream").
MIC_WAKE_HZ = 2_000_000
MIC_CLOCKS_HZ = (3_200_000, 4_000_000)  # STREAM_START clock code 0 and 1
MIC_NOT_PROBED, MIC_SELECT_GND, MIC_SELECT_VDD, MIC_STUCK, MIC_AMBIGUOUS = range(5)
MIC_VERDICT_NAMES = (
    "not probed",
    "SELECT = GND (data read at the rising edge)",
    "SELECT = VDD (data read at the falling edge)",
    "line stuck: no microphone, no power or no clock",
    "ambiguous: the line changes but fits neither SELECT setting",
)
MIC_USE_NONE, MIC_USE_L, MIC_USE_H = 0, 1, 2
MIC_PROBE_PERIODS = 100_000
REBOOT_MAGIC = 0x544F4F42  # the bytes "BOOT"

OUT_OK, OUT_NOK, OUT_UNDEFINED, OUT_READY, OUT_BUSY, OUT_WATCHDOG = (1 << i for i in range(6))
STATE_HELLO, STATE_STREAMING, STATE_HEALTHY = 1, 2, 4

BOARD_NAMES = {1: "Pico 2 W", 0xFE: "firmware core built for a PC", 0xFF: "Python simulator"}

_HEAD = struct.Struct("<4sBBHIQ")  # 20 bytes; the header CRC follows
_U32 = struct.Struct("<I")
_INFO = struct.Struct("<BBBBBBHII8sHHI16s")
_STATS = struct.Struct("<9IBBH")
_MIC_STATUS = struct.Struct("<BBBB8I4x")


@dataclass(frozen=True)
class Frame:
    type: int
    flags: int
    seq: int
    t_us: int
    payload: bytes


def encode_frame(ftype: int, payload: bytes = b"", *, flags: int = 0, seq: int = 0, t_us: int = 0) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too long")
    head = _HEAD.pack(MAGIC, ftype, flags, len(payload), seq & 0xFFFFFFFF, t_us)
    body = head + _U32.pack(zlib.crc32(head)) + payload
    return body + _U32.pack(zlib.crc32(body))


class FrameParser:
    """Turns a byte stream into frames. Feed it whatever arrives, in any chunking."""

    def __init__(self, max_payload: int = MAX_PAYLOAD) -> None:
        self._buf = bytearray()
        self._max_payload = max_payload
        self.junk_bytes = 0  # bytes discarded while looking for a frame
        self.header_errors = 0
        self.crc_errors = 0

    def feed(self, data: bytes) -> list[Frame]:
        buf = self._buf
        buf += data
        frames: list[Frame] = []
        pos = 0
        while True:
            start = buf.find(MAGIC, pos)
            if start < 0:
                # No frame start in sight. Keep the last 3 bytes: they may be half a magic.
                keep_from = max(pos, len(buf) - (len(MAGIC) - 1))
                self.junk_bytes += keep_from - pos
                pos = keep_from
                break
            self.junk_bytes += start - pos
            pos = start
            if len(buf) - pos < HDR_LEN:
                break
            _, ftype, flags, length, seq, t_us = _HEAD.unpack_from(buf, pos)
            (hdr_crc,) = _U32.unpack_from(buf, pos + 20)
            if hdr_crc != zlib.crc32(bytes(buf[pos : pos + 20])) or length > self._max_payload:
                self.header_errors += 1
                self.junk_bytes += 1
                pos += 1
                continue
            end = pos + HDR_LEN + length
            if len(buf) < end + CRC_LEN:
                break
            (crc,) = _U32.unpack_from(buf, end)
            if crc != zlib.crc32(bytes(buf[pos:end])):
                self.crc_errors += 1
                self.junk_bytes += 1
                pos += 1
                continue
            frames.append(Frame(ftype, flags, seq, t_us, bytes(buf[pos + HDR_LEN : end])))
            pos = end + CRC_LEN
        del buf[:pos]
        return frames


# ---- payloads ----


@dataclass(frozen=True)
class Info:
    proto_major: int
    proto_minor: int
    fw_version: str
    board: int
    hb_timeout_ms: int
    sys_clk_hz: int
    fc_sys_khz: int
    unique_id: str
    pdm_payload: int
    tach_payload: int
    features: int
    build: str

    @property
    def board_name(self) -> str:
        return BOARD_NAMES.get(self.board, f"unknown board {self.board:#x}")


def pack_info(*, proto_major, proto_minor, fw, board, hb_timeout_ms, sys_clk_hz, fc_sys_khz, unique_id,
              features, build) -> bytes:
    return _INFO.pack(proto_major, proto_minor, *fw, board, hb_timeout_ms, sys_clk_hz, fc_sys_khz,
                      unique_id, PDM_PAYLOAD, TACH_PAYLOAD, features, build.encode()[:15])


def unpack_info(payload: bytes) -> Info:
    (pmaj, pmin, f0, f1, f2, board, hb_ms, clk, fc, uid, pdm, tach, features, build) = _INFO.unpack(
        payload[: _INFO.size]
    )
    return Info(pmaj, pmin, f"{f0}.{f1}.{f2}", board, hb_ms, clk, fc, uid.hex(), pdm, tach, features,
                build.rstrip(b"\0").decode(errors="replace"))


@dataclass(frozen=True)
class Stats:
    uptime_ms: int
    pdm_generated: int
    pdm_dropped: int
    tach_generated: int
    tach_dropped: int
    tx_high_water: int
    rx_junk_bytes: int
    hb_received: int
    wd_trips: int
    outputs: int
    state: int


def pack_stats(s: Stats) -> bytes:
    return _STATS.pack(s.uptime_ms & 0xFFFFFFFF, s.pdm_generated, s.pdm_dropped, s.tach_generated,
                       s.tach_dropped, s.tx_high_water, s.rx_junk_bytes, s.hb_received, s.wd_trips,
                       s.outputs, s.state, 0)


def unpack_stats(payload: bytes) -> Stats:
    return Stats(*_STATS.unpack(payload[: _STATS.size])[:11])


def pack_hello(major: int = PROTO_MAJOR, minor: int = PROTO_MINOR) -> bytes:
    return struct.pack("<BBH", major, minor, 0)


def pack_stream_start(source: int, mode: int, seed: int, duration_ms: int, *, clock: int = 0,
                      select: int = 0) -> bytes:
    """clock and select apply to the microphone only: clock code, and which half to read
    (0 = let the Pico find out, MIC_USE_L, MIC_USE_H)."""
    return struct.pack("<BBBBII", source, mode, clock, select, seed, duration_ms)


def pack_reboot() -> bytes:
    return struct.pack("<I", REBOOT_MAGIC)


@dataclass(frozen=True)
class MicStatus:
    """What the Pico's SELECT probe saw. L and H are the two reads in each clock period."""

    verdict: int
    use: int  # which read is being forwarded: MIC_USE_L or MIC_USE_H
    forced: bool  # chosen by the command, not by the probe
    clock_code: int
    clock_hz: int
    periods: int
    ones_l: int
    ones_h: int
    lh_10: int  # periods where L was 1 and the H after it was 0
    lh_01: int
    hl_10: int  # periods where H was 1 and the L after it was 0
    hl_01: int

    @property
    def verdict_name(self) -> str:
        return MIC_VERDICT_NAMES[self.verdict] if self.verdict < len(MIC_VERDICT_NAMES) else str(self.verdict)


def pack_mic_status(s: MicStatus) -> bytes:
    return _MIC_STATUS.pack(s.verdict, s.use, 1 if s.forced else 0, s.clock_code, s.clock_hz, s.periods,
                            s.ones_l, s.ones_h, s.lh_10, s.lh_01, s.hl_10, s.hl_01)


def unpack_mic_status(payload: bytes) -> MicStatus:
    v = _MIC_STATUS.unpack(payload[: _MIC_STATUS.size])
    return MicStatus(v[0], v[1], bool(v[2]), *v[3:])


def pack_set_outputs(mask: int) -> bytes:
    return struct.pack("<B3x", mask)


def pack_heartbeat(counter: int, app_alive: bool) -> bytes:
    return struct.pack("<IB3x", counter & 0xFFFFFFFF, 1 if app_alive else 0)


def unpack_hb_status(payload: bytes) -> tuple[int, int, int, int]:
    """Returns (counter, outputs, state, timeout_ms)."""
    return struct.unpack("<IBBH", payload[:8])


def pack_hb_status(counter: int, outputs: int, state: int, timeout_ms: int) -> bytes:
    return struct.pack("<IBBH", counter, outputs, state, timeout_ms)


def pack_ack(cmd_type: int, status: int, cmd_seq: int) -> bytes:
    return struct.pack("<BBHI", cmd_type, status, 0, cmd_seq)


def unpack_ack(payload: bytes) -> tuple[int, int, int]:
    """Returns (cmd_type, status, cmd_seq)."""
    cmd, status, _, seq = struct.unpack("<BBHI", payload[:8])
    return cmd, status, seq


def pack_event(code: int, arg: int) -> bytes:
    return struct.pack("<B3xI", code, arg & 0xFFFFFFFF)


def unpack_event(payload: bytes) -> tuple[int, int]:
    """Returns (code, arg)."""
    return struct.unpack("<B3xI", payload[:8])


# ---- synthetic data ----


def synth_pattern(seed: int) -> bytes:
    """The 65 536-byte test pattern for a seed (protocol/README.md, "Synthetic data")."""
    x = (seed & 0xFFFFFFFF) or 1
    words = array("I", bytes(PATTERN_SIZE))
    if words.itemsize != 4:
        raise RuntimeError("this platform has no 4-byte unsigned int array type")
    for i in range(PATTERN_SIZE // 4):
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        words[i] = x
    if sys.byteorder == "big":
        words.byteswap()
    return words.tobytes()


class SynthSource:
    """Expected payload of any synthetic frame, by frame type and number."""

    def __init__(self, seed: int) -> None:
        pattern = synth_pattern(seed)
        self._wrapped = pattern + pattern[:MAX_PAYLOAD]

    def payload(self, ftype: int, n: int) -> bytes:
        if ftype == T_PDM:
            length, offset = PDM_PAYLOAD, (n * PDM_PAYLOAD) % PATTERN_SIZE
        elif ftype == T_TACH:
            length, offset = TACH_PAYLOAD, (n * TACH_PAYLOAD + PATTERN_SIZE // 2) % PATTERN_SIZE
        else:
            raise ValueError("not a stream type")
        return self._wrapped[offset : offset + length]
