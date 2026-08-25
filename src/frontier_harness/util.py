"""Small deterministic utilities used throughout the runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Hashable, Iterable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

UniqueT = TypeVar("UniqueT", bound=Hashable)


def utc_now() -> str:
    """Return an RFC 3339 UTC timestamp with microsecond precision."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically for hashing and durable storage."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Atomically replace *path* with *data* in the same filesystem."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        with suppress(OSError):
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def normalize_key(value: str) -> str:
    """Normalize free text for inexpensive duplicate detection."""

    return re.sub(r"\s+", " ", value.strip().casefold())


def safe_slug(value: str, *, limit: int = 64) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return (slug[:limit] or "item").lower()


def redact_secrets(text: str) -> str:
    """Best-effort redaction for exported diagnostics, never the local ledger.

    This intentionally targets high-confidence credential shapes only. The local
    ledger remains lossless; redaction is an export boundary.
    """

    patterns = (
        (r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}\b", "[REDACTED_OPENAI_KEY]"),
        (r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "[REDACTED_GITHUB_TOKEN]"),
        (r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "[REDACTED_GITHUB_TOKEN]"),
        (r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge mappings; non-mappings replace the base value."""

    result: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = deep_merge(current, value)
        else:
            result[key] = value
    return result


def unique_preserving_order(values: Iterable[UniqueT]) -> list[UniqueT]:
    seen: set[UniqueT] = set()
    output: list[UniqueT] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output
