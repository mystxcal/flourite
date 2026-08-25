from pathlib import Path

from ..blobs import BlobStore
from ..config import HarnessConfig
from .base import ArtifactAdapter, CallWorkspace
from .generic import MarkdownAdapter
from .profiles import get_profile
from .software import SoftwareAdapter


def create_adapter(
    name: str,
    *,
    run_dir: Path,
    blobs: BlobStore,
    workspace: Path | None,
    config: HarnessConfig,
) -> ArtifactAdapter:
    if name == "software":
        return SoftwareAdapter(
            run_dir=run_dir,
            blobs=blobs,
            workspace=workspace,
            policy=config.software,
        )
    return MarkdownAdapter(
        profile=get_profile(name),
        run_dir=run_dir,
        blobs=blobs,
        workspace=workspace,
    )


__all__ = [
    "ArtifactAdapter",
    "CallWorkspace",
    "MarkdownAdapter",
    "SoftwareAdapter",
    "create_adapter",
]
