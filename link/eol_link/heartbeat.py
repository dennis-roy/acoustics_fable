"""The heartbeat process, and the application's check-in with it.

Why a separate process: the Pico opens every output contact if heartbeats stop for
500 ms. If the heartbeat came from the application, a long analysis could delay it and
trip the outputs for no reason. So one small process does nothing but send it:

    application --"alive" every 0.25 s--> heartbeat process --HEARTBEAT every 0.1 s--> Pico

- The heartbeat process is tiny, asks Linux for real-time priority and locks its memory,
  so load from analysis cannot starve it.
- The application proves it is alive by checking in over a local socket. If check-ins
  stop for 2 s, the heartbeat carries APP_ALIVE = 0 and the Pico opens the outputs.
- If Linux, USB or this process dies, heartbeats stop and the Pico opens the outputs
  after 500 ms.
"""

from __future__ import annotations

import ctypes
import logging
import os
import select
import socket
import threading
import time

from . import protocol as p
from .transport import LinkLost, Port

HEARTBEAT_PERIOD_S = 0.100
APP_CHECKIN_PERIOD_S = 0.250
APP_DEADLINE_S = 2.0
REOPEN_DELAY_S = 0.5
REALTIME_PRIORITY = 50

log = logging.getLogger("eol.heartbeat")


def make_realtime() -> list[str]:
    """Asks Linux for real-time scheduling and locked memory. Returns what it could not get."""
    missing = []
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(REALTIME_PRIORITY))
    except (AttributeError, OSError) as exc:
        missing.append(f"real-time priority ({exc})")
    try:
        mcl_current, mcl_future = 1, 2  # Linux values
        if ctypes.CDLL(None, use_errno=True).mlockall(mcl_current | mcl_future) != 0:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    except (AttributeError, OSError) as exc:
        missing.append(f"locked memory ({exc})")
    return missing


class HeartbeatDaemon:
    def __init__(self, hb_port_path: str, socket_path: str) -> None:
        self._port_path = hb_port_path
        self._socket_path = socket_path
        self._stop = threading.Event()
        self._last_checkin = float("-inf")
        self.sent = 0
        self.pico_healthy: bool | None = None  # from the Pico's last HB_STATUS

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.bind(self._socket_path)
            sock.setblocking(False)
            try:
                while not self._stop.is_set():
                    try:
                        with Port(self._port_path) as port:
                            log.info("heartbeat port open: %s", self._port_path)
                            self._beat(port, sock)
                    except OSError as exc:  # includes LinkLost and a missing port
                        log.warning("heartbeat port lost (%s); the Pico will open its outputs", exc)
                        self._stop.wait(REOPEN_DELAY_S)
            finally:
                os.unlink(self._socket_path)

    def _beat(self, port: Port, sock: socket.socket) -> None:
        parser = p.FrameParser()
        next_beat = time.monotonic()
        was_alive = None
        while not self._stop.is_set():
            now = time.monotonic()
            self._drain_checkins(sock, now)
            alive = (now - self._last_checkin) <= APP_DEADLINE_S
            if alive != was_alive:
                log.info("application %s", "alive" if alive else "NOT alive: telling the Pico")
                was_alive = alive
            port.write(p.encode_frame(p.T_HEARTBEAT, p.pack_heartbeat(self.sent, alive), seq=self.sent))
            self.sent += 1

            next_beat += HEARTBEAT_PERIOD_S
            if next_beat < now:  # we were held up; do not send a burst to catch up
                next_beat = now + HEARTBEAT_PERIOD_S
            while (wait := next_beat - time.monotonic()) > 0 and not self._stop.is_set():
                for f in parser.feed(port.read(min(wait, 0.05))):
                    if f.type == p.T_HB_STATUS:
                        healthy = bool(p.unpack_hb_status(f.payload)[2] & p.STATE_HEALTHY)
                        if healthy != self.pico_healthy:
                            log.info("Pico watchdog %s", "healthy" if healthy else "TRIPPED")
                            self.pico_healthy = healthy

    def _drain_checkins(self, sock: socket.socket, now: float) -> None:
        while select.select([sock], [], [], 0)[0]:
            if sock.recv(16) == b"alive":
                self._last_checkin = now


class AppLiveness:
    """Used by the application: tells the heartbeat process "I am alive".

    Call beat() from the application's main loop, so a stuck main loop stops the check-ins.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.setblocking(False)

    def beat(self) -> bool:
        """Returns False if the heartbeat process is not there to hear it."""
        try:
            self._sock.sendto(b"alive", self._socket_path)
            return True
        except OSError:
            return False

    def close(self) -> None:
        self._sock.close()
