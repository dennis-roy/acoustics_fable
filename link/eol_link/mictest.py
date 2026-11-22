"""Microphone test: is there a live microphone, and does its stream look right?

With no reference instruments, "right" means things that can be checked without them:

  - the Pico's SELECT probe finds a microphone, and says which way SELECT is set;
  - every frame arrives (sequence numbers, CRCs);
  - about half the bits are 1 and there are no long runs (pdmcheck.py);
  - the level in each frequency band, reported for comparison between runs.

Absolute sound level is not measured. See section 10 of the Phase 0 review.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import pdmspec
from .capture import DEFAULT_PREROLL_S, MicCapture, capture_mic
from .client import PicoLink
from .pdmcheck import PdmCheck, check_pdm
from .recording import write_recording

SETTLE_S = DEFAULT_PREROLL_S  # the start of the stream is received but left out of the analysis


@dataclass
class MicTestResult:
    seconds: float
    capture: MicCapture | None = None
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
    """Call link.hello() first. `seconds` includes the first SETTLE_S, which is not analysed.
    `select`: 0 lets the Pico find out; MIC_USE_L or MIC_USE_H forces it."""
    res = MicTestResult(seconds)
    if seconds <= SETTLE_S:
        res.failures.append(f"capture too short to analyse: it needs more than {SETTLE_S} s")
        return res
    try:
        cap = capture_mic(link, seconds - SETTLE_S, preroll_s=SETTLE_S, clock=clock, select=select)
    except ValueError as exc:
        res.failures.append(str(exc))
        return res
    res.capture = cap
    if cap.raw:
        res.pdm = check_pdm(cap.raw)
        if pdmspec.have_numpy() and len(cap.raw) * 8 >= pdmspec.NFFT:
            res.bands = pdmspec.band_levels(cap.raw, cap.clock_hz)
    if save_dir and cap.raw:
        res.saved, res.sha256 = _save(save_dir, cap, mic_id)
    res.failures += cap.problems()
    if cap.frames and res.pdm is None:
        res.failures.append("no microphone data after the pre-roll")
    res.failures += list(res.pdm.reasons if res.pdm else ())
    return res


def _save(save_dir: str, cap: MicCapture, mic_id: str) -> tuple[str, str]:
    """Writes the recording (after the pre-roll) and its description. Returns (path, SHA-256)."""
    st, info = cap.status, cap.info
    meta = {
        "purpose": "bench",
        "clock_hz": cap.clock_hz,
        "seconds": len(cap.raw) * 8 / cap.clock_hz,
        "captured_utc": cap.started_utc,
        "preroll_s": cap.preroll_s,
        "complete": not cap.problems(),
        "microphone_id": mic_id or "not recorded",
        "pico": {"serial": info.unique_id, "firmware": info.fw_version, "build": info.build,
                 "protocol": f"{info.proto_major}.{info.proto_minor}"},
        "filter": "none: this is the unfiltered stream",
        "select_probe": None if st is None else {
            "verdict": st.verdict_name, "forced": st.forced, "periods": st.periods,
            "ones_l": st.ones_l, "ones_h": st.ones_h,
            "lh_10": st.lh_10, "lh_01": st.lh_01, "hl_10": st.hl_10, "hl_01": st.hl_01},
    }
    name = time.strftime("mic_%Y%m%dT%H%M%SZ", time.gmtime())
    path, digest = write_recording(save_dir, name, cap.raw, meta)
    return str(path), digest


def format_result(res: MicTestResult) -> str:
    cap = res.capture
    if cap is None:
        return "\n".join(["RESULT           FAIL"] + [f"  - {reason}" for reason in res.failures])
    st = cap.status
    lines = [f"clock            {cap.clock_hz / 1e6:.3f} MHz"]
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
        f"frames           {cap.frames} received (Pico generated {cap.pico_generated})",
        f"missing frames   {cap.seq_gaps}",
        f"CRC errors       {cap.crc_errors}",
        f"stray bytes      {cap.junk_bytes}",
        f"Pico dropped     {cap.pico_dropped}",
    ]
    if res.pdm:
        run = f"{res.pdm.longest_run_bits} bits" if res.pdm.longest_run_bits else "under 32 bits"
        lines += [
            f"analysed         {len(cap.raw) * 8 / cap.clock_hz:.2f} s (after the first {cap.preroll_s} s)",
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
