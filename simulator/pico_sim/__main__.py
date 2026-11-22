"""Run the simulator from a shell: python3 -m pico_sim [--proto-major N] [--mic ...]

It prints the two port paths, then runs until Ctrl-C. In another terminal:
    python3 -m eol_link --port <data path> --hb-port <heartbeat path> linktest
"""

from __future__ import annotations

import argparse

from eol_link import protocol as p

from .sim import PicoSim


def main() -> None:
    ap = argparse.ArgumentParser(prog="pico_sim", description="Pico front end simulator")
    ap.add_argument("--proto-major", type=int, default=p.PROTO_MAJOR,
                    help="pretend to speak another protocol version (the receiver must refuse it)")
    ap.add_argument("--mic", default="gnd", choices=["gnd", "vdd", "stuck", "none"],
                    help="simulated microphone: SELECT tied to gnd (default) or vdd, a stuck line, or none")
    ap.add_argument("--mic-release", default="low", choices=["low", "hold", "high"],
                    help="what the data line does while the microphone is not driving it")
    args = ap.parse_args()
    sim = PicoSim(proto_major=args.proto_major, mic=None if args.mic == "none" else args.mic,
                  mic_release=args.mic_release)
    print(f"PORTS {sim.data_path} {sim.hb_path}", flush=True)
    try:
        sim.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
