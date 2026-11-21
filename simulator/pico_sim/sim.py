"""The Pico simulator.

It behaves like the firmware core (firmware/src/core.c) as described in
protocol/README.md, and offers the same two serial ports as pseudo-terminals. It was
written from the protocol document, not translated from the C, so the two check each
other: the test suite runs the same checks against this and against the C core.

It can also misbehave on purpose (Faults), to prove the receiver notices.
"""

from __future__ import annotations

import os
import select
import threading
import time
import tty
from dataclasses import dataclass, field

from eol_link import protocol as p

FW_VERSION = (0, 2, 0)
HB_TIMEOUT_MS = 500
TX_BUFFER = 65536
TX_RESERVE = 512
MAX_FRAMES_PER_POLL = 8
STATS_BACKLOG_LIMIT = 4096
FEATURES = 0x1 | 0x8  # synthetic source, outputs
WD_HB_TIMEOUT, WD_APP_DEAD = 1, 2


@dataclass
class Faults:
    """Deliberate faults, by PDM frame number within a stream."""

    drop_pdm: set[int] = field(default_factory=set)  # never sent: the receiver sees a gap
    corrupt_pdm: set[int] = field(default_factory=set)  # a bit flipped on the wire: CRC fails
    wrong_data_pdm: set[int] = field(default_factory=set)  # wrong contents, correct CRC


class _Pty:
    def __init__(self) -> None:
        self.master, self._slave = os.openpty()
        tty.setraw(self._slave)  # no echo, no line editing
        os.set_blocking(self.master, False)
        self.path = os.ttyname(self._slave)

    def read(self) -> bytes:
        try:
            return os.read(self.master, 65536)
        except BlockingIOError:
            return b""

    def write(self, data: bytes) -> int:
        try:
            return os.write(self.master, data)
        except BlockingIOError:
            return 0

    def close(self) -> None:
        os.close(self.master)
        os.close(self._slave)


class PicoSim:
    def __init__(self, *, proto_major: int = p.PROTO_MAJOR, proto_minor: int = p.PROTO_MINOR,
                 faults: Faults | None = None) -> None:
        self._proto = (proto_major, proto_minor)
        self._faults = faults or Faults()
        self._data, self._hb = _Pty(), _Pty()
        self.data_path, self.hb_path = self._data.path, self._hb.path
        self._data_parser = p.FrameParser(max_payload=64)
        self._hb_parser = p.FrameParser(max_payload=64)
        self._tx = bytearray()
        self._hb_tx = bytearray()
        self._seq = {t: 0 for t in (p.T_INFO, p.T_EVENT, p.T_STATS, p.T_ACK, p.T_HB_STATUS)}
        self._t_start = time.monotonic_ns()
        self._now = 0

        self._hello_ok = False
        self._streaming = False
        self._mode = p.MODE_REAL
        self._seed = 0
        self._synth: p.SynthSource | None = None
        self._t0 = self._next_due = self._duration_us = 0
        self._gen = {p.T_PDM: 0, p.T_TACH: 0}
        self._dropped = {p.T_PDM: 0, p.T_TACH: 0}
        self._gap = {p.T_PDM: False, p.T_TACH: False}
        self._ovf_unreported = 0
        self._last_ovf_event = self._last_stats = 0
        self._tx_high = 0

        self._hb_seen = self._hb_app_alive = self._healthy = False
        self._hb_last = self._hb_count = self._wd_trips = 0
        self._requested = 0
        self.outputs = 0  # what the output pins would show right now
        self.output_log: list[tuple[int, int]] = []  # (microseconds, mask) at each change

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- running ----

    def start(self) -> "PicoSim":
        self._thread = threading.Thread(target=self.run, name="pico-sim", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        self._data.close()
        self._hb.close()

    def run(self) -> None:
        while not self._stop.is_set():
            select.select([self._data.master, self._hb.master], [], [], 0.001)
            self._now = (time.monotonic_ns() - self._t_start) // 1000
            for f in self._data_parser.feed(self._data.read()):
                self._on_command(f)
            for f in self._hb_parser.feed(self._hb.read()):
                self._on_heartbeat(f)
            self._safety()
            self._stream()
            if self._now - self._last_stats >= 1_000_000 and len(self._tx) <= STATS_BACKLOG_LIMIT:
                self._send_stats()
            if self._tx:
                del self._tx[: self._data.write(bytes(self._tx[:16384]))]
            if self._hb_tx:
                del self._hb_tx[: self._hb.write(bytes(self._hb_tx))]

    # ---- transmit ----

    def _emit(self, ftype: int, payload: bytes, *, seq: int | None = None, flags: int = 0,
              t_us: int | None = None, is_stream: bool = False, wire: bytes | None = None) -> bool:
        if seq is None:
            seq = self._seq[ftype]
            self._seq[ftype] += 1
        frame = wire or p.encode_frame(ftype, payload, flags=flags, seq=seq,
                                       t_us=self._now if t_us is None else t_us)
        if len(self._tx) + len(frame) + (TX_RESERVE if is_stream else 0) > TX_BUFFER:
            return False
        self._tx += frame
        self._tx_high = max(self._tx_high, len(self._tx))
        return True

    def _event(self, code: int, arg: int = 0) -> None:
        self._emit(p.T_EVENT, p.pack_event(code, arg))

    def _state(self) -> int:
        return ((p.STATE_HELLO if self._hello_ok else 0) | (p.STATE_STREAMING if self._streaming else 0)
                | (p.STATE_HEALTHY if self._healthy else 0))

    def _send_stats(self) -> None:
        self._emit(p.T_STATS, p.pack_stats(p.Stats(
            self._now // 1000, self._gen[p.T_PDM], self._dropped[p.T_PDM], self._gen[p.T_TACH],
            self._dropped[p.T_TACH], self._tx_high,
            self._data_parser.junk_bytes + self._hb_parser.junk_bytes, self._hb_count, self._wd_trips,
            self.outputs, self._state())))
        self._last_stats = self._now

    # ---- outputs and watchdog ----

    def _safety(self) -> None:
        fresh = self._hb_seen and (self._now - self._hb_last) <= HB_TIMEOUT_MS * 1000
        healthy = fresh and self._hb_app_alive
        if self._healthy and not healthy:
            self._requested = 0
            self._wd_trips += 1
            self._event(p.EV_WATCHDOG_TRIP, WD_APP_DEAD if fresh else WD_HB_TIMEOUT)
        elif healthy and not self._healthy:
            self._event(p.EV_WATCHDOG_OK)
        self._healthy = healthy

        actual = 0
        if healthy:
            actual = self._requested & 0x1F
            if (self._now // 500_000) & 1:
                actual |= p.OUT_WATCHDOG
        if actual != self.outputs:
            self.outputs = actual
            self.output_log.append((self._now, actual))

    def _on_heartbeat(self, f: p.Frame) -> None:
        if f.type != p.T_HEARTBEAT or len(f.payload) < 8:
            return
        self._hb_seen = True
        self._hb_last = self._now
        self._hb_app_alive = bool(f.payload[4] & 1)
        self._hb_count += 1
        self._safety()
        counter = int.from_bytes(f.payload[:4], "little")
        reply = p.encode_frame(p.T_HB_STATUS, p.pack_hb_status(counter, self.outputs, self._state(), HB_TIMEOUT_MS),
                               seq=self._seq[p.T_HB_STATUS], t_us=self._now)
        self._seq[p.T_HB_STATUS] += 1
        if len(self._hb_tx) + len(reply) <= 128:
            self._hb_tx += reply

    # ---- stream ----

    def _stream_frame(self, ftype: int, t_us: int) -> None:
        n = self._gen[ftype]
        self._gen[ftype] += 1
        payload = self._synth.payload(ftype, n)
        flags = p.F_SYNTH | (p.F_GAP if self._gap[ftype] else 0)
        wire = None
        if ftype == p.T_PDM:
            if n in self._faults.drop_pdm:
                self._gap[ftype] = True
                return
            if n in self._faults.wrong_data_pdm:
                payload = bytes([payload[0] ^ 0x01]) + payload[1:]
            if n in self._faults.corrupt_pdm:
                good = bytearray(p.encode_frame(ftype, payload, flags=flags, seq=n, t_us=t_us))
                good[p.HDR_LEN + 100] ^= 0x10
                wire = bytes(good)
        if self._emit(ftype, payload, seq=n, flags=flags, t_us=t_us, is_stream=True, wire=wire):
            self._gap[ftype] = False
        else:
            self._gap[ftype] = True
            self._dropped[ftype] += 1
            self._ovf_unreported += 1

    def _stream_stop(self) -> None:
        if self._streaming:
            self._streaming = False
            self._event(p.EV_STREAM_STOPPED, self._gen[p.T_PDM])

    def _stream(self) -> None:
        if not self._streaming:
            return
        timed_out = self._duration_us and (self._now - self._t0) >= self._duration_us
        if self._mode == p.MODE_REAL:
            limit = self._t0 + self._duration_us if timed_out else self._now
            for _ in range(MAX_FRAMES_PER_POLL):
                if self._next_due >= limit:
                    break
                self._stream_frame(p.T_PDM, self._next_due)
                self._stream_frame(p.T_TACH, self._next_due)
                self._next_due += 10_000
            if timed_out and self._next_due >= limit:
                self._stream_stop()
        else:
            frame = p.HDR_LEN + p.PDM_PAYLOAD + p.CRC_LEN
            for _ in range(MAX_FRAMES_PER_POLL):
                if timed_out or len(self._tx) + frame + TX_RESERVE > TX_BUFFER:
                    break
                self._stream_frame(p.T_PDM, self._now)
            if timed_out:
                self._stream_stop()
        if self._ovf_unreported and self._now - self._last_ovf_event >= 1_000_000:
            self._event(p.EV_TX_OVERFLOW, self._ovf_unreported)
            self._ovf_unreported = 0
            self._last_ovf_event = self._now

    # ---- commands ----

    def _stream_start(self, payload: bytes) -> int:
        if not self._hello_ok:
            return p.ST_BAD_VERSION
        if len(payload) < 12 or payload[1] > p.MODE_MAX:
            return p.ST_BAD_ARG
        if payload[0] == p.SRC_MIC:
            return p.ST_NOT_IMPLEMENTED
        if payload[0] != p.SRC_SYNTH:
            return p.ST_BAD_ARG
        if self._streaming:
            return p.ST_BUSY
        duration_ms = int.from_bytes(payload[8:12], "little")
        if duration_ms > 3_600_000:
            return p.ST_BAD_ARG
        self._mode = payload[1]
        self._seed = int.from_bytes(payload[4:8], "little")
        self._duration_us = duration_ms * 1000
        self._synth = p.SynthSource(self._seed)
        self._gen = {p.T_PDM: 0, p.T_TACH: 0}
        self._gap = {p.T_PDM: False, p.T_TACH: False}
        self._t0 = self._next_due = self._now
        self._streaming = True
        return p.ST_OK

    def _set_outputs(self, payload: bytes) -> int:
        if not self._hello_ok:
            return p.ST_BAD_VERSION
        if len(payload) < 4 or payload[0] & ~0x1F:
            return p.ST_BAD_ARG
        results = payload[0] & 0x07
        if results & (results - 1):
            return p.ST_BAD_ARG
        if not self._healthy:
            return p.ST_SAFE_STATE
        self._requested = payload[0]
        self._safety()
        return p.ST_OK

    def _on_command(self, f: p.Frame) -> None:
        status = p.ST_OK
        if f.type == p.T_HELLO:
            self._hello_ok = len(f.payload) >= 4 and f.payload[0] == self._proto[0]
            self._emit(p.T_INFO, p.pack_info(
                proto_major=self._proto[0], proto_minor=self._proto[1], fw=FW_VERSION, board=0xFF,
                hb_timeout_ms=HB_TIMEOUT_MS, sys_clk_hz=128_000_000, fc_sys_khz=0, unique_id=b"PYSIM\0\0\0",
                features=FEATURES, build="sim"))
            status = p.ST_OK if self._hello_ok else p.ST_BAD_VERSION
        elif f.type == p.T_STREAM_START:
            status = self._stream_start(f.payload)
        elif f.type == p.T_STREAM_STOP:
            self._stream_stop()
        elif f.type == p.T_SET_OUTPUTS:
            status = self._set_outputs(f.payload)
        elif f.type == p.T_GET_STATS:
            self._send_stats()
        else:
            return
        self._emit(p.T_ACK, p.pack_ack(f.type, status, f.seq))
        if f.type == p.T_STREAM_START and status == p.ST_OK:
            self._event(p.EV_STREAM_STARTED, self._seed)
