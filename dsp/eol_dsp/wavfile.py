"""Writes samples as a 32-bit floating-point WAV file, so nothing is clipped or rounded.

Python's wave module writes integer samples only, hence this small writer. Audacity and
most sound tools open the result. 1.0 in the file is full scale of the 1-bit stream.
"""

from __future__ import annotations

import struct

import numpy as np

_IEEE_FLOAT = 3


def write_float_wav(path: str, values: np.ndarray, rate_hz: float) -> None:
    data = np.asarray(values, dtype="<f4").tobytes()
    n = len(data) // 4
    fmt = struct.pack("<HHIIHH", _IEEE_FLOAT, 1, int(round(rate_hz)), int(round(rate_hz)) * 4, 4, 32)
    fmt_chunk = b"fmt " + struct.pack("<I", len(fmt) + 2) + fmt + b"\0\0"  # cbSize = 0
    fact_chunk = b"fact" + struct.pack("<II", 4, n)
    data_chunk = b"data" + struct.pack("<I", len(data)) + data
    body = b"WAVE" + fmt_chunk + fact_chunk + data_chunk
    with open(path, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", len(body)) + body)


def read_float_wav(path: str) -> tuple[np.ndarray, int]:
    """Reads back what write_float_wav wrote (used by the tests)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos, rate, values = 12, 0, None
    while pos < len(raw):
        cid, size = raw[pos : pos + 4], struct.unpack_from("<I", raw, pos + 4)[0]
        body = raw[pos + 8 : pos + 8 + size]
        if cid == b"fmt ":
            tag, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", body)
            if (tag, channels, bits) != (_IEEE_FLOAT, 1, 32):
                raise ValueError("not a mono 32-bit float WAV")
        elif cid == b"data":
            values = np.frombuffer(body, dtype="<f4")
        pos += 8 + size + (size & 1)
    return values, rate
