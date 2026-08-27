"""Markdown artifact adapter used by generic, research, formal, decision, and creative profiles."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..blobs import BlobStore
from ..ids import new_id
from ..models import ArtifactRef
from ..util import utc_now
from .base import ArtifactAdapter, CallWorkspace
from .profiles import AdapterProfile


class MarkdownAdapter(ArtifactAdapter):
    artifact_kind = "markdown"
    profile: AdapterProfile

    def __init__(
        self,
        *,
        profile: AdapterProfile,
        run_dir: Path,
        blobs: BlobStore,
        workspace: Path | None,
    ) -> None:
        super().__init__(run_dir=run_dir, blobs=blobs, workspace=workspace)
        self.profile = profile
        self.name = profile.name
        self.guidance = profile.guidance

    def prepare(self) -> dict[str, object]:
        (self.run_dir / "capsules").mkdir(parents=True, exist_ok=True)
        return {
            "profile": self.profile.name,
            "artifact_kind": self.artifact_kind,
            "profile_guidance": self.profile.guidance,
        }

    def open_call(
        self,
        *,
        call_id: str,
        call_kind: str,
        current_artifact: ArtifactRef | None,
    ) -> CallWorkspace:
        root = self.run_dir / "capsules" / call_id
        if root.exists():
            shutil.rmtree(root)
        context = root / "input"
        output = root / "output"
        context.mkdir(parents=True)
        output.mkdir(parents=True)
        expected_artifact = output / "artifact.md"
        if current_artifact is not None:
            if current_artifact.kind != self.artifact_kind:
                raise ValueError(
                    f"Expected {self.artifact_kind} recovery artifact, got {current_artifact.kind}"
                )
            self.blobs.materialize(current_artifact.blob, expected_artifact)
        return CallWorkspace(
            call_id=call_id,
            call_kind=call_kind,
            root=root,
            cwd=root,
            context_dir=context,
            output_dir=output,
            expected_artifact_path=expected_artifact,
            metadata={"profile": self.profile.name},
        )

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
        path = self.resolve_declared_path(workspace, declared_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"The model declared artifact {declared_path!r}, but no file exists there"
            )
        blob = self.blobs.put_file(
            path,
            media_type="text/markdown; charset=utf-8",
            original_name=f"artifact-v{version}.md",
        )
        return ArtifactRef(
            artifact_id=new_id("art"),
            version=version,
            blob=blob,
            kind=self.artifact_kind,
            summary=summary,
            parent_artifact_id=parent.artifact_id if parent else None,
            source_action_ids=source_action_ids,
            created_at=utc_now(),
        )

    def capture_candidate_artifact(
        self,
        workspace: CallWorkspace,
        *,
        summary: str,
        parent: ArtifactRef | None,
        source_action_ids: list[str],
    ) -> ArtifactRef | None:
        if not workspace.expected_artifact_path.is_file():
            return None
        blob = self.blobs.put_file(
            workspace.expected_artifact_path,
            media_type="text/markdown; charset=utf-8",
            original_name=f"{workspace.call_id}-candidate.md",
        )
        return ArtifactRef(
            artifact_id=new_id("art"),
            version=(parent.version + 1 if parent else 1),
            blob=blob,
            kind=self.artifact_kind,
            summary=summary,
            parent_artifact_id=parent.artifact_id if parent else None,
            source_action_ids=source_action_ids,
            created_at=utc_now(),
        )

    def close_call(self, workspace: CallWorkspace) -> None:
        # Capsules are intentionally retained by default. The engine may remove
        # them after capture when configured; the adapter owns no extra handles.
        return None

    def materialize_final(self, artifact: ArtifactRef, destination: Path) -> Path:
        return self.blobs.materialize(artifact.blob, destination)
