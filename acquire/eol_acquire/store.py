"""Permanent recordings: the raw 1-bit stream of calibrated samples, kept with a SHA-256.

The spec's rule (data retention, decision D5): raw data of calibrated samples (Golden OK
and failure-mode samples) is stored permanently with a hash; raw data of test and
production parts is never stored. So a recording can only be saved with one of the
purposes below, and there is no purpose for a production part.

  golden        a Golden OK sample                       the capture must be valid
  failure_mode  a sample of a named failure mode         the capture must be valid
  bench         an engineering recording (bench tests)   may be invalid: that can be the point

Layout:  <data dir>/recordings/<year>/<month>/<id>.pdm and <id>.json
The file format, and how it is written safely, is in eol_link/recording.py.

Deleting a recording is not offered here. It belongs to Setup, audited (step 5).
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from eol_link.capture import MicCapture
from eol_link.recording import verify_recording, write_recording

from .evaluate import Evaluation

PURPOSES = ("golden", "failure_mode", "bench")
MIN_FREE_BYTES = 500 * 1024 * 1024
# Spec, Setup: "alphanumeric plus space - _ . / ( )"; part number up to 20, mode name up to 30.
_NAME = re.compile(r"[A-Za-z0-9 \-_./()]+")
PART_MAX, MODE_MAX, NOTE_MAX = 20, 30, 200


class StoreError(ValueError):
    pass


def software_version() -> str:
    """The git commit of this program, with "+changes" if the checkout differs from it."""
    repo = Path(__file__).resolve().parents[2]
    try:
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return (sha + ("+changes" if dirty else "")) if sha else "unknown"


def _check_name(value: str | None, what: str, limit: int, required: bool) -> str | None:
    if not value:
        if required:
            raise StoreError(f"{what} is required")
        return None
    if len(value) > limit or not _NAME.fullmatch(value):
        raise StoreError(f"{what} must be up to {limit} characters: letters, digits, space and - _ . / ( )")
    return value


@dataclass
class VerifyReport:
    intact: list[str] = field(default_factory=list)
    damaged: list[tuple[str, str]] = field(default_factory=list)  # (recording, why)
    unfinished: list[str] = field(default_factory=list)  # data without a description: an interrupted save

    @property
    def ok(self) -> bool:
        return not self.damaged


class RecordingStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir) / "recordings"

    def save(self, cap: MicCapture, ev: Evaluation, *, purpose: str, microphone_id: str,
             part: str | None = None, failure_mode: str | None = None, note: str = "",
             software: str = "unknown") -> dict:
        if purpose not in PURPOSES:
            raise StoreError(f"purpose must be one of {', '.join(PURPOSES)}. Raw data of test and production "
                             "parts is never stored")
        if purpose != "bench" and not ev.valid:
            raise StoreError("an INVALID capture cannot be stored as a calibrated sample: "
                             + "; ".join(ev.reasons))
        part = _check_name(part, "part number", PART_MAX, required=False)
        failure_mode = _check_name(failure_mode, "failure mode", MODE_MAX, required=purpose == "failure_mode")
        if failure_mode and purpose != "failure_mode":
            raise StoreError("a failure mode name only goes with purpose failure_mode")
        if not microphone_id:
            raise StoreError("the microphone identity is required (decision D5)")
        if len(note) > NOTE_MAX:
            raise StoreError(f"the note must be up to {NOTE_MAX} characters")
        if not cap.raw:
            raise StoreError("the capture holds no data")

        self.root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.root).free
        if free < MIN_FREE_BYTES + len(cap.raw):
            raise StoreError(f"not enough disk space: {free // 2**20} MB free, "
                             f"{MIN_FREE_BYTES // 2**20} MB must stay free")

        stamp = cap.started_utc.replace("-", "").replace(":", "")
        rec_id = f"rec-{stamp}-{secrets.token_hex(3)}"
        info, st = cap.info, cap.status
        meta = {
            "id": rec_id,
            "purpose": purpose,
            "part": part,
            "failure_mode": failure_mode,
            "note": note,
            "microphone_id": microphone_id,
            "captured_utc": cap.started_utc,
            "saved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "clock_hz": cap.clock_hz,
            "seconds": len(cap.raw) * 8 / cap.clock_hz,
            "preroll_s": cap.preroll_s,
            "pico_first_t_us": cap.first_t_us,
            "pico": {"serial": info.unique_id, "firmware": info.fw_version, "build": info.build,
                     "protocol": f"{info.proto_major}.{info.proto_minor}"},
            "filter": {"name": ev.filter_name, "sha256": ev.filter_sha256,
                       "note": "the filter in use when saved; the file itself is unfiltered"},
            "software": software,
            "select_probe": None if st is None else {
                "verdict": st.verdict_name, "forced": st.forced, "periods": st.periods,
                "ones_l": st.ones_l, "ones_h": st.ones_h,
                "lh_10": st.lh_10, "lh_01": st.lh_01, "hl_10": st.hl_10, "hl_01": st.hl_01},
            "link": {"frames": cap.frames, "missing": cap.seq_gaps, "crc_errors": cap.crc_errors,
                     "pico_dropped": cap.pico_dropped},
            "evaluation": ev.to_dict(),
        }
        directory = self.root / cap.started_utc[:4] / cap.started_utc[5:7]
        pdm, digest = write_recording(directory, rec_id, cap.raw, meta)
        return {**meta, "bytes": len(cap.raw), "sha256": digest, "path": str(pdm)}

    def descriptions(self) -> list[Path]:
        return sorted(self.root.glob("*/*/rec-*.json")) if self.root.exists() else []

    def list(self) -> list[dict]:
        out = []
        for desc in self.descriptions():
            try:
                out.append(json.loads(desc.read_text()))
            except (OSError, ValueError):
                out.append({"id": desc.stem, "purpose": "?", "damaged": True})
        return out

    def verify(self) -> VerifyReport:
        report = VerifyReport()
        described = set()
        for desc in self.descriptions():
            described.add(desc.with_suffix(".pdm"))
            why = verify_recording(desc)
            if why:
                report.damaged.append((desc.stem, why))
            else:
                report.intact.append(desc.stem)
        if self.root.exists():
            for pdm in sorted(self.root.glob("*/*/rec-*.pdm")):
                if pdm not in described:
                    report.unfinished.append(pdm.stem)
            report.unfinished += sorted(t.name for t in self.root.glob("*/*/*.tmp"))
        return report
