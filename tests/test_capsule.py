from __future__ import annotations

import json
from pathlib import Path

import pytest

from frontier_harness.adapters.generic import MarkdownAdapter
from frontier_harness.adapters.profiles import get_profile
from frontier_harness.blobs import BlobStore
from frontier_harness.capsule import CapsuleBuilder, CapsuleSpec, stage_sources
from frontier_harness.models import ContextLens


def test_staged_sources_are_immutable_and_exclusions_are_pruned(tmp_path: Path) -> None:
    supplied = tmp_path / "supplied"
    supplied.mkdir()
    source = supplied / "brief.md"
    source.write_text("version one\n", encoding="utf-8")
    (supplied / ".git").mkdir()
    (supplied / ".git" / "private").write_text("not staged", encoding="utf-8")
    excluded_root = supplied / "generated"
    excluded_root.mkdir()
    (excluded_root / "result.txt").write_text("not staged", encoding="utf-8")

    run_dir = tmp_path / "run"
    blobs = BlobStore(run_dir / "blobs")
    staged = stage_sources(
        [supplied],
        run_dir=run_dir,
        blobs=blobs,
        max_total_bytes=10_000,
        max_files=10,
        excluded_globs=[".git/**"],
        exclude_roots=[excluded_root],
    )

    assert [item.display_name for item in staged] == ["supplied/brief.md"]
    captured = staged[0]
    assert captured.stored_path.read_text(encoding="utf-8") == "version one\n"

    # The durable snapshot and blob cannot change with the original file.
    source.write_text("version two\n", encoding="utf-8")
    assert captured.stored_path.read_text(encoding="utf-8") == "version one\n"
    assert blobs.read_text(captured.blob) == "version one\n"

    adapter = MarkdownAdapter(
        profile=get_profile("generic"),
        run_dir=run_dir,
        blobs=blobs,
        workspace=None,
    )
    adapter.prepare()
    workspace = adapter.open_call(
        call_id="capsule",
        call_kind="worker",
        current_artifact=None,
    )
    builder = CapsuleBuilder(adapter=adapter, blobs=blobs, sources=staged)
    manifest = builder.populate(
        workspace,
        CapsuleSpec(
            task="Use the supplied brief.",
            assignment="Read the brief.",
        ),
    )
    assert (workspace.context_dir / "VERIFICATION_CONTRACT.json").read_text() == "{}"
    lens = ContextLens.model_validate(
        json.loads((workspace.context_dir / "CONTEXT_LENS.json").read_text())
    )
    assert manifest["context_lens_digest"] == lens.digest
    assert lens.task_source_digest
    assert lens.artifact_view == "none"
    assert json.loads((workspace.context_dir / "OBSERVATION_CONTRACT.json").read_text()) == []
    capsule_copy = next((workspace.context_dir / "sources").iterdir())
    capsule_copy.write_text("worker mutation\n", encoding="utf-8")

    assert captured.stored_path.read_text(encoding="utf-8") == "version one\n"
    assert blobs.read_text(captured.blob) == "version one\n"


def test_source_limits_are_enforced_before_partial_overflow(tmp_path: Path) -> None:
    supplied = tmp_path / "supplied"
    supplied.mkdir()
    (supplied / "a.txt").write_text("a", encoding="utf-8")
    (supplied / "b.txt").write_text("b", encoding="utf-8")
    run_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="limit of 1 files"):
        stage_sources(
            [supplied],
            run_dir=run_dir,
            blobs=BlobStore(run_dir / "blobs"),
            max_total_bytes=100,
            max_files=1,
        )

    oversized = tmp_path / "oversized.bin"
    oversized.write_bytes(b"12345")
    with pytest.raises(ValueError, match="limit of 4 bytes"):
        stage_sources(
            [oversized],
            run_dir=tmp_path / "other-run",
            blobs=BlobStore(tmp_path / "other-run" / "blobs"),
            max_total_bytes=4,
        )


def test_recursive_source_staging_does_not_follow_symlinks_outside_root(
    tmp_path: Path,
) -> None:
    supplied = tmp_path / "supplied"
    supplied.mkdir()
    (supplied / "inside.txt").write_text("inside", encoding="utf-8")
    external = tmp_path / "external.txt"
    external.write_text("outside-secret", encoding="utf-8")
    link = supplied / "external-link.txt"
    try:
        link.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")

    run_dir = tmp_path / "run"
    staged = stage_sources(
        [supplied],
        run_dir=run_dir,
        blobs=BlobStore(run_dir / "blobs"),
        max_total_bytes=10_000,
    )
    assert [item.display_name for item in staged] == ["supplied/inside.txt"]
