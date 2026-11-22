"""The acquisition service: the one program that runs captures and stores recordings.

Why a service: only one program at a time can use the Pico's data port, and Setup needs
a capture to wait between "capture" and the operator's Save or Retest. The service owns
both, and the application (step 5 on) asks it over a local socket.

    application --capture--> service --Pico--> capture + INVALID checks --> held in memory
    application --save-----> service --> permanent recording (only golden, failure_mode, bench)
    application --discard--> service --> dropped, never written

One capture is held at a time. A new capture replaces an unsaved one. Captures of test
and production parts are never saved: there is no purpose for them.

Requests and replies are one JSON object per line. Every reply has "ok"; a refused
request has "ok": false and "error". See ../README.md for the list.

It opens the Pico's port only during a capture, so other tools can use the Pico between
captures. (When Start comes to the Pico in step 7, the service will keep the port open
to hear it.)
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socketserver
from dataclasses import asdict
from pathlib import Path

from eol_link import protocol as p
from eol_link.capture import DEFAULT_PREROLL_S, MicCapture, capture_mic
from eol_link.client import CommandFailed, PicoLink, VersionMismatch
from eol_link.transport import LinkLost, Port, find_ports

from .evaluate import Evaluation, Limits, evaluate
from .store import RecordingStore, StoreError, software_version

MAX_SECONDS = 60.0
log = logging.getLogger("eol.acquire")


class RequestError(ValueError):
    pass


class AcquisitionService:
    def __init__(self, store: RecordingStore, *, microphone_id: str, data_port: str | None = None,
                 decimation: int = 16, preroll_s: float = DEFAULT_PREROLL_S, limits: Limits = Limits()) -> None:
        if not microphone_id:
            raise ValueError("the microphone identity is required (decision D5)")
        self.store = store
        self.microphone_id = microphone_id
        self.data_port = data_port
        self.decimation = decimation
        self.preroll_s = preroll_s
        self.limits = limits
        self.software = software_version()
        self._pending: tuple[str, MicCapture, Evaluation] | None = None

    # ---- requests ----

    def handle(self, req: dict) -> dict:
        try:
            cmd = req.get("cmd")
            fn = {"status": self._status, "capture": self._capture, "save": self._save,
                  "discard": self._discard}.get(cmd)
            if fn is None:
                raise RequestError(f"unknown command {cmd!r}")
            return {"ok": True, **fn(req)}
        except (RequestError, StoreError, VersionMismatch, CommandFailed, TimeoutError) as exc:
            return {"ok": False, "error": str(exc)}
        except (LinkLost, OSError) as exc:
            return {"ok": False, "error": f"Pico link: {exc}"}
        except Exception as exc:  # a fault here must answer the request, not drop it
            log.exception("request %r failed", req.get("cmd"))
            return {"ok": False, "error": f"internal error: {exc!r}"}

    def _status(self, req: dict) -> dict:
        return {"pending": self._pending[0] if self._pending else None, "data_dir": str(self.store.root.parent),
                "microphone_id": self.microphone_id, "decimation": self.decimation, "preroll_s": self.preroll_s,
                "limits": asdict(self.limits), "software": self.software, "recordings": len(self.store.descriptions())}

    def _port(self) -> str:
        if self.data_port:
            return self.data_port
        found = find_ports()
        if not found:
            raise RequestError("no Pico found under /dev/serial/by-id/")
        return found[0]

    def _capture(self, req: dict) -> dict:
        seconds = req.get("seconds")
        if not isinstance(seconds, (int, float)) or not 0 < seconds <= MAX_SECONDS:
            raise RequestError(f"seconds must be a number above 0 and up to {MAX_SECONDS:g}")
        clock_mhz = req.get("clock_mhz", 3.2)
        clocks = [hz / 1e6 for hz in p.MIC_CLOCKS_HZ]
        if clock_mhz not in clocks:
            raise RequestError(f"clock_mhz must be one of {clocks}")
        self._pending = None  # an unsaved capture is dropped as soon as a new one starts
        with Port(self._port()) as port:
            link = PicoLink(port)
            link.hello()
            try:
                cap = capture_mic(link, float(seconds), preroll_s=self.preroll_s, clock=clocks.index(clock_mhz))
            except ValueError as exc:
                raise RequestError(str(exc)) from exc
        ev = evaluate(cap, self.limits, self.decimation)
        capture_id = secrets.token_hex(6)
        self._pending = (capture_id, cap, ev)
        log.info("capture %s: %.1f s, %s", capture_id, seconds, "valid" if ev.valid else "INVALID: " + "; ".join(ev.reasons))
        return {"capture": summary(capture_id, cap, ev)}

    def _take(self, req: dict) -> tuple[str, MicCapture, Evaluation]:
        capture_id = req.get("capture_id")
        if not self._pending:
            raise RequestError("there is no capture waiting to be saved or discarded")
        if capture_id != self._pending[0]:
            raise RequestError(f"capture {capture_id} is not the one waiting ({self._pending[0]}); "
                               "only the latest capture can be saved")
        return self._pending

    def _save(self, req: dict) -> dict:
        capture_id, cap, ev = self._take(req)
        record = self.store.save(cap, ev, purpose=req.get("purpose", ""), microphone_id=self.microphone_id,
                                 part=req.get("part"), failure_mode=req.get("failure_mode"),
                                 note=req.get("note", ""), software=self.software)
        self._pending = None
        log.info("capture %s saved as %s (%s)", capture_id, record["id"], record["purpose"])
        return {"recording": {k: record[k] for k in ("id", "purpose", "path", "sha256", "bytes", "seconds")}}

    def _discard(self, req: dict) -> dict:
        capture_id, _, _ = self._take(req)
        self._pending = None
        log.info("capture %s discarded", capture_id)
        return {"discarded": capture_id}

    # ---- socket ----

    def serve(self, socket_path: str | Path) -> None:
        socket_path = Path(socket_path)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()  # left over from a stopped service
        service = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                for line in self.rfile:
                    try:
                        req = json.loads(line)
                        reply = service.handle(req) if isinstance(req, dict) else {"ok": False, "error": "not an object"}
                    except ValueError:
                        reply = {"ok": False, "error": "not JSON"}
                    self.wfile.write((json.dumps(reply) + "\n").encode())
                    self.wfile.flush()

        with socketserver.UnixStreamServer(str(socket_path), Handler) as server:
            os.chmod(socket_path, 0o660)
            self._server = server
            log.info("listening on %s; recordings in %s", socket_path, self.store.root)
            try:
                server.serve_forever()
            finally:
                socket_path.unlink(missing_ok=True)

    def shutdown(self) -> None:
        self._server.shutdown()


def summary(capture_id: str, cap: MicCapture, ev: Evaluation) -> dict:
    st = cap.status
    return {
        "capture_id": capture_id,
        "valid": ev.valid,
        "reasons": ev.reasons,
        "captured_utc": cap.started_utc,
        "seconds": len(cap.raw) * 8 / cap.clock_hz,
        "clock_hz": cap.clock_hz,
        "probe": st.verdict_name if st else None,
        "frames": cap.frames,
        "missing": cap.seq_gaps,
        "pico": {"serial": cap.info.unique_id, "firmware": cap.info.fw_version, "build": cap.info.build},
        **{k: v for k, v in ev.to_dict().items() if k not in ("valid", "reasons")},
    }
