"""Thin domain-adapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..blobs import BlobStore
from ..models import ArtifactRef, EvidenceRecord

if TYPE_CHECKING:
    from .profiles import AdapterProfile


@dataclass(slots=True)
class CallWorkspace:
    call_id: str
    call_kind: str
    root: Path
    cwd: Path
    context_dir: Path
    output_dir: Path
    expected_artifact_path: Path
    baseline_commit: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ArtifactAdapter(ABC):
    name = "base"
    artifact_kind = "unknown"
    profile: AdapterProfile | None = None
    guidance = ""
    final_suffix = ".md"

    def __init__(
        self,
        *,
        run_dir: Path,
        blobs: BlobStore,
        workspace: Path | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.blobs = blobs
        self.workspace = workspace

    @abstractmethod
    def prepare(self) -> dict[str, Any]:
        """Prepare durable adapter state and return metadata for the run ledger."""

    @abstractmethod
    def open_call(
        self,
        *,
        call_id: str,
        call_kind: str,
        current_artifact: ArtifactRef | None,
    ) -> CallWorkspace:
        """Create an isolated workspace for one Codex call."""

    @abstractmethod
    def capture_artifact(
        self,
        workspace: CallWorkspace,
        *,
        declared_path: str,
        version: int,
        summary: str,
        parent: ArtifactRef | None,
        source_action_ids: list[str],
    ) -> ArtifactRef:
        """Capture the call's integrated artifact into the blob store."""

    def capture_candidate_artifact(
        self,
        workspace: CallWorkspace,
        *,
        summary: str,
        parent: ArtifactRef | None,
        source_action_ids: list[str],
    ) -> ArtifactRef | None:
        """Capture a complete branch state without making it authoritative."""

        return None

    @abstractmethod
    def close_call(self, workspace: CallWorkspace) -> None:
        """Release temporary resources. Durable capsules may remain by policy."""

    @abstractmethod
    def materialize_final(self, artifact: ArtifactRef, destination: Path) -> Path:
        """Write the final deliverable to a user-visible path."""

    def deterministic_checks(self, artifact: ArtifactRef) -> list[EvidenceRecord]:
        return []

    def staged_checks(self, artifact: ArtifactRef, *, stage: str) -> list[EvidenceRecord]:
        """Run explicitly cheap checks at an intermediate artifact boundary."""

        return []

    def apply_final_explicit(self, artifact: ArtifactRef) -> dict[str, Any] | None:
        """Apply a final artifact under explicit operator authority, if supported."""

        return None

    @staticmethod
    def ensure_runtime_directory(path: Path) -> Path:
        """Make a model-facing runtime namespace a real local directory.

        Workers have trusted filesystem access and may accidentally delete or
        replace a runtime-owned directory while cleaning their deliverable. A
        later move must reconstruct that control plane rather than confusing
        the worker's residue with durable run failure. Real directories keep
        their contents; files, sockets, and symlinks are disposable residue.
        """

        if path.is_symlink() or (path.exists() and not path.is_dir()):
            path.unlink()
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError(f"Runtime namespace is not a directory: {path}")
        return path

    @staticmethod
    def resolve_declared_path(workspace: CallWorkspace, declared_path: str) -> Path:
        candidate = (workspace.cwd / declared_path).resolve()
        root = workspace.cwd.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"Artifact reference escapes the call workspace: {declared_path}"
            ) from exc
        return candidate
