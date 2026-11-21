"""Link proof with synthetic data (bench items 16 to 19 of the Phase 0 review).

The Pico sends a test pattern both ends can generate. Every frame is checked for
sequence, CRC and content, so loss or corruption cannot pass unnoticed.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field

from . import protocol as p
from .client import PicoLink

MAX_RATE_PASS_BYTES_PER_S = 650_000  # bench item 16
REAL_RATE_TOLERANCE = 0.02


@dataclass
class LinkTestResult:
    mode: int
    seconds: float = 0.0
    frames: dict[int, int] = field(default_factory=lambda: {p.T_PDM: 0, p.T_TACH: 0})
    stream_bytes: int = 0
    seq_gaps: int = 0  # frames missing, judged by sequence numbers
    content_errors: int = 0  # frames whose CRC was right but whose data was wrong
    gap_flags: int = 0  # frames the Pico marked "something was dropped before this"
    overflow_events: int = 0
    pico_generated: int | None = None  # PDM frames the Pico says it made
    pico_dropped: int | None = None  # stream frames the Pico says it dropped
    crc_errors: int = 0
    header_errors: int = 0
    junk_bytes: int = 0
    ended: bool = False  # the Pico's "stream stopped" event arrived
    rate_bytes_per_s: float = 0.0
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


DEFAULT_SEED = 0x1234ABCD


class LinkTest:
    """One synthetic stream: start() it, then finish() receives it and checks every frame."""

    def __init__(self, link: PicoLink, mode: int = p.MODE_REAL, seed: int = DEFAULT_SEED,
                 min_max_rate: float = MAX_RATE_PASS_BYTES_PER_S) -> None:
        """min_max_rate: bytes/s a maximum-rate run must reach. It is a target for the real
        USB link; pass 0 for a simulator, whose speed says nothing about USB."""
        self._link, self._mode, self._seed, self._min_max_rate = link, mode, seed, min_max_rate
        self._errors_before = (0, 0, 0)

    def _parser_errors(self) -> tuple[int, int, int]:
        ps = self._link.parser
        return ps.crc_errors, ps.header_errors, ps.junk_bytes

    def start(self, seconds: float) -> None:
        """Call link.hello() first."""
        link = self._link
        link.command(p.T_STREAM_STOP)  # in case an earlier session left a stream running
        # Errors are counted from here. Anything earlier may be the tail of an old session.
        self._errors_before = self._parser_errors()
        link.command(p.T_STREAM_START, p.pack_stream_start(p.SRC_SYNTH, self._mode, self._seed, int(seconds * 1000)))

    def finish(self) -> LinkTestResult:
        link, seed = self._link, self._seed
        res = LinkTestResult(self._mode)
        synth = p.SynthSource(seed)
        expected = {p.T_PDM: 0, p.T_TACH: 0}
        started = False
        t_first = t_last = 0.0
        first_bytes = 0
        overhead = p.HDR_LEN + p.CRC_LEN
        for f in link.frames(timeout=2.0):
            if f.type == p.T_EVENT:
                code, arg = p.unpack_event(f.payload)
                if code == p.EV_STREAM_STARTED and arg == seed:
                    started = True
                elif code == p.EV_TX_OVERFLOW and started:
                    res.overflow_events += 1
                elif code == p.EV_STREAM_STOPPED and started:
                    res.pico_generated = arg
                    res.ended = True
                    break
            elif f.type in expected and started:
                t_last = time.monotonic()
                if not t_first:
                    t_first = t_last
                    first_bytes = len(f.payload) + overhead
                res.seq_gaps += (f.seq - expected[f.type]) & 0xFFFFFFFF
                expected[f.type] = f.seq + 1
                res.gap_flags += bool(f.flags & p.F_GAP)
                res.content_errors += f.payload != synth.payload(f.type, f.seq)
                res.frames[f.type] += 1
                res.stream_bytes += len(f.payload) + overhead

        res.seconds = t_last - t_first
        if res.seconds > 0:
            # The first frame marks the start of the timed interval, so its bytes are not in it.
            res.rate_bytes_per_s = (res.stream_bytes - first_bytes) / res.seconds
        now = self._parser_errors()
        res.crc_errors, res.header_errors, res.junk_bytes = (n - b for n, b in zip(now, self._errors_before))
        if res.ended:
            stats = link.get_stats()
            res.pico_dropped = stats.pdm_dropped + stats.tach_dropped
            # Frames the Pico generated after the last one we received also count as missing.
            # At real rate it makes exactly one TACH frame per PDM frame.
            res.seq_gaps += abs(res.pico_generated - expected[p.T_PDM])
            if self._mode == p.MODE_REAL:
                res.seq_gaps += abs(res.pico_generated - expected[p.T_TACH])
        _judge(res, self._min_max_rate)
        return res


def run_linktest(link: PicoLink, seconds: float, mode: int = p.MODE_REAL, seed: int = DEFAULT_SEED,
                 min_max_rate: float = MAX_RATE_PASS_BYTES_PER_S) -> LinkTestResult:
    test = LinkTest(link, mode, seed, min_max_rate)
    test.start(seconds)
    return test.finish()


def _judge(res: LinkTestResult, min_max_rate: float) -> None:
    fail = res.failures.append
    if not res.ended:
        fail("the stream never finished: no 'stream stopped' event from the Pico")
    if res.frames[p.T_PDM] == 0:
        fail("no PDM frames received")
    for name, value in (
        ("frames missing (sequence gaps)", res.seq_gaps),
        ("frames with wrong data", res.content_errors),
        ("CRC errors", res.crc_errors),
        ("header errors", res.header_errors),
        ("stray bytes between frames", res.junk_bytes),
        ("frames flagged GAP by the Pico", res.gap_flags),
        ("overflow events from the Pico", res.overflow_events),
        ("frames the Pico says it dropped", res.pico_dropped or 0),
    ):
        if value:
            fail(f"{name}: {value}")
    if res.mode == p.MODE_REAL and res.seconds >= 1.0:
        error = res.rate_bytes_per_s / p.REAL_RATE_BYTES_PER_S - 1
        if abs(error) > REAL_RATE_TOLERANCE:
            fail(f"rate {res.rate_bytes_per_s / 1000:.1f} kB/s is {error:+.1%} from the expected "
                 f"{p.REAL_RATE_BYTES_PER_S / 1000:.1f} kB/s")
    if res.mode == p.MODE_MAX and res.rate_bytes_per_s < min_max_rate:
        fail(f"maximum rate {res.rate_bytes_per_s / 1000:.0f} kB/s is under {min_max_rate / 1000:.0f} kB/s")


def format_result(res: LinkTestResult) -> str:
    lines = [
        f"mode             {'real rate' if res.mode == p.MODE_REAL else 'maximum rate'}",
        f"duration         {res.seconds:.2f} s",
        f"frames           {res.frames[p.T_PDM]} PDM, {res.frames[p.T_TACH]} TACH"
        f" (Pico generated {res.pico_generated} PDM)",
        f"rate             {res.rate_bytes_per_s / 1000:.1f} kB/s",
        f"missing frames   {res.seq_gaps}",
        f"wrong data       {res.content_errors}",
        f"CRC errors       {res.crc_errors}",
        f"header errors    {res.header_errors}",
        f"stray bytes      {res.junk_bytes}",
        f"Pico dropped     {res.pico_dropped}",
        "RESULT           " + ("PASS" if res.passed else "FAIL"),
    ]
    return "\n".join(lines + [f"  - {reason}" for reason in res.failures])


class CpuLoad:
    """Keeps every CPU core busy, for bench item 18 (link test with the CM5 loaded)."""

    _BURN = "import hashlib\nh = hashlib.sha256()\nb = bytes(1 << 20)\nwhile True:\n    h.update(b)\n"

    def __init__(self, workers: int) -> None:
        self._workers = workers
        self._procs: list[subprocess.Popen] = []

    def __enter__(self) -> "CpuLoad":
        self._procs = [subprocess.Popen([sys.executable, "-c", self._BURN]) for _ in range(self._workers)]
        return self

    def __exit__(self, *exc) -> None:
        for proc in self._procs:
            proc.kill()
        for proc in self._procs:
            proc.wait()
