"""Command line: python3 -m eol_dsp {filters,decimate,timing}"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

import numpy as np

from .decimate import DEFAULT_DECIMATION, decimate_bits, filters, get_filter, level_dbfs
from .wavfile import write_float_wav


def cmd_filters(args) -> int:
    for f in filters():
        print(f"{f.name}")
        print(f"  {f.clock_hz / 1e6:g} MHz / {f.decimation} = {f.rate_hz / 1000:g} kHz, {len(f.taps)} taps, "
              f"passband to {f.pass_hz / 1000:g} kHz, stopband from {f.stop_hz / 1000:g} kHz, "
              f"{f.atten_db:g} dB")
        print(f"  sha256 {f.sha256}")
    return 0


def cmd_decimate(args) -> int:
    src = Path(args.capture)
    raw = src.read_bytes()
    meta_path = src.with_suffix(".json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    clock = args.clock * 1e6 if args.clock else meta.get("clock_hz", 3_200_000)
    if not meta:
        print(f"note: no {meta_path.name} beside the capture; assuming a {clock / 1e6:g} MHz clock")
    elif meta.get("complete") is False:
        print("note: the capture is marked incomplete (frames were missing)")
    spec = get_filter(clock, args.ratio)
    t0 = time.perf_counter()
    s = decimate_bits(raw, spec)
    took = time.perf_counter() - t0
    peak, rms = level_dbfs(s.inner())
    print(f"capture     {src.name}, {len(raw) * 8 / clock:.2f} s at {clock / 1e6:g} MHz")
    print(f"filter      {spec.name}")
    print(f"            sha256 {spec.sha256}")
    print(f"output      {len(s.values)} samples at {s.rate_hz / 1000:g} kHz ({s.seconds:.2f} s), "
          f"made in {took:.2f} s")
    print(f"level       peak {peak:.1f} dBFS, rms {rms:.1f} dBFS "
          f"(leaving out {spec.edge_s * 1e6:.0f} us at each end)")
    if args.wav:
        write_float_wav(args.wav, s.values, s.rate_hz)
        info = {
            "file": os.path.basename(args.wav),
            "format": "32-bit float WAV, mono; 1.0 = full scale of the 1-bit stream",
            "rate_hz": s.rate_hz,
            "samples": len(s.values),
            "timing": "sample j is the moment of source bit j * decimation; the filter delay is removed",
            "edge_s": spec.edge_s,
            "source": src.name,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "source_clock_hz": clock,
            "filter": spec.name,
            "filter_sha256": spec.sha256,
        }
        Path(args.wav).with_suffix(".json").write_text(json.dumps(info, indent=2) + "\n")
        print(f"wrote       {args.wav} (and {Path(args.wav).with_suffix('.json').name})")
    return 0


def _machine() -> str:
    model = Path("/proc/device-tree/model")
    if model.exists():
        return model.read_text().rstrip("\0\n")
    return f"{platform.system()} {platform.machine()} (not a Raspberry Pi)"


def cmd_timing(args) -> int:
    import scipy

    seconds, repeats = 10, 5
    print(f"machine   {_machine()}")
    print(f"software  Python {sys.version.split()[0]}, numpy {np.__version__}, scipy {scipy.__version__}")
    print(f"input     {seconds} s of 1-bit data at 3.2 MHz, {repeats} runs each, one core\n")
    raw = np.random.default_rng(1).integers(0, 256, size=3_200_000 * seconds // 8, dtype=np.uint8).tobytes()
    for f in filters():
        times = []
        for _ in range(repeats):
            t0 = time.perf_counter()
            decimate_bits(raw, f)
            times.append(time.perf_counter() - t0)
        print(f"divide by {f.decimation:<2} ({len(f.taps)} taps)   median {statistics.median(times):.2f} s"
              f"   worst {max(times):.2f} s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="eol_dsp", description="Decimation filter for the 1-bit microphone stream")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("filters", help="list the filters, with names and SHA-256").set_defaults(fn=cmd_filters)
    de = sub.add_parser("decimate", help="turn a saved capture (.pdm) into samples")
    de.add_argument("capture", help="a .pdm file saved by 'eol_link mictest --save'")
    de.add_argument("--ratio", type=int, default=DEFAULT_DECIMATION, choices=[16, 32],
                    help="16 gives 200 kHz (default), 32 gives 100 kHz")
    de.add_argument("--clock", type=float, metavar="MHZ", help="clock of the capture (default: from its .json)")
    de.add_argument("--wav", metavar="FILE", help="also write the samples as a WAV file")
    de.set_defaults(fn=cmd_decimate)
    sub.add_parser("timing", help="how long the filters take on this machine").set_defaults(fn=cmd_timing)
    args = ap.parse_args()
    try:
        return args.fn(args)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
