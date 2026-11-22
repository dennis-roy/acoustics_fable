"""Command line: python3 -m eol_link {info,linktest,mictest,wdtest,heartbeat,bootloader}"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from . import protocol as p
from .client import CommandFailed, PicoLink, VersionMismatch
from .heartbeat import HeartbeatDaemon, make_realtime
from . import mictest
from .linktest import CpuLoad, format_result, run_linktest
from .transport import Port, find_ports
from .wdtest import run_wdtest

DEFAULT_SOCKET = "/run/eol/heartbeat.sock"


def _ports(args) -> tuple[str, str]:
    if args.port and args.hb_port:
        return args.port, args.hb_port
    found = find_ports()
    if not found:
        sys.exit("No Pico found under /dev/serial/by-id/. Plug it in, or give --port and --hb-port.")
    return args.port or found[0], args.hb_port or found[1]


def cmd_info(args) -> int:
    data_port, _ = _ports(args)
    with Port(data_port) as port:
        link = PicoLink(port)
        try:
            info = link.hello()
            verdict = "accepted"
        except VersionMismatch as exc:
            info, verdict = link.info, f"REFUSED: {exc}"
    if info:
        print(f"board            {info.board_name}")
        print(f"firmware         {info.fw_version} (build {info.build})")
        print(f"protocol         {info.proto_major}.{info.proto_minor} (this software: {p.PROTO_MAJOR}.{p.PROTO_MINOR})")
        print(f"serial           {info.unique_id}")
        print(f"system clock     {info.sys_clk_hz} Hz configured, {info.fc_sys_khz} kHz counted")
        print(f"watchdog timeout {info.hb_timeout_ms} ms")
        print(f"microphone       {'capture built in' if info.features & p.FEATURE_MIC else 'not in this firmware'}")
    print(f"handshake        {verdict}")
    return 0 if verdict == "accepted" else 1


def cmd_linktest(args) -> int:
    data_port, _ = _ports(args)
    with Port(data_port) as port:
        link = PicoLink(port)
        try:
            info = link.hello()
        except VersionMismatch as exc:
            print(f"REFUSED: {exc}")
            return 1
        print(f"Connected to {info.board_name}, firmware {info.fw_version}")
        mode = p.MODE_MAX if args.max_rate else p.MODE_REAL
        with CpuLoad((os.cpu_count() or 1) if args.busy else 0):
            result = run_linktest(link, args.seconds, mode, min_max_rate=args.min_rate * 1000)
    print(format_result(result))
    return 0 if result.passed else 1


def cmd_mictest(args) -> int:
    data_port, _ = _ports(args)
    with Port(data_port) as port:
        link = PicoLink(port)
        try:
            info = link.hello()
        except VersionMismatch as exc:
            print(f"REFUSED: {exc}")
            return 1
        print(f"Connected to {info.board_name}, firmware {info.fw_version}")
        select = {"auto": 0, "rising": p.MIC_USE_L, "falling": p.MIC_USE_H}[args.read]
        try:
            result = mictest.run_mictest(link, args.seconds, clock=p.MIC_CLOCKS_HZ.index(round(args.clock * 1e6)),
                                         select=select, save_dir=args.save, mic_id=args.mic_id)
        except CommandFailed as exc:
            print(f"RESULT           FAIL\n  - {exc}")
            return 1
    print(mictest.format_result(result))
    return 0 if result.passed else 1


def cmd_bootloader(args) -> int:
    data_port, _ = _ports(args)
    with Port(data_port) as port:
        link = PicoLink(port)
        try:
            link.hello()
        except VersionMismatch as exc:
            print(f"REFUSED: {exc}")
            return 1
        link.reboot_to_bootloader()
    print("The Pico is restarting as a USB drive. Copy the new eol_pico.uf2 onto it.")
    return 0


def cmd_wdtest(args) -> int:
    data_port, hb_port = _ports(args)
    with Port(data_port) as data, Port(hb_port) as hb:
        link = PicoLink(data)
        try:
            link.hello()
        except VersionMismatch as exc:
            print(f"REFUSED: {exc}")
            return 1
        failures = run_wdtest(link, hb)
    print("RESULT           " + ("PASS" if not failures else "FAIL"))
    for reason in failures:
        print(f"  - {reason}")
    return 1 if failures else 0


def cmd_heartbeat(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    _, hb_port = _ports(args)
    for missing in make_realtime():
        logging.getLogger("eol.heartbeat").warning("running without %s", missing)
    os.makedirs(os.path.dirname(args.socket), exist_ok=True)
    try:
        HeartbeatDaemon(hb_port, args.socket).run()
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="eol_link", description="Link to the Pico front end")
    ap.add_argument("--port", help="DATA serial port (default: find the Pico)")
    ap.add_argument("--hb-port", help="heartbeat serial port (default: find the Pico)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="version handshake; print what the Pico reports").set_defaults(fn=cmd_info)

    lt = sub.add_parser("linktest", help="synthetic-data link proof")
    lt.add_argument("--seconds", type=float, default=10.0)
    lt.add_argument("--max-rate", action="store_true", help="as fast as the link allows (bench item 16)")
    lt.add_argument("--min-rate", type=float, default=650.0, metavar="KB_PER_S",
                    help="what --max-rate must reach, in kB/s (default 650; use 0 with a simulator)")
    lt.add_argument("--busy", action="store_true", help="load every CPU core meanwhile (bench item 18)")
    lt.set_defaults(fn=cmd_linktest)

    mt = sub.add_parser("mictest", help="capture from the microphone and check the stream")
    mt.add_argument("--seconds", type=float, default=5.0)
    mt.add_argument("--clock", type=float, default=3.2, choices=[3.2, 4.0], metavar="MHZ",
                    help="microphone clock: 3.2 (default) or 4.0")
    mt.add_argument("--read", default="auto", choices=["auto", "rising", "falling"],
                    help="which clock edge to read at; auto lets the Pico find out (default)")
    mt.add_argument("--save", metavar="DIR", help="keep the raw 1-bit stream there, with its SHA-256")
    mt.add_argument("--mic-id", default="", help="written into the saved description")
    mt.set_defaults(fn=cmd_mictest)

    sub.add_parser("bootloader", help="restart the Pico as a USB drive, for new firmware").set_defaults(fn=cmd_bootloader)

    sub.add_parser("wdtest", help="check the heartbeat watchdog through the Pico's own reports").set_defaults(fn=cmd_wdtest)

    hb = sub.add_parser("heartbeat", help="run the heartbeat process")
    hb.add_argument("--socket", default=DEFAULT_SOCKET, help="where the application checks in")
    hb.set_defaults(fn=cmd_heartbeat)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
