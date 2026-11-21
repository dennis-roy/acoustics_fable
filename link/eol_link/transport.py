"""Opening the Pico's serial ports. Standard library only (no pyserial)."""

from __future__ import annotations

import glob
import os
import select
import termios
import tty

# The firmware's USB strings (firmware/src/usb_descriptors.c) give these names on Linux.
BY_ID_GLOB = "/dev/serial/by-id/usb-Zynith_EOL_Pico_front_end_*-if{:02d}"
DATA_INTERFACE, HB_INTERFACE = 0, 2


class LinkLost(OSError):
    """The port went away (cable pulled, Pico reset, simulator stopped)."""


def find_ports() -> tuple[str, str] | None:
    """Returns (data port, heartbeat port) of the one connected Pico, or None."""
    data = sorted(glob.glob(BY_ID_GLOB.format(DATA_INTERFACE)))
    hb = sorted(glob.glob(BY_ID_GLOB.format(HB_INTERFACE)))
    if len(data) == 1 and len(hb) == 1:
        return data[0], hb[0]
    return None


class Port:
    def __init__(self, path: str) -> None:
        self.path = path
        self._fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        try:
            tty.setraw(self._fd)  # no echo, no line editing, no CR/LF translation
        except termios.error:
            os.close(self._fd)
            raise

    def read(self, timeout: float) -> bytes:
        """Waits up to `timeout` seconds. Returns b"" if nothing arrived."""
        try:
            ready, _, _ = select.select([self._fd], [], [], max(timeout, 0))
            if not ready:
                return b""
            data = os.read(self._fd, 65536)
        except OSError as exc:
            raise LinkLost(f"{self.path}: {exc}") from exc
        if not data:
            raise LinkLost(f"{self.path}: closed by the other end")
        return data

    def write(self, data: bytes) -> None:
        try:
            while data:
                data = data[os.write(self._fd, data) :]
        except OSError as exc:
            raise LinkLost(f"{self.path}: {exc}") from exc

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> "Port":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
