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

from . import micsig

FW_VERSION = (0, 3, 0)
HB_TIMEOUT_MS = 500
TX_BUFFER = 65536
TX_RESERVE = 512
MAX_FRAMES_PER_POLL = 8
STATS_BACKLOG_LIMIT = 4096
CAPTURE_RING_US = 40_960  # the firmware's capture ring: 8192 words of 32 samples at 3.2 MHz
WD_HB_TIMEOUT, WD_APP_DEAD = 1, 2


@dataclass
class Faults:
    """Deliberate faults, by PDM frame number within a stream."""

    drop_pdm: set[int] = field(default_factory=set)  # never sent: the receiver sees a gap
    corrupt_pdm: set[int] = field(default_factory=set)  # a bit flipped on the wire: CRC fails
    wrong_data_pdm: set[int] = field(default_factory=set)  # wrong contents, correct CRC


def mic_verdict(periods: int, *, ones_l: int, ones_h: int, lh_10: int, lh_01: int, hl_10: int,
                hl_01: int) -> int:
    """The SELECT probe's decision (protocol/README.md, "The SELECT probe")."""
    small, large = periods // 64, periods // 16
    same_if_gnd, same_if_vdd = min(lh_10, lh_01), min(hl_10, hl_01)
    if same_if_gnd < small and same_if_vdd >= large:
        return p.MIC_SELECT_GND
    if same_if_vdd < small and same_if_gnd >= large:
        return p.MIC_SELECT_VDD
    if same_if_gnd < small and same_if_vdd < small:
        l_alive = periods // 8 < ones_l < periods - periods // 8
        h_alive = periods // 8 < ones_h < periods - periods // 8
        if l_alive and not h_alive:
            return p.MIC_SELECT_GND
        if h_alive and not l_alive:
            return p.MIC_SELECT_VDD
        return p.MIC_STUCK
    return p.MIC_AMBIGUOUS


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
                 faults: Faults | None = None, mic: str | None = "gnd", mic_release: str = "low") -> None:
        """mic: how the simulated microphone's SELECT pin is tied ("gnd" or "vdd"), "stuck"
        for a dead data line, or None for firmware without microphone capture.
        mic_release: what the data line does while the microphone is not driving it:
        "low" (a pull-down), "hold" (it keeps its level) or "high"."""
        if mic not in ("gnd", "vdd", "stuck", None) or mic_release not in ("low", "hold", "high"):
            raise ValueError("unknown microphone setting")
        self._mic, self._mic_release = mic, mic_release
        if mic:
            micsig.block()  # made now: making it mid-stream would stall the stream
        self.rebooted = False  # the restart-to-bootloader command was accepted
        self.mic_clock_log: list[int] = []  # every microphone clock change, in hertz; 0 = stopped
        self._stall_until = 0.0
        self._proto = (proto_major, proto_minor)
        self._faults = faults or Faults()
        self._data, self._hb = _Pty(), _Pty()
        self.data_path, self.hb_path = self._data.path, self._hb.path
        self._data_parser = p.FrameParser(max_payload=64)
        self._hb_parser = p.FrameParser(max_payload=64)
        self._tx = bytearray()
        self._hb_tx = bytearray()
        self._seq = {t: 0 for t in (p.T_INFO, p.T_EVENT, p.T_STATS, p.T_ACK, p.T_MIC_STATUS, p.T_HB_STATUS)}
        self._t_start = time.monotonic_ns()
        self._now = 0

        self._hello_ok = False
        self._streaming = False
        self._src = p.SRC_SYNTH
        self._mic_phase = ""
        self._mic_t = self._mic_clock_code = self._mic_clock_hz = self._mic_frame_us = 0
        self._mic_forced = self._mic_use = self._mic_frames_target = self._mic_polled = 0
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

    def stall(self, seconds: float) -> None:
        """Freeze for a while, as firmware would if something held up its main loop."""
        self._stall_until = time.monotonic() + seconds

    def run(self) -> None:
        while not self._stop.is_set():
            if time.monotonic() < self._stall_until:
                time.sleep(0.001)
                continue
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

    # ---- microphone stream ----

    def _mic_halves(self, periods: int) -> tuple[int, int]:
        """The first `periods` L samples and H samples as two big numbers, earliest period in
        the top bit. L is read at the end of the clock's low half, H at the end of the high half."""
        mask = (1 << periods) - 1
        data = int.from_bytes(micsig.stream(0, (periods + 7) // 8), "big") >> (-periods % 8)
        released = {"hold": data, "low": 0, "high": mask}[self._mic_release]
        if self._mic == "gnd":  # driven in the low half, released in the high half
            return data, released
        if self._mic == "vdd":  # driven in the high half, released in the next low half
            return released >> 1, data
        return 0, 0  # stuck

    def _mic_probe(self) -> p.MicStatus:
        n = p.MIC_PROBE_PERIODS
        mask = (1 << n) - 1
        low, high = self._mic_halves(n)
        next_low = (low << 1) & mask  # L of the following period, lined up with H
        pairs = mask & ~1  # the last H has no L after it
        counts = dict(
            ones_l=low.bit_count(), ones_h=high.bit_count(),
            lh_10=(low & ~high & mask).bit_count(), lh_01=(~low & high & mask).bit_count(),
            hl_10=(high & ~next_low & pairs).bit_count(), hl_01=(~high & next_low & pairs).bit_count())
        verdict = mic_verdict(n, **counts)
        use = self._mic_forced or {p.MIC_SELECT_GND: p.MIC_USE_L, p.MIC_SELECT_VDD: p.MIC_USE_H}.get(verdict, 0)
        return p.MicStatus(verdict, use, bool(self._mic_forced), self._mic_clock_code, self._mic_clock_hz,
                           n, **counts)

    def _mic_payload(self, n: int) -> bytes:
        """PDM frame n: the half of the clock that is being forwarded."""
        data_half = p.MIC_USE_L if self._mic == "gnd" else p.MIC_USE_H
        first = n * p.PDM_PAYLOAD
        if self._mic == "stuck":
            return bytes(p.PDM_PAYLOAD)
        if self._mic_use == data_half:
            return micsig.stream(first, p.PDM_PAYLOAD)
        if self._mic_release != "hold":  # the released half sits at one level
            return bytes([0x00 if self._mic_release == "low" else 0xFF]) * p.PDM_PAYLOAD
        if self._mic == "gnd":  # H is the same bit as L, held
            return micsig.stream(first, p.PDM_PAYLOAD)
        # vdd, reading L: each L is the bit before it, held. The data one period late.
        late = int.from_bytes(micsig.stream(first - 1, p.PDM_PAYLOAD + 1), "big") >> 1
        return late.to_bytes(p.PDM_PAYLOAD + 1, "big")[1:]

    def _mic_set_clock(self, hz: int) -> None:
        if not self.mic_clock_log or self.mic_clock_log[-1] != hz:
            self.mic_clock_log.append(hz)

    def _mic_poll(self) -> None:
        since = self._now - self._mic_t
        behind_us, self._mic_polled = self._now - self._mic_polled, self._now
        if self._mic_phase == "wake" and since >= 100_000:
            self._mic_set_clock(self._mic_clock_hz)
            self._mic_phase, self._mic_t = "settle", self._now
        elif self._mic_phase == "settle" and since >= 20_000:
            self._mic_phase, self._mic_t = "probe", self._now
        elif self._mic_phase == "probe" and since >= p.MIC_PROBE_PERIODS * 1_000_000 // self._mic_clock_hz:
            status = self._mic_probe()
            self._emit(p.T_MIC_STATUS, p.pack_mic_status(status))
            self._mic_use = status.use
            if status.use == p.MIC_USE_NONE:
                self._stream_stop()
                return
            self._mic_phase, self._mic_t = "run", self._now
        elif self._mic_phase == "run":
            if behind_us > CAPTURE_RING_US * 3 // 4:
                # The capture ring was overwritten while nobody read it. Every frame that
                # was being filled in that time is lost, and counted.
                lost = max(1, (self._now - self._mic_t) // self._mic_frame_us - self._gen[p.T_PDM])
                self._gen[p.T_PDM] += lost
                self._dropped[p.T_PDM] += lost
                self._ovf_unreported += lost
                self._gap[p.T_PDM] = True
                self._event(p.EV_MIC_OVERRUN, behind_us * self._mic_clock_hz // 16_000_000)
            for _ in range(MAX_FRAMES_PER_POLL):
                n = self._gen[p.T_PDM]
                if self._now < self._mic_t + (n + 1) * self._mic_frame_us:
                    break  # frame n is not complete yet
                self._gen[p.T_PDM] += 1
                flags = p.F_GAP if self._gap[p.T_PDM] else 0
                if self._emit(p.T_PDM, self._mic_payload(n), seq=n, flags=flags,
                              t_us=self._mic_t + n * self._mic_frame_us, is_stream=True):
                    self._gap[p.T_PDM] = False
                else:
                    self._gap[p.T_PDM] = True
                    self._dropped[p.T_PDM] += 1
                    self._ovf_unreported += 1
                if self._mic_frames_target and self._gen[p.T_PDM] >= self._mic_frames_target:
                    self._stream_stop()
                    return

    # ---- both kinds of stream ----

    def _stream_stop(self) -> None:
        if self._streaming:
            if self._src == p.SRC_MIC:
                self._mic_set_clock(0)
            self._streaming = False
            self._event(p.EV_STREAM_STOPPED, self._gen[p.T_PDM])

    def _stream(self) -> None:
        if not self._streaming:
            return
        timed_out = self._duration_us and (self._now - self._t0) >= self._duration_us
        if self._src == p.SRC_MIC:
            self._mic_poll()
        elif self._mode == p.MODE_REAL:
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
            if self._mic is None:
                return p.ST_NOT_IMPLEMENTED
            if payload[1] != p.MODE_REAL or payload[2] > 1 or payload[3] > p.MIC_USE_H:
                return p.ST_BAD_ARG
        elif payload[0] != p.SRC_SYNTH:
            return p.ST_BAD_ARG
        if self._streaming:
            return p.ST_BUSY
        duration_ms = int.from_bytes(payload[8:12], "little")
        if duration_ms > 3_600_000:
            return p.ST_BAD_ARG
        self._src, self._mode = payload[0], payload[1]
        self._seed = int.from_bytes(payload[4:8], "little")
        if self._src == p.SRC_MIC:
            self._duration_us = 0  # the microphone stream counts frames instead
            self._mic_clock_code, self._mic_forced = payload[2], payload[3]
            self._mic_clock_hz = p.MIC_CLOCKS_HZ[payload[2]]
            self._mic_frame_us = p.PDM_PAYLOAD * 8 * 1_000_000 // self._mic_clock_hz
            self._mic_frames_target = duration_ms * 1000 // self._mic_frame_us
            self._mic_phase, self._mic_t, self._mic_use = "wake", self._now, 0
            self._mic_polled = self._now
            self._mic_set_clock(p.MIC_WAKE_HZ)
        else:
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
                features=p.FEATURE_SYNTH | p.FEATURE_OUTPUTS | (p.FEATURE_MIC if self._mic else 0),
                build="sim"))
            status = p.ST_OK if self._hello_ok else p.ST_BAD_VERSION
        elif f.type == p.T_STREAM_START:
            status = self._stream_start(f.payload)
        elif f.type == p.T_STREAM_STOP:
            self._stream_stop()
        elif f.type == p.T_SET_OUTPUTS:
            status = self._set_outputs(f.payload)
        elif f.type == p.T_GET_STATS:
            self._send_stats()
        elif f.type == p.T_REBOOT:
            if not self._hello_ok:
                status = p.ST_BAD_VERSION
            elif len(f.payload) < 4 or int.from_bytes(f.payload[:4], "little") != p.REBOOT_MAGIC:
                status = p.ST_BAD_ARG
            else:
                self.rebooted = True
        else:
            return
        self._emit(p.T_ACK, p.pack_ack(f.type, status, f.seq))
        if f.type == p.T_STREAM_START and status == p.ST_OK:
            self._event(p.EV_STREAM_STARTED, self._mic_clock_hz if self._src == p.SRC_MIC else self._seed)
