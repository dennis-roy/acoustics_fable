"""SQLite in WAL mode: opening, migrations, transactions, first-run contents.

Why SQLite: docs/decisions/2026-11-22-database-sqlite-wal.md.
Migrations are the numbered files in migrations/; PRAGMA user_version holds the number
of the last one applied. A migration runs in one transaction: all of it or none.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
MASTER_USERNAME = "master"
MASTER_DEFAULT_PASSWORD = "change-me-now"  # documented default; must be changed at first login

# Programming variables: key, kind, default, min, max, step, unit, description.
SETTINGS = [
    ("capture_limit_s", "int", "10", 5, 60, 1, "s", "Longest capture a part may be given in Setup (spec default 10 s, 1 s steps)"),
    ("samples_per_mode", "int", "10", 1, 25, 1, "", "Samples to collect per failure mode (recommended 10, up to 25)"),
    ("golden_samples", "int", "10", 1, 25, 1, "", "Golden OK samples to collect per part"),
    ("validation_every", "int", "5", 2, 10, 1, "", "Every Nth sample goes to the validation set; the rest train"),
    ("peak_threshold", "float", "0.7", 0.1, 1.0, 0.05, "", "Analysis: peaks above this share of the tallest are listed with their decay times"),
    ("lockout_attempts", "int", "5", 3, 10, 1, "", "Wrong passwords in a row before an account locks"),
    ("lockout_minutes", "int", "15", 1, 240, 1, "min", "How long a locked account stays locked"),
    ("session_idle_minutes", "int", "480", 5, 1440, 5, "min", "Log out after this long without activity"),
]


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")  # a committed result survives a power cut
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrations() -> list[tuple[int, Path]]:
    out = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = re.match(r"(\d{4})_", path.name)
        if m:
            out.append((int(m.group(1)), path))
    return out


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Applies every migration newer than the database. Returns the numbers applied."""
    applied = []
    for number, path in migrations():
        if number <= schema_version(conn):
            continue
        sql = path.read_text()
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(number)
    return applied


def seed(conn: sqlite3.Connection, hash_password) -> None:
    """First-run contents: programming variables, the master login, one station. Safe to repeat."""
    with transaction(conn):
        for key, kind, default, lo, hi, step, unit, desc in SETTINGS:
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value, kind, min_value, max_value, step, default_value, unit, description)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (key, default, kind, lo, hi, step, default, unit, desc))
        if not conn.execute("SELECT 1 FROM users WHERE is_master = 1").fetchone():
            conn.execute(
                "INSERT INTO users (username, role, password_hash, must_change_password, is_master, created_utc)"
                " VALUES (?, 'programming', ?, 1, 1, ?)", (MASTER_USERNAME, hash_password(MASTER_DEFAULT_PASSWORD), utcnow()))
            audit(conn, None, "master_created", "user", MASTER_USERNAME, {"note": "documented default password, change forced"})
        if not conn.execute("SELECT 1 FROM stations").fetchone():
            conn.execute("INSERT INTO stations (name, created_utc) VALUES ('Station 1', ?)", (utcnow(),))


def open_database(path: str | Path, hash_password) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    migrate(conn)
    seed(conn, hash_password)
    return conn


def audit(conn: sqlite3.Connection, user_id: int | None, action: str, object_type: str | None = None,
          object_id: str | int | None = None, detail: dict | None = None) -> None:
    conn.execute("INSERT INTO audit_log (at_utc, user_id, action, object_type, object_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
                 (utcnow(), user_id, action, object_type, None if object_id is None else str(object_id),
                  json.dumps(detail) if detail else None))


def setting(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value, kind FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise KeyError(key)
    return {"int": int, "float": float, "bool": lambda v: v == "1", "text": str}[row["kind"]](row["value"])
