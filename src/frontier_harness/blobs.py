"""Content-addressed blob storage for artifacts, evidence, and raw traces."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from .errors import LedgerIntegrityError
from .models import BlobRef
from .util import atomic_write_bytes, sha256_bytes


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, digest: str) -> Path:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise LedgerIntegrityError(f"Invalid SHA-256 blob digest: {digest!r}")
        return self.root / "sha256" / digest[:2] / digest[2:]

    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        original_name: str | None = None,
    ) -> BlobRef:
        digest = sha256_bytes(data)
        path = self._path_for(digest)
        if not path.exists():
            atomic_write_bytes(path, data, mode=0o600)
        else:
            existing = path.read_bytes()
            if len(existing) != len(data) or sha256_bytes(existing) != digest:
                raise LedgerIntegrityError(f"Blob collision or corruption at {path}")
        return BlobRef(
            digest=digest,
            size=len(data),
            media_type=media_type,
            relative_path=path.relative_to(self.root).as_posix(),
            original_name=original_name,
        )

    def put_text(
        self,
        text: str,
        *,
        media_type: str = "text/plain; charset=utf-8",
        original_name: str | None = None,
    ) -> BlobRef:
        return self.put_bytes(
            text.encode("utf-8"), media_type=media_type, original_name=original_name
        )

    def put_file(
        self,
        source: Path,
        *,
        media_type: str | None = None,
        original_name: str | None = None,
    ) -> BlobRef:
        if not source.is_file():
            raise FileNotFoundError(source)
        guessed = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        return self.put_bytes(
            source.read_bytes(),
            media_type=media_type or guessed,
            original_name=original_name or source.name,
        )

    def path(self, ref: BlobRef) -> Path:
        expected = self._path_for(ref.digest)
        expected_relative = expected.relative_to(self.root).as_posix()
        if ref.relative_path != expected_relative:
            raise LedgerIntegrityError(
                f"Blob reference path does not match its digest: {ref.digest}"
            )
        return expected

    def read_bytes(self, ref: BlobRef) -> bytes:
        path = self.path(ref)
        data = path.read_bytes()
        if sha256_bytes(data) != ref.digest or len(data) != ref.size:
            raise LedgerIntegrityError(f"Blob failed integrity verification: {ref.digest}")
        return data

    def read_text(self, ref: BlobRef) -> str:
        return self.read_bytes(ref).decode("utf-8")

    def materialize(self, ref: BlobRef, destination: Path, *, overwrite: bool = True) -> Path:
        data = self.read_bytes(ref)
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        atomic_write_bytes(destination, data, mode=0o600)
        return destination

    def verify(self, ref: BlobRef) -> None:
        path = self.path(ref)
        if not path.exists():
            raise LedgerIntegrityError(f"Missing blob: {ref.digest}")
        data = path.read_bytes()
        if len(data) != ref.size or sha256_bytes(data) != ref.digest:
            raise LedgerIntegrityError(f"Corrupted blob: {ref.digest}")
