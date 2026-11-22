"""The application's side of the DATA port: handshake, commands, incoming frames."""

from __future__ import annotations

import random
import time
from collections import deque
from typing import Iterator

from . import protocol as p
from .transport import Port

# Oldest Pico protocol minor version this code can work with.
MIN_PICO_PROTO_MINOR = 0


class VersionMismatch(Exception):
    """The Pico's firmware speaks a protocol this software must not use."""


class CommandFailed(Exception):
    def __init__(self, cmd_type: int, status: int) -> None:
        self.cmd_type, self.status = cmd_type, status
        name = p.STATUS_NAMES[status] if status < len(p.STATUS_NAMES) else str(status)
        super().__init__(f"command {cmd_type:#04x} refused: {name}")


class PicoLink:
    def __init__(self, port: Port) -> None:
        self._port = port
        self.parser = p.FrameParser()
        self._pending: deque[p.Frame] = deque()
        # Start somewhere random so an ACK left over from an earlier session cannot match.
        self._seq = random.getrandbits(32)
        self.info: p.Info | None = None

    def _read(self, timeout: float) -> None:
        self._pending.extend(self.parser.feed(self._port.read(timeout)))

    def frames(self, timeout: float) -> Iterator[p.Frame]:
        """Yields frames as they arrive. Stops after `timeout` seconds with nothing new."""
        while True:
            deadline = time.monotonic() + timeout
            while not self._pending:  # one read may bring only part of a frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._read(remaining)
            yield self._pending.popleft()

    def command(self, ftype: int, payload: bytes = b"", timeout: float = 2.0) -> None:
        """Sends a command and waits for its ACK. Other frames stay queued for frames()."""
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        self._port.write(p.encode_frame(ftype, payload, seq=self._seq))
        deadline = time.monotonic() + timeout
        seen = 0
        while True:
            for i in range(seen, len(self._pending)):
                f = self._pending[i]
                if f.type == p.T_ACK and p.unpack_ack(f.payload)[::2] == (ftype, self._seq):
                    del self._pending[i]
                    status = p.unpack_ack(f.payload)[1]
                    if status != p.ST_OK:
                        raise CommandFailed(ftype, status)
                    return
            seen = len(self._pending)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no ACK for command {ftype:#04x}")
            self._read(remaining)

    def hello(self) -> p.Info:
        """The version handshake. Raises VersionMismatch if this Pico must not be used."""
        refused = False
        try:
            self.command(p.T_HELLO, p.pack_hello())
        except CommandFailed as exc:
            if exc.status != p.ST_BAD_VERSION:
                raise
            refused = True
        info = None
        for f in list(self._pending):
            if f.type == p.T_INFO:
                info = p.unpack_info(f.payload)
                self._pending.remove(f)
        if info is None:
            raise VersionMismatch("the Pico did not send INFO")
        self.info = info
        if refused or info.proto_major != p.PROTO_MAJOR or info.proto_minor < MIN_PICO_PROTO_MINOR:
            raise VersionMismatch(
                f"Pico firmware {info.fw_version} speaks protocol {info.proto_major}.{info.proto_minor}; "
                f"this software needs {p.PROTO_MAJOR}.{MIN_PICO_PROTO_MINOR} or a later 1.x"
            )
        return info

    def set_outputs(self, mask: int) -> None:
        self.command(p.T_SET_OUTPUTS, p.pack_set_outputs(mask))

    def reboot_to_bootloader(self) -> None:
        """The Pico restarts as a USB drive, ready for new firmware. Its serial ports vanish."""
        self.command(p.T_REBOOT, p.pack_reboot())

    def get_stats(self) -> p.Stats:
        self.command(p.T_GET_STATS)
        # The Pico queues the STATS frame just before the ACK, so it is the newest one here.
        stats = [f for f in self._pending if f.type == p.T_STATS]
        for f in stats:
            self._pending.remove(f)
        return p.unpack_stats(stats[-1].payload)
