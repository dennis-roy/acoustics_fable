"""The simulated microphone signal (protocol/README.md, "Simulated microphone signal").

A 1-bit stream as a microphone would send it, made with whole numbers only so that the
C stand-in (firmware/host/core_host.c) and this file produce exactly the same bits.
"""

from __future__ import annotations

from functools import lru_cache

PERIODS = 320_000  # the signal repeats after this many clock periods
BLOCK_BYTES = PERIODS // 8


@lru_cache(maxsize=1)
def block() -> bytes:
    """One repetition, 40 000 bytes. Bit 7 of byte 0 is period 0."""
    out = bytearray(BLOCK_BYTES)
    v1 = v2 = 0
    byte = 0
    for n in range(PERIODS):
        x = (16384 if n % 3200 < 1600 else -16384) + (4096 if n % 100 < 50 else -4096)
        y = 65536 if v2 >= 0 else -65536
        v1 += x - y
        v2 += v1 - y
        byte = (byte << 1) | (y > 0)
        if n & 7 == 7:
            out[n >> 3] = byte
            byte = 0
    return bytes(out)


def stream(first_byte: int, length: int) -> bytes:
    """`length` bytes of the endless, repeating signal, starting `first_byte` bytes in."""
    data = block()
    start = first_byte % BLOCK_BYTES
    reps = (start + length) // BLOCK_BYTES + 1
    return (data * reps)[start : start + length]
