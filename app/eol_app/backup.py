"""Backup and restore of the database, the calibrated raw recordings and the models (spec, deployment 5).

A backup is a folder eol-backup-<time>/ holding eol.sqlite3 (taken with SQLite's online backup,
so the application may keep running), recordings/, models/ and manifest.json with the SHA-256
of every file. Restore checks every SHA-256 before copying anything back.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def backup(conn: sqlite3.Connection, db_path: str, dest: str) -> Path:
    data = Path(db_path).parent
    out = Path(dest) / time.strftime("eol-backup-%Y%m%dT%H%M%SZ", time.gmtime())
    out.mkdir(parents=True)
    target = sqlite3.connect(out / "eol.sqlite3")
    conn.backup(target)
    target.close()
    for sub in ("recordings", "models"):
        if (data / sub).exists():
            shutil.copytree(data / sub, out / sub)
    files = {str(p.relative_to(out)): _sha(p) for p in sorted(out.rglob("*")) if p.is_file()}
    (out / "manifest.json").write_text(json.dumps({"made_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                    "files": files}, indent=1) + "\n")
    return out


def restore(source: str, db_path: str, force: bool = False) -> str:
    src = Path(source)
    manifest = json.loads((src / "manifest.json").read_text())
    for rel, digest in manifest["files"].items():
        if _sha(src / rel) != digest:
            raise SystemExit(f"backup damaged: {rel} does not match its SHA-256; nothing restored")
    data = Path(db_path).parent
    if Path(db_path).exists() and not force:
        raise SystemExit(f"{db_path} exists; add --force to replace it (stop the services first)")
    data.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(db_path + suffix).unlink(missing_ok=True)
    shutil.copy2(src / "eol.sqlite3", db_path)
    for sub in ("recordings", "models"):
        if (src / sub).exists():
            shutil.copytree(src / sub, data / sub, dirs_exist_ok=True)
    return f"restored {len(manifest['files'])} files from {src} (backup made {manifest['made_utc']})"
