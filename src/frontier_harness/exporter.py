"""Portable run exports.

Two deliberately different modes avoid a misleading middle ground:

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
from typing import Literal

from .engine import FrontierEngine
from .models import ArtifactRef
from .util import atomic_write_text, canonical_json, redact_secrets

ExportMode = Literal["diagnostic", "audit"]


def _write_json(path: Path, value: object, *, redact: bool) -> None:
    text = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    if redact:
        text = redact_secrets(text)
    atomic_write_text(path, text)


def _materialize_artifact(
    engine: FrontierEngine,
    artifact: ArtifactRef,
    destination: Path,
    *,
    redact: bool,
) -> None:
    if not redact:
        engine.blobs.materialize(artifact.blob, destination)
        return
    # All built-in artifact adapters currently emit UTF-8 Markdown or Git
    # patches. Redact the content itself rather than merely redacting metadata.
    text = engine.blobs.read_bytes(artifact.blob).decode("utf-8", errors="replace")
    atomic_write_text(destination, redact_secrets(text))


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


def export_run(
    engine: FrontierEngine,
    destination: Path,
    *,
    mode: ExportMode = "diagnostic",
) -> Path:
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sfh-export-") as temp:
        root = Path(temp) / f"{engine.state.run_id}-{mode}"
        root.mkdir(parents=True)
        redact = mode == "diagnostic" and engine.config.run.export_redacts_secrets

        _write_json(
            root / "export-manifest.json",
            {
                "run_id": engine.state.run_id,
                "mode": mode,
                "redacted": redact,
                "integrity_note": (
                    "This diagnostic export is transformed and should not be used to verify the local hash chain."
                    if mode == "diagnostic"
                    else "This audit export contains the exact ledger and content-addressed blobs."
                ),
                "privacy_note": (
                    "Credential-pattern redaction is best-effort. The bundle may still contain private or identifying content."
                    if mode == "diagnostic"
                    else "The audit bundle is intentionally lossless and must be handled as sensitive data."
                ),
            },
            redact=False,
        )
        _write_json(
            root / "state.json",
            engine.state.model_dump(mode="json"),
            redact=redact,
        )
        _write_json(
            root / "config.json",
            engine.config.model_dump(mode="json"),
            redact=redact,
        )
        if (engine.run_dir / engine.SEAL_FILE).exists():
            shutil.copy2(engine.run_dir / engine.SEAL_FILE, root / "seal.json")

        events_path = root / "events.jsonl"
        lines: list[str] = []
        for event in engine.events():
            line = canonical_json(event.model_dump(mode="json"))
            lines.append(redact_secrets(line) if redact else line)
        atomic_write_text(events_path, "\n".join(lines) + "\n")

        artifacts_dir = root / "artifacts"
        artifacts_dir.mkdir()
        for artifact in engine.state.artifact_history:
            suffix = ".patch" if artifact.kind == "git-patch" else ".md"
            _materialize_artifact(
                engine,
                artifact,
                artifacts_dir / f"v{artifact.version:03d}-{artifact.artifact_id}{suffix}",
                redact=redact,
            )
        if engine.state.final_artifact is not None:
            suffix = ".patch" if engine.state.final_artifact.kind == "git-patch" else ".md"
            _materialize_artifact(
                engine,
                engine.state.final_artifact,
                root / f"final{suffix}",
                redact=redact,
            )

        if mode == "audit":
            engine.ledger.backup(root / engine.LEDGER_FILE)
            for name in ("blobs", "sources", "capsules", "software"):
                source = engine.run_dir / name
                if source.exists():
                    shutil.copytree(source, root / name, symlinks=True)
            warning = (
                "AUDIT EXPORT: This bundle is lossless and may contain credentials, private source files, "
                "prompts, and raw model/tool traces. Handle it as sensitive data.\n"
            )
            atomic_write_text(root / "SENSITIVE.txt", warning)

        archive = destination
        if archive.suffix.casefold() != ".zip":
            archive = archive.with_suffix(".zip")
        tmp_archive = archive.with_name(f".{archive.name}.tmp")
        with zipfile.ZipFile(tmp_archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            _archive_tree(bundle, root)
        tmp_archive.replace(archive)
        return archive
