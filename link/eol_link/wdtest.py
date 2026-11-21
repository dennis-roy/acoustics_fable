"""Checks the Pico's heartbeat watchdog using only what the Pico reports about itself.

It cannot see the output pins. It proves the logic: healthy with heartbeats, tripped
and refusing outputs without them. Whether the contacts really open is bench item 32,
with the interface board.

Stop the heartbeat service first: this test has to own the heartbeat port.
"""

from __future__ import annotations

import time

from . import protocol as p
from .client import CommandFailed, PicoLink
from .transport import Port

MARGIN_S = 0.2


def run_wdtest(link: PicoLink, hb_port: Port) -> list[str]:
    """Returns a list of failures; empty means pass. Call link.hello() first."""
    failures: list[str] = []
    timeout_s = link.info.hb_timeout_ms / 1000

    for n in range(5):
        hb_port.write(p.encode_frame(p.T_HEARTBEAT, p.pack_heartbeat(n, True), seq=n))
        time.sleep(0.1)
    replies = [f for f in p.FrameParser().feed(hb_port.read(0.2)) if f.type == p.T_HB_STATUS]
    if not replies:
        failures.append("no HB_STATUS replies on the heartbeat port")
    elif not p.unpack_hb_status(replies[-1].payload)[2] & p.STATE_HEALTHY:
        failures.append("not healthy after 5 heartbeats")

    hb_port.write(p.encode_frame(p.T_HEARTBEAT, p.pack_heartbeat(5, True), seq=5))
    try:
        link.set_outputs(p.OUT_READY)
    except CommandFailed as exc:
        failures.append(f"SET_OUTPUTS while healthy: {exc}")
    before = link.get_stats()
    if not before.outputs & p.OUT_READY:
        failures.append("READY not reported on after SET_OUTPUTS")

    time.sleep(timeout_s + MARGIN_S)  # no heartbeats now
    after = link.get_stats()
    if after.state & p.STATE_HEALTHY:
        failures.append(f"still healthy {timeout_s + MARGIN_S:.1f} s after the last heartbeat")
    if after.outputs:
        failures.append(f"outputs still {after.outputs:#04x} after the watchdog should have tripped")
    if after.wd_trips != before.wd_trips + 1:
        failures.append(f"trip counter went from {before.wd_trips} to {after.wd_trips}, expected +1")
    try:
        link.set_outputs(p.OUT_READY)
        failures.append("SET_OUTPUTS accepted while tripped")
    except CommandFailed as exc:
        if exc.status != p.ST_SAFE_STATE:
            failures.append(f"SET_OUTPUTS while tripped: {exc}, expected SAFE_STATE")
    return failures
