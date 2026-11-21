"""Run the simulator from a shell: python3 -m pico_sim [--proto-major N]

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
    args = ap.parse_args()
    sim = PicoSim(proto_major=args.proto_major)
    print(f"PORTS {sim.data_path} {sim.hb_path}", flush=True)
    try:
        sim.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
