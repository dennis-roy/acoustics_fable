"""The INVALID checks on one capture (review section 4.8), and its levels.

A capture is INVALID if any of these fails. The screen shows which (decision D7).

  1. the data line is stuck for the whole capture       (pdmcheck)
  2. a run of equal bits is longer than the limit      (pdmcheck; overload)
  3. the share of 1 bits is outside its limits         (pdmcheck)
  4. the peak of the filtered signal is above the limit (needs the decimation filter)
  5. any frame was lost or corrupt, or the probe found no microphone

The limits are starting values, NOT VERIFIED (review 4.8). Bench item 39 (13.3) found
that check 2 starts to fire exactly when peaks reach full scale. Every evaluation
records the limits it used, so a later change of limits stays traceable.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from eol_dsp import get_filter
from eol_dsp.decimate import decimate_bits, level_dbfs
from eol_link import pdmspec
from eol_link.capture import MicCapture
from eol_link.pdmcheck import check_pdm


@dataclass(frozen=True)
class Limits:
    ones_min: float = 0.45
    ones_max: float = 0.55
    max_run_bits: int = 32
    peak_max_dbfs: float = -3.0


@dataclass
class Evaluation:
    valid: bool
    reasons: list[str]
    limits: Limits
    ones_fraction: float | None = None
    longest_run_bits: int | None = None  # 0 = under 32 bits
    peak_dbfs: float | None = None  # of the filtered signal; 0 dBFS = full scale
    rms_dbfs: float | None = None  # 0 dBFS = a full-scale sine
    bands: list[dict] = field(default_factory=list)
    filter_name: str | None = None
    filter_sha256: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["limits"] = asdict(self.limits)
        return d


def _finite(x: float) -> float | None:
    return round(x, 2) if math.isfinite(x) else None


def evaluate(cap: MicCapture, limits: Limits = Limits(), decimation: int = 16) -> Evaluation:
    reasons = list(cap.problems())
    ev = Evaluation(False, reasons, limits)
    if not cap.raw:
        if not reasons:
            reasons.append("no microphone data after the pre-roll")
        return ev

    pdm = check_pdm(cap.raw, ones_min=limits.ones_min, ones_max=limits.ones_max, max_run_bits=limits.max_run_bits)
    ev.ones_fraction, ev.longest_run_bits = round(pdm.ones_fraction, 5), pdm.longest_run_bits
    reasons += pdm.reasons

    try:
        spec = get_filter(cap.clock_hz, decimation)
    except ValueError as exc:
        reasons.append(f"peak check not possible: {exc}")
    else:
        samples = decimate_bits(cap.raw, spec)
        peak, rms = level_dbfs(samples.inner())
        ev.peak_dbfs, ev.rms_dbfs = _finite(peak), _finite(rms)
        ev.filter_name, ev.filter_sha256 = spec.name, spec.sha256
        if peak > limits.peak_max_dbfs:
            reasons.append(f"peak {peak:.1f} dBFS is above {limits.peak_max_dbfs:g} dBFS: "
                           "the microphone is too close or the sound too loud")

    if pdmspec.have_numpy() and len(cap.raw) * 8 >= pdmspec.NFFT:
        ev.bands = [{"low_hz": b.low_hz, "high_hz": b.high_hz, "dbfs": round(b.dbfs, 2),
                     "peak_hz": round(b.peak_hz, 1)} for b in pdmspec.band_levels(cap.raw, cap.clock_hz)]
    ev.valid = not reasons
    return ev
