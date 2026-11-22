"""Where data and the service's socket live.

Production (after the install scripts of step 11): /var/lib/eol and /run/eol. Those need
root to create, so on a bench machine without them everything goes under ~/eol-data.
Either can be set explicitly with EOL_DATA_DIR and EOL_RUN_DIR.
"""

from __future__ import annotations

import os
from pathlib import Path

PRODUCTION_DATA = Path("/var/lib/eol")
PRODUCTION_RUN = Path("/run/eol")
BENCH = Path.home() / "eol-data"


def _writable(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def data_dir() -> Path:
    if os.environ.get("EOL_DATA_DIR"):
        return Path(os.environ["EOL_DATA_DIR"])
    return PRODUCTION_DATA if _writable(PRODUCTION_DATA) else BENCH


def run_dir() -> Path:
    if os.environ.get("EOL_RUN_DIR"):
        return Path(os.environ["EOL_RUN_DIR"])
    return PRODUCTION_RUN if _writable(PRODUCTION_RUN) else BENCH / "run"


def acquire_socket() -> Path:
    return run_dir() / "acquire.sock"
