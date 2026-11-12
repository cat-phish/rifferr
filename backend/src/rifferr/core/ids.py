"""ULIDs: 26-character, time-sortable ids (RUN-13). Stdlib only."""

from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(now_ms: int | None = None, random: bytes | None = None) -> str:
    """48-bit millisecond timestamp + 80 random bits, Crockford base32."""
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    rnd = os.urandom(10) if random is None else random
    if not 0 <= ms < 2**48 or len(rnd) != 10:
        raise ValueError("ULID needs a 48-bit timestamp and 10 random bytes")
    value = (ms << 80) | int.from_bytes(rnd, "big")
    chars: list[str] = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))
