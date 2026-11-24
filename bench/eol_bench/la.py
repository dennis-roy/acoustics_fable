"""Talking to the logic analyser (firmware_la/) and turning its counts into times and frequencies.

Times come from the analyser's own crystal (150 MHz system clock). Its tolerance is unknown, so a
frequency read here is a ratio to that crystal, not an absolute figure (review 10.1).
"""

from __future__ import annotations

import base64
import glob
import json
import time
from dataclasses import dataclass

from eol_link.transport import Port

BY_ID_GLOB = "/dev/serial/by-id/usb-Zynith_EOL_logic_analyser_*-if00"


class AnalyserError(Exception):
    pass


def find_port() -> str | None:
    found = sorted(glob.glob(BY_ID_GLOB))
    return found[0] if len(found) == 1 else None


class Analyser:
    def __init__(self, port: Port) -> None:
        self._port = port
        self._buf = b""

    def _line(self, deadline: float) -> str:
        while b"\n" not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AnalyserError("no answer from the analyser")
            self._buf += self._port.read(left)
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode(errors="replace").strip()

    def command(self, text: str, timeout: float = 5.0, on_armed=None) -> dict:
        self._port.write((text + "\n").encode())
        deadline = time.monotonic() + timeout
        while True:
            line = self._line(deadline)
            if line.startswith("{"):  # anything else is noise from before
                reply = json.loads(line)
                if "error" in reply:
                    raise AnalyserError(reply["error"])
                if reply.get("armed") == "counts":
                    if on_armed is not None:
                        on_armed()
                    continue
                return reply

    def burst(self, samples: int, div: int, trigger: int | None, rising: bool = True, timeout_ms: int = 2000) -> tuple[dict, bytes]:
        head = self.command(f"burst {samples} {div} {'-' if trigger is None else trigger} {'r' if rising else 'f'} {timeout_ms}",
                            timeout=timeout_ms / 1000 + 5)
        chunks, deadline = [], time.monotonic() + 30
        while (line := self._line(deadline)) != "END":
            chunks.append(base64.b64decode(line))
        data = b"".join(chunks)
        if len(data) != head["samples"]:
            raise AnalyserError(f"burst arrived incomplete: {len(data)} of {head['samples']} samples")
        return head, data


# ---- pulse widths ----

@dataclass
class CountsReport:
    gates: int
    gate_us: float
    freq_hz: float
    min_count: int
    max_count: int
    ref_freq_hz: float | None
    change_ms: float | None
    after_freq_hz: float | None
    after_gates: int
    after_min: int
    after_max: int
    problems: list[str]


def counts_report(j: dict, expected_gates: int) -> CountsReport:
    if j.get("mode") != "counts":
        raise AnalyserError("counting mode requires analyser firmware 0.2.0 or newer")
    integer_fields = ("clk_hz", "gate_cycles", "gates", "sum", "min", "max", "zero_gates",
                      "ref_n", "ref_sum", "change_at", "after_n", "after_sum", "after_min", "after_max")
    if any(type(j.get(key)) is not int for key in integer_fields):
        raise AnalyserError("incomplete or malformed counting report")
    if any(type(j.get(key)) is not bool for key in ("overrun", "timed_out")):
        raise AnalyserError("invalid counting status flags")
    if j["clk_hz"] <= 0 or j["gate_cycles"] != 1500:
        raise AnalyserError("invalid counting clock or gate length")
    problems = []
    if j["overrun"]:
        problems.append("DMA overrun, invalid count or PIO FIFO stall: measurement invalid")
    if j["timed_out"]:
        problems.append("capture timed out before all gates arrived")
    if j["gates"] != expected_gates:
        problems.append(f"incomplete capture: {j['gates']} of {expected_gates} gates")
    if not j["gates"] or not j["sum"]:
        problems.append("no signal")
    if j["zero_gates"]:
        problems.append("one or more gates had no edges: clock stopped or input too slow")
    if not 0 <= j["min"] <= j["max"] <= 187:
        problems.append("count outside the PIO sampling limit")
    for count_key, sum_key, lo, hi in (("gates", "sum", j["min"], j["max"]),
                                      ("ref_n", "ref_sum", j["min"], j["max"]),
                                      ("after_n", "after_sum", j["after_min"], j["after_max"])):
        if not 0 <= j[count_key] <= j["gates"] or not 0 <= lo <= hi <= 187 or not lo * j[count_key] <= j[sum_key] <= hi * j[count_key]:
            problems.append(f"inconsistent {count_key} statistics")
    if not 0 <= j["zero_gates"] <= j["gates"] or j["ref_n"] != min(64, j["gates"]):
        problems.append("inconsistent reference or zero-gate count")
    change = j["change_at"]
    if change != -1 and not 64 <= change < j["gates"]:
        problems.append("invalid frequency change index")
    expected_after = max(0, j["gates"] - (change + 16 if change >= 0 else 64))
    if j["after_n"] != expected_after:
        problems.append("inconsistent post-settle gate count")
    scale = j["clk_hz"] / j["gate_cycles"]
    return CountsReport(
        j["gates"], 1e6 / scale, scale * j["sum"] / j["gates"] if j["gates"] else 0,
        j["min"], j["max"], scale * j["ref_sum"] / j["ref_n"] if j["ref_n"] else None,
        j["change_at"] / scale * 1000 if j["change_at"] >= 0 else None,
        scale * j["after_sum"] / j["after_n"] if j["after_n"] else None,
        j["after_n"], j["after_min"], j["after_max"], problems)

@dataclass
class WidthsReport:
    periods: int
    freq_hz: float  # mean, relative to the analyser's crystal
    duty: float  # share of each period that is high
    min_high_ns: float
    min_low_ns: float
    max_high_ns: float
    max_low_ns: float
    ref_freq_hz: float | None  # mean of the first periods
    change_ms: float | None  # first rising edge to the first period of another frequency
    after_freq_hz: float | None
    min_near_change_ns: float | None
    problems: list[str]


def widths_report(j: dict) -> WidthsReport:
    clk = j["clk_hz"]
    ns = 1e9 / clk
    problems = []
    if j["overrun"]:
        problems.append("the analyser fell behind and stopped: results are not valid")
    if j["timed_out"]:
        problems.append("no signal on the input before the timeout: check the wiring and that the clock runs")
    n = j["periods"]
    if n == 0:
        return WidthsReport(0, 0, 0, 0, 0, 0, 0, None, None, None, None, problems + ["no signal"])
    period = j["total_cycles"] / n
    ref = clk / (j["ref_sum"] / j["ref_periods"]) if j["ref_periods"] else None
    change = j["change_at"] >= 0
    return WidthsReport(
        n, clk / period, j["high"]["sum"] / (j["high"]["sum"] + j["low"]["sum"]),
        j["high"]["min"] * ns, j["low"]["min"] * ns, j["high"]["max"] * ns, j["low"]["max"] * ns, ref,
        j["cycles_to_change"] / clk * 1000 if change else None,
        clk / (j["after_sum"] / j["after_periods"]) if change and j["after_periods"] else None,
        min(j["min_high_near_change"], j["min_low_near_change"]) * ns if change else None, problems)


# ---- bursts ----

def edges(data: bytes, ch: int) -> list[tuple[int, int]]:
    """(sample index, new level) of every change of one channel."""
    out, last = [], (data[0] >> ch) & 1 if data else 0
    for i in range(1, len(data)):
        v = (data[i] >> ch) & 1
        if v != last:
            out.append((i, v))
            last = v
    return out


def data_delay(data: bytes, ns_per_sample: float, clk_ch: int, data_ch: int) -> dict:
    """For every change of the data line: time since the last rising and since the last falling clock edge."""
    clk = edges(data, clk_ch)
    since = {1: [], 0: []}
    ci, last = 0, {1: None, 0: None}
    for i, _ in edges(data, data_ch):
        while ci < len(clk) and clk[ci][0] <= i:
            last[clk[ci][1]] = clk[ci][0]
            ci += 1
        for level in (1, 0):
            if last[level] is not None:
                since[level].append((i - last[level]) * ns_per_sample)

    def summary(xs):
        xs = sorted(xs)
        return {"changes": len(xs), "min_ns": xs[0], "median_ns": xs[len(xs) // 2], "max_ns": xs[-1]} if xs else {"changes": 0}

    rising, falling = summary(since[1]), summary(since[0])
    follows = None
    if rising.get("changes") and falling.get("changes"):
        follows = "rising" if rising["median_ns"] < falling["median_ns"] else "falling"
    return {"after_rising": rising, "after_falling": falling, "data_follows": follows}
