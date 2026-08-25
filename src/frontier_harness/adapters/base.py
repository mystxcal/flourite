"""Thin domain-adapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..blobs import BlobStore
from ..models import ArtifactRef, BlobRef, EvidenceRecord, ObjectiveMeasurement


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

    @property
    def objective_enabled(self) -> bool:
        return False

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

    @abstractmethod
    def capture_worker_result(self, workspace: CallWorkspace, declared_path: str) -> BlobRef:
        """Capture a worker result referenced by its minimal envelope."""

    def capture_worker_patch(self, workspace: CallWorkspace) -> BlobRef | None:
        return None

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

    def measure_candidate(self, workspace: CallWorkspace) -> ObjectiveMeasurement | None:
        """Measure a candidate with a domain-owned objective, when configured."""

        return None

    @abstractmethod
    def close_call(self, workspace: CallWorkspace) -> None:
        """Release temporary resources. Durable capsules may remain by policy."""

    @abstractmethod
    def artifact_text(self, artifact: ArtifactRef) -> str:
        """Return a controller-readable representation of the current artifact."""

    @abstractmethod
    def materialize_final(self, artifact: ArtifactRef, destination: Path) -> Path:
        """Write the final deliverable to a user-visible path."""

    def deterministic_checks(self, artifact: ArtifactRef) -> list[EvidenceRecord]:
        return []

    def staged_checks(self, artifact: ArtifactRef, *, stage: str) -> list[EvidenceRecord]:
        """Run explicitly cheap checks at an intermediate artifact boundary."""

        return []

    def verification_contract(self) -> dict[str, Any]:
        """Expose exact adapter-owned acceptance machinery to every model call."""

        return {}

    def capture_evidence_artifacts(
        self, workspace: CallWorkspace, declared_paths: list[str]
    ) -> list[BlobRef]:
        """Durably preserve model-declared diagnostic outputs before cleanup."""

        return []

    def apply_final(self, artifact: ArtifactRef) -> dict[str, Any] | None:
        return None

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
