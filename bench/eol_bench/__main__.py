"""Command line: python3 -m eol_bench {info,levels,clock,wakeup,burst,delay,gen}

Bench items from docs/phase0/PHASE0_HARDWARE_DESIGN_REVIEW.md section 10. Wiring: firmware_la/README.md.
"""

from __future__ import annotations

import argparse
import sys
import threading

from eol_link.transport import Port

from .la import Analyser, AnalyserError, data_delay, edges, find_port, widths_report


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


def main() -> int:
    ap = argparse.ArgumentParser(prog="eol_bench", description="Logic analyser on a second Pico, and bench items")
    ap.add_argument("--port", help="the analyser's serial port (default: find it)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="what the analyser reports about itself")
    sub.add_parser("levels", help="the eight inputs, now")
    ck = sub.add_parser("clock", help="frequency, duty and shortest pulse of a running clock (bench items 2, 3, 4)")
    ck.add_argument("--ch", type=int, default=0)
    ck.add_argument("--periods", type=int, default=2_000_000)
    ck.add_argument("--duty-min", type=float, default=48.5)
    ck.add_argument("--duty-max", type=float, default=51.5)
    ck.add_argument("--skip", type=int, default=300_000, help="periods ignored first (the 2 MHz wake-up is about 200 000)")
    ck.add_argument("--manual", action="store_true", help="start a microphone stream of at least 1.5 s yourself within 10 s")
    wk = sub.add_parser("wakeup", help="microphone wake-up: time to the frequency change and no short pulse (bench items 8, 9)")
    wk.add_argument("--ch", type=int, default=0)
    wk.add_argument("--manual", action="store_true", help="start the microphone stream yourself within 10 s")
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

    def measure():
        try:
            result["j"] = la.command(command, timeout=25)
        except AnalyserError as exc:
            result["error"] = exc

    t = threading.Thread(target=measure)
    t.start()
    if manual:
        print("Analyser armed: start a microphone stream now (within 10 s).")
    else:
        _start_mic_stream(seconds)
    t.join()
    if "error" in result:
        raise result["error"]
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
