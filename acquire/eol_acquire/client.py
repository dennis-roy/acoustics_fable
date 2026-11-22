"""Talks to the acquisition service over its local socket."""

from __future__ import annotations

import json
import socket
from pathlib import Path


class AcquireError(Exception):
    """The service refused the request, or could not be reached."""


class AcquireClient:
    def __init__(self, socket_path: str | Path, timeout: float = 120.0) -> None:
        self._path = str(socket_path)
        self._timeout = timeout

    def request(self, req: dict) -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self._timeout)
                s.connect(self._path)
                s.sendall((json.dumps(req) + "\n").encode())
                reply = s.makefile("rb").readline()
        except FileNotFoundError:
            raise AcquireError(f"the acquisition service is not running (no socket at {self._path})") from None
        except OSError as exc:
            raise AcquireError(f"cannot reach the acquisition service at {self._path}: {exc}") from None
        if not reply:
            raise AcquireError("the acquisition service closed the connection without replying")
        data = json.loads(reply)
        if not data.get("ok"):
            raise AcquireError(data.get("error", "refused"))
        return data

    def status(self) -> dict:
        return self.request({"cmd": "status"})

    def capture(self, seconds: float, clock_mhz: float = 3.2) -> dict:
        return self.request({"cmd": "capture", "seconds": seconds, "clock_mhz": clock_mhz})["capture"]

    def save(self, capture_id: str, purpose: str, *, part: str | None = None, failure_mode: str | None = None,
             note: str = "") -> dict:
        return self.request({"cmd": "save", "capture_id": capture_id, "purpose": purpose, "part": part,
                             "failure_mode": failure_mode, "note": note})["recording"]

    def fetch(self, capture_id: str) -> tuple[bytes, int]:
        """(raw bytes, clock in Hz) of the waiting capture, held in memory only."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self._timeout)
                s.connect(self._path)
                s.sendall((json.dumps({"cmd": "fetch", "capture_id": capture_id}) + "\n").encode())
                f = s.makefile("rb")
                head = json.loads(f.readline() or b"{}")
                if not head.get("ok"):
                    raise AcquireError(head.get("error", "refused"))
                raw = f.read(head["bytes"])
        except OSError as exc:
            raise AcquireError(f"cannot reach the acquisition service at {self._path}: {exc}") from None
        if len(raw) != head["bytes"]:
            raise AcquireError("the capture arrived incomplete")
        return raw, head["clock_hz"]

    def set_outputs(self, mask: int) -> dict:
        return self.request({"cmd": "set_outputs", "mask": mask})

    def discard(self, capture_id: str) -> dict:
        return self.request({"cmd": "discard", "capture_id": capture_id})
