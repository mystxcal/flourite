from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from frontier_harness.adapters.software import SoftwareAdapter
from frontier_harness.blobs import BlobStore
from frontier_harness.config import SoftwarePolicy
from frontier_harness.errors import WorkspaceError
from frontier_harness.util import atomic_write_text


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=True,
    )


def _adapter(tmp_path: Path, repo: Path) -> SoftwareAdapter:
    run_dir = tmp_path / "run"
    return SoftwareAdapter(
        run_dir=run_dir,
        blobs=BlobStore(run_dir / "blobs"),
        workspace=repo,
        policy=SoftwarePolicy(checks=[]),
    )


def test_software_snapshot_isolation_capture_and_idempotent_apply(
    tmp_path: Path, git_repo: Path
) -> None:
    # The user's real starting artifact is dirty and includes an untracked file.
    (git_repo / "app.txt").write_text("base\nuser-dirty\n", encoding="utf-8")
    (git_repo / "notes.txt").write_text("untracked baseline\n", encoding="utf-8")

    adapter = _adapter(tmp_path, git_repo)
    metadata = adapter.prepare()
    assert metadata["source_was_dirty"] is True

    workspace = adapter.open_call(
        call_id="bootstrap",
        call_kind="bootstrap",
        current_artifact=None,
    )
    try:
        assert (workspace.cwd / "app.txt").read_text() == "base\nuser-dirty\n"
        assert (workspace.cwd / "notes.txt").read_text() == "untracked baseline\n"
        (workspace.cwd / "app.txt").write_text("base\nuser-dirty\nmodel-change\n", encoding="utf-8")
        (workspace.cwd / "new.py").write_text("print('new')\n", encoding="utf-8")
        atomic_write_text(workspace.expected_artifact_path, "Implemented candidate.\n")
        artifact = adapter.capture_artifact(
            workspace,
            declared_path=workspace.expected_artifact_path.relative_to(workspace.cwd).as_posix(),
            version=1,
            summary="candidate",
            parent=None,
            source_action_ids=[],
        )
    finally:
        adapter.close_call(workspace)

    # Candidate edits never touched the source repo.
    assert (git_repo / "app.txt").read_text() == "base\nuser-dirty\n"
    assert not (git_repo / "new.py").exists()

    verification = adapter.open_call(
        call_id="verify",
        call_kind="verify",
        current_artifact=artifact,
    )
    try:
        assert "model-change" in (verification.cwd / "app.txt").read_text()
        assert (verification.cwd / "new.py").exists()
    finally:
        adapter.close_call(verification)

    first = adapter.apply_final_explicit(artifact)
    assert first and first["applied"] is True
    assert "model-change" in (git_repo / "app.txt").read_text()
    assert (git_repo / "new.py").exists()

    second = adapter.apply_final_explicit(artifact)
    assert second and second["applied"] is False
    assert second["reason"] == "final patch was already applied exactly"


def test_reopening_an_interrupted_call_preserves_live_work(tmp_path: Path, git_repo: Path) -> None:
    adapter = _adapter(tmp_path, git_repo)
    adapter.prepare()
    first = adapter.open_call(
        call_id="interrupted",
        call_kind="lead",
        current_artifact=None,
    )
    (first.cwd / "app.txt").write_text("valuable unfinished work\n", encoding="utf-8")
    (first.output_dir / "workspace.md").write_text("live map\n", encoding="utf-8")

    resumed = adapter.open_call(
        call_id="interrupted",
        call_kind="lead",
        current_artifact=None,
    )
    try:
        assert resumed.metadata["resumed"] is True
        assert (resumed.cwd / "app.txt").read_text() == "valuable unfinished work\n"
        assert (resumed.output_dir / "workspace.md").read_text() == "live map\n"
        assert resumed.baseline_commit == first.baseline_commit
    finally:
        adapter.close_call(resumed)


def test_apply_refuses_source_change_after_snapshot(tmp_path: Path, git_repo: Path) -> None:
    adapter = _adapter(tmp_path, git_repo)
    adapter.prepare()
    workspace = adapter.open_call(
        call_id="candidate",
        call_kind="final",
        current_artifact=None,
    )
    try:
        (workspace.cwd / "app.txt").write_text("candidate\n", encoding="utf-8")
        atomic_write_text(workspace.expected_artifact_path, "summary\n")
        artifact = adapter.capture_artifact(
            workspace,
            declared_path=workspace.expected_artifact_path.relative_to(workspace.cwd).as_posix(),
            version=1,
            summary="candidate",
            parent=None,
            source_action_ids=[],
        )
    finally:
        adapter.close_call(workspace)

    (git_repo / "app.txt").write_text("external change\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="changed since the immutable snapshot"):
        adapter.apply_final_explicit(artifact)


def test_deterministic_checks_run_in_isolation(tmp_path: Path, git_repo: Path) -> None:
    run_dir = tmp_path / "run"
    adapter = SoftwareAdapter(
        run_dir=run_dir,
        blobs=BlobStore(run_dir / "blobs"),
        workspace=git_repo,
        policy=SoftwarePolicy(
            checks=[
                f'{shlex.quote(sys.executable)} -c "from pathlib import Path; '
                "Path('check-side-effect').write_text('x')\""
            ],
        ),
    )
    adapter.prepare()
    workspace = adapter.open_call(
        call_id="artifact",
        call_kind="final",
        current_artifact=None,
    )
    try:
        atomic_write_text(workspace.expected_artifact_path, "summary\n")
        artifact = adapter.capture_artifact(
            workspace,
            declared_path=workspace.expected_artifact_path.relative_to(workspace.cwd).as_posix(),
            version=1,
            summary="baseline",
            parent=None,
            source_action_ids=[],
        )
    finally:
        adapter.close_call(workspace)

    evidence = adapter.deterministic_checks(artifact)
    assert len(evidence) == 1
    assert not evidence[0].negative_result
    assert not (git_repo / "check-side-effect").exists()


def test_declared_generated_deliverable_survives_between_isolated_calls(
    tmp_path: Path, git_repo: Path
) -> None:
    run_dir = tmp_path / "run-deliverable"
    adapter = SoftwareAdapter(
        run_dir=run_dir,
        blobs=BlobStore(run_dir / "blobs"),
        workspace=git_repo,
        policy=SoftwarePolicy(
            release_artifacts=["dist/*.mp4"],
        ),
    )
    adapter.prepare()
    workspace = adapter.open_call(
        call_id="render",
        call_kind="final",
        current_artifact=None,
    )
    try:
        rendered = workspace.cwd / "dist" / "film.mp4"
        rendered.parent.mkdir(parents=True)
        rendered.write_bytes(b"rendered-film")
        atomic_write_text(workspace.expected_artifact_path, "Rendered film.\n")
        artifact = adapter.capture_artifact(
            workspace,
            declared_path=workspace.expected_artifact_path.relative_to(workspace.cwd).as_posix(),
            version=1,
            summary="film",
            parent=None,
            source_action_ids=[],
        )
    finally:
        adapter.close_call(workspace)

    assert [item.original_name for item in artifact.deliverables] == ["dist/film.mp4"]
    reopened = adapter.open_call(
        call_id="release-review",
        call_kind="release",
        current_artifact=artifact,
    )
    try:
        assert (reopened.cwd / "dist" / "film.mp4").read_bytes() == b"rendered-film"
    finally:
        adapter.close_call(reopened)


def test_missing_declared_release_artifact_cannot_pass_release_evidence(
    tmp_path: Path, git_repo: Path
) -> None:
    run_dir = tmp_path / "run-missing-deliverable"
    adapter = SoftwareAdapter(
        run_dir=run_dir,
        blobs=BlobStore(run_dir / "blobs"),
        workspace=git_repo,
        policy=SoftwarePolicy(release_artifacts=["dist/master.mp4"]),
    )
    adapter.prepare()
    workspace = adapter.open_call(
        call_id="trajectory_root",
        call_kind="lead",
        current_artifact=None,
    )
    (workspace.cwd / "app.txt").write_text("source without master\n", encoding="utf-8")
    artifact = adapter.capture_candidate_artifact(
        workspace,
        summary="incomplete release",
        parent=None,
        source_action_ids=[],
    )
    assert artifact is not None

    evidence = adapter.deterministic_checks(artifact)

    assert len(evidence) == 1
    assert evidence[0].negative_result is True
    assert "dist/master.mp4" in evidence[0].summary
    assert workspace.cwd.is_dir()


def test_source_only_release_check_is_promoted_to_preflight(tmp_path: Path, git_repo: Path) -> None:
    command = f"{shlex.quote(sys.executable)} -c \"print('checked')\" --source-only"
    run_dir = tmp_path / "run-preflight"
    adapter = SoftwareAdapter(
        run_dir=run_dir,
        blobs=BlobStore(run_dir / "blobs"),
        workspace=git_repo,
        policy=SoftwarePolicy(checks=[command]),
    )
    adapter.prepare()
    workspace = adapter.open_call(
        call_id="artifact",
        call_kind="bootstrap",
        current_artifact=None,
    )
    try:
        atomic_write_text(workspace.expected_artifact_path, "summary\n")
        artifact = adapter.capture_artifact(
            workspace,
            declared_path=workspace.expected_artifact_path.relative_to(workspace.cwd).as_posix(),
            version=1,
            summary="baseline",
            parent=None,
            source_action_ids=[],
        )
    finally:
        adapter.close_call(workspace)

    evidence = adapter.staged_checks(artifact, stage="preflight")
    assert len(evidence) == 1
    assert evidence[0].negative_result is False
    assert evidence[0].artifact_scope == "whole_artifact"


def test_deterministic_checks_resolve_python_from_harness_environment(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    adapter = SoftwareAdapter(
        run_dir=run_dir,
        blobs=BlobStore(run_dir / "blobs"),
        workspace=git_repo,
        policy=SoftwarePolicy(
            checks=['python -c "import sys; assert sys.executable"'],
        ),
    )
    adapter.prepare()
    workspace = adapter.open_call(
        call_id="artifact",
        call_kind="final",
        current_artifact=None,
    )
    try:
        atomic_write_text(workspace.expected_artifact_path, "summary\n")
        artifact = adapter.capture_artifact(
            workspace,
            declared_path=workspace.expected_artifact_path.relative_to(workspace.cwd).as_posix(),
            version=1,
            summary="baseline",
            parent=None,
            source_action_ids=[],
        )
    finally:
        adapter.close_call(workspace)

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    evidence = adapter.deterministic_checks(artifact)
    assert not evidence[0].negative_result


def test_candidate_artifact_carries_a_complete_lineage_state(
    tmp_path: Path, git_repo: Path
) -> None:
    adapter = _adapter(tmp_path, git_repo)
    adapter.prepare()
    first = adapter.open_call(
        call_id="lineage-one",
        call_kind="worker-explore",
        current_artifact=None,
    )
    try:
        (first.cwd / "app.txt").write_text("base\nlineage-one\n", encoding="utf-8")
        candidate = adapter.capture_candidate_artifact(
            first,
            summary="lineage one",
            parent=None,
            source_action_ids=["act_one"],
        )
    finally:
        adapter.close_call(first)
    assert candidate is not None

    second = adapter.open_call(
        call_id="lineage-two",
        call_kind="worker-explore",
        current_artifact=candidate,
    )
    try:
        assert (second.cwd / "app.txt").read_text(encoding="utf-8") == "base\nlineage-one\n"
    finally:
        adapter.close_call(second)


def test_runtime_inside_source_repo_is_not_snapshotted(tmp_path: Path, git_repo: Path) -> None:
    run_dir = git_repo / ".frontier" / "runs" / "run-test"
    adapter = SoftwareAdapter(
        run_dir=run_dir,
        blobs=BlobStore(run_dir / "blobs"),
        workspace=git_repo,
        policy=SoftwarePolicy(),
    )
    metadata = adapter.prepare()
    assert metadata["source_was_dirty"] is False

    workspace = adapter.open_call(
        call_id="inside-repo",
        call_kind="bootstrap",
        current_artifact=None,
    )
    try:
        assert not (workspace.cwd / ".frontier").exists()
        assert (workspace.cwd / "app.txt").read_text(encoding="utf-8") == "base\n"
    finally:
        adapter.close_call(workspace)


def test_include_untracked_false_is_consistent_through_apply(
    tmp_path: Path, git_repo: Path
) -> None:
    ignored = git_repo / "local-notes.txt"
    ignored.write_text("local only\n", encoding="utf-8")
    run_dir = tmp_path / "run-no-untracked"
    adapter = SoftwareAdapter(
        run_dir=run_dir,
        blobs=BlobStore(run_dir / "blobs"),
        workspace=git_repo,
        policy=SoftwarePolicy(
            include_untracked=False,
            checks=[],
        ),
    )
    metadata = adapter.prepare()
    assert metadata["source_was_dirty"] is False

    workspace = adapter.open_call(
        call_id="candidate-no-untracked",
        call_kind="final",
        current_artifact=None,
    )
    try:
        assert not (workspace.cwd / "local-notes.txt").exists()
        (workspace.cwd / "app.txt").write_text("changed\n", encoding="utf-8")
        atomic_write_text(workspace.expected_artifact_path, "summary\n")
        artifact = adapter.capture_artifact(
            workspace,
            declared_path=workspace.expected_artifact_path.relative_to(workspace.cwd).as_posix(),
            version=1,
            summary="candidate",
            parent=None,
            source_action_ids=[],
        )
    finally:
        adapter.close_call(workspace)

    # Untracked content is outside the declared artifact contract when disabled.
    ignored.write_text("changed locally\n", encoding="utf-8")
    result = adapter.apply_final_explicit(artifact)
    assert result and result["applied"] is True
    assert (git_repo / "app.txt").read_text(encoding="utf-8") == "changed\n"
    assert ignored.read_text(encoding="utf-8") == "changed locally\n"
