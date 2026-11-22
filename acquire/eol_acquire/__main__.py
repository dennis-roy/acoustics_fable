"""Command line: python3 -m eol_acquire {serve,status,capture,save,discard,list,verify}"""

from __future__ import annotations

import argparse
import logging
import sys

from .client import AcquireClient, AcquireError
from .evaluate import Limits
from .paths import acquire_socket, data_dir
from .service import AcquisitionService
from .store import PURPOSES, RecordingStore


def _fmt_capture(c: dict) -> str:
    lines = [
        f"capture id       {c['capture_id']}",
        f"captured         {c['captured_utc']}, {c['seconds']:.2f} s at {c['clock_hz'] / 1e6:g} MHz",
        f"probe            {c['probe']}",
        f"frames           {c['frames']} received, {c['missing']} missing",
    ]
    if c.get("ones_fraction") is not None:
        run = f"{c['longest_run_bits']} bits" if c["longest_run_bits"] else "under 32 bits"
        lines += [f"bits that are 1  {c['ones_fraction']:.2%}", f"longest run      {run}"]
    if c.get("peak_dbfs") is not None:
        lines.append(f"level            peak {c['peak_dbfs']:.1f} dBFS, rms {c['rms_dbfs']:.1f} dBFS (filter {c['filter_name']})")
    for b in c.get("bands", []):
        lines.append(f"band {b['low_hz'] / 1000:5.1f} to {b['high_hz'] / 1000:4.0f} kHz  {b['dbfs']:6.1f} dBFS")
    lines.append("RESULT           " + ("VALID" if c["valid"] else "INVALID"))
    lines += [f"  - {r}" for r in c["reasons"]]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(prog="eol_acquire", description="Acquisition service and recording store")
    ap.add_argument("--socket", default=None, help=f"service socket (default {acquire_socket()})")
    ap.add_argument("--data-dir", default=None, help=f"where recordings are kept (default {data_dir()})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sv = sub.add_parser("serve", help="run the acquisition service")
    sv.add_argument("--mic-id", required=True, help="identity of the microphone in use, stored with every recording")
    sv.add_argument("--port", help="the Pico's DATA port (default: find it)")
    sv.add_argument("--decimation", type=int, default=16, choices=[16, 32])

    sub.add_parser("status", help="what the service is doing")
    cp = sub.add_parser("capture", help="capture from the microphone and run the INVALID checks")
    cp.add_argument("--seconds", type=float, default=5.0)
    cp.add_argument("--clock", type=float, default=3.2, choices=[3.2, 4.0], metavar="MHZ")
    sa = sub.add_parser("save", help="keep the waiting capture as a permanent recording")
    sa.add_argument("capture_id")
    sa.add_argument("--purpose", required=True, choices=PURPOSES)
    sa.add_argument("--part")
    sa.add_argument("--failure-mode")
    sa.add_argument("--note", default="")
    dc = sub.add_parser("discard", help="drop the waiting capture")
    dc.add_argument("capture_id")
    sub.add_parser("list", help="the permanent recordings (does not need the service)")
    sub.add_parser("verify", help="check every recording against its SHA-256 (does not need the service)")
    args = ap.parse_args()

    sock = args.socket or acquire_socket()
    store = RecordingStore(args.data_dir or data_dir())

    if args.cmd == "serve":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
        service = AcquisitionService(store, microphone_id=args.mic_id, data_port=args.port,
                                     decimation=args.decimation, limits=Limits())
        try:
            service.serve(sock)
        except KeyboardInterrupt:
            pass
        return 0

    if args.cmd == "list":
        recs = store.list()
        for r in recs:
            extra = r.get("failure_mode") or ""
            print(f"{r.get('id')}  {r.get('purpose', '?'):12} {r.get('part') or '-':10} {extra:12} "
                  f"{r.get('seconds', 0):5.1f} s  {r.get('captured_utc', '')}")
        print(f"{len(recs)} recording(s) in {store.root}")
        return 0

    if args.cmd == "verify":
        rep = store.verify()
        for name, why in rep.damaged:
            print(f"DAMAGED     {name}: {why}")
        for name in rep.unfinished:
            print(f"UNFINISHED  {name}: data without a description, from an interrupted save; not a recording")
        print(f"{len(rep.intact)} intact, {len(rep.damaged)} damaged, {len(rep.unfinished)} unfinished, in {store.root}")
        print("RESULT           " + ("PASS" if rep.ok else "FAIL"))
        return 0 if rep.ok else 1

    client = AcquireClient(sock)
    try:
        if args.cmd == "status":
            for k, v in client.status().items():
                if k != "ok":
                    print(f"{k:14} {v}")
        elif args.cmd == "capture":
            c = client.capture(args.seconds, args.clock)
            print(_fmt_capture(c))
            print(f"\nKeep it:  python3 -m eol_acquire save {c['capture_id']} --purpose {{golden,failure_mode,bench}}")
            print(f"Drop it:  python3 -m eol_acquire discard {c['capture_id']}")
            return 0 if c["valid"] else 1
        elif args.cmd == "save":
            r = client.save(args.capture_id, args.purpose, part=args.part, failure_mode=args.failure_mode,
                            note=args.note)
            print(f"saved            {r['id']} ({r['purpose']}, {r['seconds']:.2f} s, {r['bytes']} bytes)")
            print(f"file             {r['path']}")
            print(f"SHA-256          {r['sha256']}")
        elif args.cmd == "discard":
            print(f"discarded        {client.discard(args.capture_id)['discarded']}")
    except AcquireError as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
