"""One microphone capture from the Pico: start the stream, receive every frame, account for it.

Used by the microphone test (mictest.py) and by the acquisition service (../acquire).
It only receives and counts. Whether the capture is good enough is decided elsewhere.

Pre-roll: the first part of the stream is received and checked for loss but left out of
the recording. On the bench the microphone's output settles below 200 Hz for about
0.4 s after it wakes (review section 13.5), so 0.5 s is the usual pre-roll.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import protocol as p
from .client import PicoLink

DEFAULT_PREROLL_S = 0.5


@dataclass
class MicCapture:
    seconds: float  # asked for, after the pre-roll
    preroll_s: float
    clock_hz: int
    started_utc: str  # wall clock of this computer when the stream was asked for
    info: p.Info
    raw: bytes = b""  # the recording: PDM payload after the pre-roll, bit 7 of byte 0 first
    status: p.MicStatus | None = None
    first_t_us: int | None = None  # Pico clock at the first bit of `raw`
    frames: int = 0  # all PDM frames received, pre-roll included
    preroll_frames: int = 0
    seq_gaps: int = 0
    gap_flags: int = 0
    overruns: int = 0
    overflow_events: int = 0
    synth_frames: int = 0
    pico_generated: int | None = None
    pico_dropped: int | None = None
    crc_errors: int = 0
    header_errors: int = 0
    junk_bytes: int = 0
    ended: bool = False

    @property
    def frame_s(self) -> float:
        return p.PDM_PAYLOAD * 8 / self.clock_hz

    def problems(self) -> list[str]:
        """Everything about the probe and the link that makes this capture unusable."""
        out = []
        st = self.status
        if st is None:
            out.append("the Pico sent no probe result")
        elif st.verdict not in (p.MIC_SELECT_GND, p.MIC_SELECT_VDD):
            out.append(f"probe: {st.verdict_name}")
        if not self.ended:
            out.append("the stream never finished: no 'stream stopped' event from the Pico")
        if self.frames == 0:
            out.append("no microphone frames received")
        for name, value in (
            ("frames missing (sequence gaps)", self.seq_gaps),
            ("CRC errors", self.crc_errors),
            ("header errors", self.header_errors),
            ("stray bytes between frames", self.junk_bytes),
            ("frames flagged GAP by the Pico", self.gap_flags),
            ("times the Pico's capture fell behind", self.overruns),
            ("overflow events from the Pico", self.overflow_events),
            ("frames the Pico says it dropped", self.pico_dropped or 0),
            ("frames marked synthetic", self.synth_frames),
        ):
            if value:
                out.append(f"{name}: {value}")
        return out


def capture_mic(link: PicoLink, seconds: float, *, preroll_s: float = DEFAULT_PREROLL_S, clock: int = 0,
                select: int = 0) -> MicCapture:
    """Records `seconds` of microphone stream after `preroll_s` of pre-roll. Call link.hello() first.

    clock: 0 = 3.2 MHz, 1 = 4.0 MHz. select: 0 lets the Pico find out which half of the
    clock carries the data; MIC_USE_L or MIC_USE_H forces it."""
    if link.info is None or link.info.proto_minor < 1 or not link.info.features & p.FEATURE_MIC:
        raise ValueError("this Pico's firmware has no microphone capture (it needs protocol 1.1 or later)")
    if seconds <= 0 or preroll_s < 0:
        raise ValueError("the capture must be longer than 0 s and the pre-roll not negative")
    cap = MicCapture(seconds, preroll_s, p.MIC_CLOCKS_HZ[clock],
                     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), link.info)
    cap.preroll_frames = int(preroll_s / cap.frame_s)
    total_ms = int(round((preroll_s + seconds) * 1000))

    link.command(p.T_STREAM_STOP)  # nothing left over from an earlier session
    ps = link.parser
    before = (ps.crc_errors, ps.header_errors, ps.junk_bytes)
    link.command(p.T_STREAM_START, p.pack_stream_start(p.SRC_MIC, p.MODE_REAL, 0, total_ms,
                                                       clock=clock, select=select))
    kept: list[bytes] = []
    expected = 0
    started = False
    for f in link.frames(timeout=2.0):
        if f.type == p.T_EVENT:
            code, arg = p.unpack_event(f.payload)
            if code == p.EV_STREAM_STARTED:
                started = True
            elif code == p.EV_MIC_OVERRUN and started:
                cap.overruns += 1
            elif code == p.EV_TX_OVERFLOW and started:
                cap.overflow_events += 1
            elif code == p.EV_STREAM_STOPPED and started:
                cap.pico_generated = arg
                cap.ended = True
                break
        elif f.type == p.T_MIC_STATUS and started:
            cap.status = p.unpack_mic_status(f.payload)
        elif f.type == p.T_PDM and started:
            cap.seq_gaps += (f.seq - expected) & 0xFFFFFFFF
            expected = f.seq + 1
            cap.gap_flags += bool(f.flags & p.F_GAP)
            cap.synth_frames += bool(f.flags & p.F_SYNTH)
            cap.frames += 1
            if f.seq >= cap.preroll_frames:
                if cap.first_t_us is None:
                    cap.first_t_us = f.t_us
                kept.append(f.payload)

    cap.crc_errors, cap.header_errors, cap.junk_bytes = (
        now - b for now, b in zip((ps.crc_errors, ps.header_errors, ps.junk_bytes), before))
    if cap.ended:
        cap.pico_dropped = link.get_stats().pdm_dropped
        cap.seq_gaps += abs(cap.pico_generated - expected)
    else:
        link.command(p.T_STREAM_STOP)  # never leave the microphone clock running
    cap.raw = b"".join(kept)
    return cap
