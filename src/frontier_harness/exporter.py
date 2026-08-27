"""Portable kernel run exports.

Two modes avoid a misleading middle ground:

* diagnostic: redacted, human-readable state and events plus artifacts;
* audit: exact ledger, blob store, sources, and retained capsules.

The audit bundle is lossless and may contain secrets present in tool/model
traces.  The diagnostic bundle is safer to share but is not an integrity-preserving
substitute for the local ledger.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .util import atomic_write_text, canonical_json, redact_secrets

if TYPE_CHECKING:
    from .runtime.engine import KernelEngine

ExportMode = Literal["diagnostic", "audit"]


def _write_json(path: Path, value: object, *, redact: bool) -> None:
    text = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    if redact:
        text = redact_secrets(text)
    atomic_write_text(path, text)


def _write_symlink(bundle: zipfile.ZipFile, path: Path, archive_name: str) -> None:
    info = zipfile.ZipInfo(archive_name)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    bundle.writestr(info, os.readlink(path))


def _archive_tree(bundle: zipfile.ZipFile, root: Path) -> None:
    """Archive without dereferencing symlinks outside the export root."""

    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in list(dirnames):
            path = current / name
            if path.is_symlink():
                archive_name = path.relative_to(root.parent).as_posix()
                _write_symlink(bundle, path, archive_name)
                dirnames.remove(name)
        for name in filenames:
            path = current / name
            archive_name = path.relative_to(root.parent).as_posix()
            if path.is_symlink():
                _write_symlink(bundle, path, archive_name)
            elif path.is_file():
                bundle.write(path, archive_name)


def export_kernel_run(
    engine: KernelEngine,
    destination: Path,
    *,
    mode: ExportMode = "diagnostic",
) -> Path:
    """Export the canonical kernel directly from its journal projection."""

    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="flourite-kernel-export-") as temp:
        root = Path(temp) / f"{engine.state.run_id}-{mode}"
        root.mkdir(parents=True)
        redact = mode == "diagnostic" and engine.config.run.export_redacts_secrets
        _write_json(
            root / "export-manifest.json",
            {
                "run_id": engine.state.run_id,
                "architecture": engine.ARCHITECTURE,
                "mode": mode,
                "redacted": redact,
                "integrity_note": (
                    "This transformed diagnostic is not a substitute for ledger verification."
                    if mode == "diagnostic"
                    else "This audit contains the exact ledger and content-addressed objects."
                ),
                "privacy_note": (
                    "Pattern redaction is best-effort; review before sharing."
                    if mode == "diagnostic"
                    else "This lossless audit may contain private sources, prompts, and traces."
                ),
            },
            redact=False,
        )
        _write_json(root / "state.json", engine.state.model_dump(mode="json"), redact=redact)
        _write_json(root / "config.json", engine.config.model_dump(mode="json"), redact=redact)
        lines = [canonical_json(event.model_dump(mode="json")) for event in engine.journal.events()]
        if redact:
            lines = [redact_secrets(line) for line in lines]
        atomic_write_text(root / "events.jsonl", "\n".join(lines) + "\n")

        if engine.state.current_workspace is not None:
            current = engine.materialize_current(root / "current-artifact")
            if redact and current.is_file():
                atomic_write_text(
                    current,
                    redact_secrets(current.read_text(encoding="utf-8", errors="replace")),
                )

        if mode == "audit":
            engine.journal.ledger.backup(root / engine.LEDGER_FILE)
            for name in (
                "blobs",
                "sources",
                "kernel-executions",
                "provider-sessions",
            ):
                source = engine.run_dir / name
                if source.is_dir():
                    shutil.copytree(source, root / name, symlinks=True)
                elif source.is_file():
                    shutil.copy2(source, root / name)
            atomic_write_text(
                root / "SENSITIVE.txt",
                "AUDIT EXPORT: lossless private run material; do not publish without review.\n",
            )

        archive = (
            destination
            if destination.suffix.casefold() == ".zip"
            else destination.with_suffix(".zip")
        )
        tmp_archive = archive.with_name(f".{archive.name}.tmp")
        with zipfile.ZipFile(tmp_archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            _archive_tree(bundle, root)
        tmp_archive.replace(archive)
        return archive
