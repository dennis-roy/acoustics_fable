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

    def command(self, text: str, timeout: float = 5.0) -> dict:
        self._port.write((text + "\n").encode())
        deadline = time.monotonic() + timeout
        while True:
            line = self._line(deadline)
            if line.startswith("{"):  # anything else is noise from before
                reply = json.loads(line)
                if "error" in reply:
                    raise AnalyserError(reply["error"])
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
