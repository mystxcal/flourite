"""Lossless, bounded staging of explicit source material."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path

from ..blobs import BlobStore
from ..core.types import ContentRef
from ..util import atomic_write_text


@dataclass(frozen=True, slots=True)
class StagedInput:
    display_name: str
    relative_path: str
    content_ref: ContentRef

    def as_dict(self) -> dict[str, object]:
        return {
            "display_name": self.display_name,
            "relative_path": self.relative_path,
            "content_ref": self.content_ref.model_dump(mode="json"),
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> StagedInput:
        return cls(
            display_name=str(value["display_name"]),
            relative_path=str(value["relative_path"]),
            content_ref=ContentRef.model_validate(value["content_ref"]),
        )


def stage_sources(
    paths: list[Path],
    *,
    blobs: BlobStore,
    manifest_path: Path,
    max_files: int,
    max_bytes: int,
    excluded_globs: list[str],
) -> list[StagedInput]:
    staged: list[StagedInput] = []
    total_bytes = 0

    def excluded(relative: str) -> bool:
        normalized = relative.replace("\\", "/")
        return any(
            fnmatch.fnmatch(normalized, pattern)
            or fnmatch.fnmatch(f"{normalized}/", pattern)
            for pattern in excluded_globs
        )

    for supplied in paths:
        source = supplied.expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(source)
        if source.is_file():
            candidates = [(source.name, source)]
        else:
            candidates = [
                (f"{source.name}/{item.relative_to(source).as_posix()}", item)
                for item in sorted(source.rglob("*"))
                if item.is_file() and not item.is_symlink()
            ]
        for relative, item in candidates:
            if excluded(relative):
                continue
            size = item.stat().st_size
            if len(staged) + 1 > max_files:
                raise ValueError(f"source staging exceeds {max_files:,} files")
            if total_bytes + size > max_bytes:
                raise ValueError(f"source staging exceeds {max_bytes:,} bytes")
            content_ref = blobs.put_file(item, original_name=Path(relative).name)
            staged.append(
                StagedInput(
                    display_name=relative,
                    relative_path=relative,
                    content_ref=content_ref,
                )
            )
            total_bytes += size

    atomic_write_text(
        manifest_path,
        json.dumps([item.as_dict() for item in staged], indent=2, ensure_ascii=False),
    )
    return staged


def load_sources(manifest_path: Path) -> list[StagedInput]:
    if not manifest_path.is_file():
        return []
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("source manifest is malformed")
    return [StagedInput.from_dict(item) for item in value if isinstance(item, dict)]
