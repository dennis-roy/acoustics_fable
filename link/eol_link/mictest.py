"""Microphone test: is there a live microphone, and does its stream look right?

With no reference instruments, "right" means things that can be checked without them:

  - the Pico's SELECT probe finds a microphone, and says which way SELECT is set;
  - every frame arrives (sequence numbers, CRCs);
  - about half the bits are 1 and there are no long runs (pdmcheck.py);
  - the level in each frequency band, reported for comparison between runs.

Absolute sound level is not measured. See section 10 of the Phase 0 review.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field

from . import pdmspec
from . import protocol as p
from .client import PicoLink
from .pdmcheck import PdmCheck, check_pdm

SETTLE_S = 0.5  # the start of the stream is received but left out of the analysis


@dataclass
class MicTestResult:
    seconds: float
    clock_hz: int = 0
    status: p.MicStatus | None = None
    frames: int = 0
    seq_gaps: int = 0
    gap_flags: int = 0
    overruns: int = 0  # times the Pico's capture fell behind
    overflow_events: int = 0
    synth_frames: int = 0  # frames marked "synthetic": not from a microphone
    pico_generated: int | None = None
    pico_dropped: int | None = None
    crc_errors: int = 0
    header_errors: int = 0
    junk_bytes: int = 0
    ended: bool = False
    analysed_bytes: int = 0
    pdm: PdmCheck | None = None
    bands: list[pdmspec.BandLevel] | None = None  # None: numpy is not installed
    saved: str | None = None
    sha256: str | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def run_mictest(link: PicoLink, seconds: float, *, clock: int = 0, select: int = 0,
                save_dir: str | None = None, mic_id: str = "") -> MicTestResult:
    """Call link.hello() first. `select`: 0 lets the Pico find out; MIC_USE_L or MIC_USE_H forces it."""
    res = MicTestResult(seconds, clock_hz=p.MIC_CLOCKS_HZ[clock])
    info = link.info
    if info is None or info.proto_minor < 1 or not info.features & p.FEATURE_MIC:
        res.failures.append("this Pico's firmware has no microphone capture (it needs protocol 1.1 or later)")
        return res

    link.command(p.T_STREAM_STOP)
    ps = link.parser
    before = (ps.crc_errors, ps.header_errors, ps.junk_bytes)
    link.command(p.T_STREAM_START, p.pack_stream_start(p.SRC_MIC, p.MODE_REAL, 0, int(seconds * 1000),
                                                       clock=clock, select=select))
    payloads: list[bytes] = []
    expected = 0
    started = False
    for f in link.frames(timeout=2.0):
        if f.type == p.T_EVENT:
            code, arg = p.unpack_event(f.payload)
            if code == p.EV_STREAM_STARTED:
                started = True
            elif code == p.EV_MIC_OVERRUN and started:
                res.overruns += 1
            elif code == p.EV_TX_OVERFLOW and started:
                res.overflow_events += 1
            elif code == p.EV_STREAM_STOPPED and started:
                res.pico_generated = arg
                res.ended = True
                break
        elif f.type == p.T_MIC_STATUS and started:
            res.status = p.unpack_mic_status(f.payload)
        elif f.type == p.T_PDM and started:
            res.seq_gaps += (f.seq - expected) & 0xFFFFFFFF
            expected = f.seq + 1
            res.gap_flags += bool(f.flags & p.F_GAP)
            res.synth_frames += bool(f.flags & p.F_SYNTH)
            res.frames += 1
            payloads.append(f.payload)

    res.crc_errors, res.header_errors, res.junk_bytes = (
        now - b for now, b in zip((ps.crc_errors, ps.header_errors, ps.junk_bytes), before))
    if res.ended:
        res.pico_dropped = link.get_stats().pdm_dropped
        res.seq_gaps += abs(res.pico_generated - expected)
    else:
        link.command(p.T_STREAM_STOP)  # do not leave the microphone clock running

    frame_s = p.PDM_PAYLOAD * 8 / res.clock_hz
    data = b"".join(payloads[int(SETTLE_S / frame_s):])
    res.analysed_bytes = len(data)
    if data:
        res.pdm = check_pdm(data)
        if pdmspec.have_numpy() and len(data) * 8 >= pdmspec.NFFT:
            res.bands = pdmspec.band_levels(data, res.clock_hz)
    if save_dir and payloads:
        res.saved, res.sha256 = _save(save_dir, b"".join(payloads), res, info, mic_id)
    _judge(res)
    return res


def _save(save_dir: str, raw: bytes, res: MicTestResult, info: p.Info, mic_id: str) -> tuple[str, str]:
    """Writes the raw 1-bit stream and a small description next to it. Returns (path, SHA-256)."""
    os.makedirs(save_dir, exist_ok=True)
    name = time.strftime("mic_%Y%m%dT%H%M%SZ", time.gmtime())
    path = os.path.join(save_dir, name + ".pdm")
    digest = hashlib.sha256(raw).hexdigest()
    with open(path, "wb") as fh:
        fh.write(raw)
    st = res.status
    with open(os.path.join(save_dir, name + ".json"), "w") as fh:
        json.dump({
            "file": name + ".pdm",
            "sha256": digest,
            "format": "raw 1-bit stream; bit 7 of byte 0 is the earliest sample",
            "clock_hz": res.clock_hz,
            "bytes": len(raw),
            "complete": res.ended and not res.seq_gaps,
            "microphone_id": mic_id or "not recorded",
            "pico_serial": info.unique_id,
            "firmware": f"{info.fw_version} (build {info.build})",
            "protocol": f"{info.proto_major}.{info.proto_minor}",
            "filter": "none: this is the unfiltered stream",
            "select_probe": None if st is None else {
                "verdict": st.verdict_name, "forced": st.forced, "periods": st.periods,
                "ones_l": st.ones_l, "ones_h": st.ones_h,
                "lh_10": st.lh_10, "lh_01": st.lh_01, "hl_10": st.hl_10, "hl_01": st.hl_01},
        }, fh, indent=2)
        fh.write("\n")
    return path, digest


def _judge(res: MicTestResult) -> None:
    fail = res.failures.append
    st = res.status
    if st is None:
        fail("the Pico sent no probe result")
    elif st.verdict not in (p.MIC_SELECT_GND, p.MIC_SELECT_VDD):
        fail(f"probe: {st.verdict_name}")
    if not res.ended:
        fail("the stream never finished: no 'stream stopped' event from the Pico")
    if res.frames == 0:
        fail("no microphone frames received")
    for name, value in (
        ("frames missing (sequence gaps)", res.seq_gaps),
        ("CRC errors", res.crc_errors),
        ("header errors", res.header_errors),
        ("stray bytes between frames", res.junk_bytes),
        ("frames flagged GAP by the Pico", res.gap_flags),
        ("times the Pico's capture fell behind", res.overruns),
        ("overflow events from the Pico", res.overflow_events),
        ("frames the Pico says it dropped", res.pico_dropped or 0),
        ("frames marked synthetic", res.synth_frames),
    ):
        if value:
            fail(f"{name}: {value}")
    if res.frames and res.pdm is None:
        fail(f"capture too short to analyse: it needs more than {SETTLE_S} s")
    for reason in res.pdm.reasons if res.pdm else ():
        fail(reason)


def format_result(res: MicTestResult) -> str:
    st = res.status
    lines = [f"clock            {res.clock_hz / 1e6:.3f} MHz"]
    if st:
        n = max(st.periods, 1)
        lines += [
            f"probe            {st.verdict_name}" + (" [read side forced by the command]" if st.forced else ""),
            f"  periods        {st.periods}",
            f"  ones           L {st.ones_l / n:.1%}, H {st.ones_h / n:.1%}",
            f"  L then H       1,0: {st.lh_10}   0,1: {st.lh_01}",
            f"  H then next L  1,0: {st.hl_10}   0,1: {st.hl_01}",
        ]
    lines += [
        f"frames           {res.frames} received (Pico generated {res.pico_generated})",
        f"missing frames   {res.seq_gaps}",
        f"CRC errors       {res.crc_errors}",
        f"stray bytes      {res.junk_bytes}",
        f"Pico dropped     {res.pico_dropped}",
    ]
    if res.pdm:
        run = f"{res.pdm.longest_run_bits} bits" if res.pdm.longest_run_bits else "under 32 bits"
        lines += [
            f"analysed         {res.analysed_bytes * 8 / res.clock_hz:.2f} s (after the first {SETTLE_S} s)",
            f"bits that are 1  {res.pdm.ones_fraction:.2%}",
            f"longest run      {run}",
        ]
    if res.bands:
        for b in res.bands:
            lines.append(f"band {b.low_hz / 1000:5.1f} to {b.high_hz / 1000:4.0f} kHz  {b.dbfs:6.1f} dBFS"
                         f"   strongest at {b.peak_hz / 1000:.2f} kHz")
    elif res.pdm:
        lines.append("band levels      not computed: python3-numpy is not installed")
    if res.saved:
        lines += [f"saved            {res.saved}", f"SHA-256          {res.sha256}"]
    lines.append("RESULT           " + ("PASS" if res.passed else "FAIL"))
    return "\n".join(lines + [f"  - {reason}" for reason in res.failures])
