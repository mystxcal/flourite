"""Sortable, dependency-free identifiers."""

from __future__ import annotations

import secrets
import time

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def _base32(number: int, width: int) -> str:
    chars: list[str] = []
    for _ in range(width):
        number, remainder = divmod(number, 32)
        chars.append(_ALPHABET[remainder])
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """Generate a compact time-sortable identifier.

    The timestamp portion sorts lexicographically; 50 random bits make collision
    probability negligible for local harness workloads.
    """

    millis = int(time.time() * 1000)
    random_bits = secrets.randbits(50)
    return f"{prefix}_{_base32(millis, 10)}{_base32(random_bits, 10)}"
