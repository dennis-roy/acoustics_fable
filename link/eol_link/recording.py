"""The recording file format: a raw 1-bit stream and a description of it, written safely.

A recording is two files with the same name:

  NAME.pdm   the PDM payload bytes exactly as the Pico sent them (bit 7 of byte 0 first)
  NAME.json  what it is: clock, length, SHA-256 of NAME.pdm, and whatever the writer adds

Written so that a power cut or an emergency exit cannot leave a recording that looks
complete but is not (review bench item 34):

  1. each file is written under a temporary name, flushed to the disk and then renamed;
  2. NAME.pdm first, NAME.json last, so a .pdm without its .json is an unfinished write;
  3. both files are made read-only.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

FORMAT = "eol-recording"
FORMAT_VERSION = 1
BIT_ORDER = "raw 1-bit stream; bit 7 of byte 0 is the earliest sample"


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o444)
    os.replace(tmp, path)


def _sync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_recording(directory: str | Path, name: str, raw: bytes, meta: dict) -> tuple[Path, str]:
    """Writes NAME.pdm and NAME.json in `directory`. Returns (path of the .pdm, its SHA-256).

    `meta` must hold at least clock_hz. The format fields, size and SHA-256 are added here."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    pdm, desc = directory / f"{name}.pdm", directory / f"{name}.json"
    if pdm.exists() or desc.exists():
        raise FileExistsError(f"a recording named {name} already exists in {directory}")
    digest = hashlib.sha256(raw).hexdigest()
    record = {"format": FORMAT, "format_version": FORMAT_VERSION, "file": pdm.name, "bit_order": BIT_ORDER,
              "bytes": len(raw), "sha256": digest, **meta}
    _write_atomic(pdm, raw)
    _sync_dir(directory)
    _write_atomic(desc, (json.dumps(record, indent=2) + "\n").encode())
    _sync_dir(directory)
    return pdm, digest


def verify_recording(desc_path: str | Path) -> str | None:
    """Checks one recording against its description. Returns None if it is intact, else why not."""
    desc_path = Path(desc_path)
    try:
        record = json.loads(desc_path.read_text())
    except (OSError, ValueError) as exc:
        return f"description unreadable: {exc}"
    if record.get("format") != FORMAT:
        return "not an eol-recording description"
    pdm = desc_path.with_name(record.get("file", ""))
    if not pdm.is_file():
        return f"{pdm.name} is missing"
    h = hashlib.sha256()
    size = 0
    with open(pdm, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
            size += len(block)
    if size != record.get("bytes"):
        return f"{pdm.name} is {size} bytes, the description says {record.get('bytes')}"
    if h.hexdigest() != record.get("sha256"):
        return f"{pdm.name} does not match its SHA-256: the data has changed"
    return None
