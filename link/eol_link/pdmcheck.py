"""Checks on the raw 1-bit microphone stream that make a capture INVALID.

A 1-bit (PDM) stream from a working, unsaturated microphone has about half its bits set
and never stays on one value for long. Three things break that:

  - no microphone, a broken wire or a stopped clock: the line is stuck, one endless run;
  - the microphone overloaded by sound that is too loud: long runs of one value;
  - a DC fault: far more ones than zeros, or the reverse.

The limits below are starting values and are NOT VERIFIED. The microphone's datasheet
does not describe its modulator, so the run length that marks real overload has to be
measured on the bench (Phase 0 review, bench section, "overload limits"). They are
arguments so the bench result can be put in configuration.

Bit order: bit 7 of byte 0 is the earliest sample (protocol/README.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_ONES_MIN = 0.45
DEFAULT_ONES_MAX = 0.55
DEFAULT_MAX_RUN_BITS = 32  # 10 microseconds at 3.2 MHz

# A run of 32 or more equal bits always contains at least 3 whole bytes of 0x00 or 0xFF.
_LONG_RUN = re.compile(rb"\x00{3,}|\xff{3,}")


@dataclass(frozen=True)
class PdmCheck:
    bits: int
    ones_fraction: float
    longest_run_bits: int  # exact when 32 or more; otherwise reported as 0 ("short")
    reasons: tuple[str, ...]  # empty = the capture passes these checks

    @property
    def valid(self) -> bool:
        return not self.reasons


def _run_length(data: bytes, start: int, end: int) -> int:
    """Exact length in bits of the run whose whole bytes are data[start:end]."""
    value = data[start]  # 0x00 or 0xFF
    bits = 8 * (end - start)
    if start > 0:  # equal bits at the low end of the byte before
        prev = data[start - 1] ^ value
        bits += 8 if prev == 0 else ((prev & -prev).bit_length() - 1)
    if end < len(data):  # equal bits at the high end of the byte after
        nxt = data[end] ^ value
        bits += 8 if nxt == 0 else 8 - nxt.bit_length()
    return bits


def check_pdm(
    data: bytes,
    *,
    ones_min: float = DEFAULT_ONES_MIN,
    ones_max: float = DEFAULT_ONES_MAX,
    max_run_bits: int = DEFAULT_MAX_RUN_BITS,
) -> PdmCheck:
    if max_run_bits < 32:
        raise ValueError("max_run_bits below 32 is not supported by this detector")
    if not data:
        return PdmCheck(0, 0.0, 0, ("no data",))

    bits = 8 * len(data)
    ones = int.from_bytes(data, "big").bit_count() / bits
    longest = max((_run_length(data, m.start(), m.end()) for m in _LONG_RUN.finditer(data)), default=0)
    if longest < 32:
        longest = 0

    reasons = []
    if longest >= bits:
        reasons.append(f"data line stuck at {data[0] & 1}: no microphone, broken wire or no clock")
    elif longest > max_run_bits:
        reasons.append(f"run of {longest} equal bits (limit {max_run_bits}): microphone overload or a glitch")
    if not ones_min <= ones <= ones_max:
        reasons.append(f"{ones:.1%} of bits are 1 (allowed {ones_min:.0%} to {ones_max:.0%})")
    return PdmCheck(bits, ones, longest, tuple(reasons))
