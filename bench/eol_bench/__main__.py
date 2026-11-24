"""Command line: python3 -m eol_bench {info,levels,clock,wakeup,burst,delay,gen}

Bench items from docs/phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md section 10. Wiring: firmware_la/README.md.
"""

from __future__ import annotations

import argparse
import sys
import threading

from eol_link.transport import Port

from .la import Analyser, AnalyserError, counts_report, data_delay, edges, find_port, widths_report


def _verdict(checks: list[tuple[str, bool, str]]) -> int:
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL':5} {name}: {detail}")
    ok = all(c[1] for c in checks)
    print("RESULT           " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _print_widths(r) -> None:
    print(f"periods          {r.periods}")
    print(f"frequency        {r.freq_hz / 1e6:.6f} MHz (mean; relative to the analyser's crystal)")
    print(f"duty             {100 * r.duty:.2f} % high")
    print(f"shortest phases  high {r.min_high_ns:.1f} ns, low {r.min_low_ns:.1f} ns")
    print(f"longest phases   high {r.max_high_ns:.1f} ns, low {r.max_low_ns:.1f} ns")
    if r.change_ms is not None:
        print(f"frequency change {r.ref_freq_hz / 1e6:.4f} MHz -> {r.after_freq_hz / 1e6:.4f} MHz, {r.change_ms:.3f} ms after the first edge")
        print(f"near the change  shortest phase {r.min_near_change_ns:.1f} ns")
    for p in r.problems:
        print(f"  - {p}")


def _print_counts(r) -> None:
    print(f"gates            {r.gates} at {r.gate_us:.3f} us each")
    print(f"counts per gate  {r.min_count} to {r.max_count}")
    print(f"mean frequency   {r.freq_hz / 1e6:.6f} MHz (relative to analyser crystal)")
    if r.change_ms is not None:
        print(f"frequency change {r.change_ms:.3f} ms after first edge (gate quantization)")
        if r.ref_freq_hz is not None and r.after_freq_hz is not None:
            print(f"before / after   {r.ref_freq_hz / 1e6:.6f} / {r.after_freq_hz / 1e6:.6f} MHz")
    for problem in r.problems:
        print(f"  - {problem}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="eol_bench", description="Logic analyser on a second Pico, and bench items")
    ap.add_argument("--port", help="the analyser's serial port (default: find it)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="what the analyser reports about itself")
    sub.add_parser("levels", help="the eight inputs, now")
    ck = sub.add_parser("clock", help="10-second frequency count; use --widths for legacy duty/pulse checks")
    ck.add_argument("--ch", type=int, default=0)
    ck.add_argument("--widths", action="store_true", help="legacy per-phase mode for short duty/pulse checks; may overrun")
    ck.add_argument("--periods", type=int, default=2_000_000)
    ck.add_argument("--duty-min", type=float, default=48.5)
    ck.add_argument("--duty-max", type=float, default=51.5)
    ck.add_argument("--skip", type=int, default=300_000, help="periods ignored first (the 2 MHz wake-up is about 200 000)")
    ck.add_argument("--manual", action="store_true", help="start an 11-second microphone stream yourself within 10 s (1.5 s for --widths)")
    wk = sub.add_parser("wakeup", help="time the startup frequency change; --widths also checks short pulses")
    wk.add_argument("--ch", type=int, default=0)
    wk.add_argument("--widths", action="store_true", help="legacy mode including shortest phase checks; may overrun")
    wk.add_argument("--manual", action="store_true", help="start the microphone stream yourself within 10 s")
    ct = sub.add_parser("counts", help="count a running input in 10 us gates (no microphone stream is started)")
    ct.add_argument("--ch", type=int, default=0, choices=range(8))
    ct.add_argument("--gates", type=int, default=1000000)
    bu = sub.add_parser("burst", help="sample all inputs and list the changes")
    bu.add_argument("--samples", type=int, default=65536)
    bu.add_argument("--div", type=int, default=1, help="sample rate = 150 MHz / div")
    bu.add_argument("--trigger", type=int, default=None)
    bu.add_argument("--falling", action="store_true")
    dl = sub.add_parser("delay", help="how long after its clock edge the data line changes (bench item 11)")
    dl.add_argument("--clk", type=int, default=0)
    dl.add_argument("--data", type=int, default=1)
    gn = sub.add_parser("gen", help="square wave on a pin, for example a stand-in tach (bench item 30)")
    gn.add_argument("--pin", type=int, default=15)
    gn.add_argument("--hz", type=int, default=100)
    gn.add_argument("--duty", type=int, default=50)
    gn.add_argument("--off", action="store_true")
    args = ap.parse_args()
    if args.cmd == "counts" and not 1 <= args.gates <= 6000000:
        ap.error("--gates must be between 1 and 6000000")

    path = args.port or find_port()
    if not path:
        sys.exit("No analyser found under /dev/serial/by-id/ (usb-Zynith_EOL_logic_analyser_...). Plug it in, or give --port.")
    with Port(path) as port:
        la = Analyser(port)
        try:
            if args.cmd == "info":
                print(la.command("id"))
            elif args.cmd == "levels":
                v = la.command("levels")["levels"]
                print("  ".join(f"GP{i}={(v >> i) & 1}" for i in range(8)))
            elif args.cmd == "counts":
                timeout_ms = (args.gates + 99) // 100 + 2000
                j = la.command(f"counts {args.ch} {args.gates} {timeout_ms}", timeout=timeout_ms / 1000 + 5)
                r = counts_report(j, args.gates)
                _print_counts(r)
                return _verdict([("complete counting capture", not r.problems, "; ".join(r.problems) or "complete")])
            elif args.cmd in ("clock", "wakeup") and not args.widths:
                gates = 1020000 if args.cmd == "clock" else 15000
                timeout_ms = 22000 if args.manual else 12000
                j = _measure_during_stream(la, f"counts {args.ch} {gates} {timeout_ms}", args.manual,
                                           seconds=11 if args.cmd == "clock" else 0.5)
                r = counts_report(j, gates)
                _print_counts(r)
                checks = [("complete counting capture", not r.problems, "; ".join(r.problems) or "complete")]
                settled_hz = r.after_freq_hz if r.after_freq_hz is not None else r.freq_hz
                checks.append(("3.200 MHz settled clock within 0.5 %", abs(settled_hz / 3.2e6 - 1) < 0.005,
                               f"{settled_hz / 1e6:.6f} MHz"))
                checks.append(("settled count 32 +/- 1 in every gate", 31 <= r.after_min <= r.after_max <= 33,
                               f"{r.after_min} to {r.after_max}"))
                if args.cmd == "clock":
                    checks.append(("at least 10 seconds of settled clock", r.after_gates >= 1000000,
                                   f"{r.after_gates} gates"))
                if args.cmd == "wakeup":
                    checks.append(("100 ms wake-up within 1 ms", r.change_ms is not None and abs(r.change_ms - 100) <= 1,
                                   str(r.change_ms)))
                    checks.append(("2.000 MHz initial clock within 0.5 %", r.ref_freq_hz is not None and abs(r.ref_freq_hz / 2e6 - 1) < 0.005,
                                   str(r.ref_freq_hz)))
                return _verdict(checks)
            elif args.cmd == "clock":
                r = widths_report(_measure_during_stream(
                    la, f"widths {args.ch} {args.periods} 10000 {args.skip}", args.manual, seconds=1.5))
                _print_widths(r)
                return _verdict([("valid measurement", not r.problems, "; ".join(r.problems) or "complete"),
                                 (f"duty {args.duty_min} to {args.duty_max} %", args.duty_min <= 100 * r.duty <= args.duty_max, f"{100 * r.duty:.2f} %")])
            elif args.cmd == "wakeup":
                r = widths_report(_measure_during_stream(la, f"widths {args.ch} 1500000 10000", args.manual, seconds=0.5))
                _print_widths(r)
                checks = [("valid measurement", not r.problems, "; ".join(r.problems) or "complete"),
                          ("a frequency change was seen", r.change_ms is not None, "")]
                if r.change_ms is not None:
                    checks += [("100 ms ± 1 ms from the first edge to the change (item 8)", abs(r.change_ms - 100) <= 1, f"{r.change_ms:.3f} ms"),
                               ("2.000 MHz before, 3.200 MHz after (±0.5 %)", abs(r.ref_freq_hz / 2e6 - 1) < 0.005 and abs(r.after_freq_hz / 3.2e6 - 1) < 0.005,
                                f"{r.ref_freq_hz / 1e6:.4f} -> {r.after_freq_hz / 1e6:.4f} MHz")]
                checks.append(("no phase shorter than 150 ns (item 9)", min(r.min_high_ns, r.min_low_ns) >= 150,
                               f"shortest {min(r.min_high_ns, r.min_low_ns):.1f} ns"))
                return _verdict(checks)
            elif args.cmd == "burst":
                head, data = la.burst(args.samples, args.div, args.trigger, not args.falling)
                ns = args.div * 1e9 / head["clk_hz"]
                print(f"{len(data)} samples, {ns:.2f} ns apart ({len(data) * ns / 1e6:.3f} ms)")
                for ch in range(8):
                    e = edges(data, ch)
                    print(f"GP{ch}: {len(e)} changes" + (f", first at {e[0][0] * ns:.1f} ns" if e else f", steady {(data[0] >> ch) & 1}"))
            elif args.cmd == "delay":
                head, data = la.burst(262144, 1, args.clk, True)
                d = data_delay(data, 1e9 / head["clk_hz"], args.clk, args.data)
                print(f"after a rising clock edge:  {d['after_rising']}")
                print(f"after a falling clock edge: {d['after_falling']}")
                print(f"the data line follows the {d['data_follows']} edge (resolution {1e9 / head['clk_hz']:.2f} ns)")
                if d["data_follows"]:
                    s = d["after_" + d["data_follows"]]
                    return _verdict([("data changes 10 to 50 ns after its edge (item 11)", 10 <= s["median_ns"] <= 50,
                                      f"median {s['median_ns']:.1f} ns, range {s['min_ns']:.1f} to {s['max_ns']:.1f} ns")])
            elif args.cmd == "gen":
                print(la.command("gen off" if args.off else f"gen {args.pin} {args.hz} {args.duty}"))
        except AnalyserError as exc:
            print(f"error: {exc}")
            return 1
    return 0


def _measure_during_stream(la: Analyser, command: str, manual: bool, seconds: float) -> dict:
    """Arms the analyser, starts a microphone stream (or asks for one), and returns the measurement."""
    result = {}
    armed = threading.Event()
    counting = command.startswith("counts ")

    def confirm_armed():
        result["armed"] = True
        armed.set()

    def measure():
        try:
            if counting:
                result["j"] = la.command(command, timeout=25, on_armed=confirm_armed)
            else:
                result["j"] = la.command(command, timeout=25)
        except Exception as exc:
            result["error"] = exc
        finally:
            armed.set()  # also wake the caller on rejection or transport failure

    t = threading.Thread(target=measure)
    t.start()
    try:
        if counting and not armed.wait(26):
            raise AnalyserError("analyser did not confirm counting capture armed")
        if counting and not result.get("armed") and "error" not in result:
            raise AnalyserError("analyser returned a result without confirming armed")
        if "error" not in result:
            if manual:
                print("Analyser armed: start a microphone stream now (within 10 s).")
            else:
                _start_mic_stream(seconds)
    finally:
        t.join()
    if "error" in result:
        raise AnalyserError(str(result["error"])) from result["error"]
    return result["j"]


def _start_mic_stream(seconds: float) -> None:
    """Starts a microphone stream on the front-end Pico (the acquisition service must be stopped)."""
    from eol_link.capture import capture_mic
    from eol_link.client import PicoLink
    from eol_link.transport import find_ports
    found = find_ports()
    if not found:
        raise AnalyserError("front-end Pico not found; use --manual")
    with Port(found[0]) as port:
        link = PicoLink(port)
        link.hello()
        capture_mic(link, seconds, preroll_s=0.5)


if __name__ == "__main__":
    sys.exit(main())
