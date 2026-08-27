"""Isolated Git-patch adapter for software engineering tasks.

The source repository is never used as a worker sandbox. At run creation we
commit an exact internal snapshot of HEAD plus the user's tracked and eligible
untracked working state. Every Codex call receives a disposable Git worktree.
The evolving artifact is a patch relative to that immutable snapshot, and the
source repository changes only through an explicit, fingerprint-checked apply.
"""

from __future__ import annotations

import fnmatch
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

from ..blobs import BlobStore
from ..config import SoftwarePolicy
from ..errors import WorkspaceError
from ..ids import new_id
from ..models import (
    ArtifactRef,
    BlobRef,
    EvidenceModality,
    EvidenceRecord,
    IndependenceClass,
)
from ..util import atomic_write_text, canonical_json, sha256_bytes, utc_now
from .base import ArtifactAdapter, CallWorkspace


class SoftwareAdapter(ArtifactAdapter):
    name = "software"
    artifact_kind = "git-patch"
    final_suffix = ".patch"

    def __init__(
        self,
        *,
        run_dir: Path,
        blobs: BlobStore,
        workspace: Path | None,
        policy: SoftwarePolicy,
    ) -> None:
        super().__init__(run_dir=run_dir, blobs=blobs, workspace=workspace)
        self.policy = policy
        self.software_dir = run_dir / "software"
        self.seed_repo = self.software_dir / "seed"
        self.worktrees_dir = self.software_dir / "worktrees"
        self.metadata_path = self.software_dir / "snapshot.json"
        self.source_root: Path | None = None
        self.source_head: str | None = None
        self.snapshot_commit: str | None = None
        self.source_fingerprint: str | None = None
        self.runtime_excluded_prefix: str | None = None
        self.apply_intent_path = self.software_dir / "apply-intent.json"
        self.apply_receipt_path = self.software_dir / "apply-receipt.json"

    @staticmethod
    def _git_env() -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "GIT_AUTHOR_NAME": "Flourite",
                "GIT_AUTHOR_EMAIL": "frontier@localhost",
                "GIT_COMMITTER_NAME": "Flourite",
                "GIT_COMMITTER_EMAIL": "frontier@localhost",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        return env

    @staticmethod
    def _check_env() -> dict[str, str]:
        """Run release checks in the harness interpreter's environment.

        Invoking ``/path/to/venv/bin/frontier`` does not add that venv to
        ``PATH``. Without this normalization, an ordinary ``python -m ...``
        check can fail even though the interpreter running the harness is the
        exact Python it intended to use.
        """

        env = {**os.environ, "CI": "1"}
        executable_dir = str(Path(sys.executable).resolve().parent)
        path_entries = [item for item in env.get("PATH", "").split(os.pathsep) if item]
        env["PATH"] = os.pathsep.join(
            [executable_dir, *[item for item in path_entries if item != executable_dir]]
        )
        return env

    @classmethod
    def _git(
        cls,
        cwd: Path,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        check: bool = True,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "commit.gpgSign=false",
            "-C",
            str(cwd),
            *args,
        ]
        env = cls._git_env()
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            env=env,
            check=False,
        )
        if check and result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise WorkspaceError(f"Git command failed ({' '.join(command)}): {stderr}")
        return result

    def _excluded(self, relative: str) -> bool:
        normalized = relative.replace("\\", "/")
        if self.runtime_excluded_prefix and (
            normalized == self.runtime_excluded_prefix
            or normalized.startswith(self.runtime_excluded_prefix + "/")
        ):
            return True
        return any(
            fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(f"{normalized}/", pattern)
            for pattern in self.policy.excluded_untracked_globs
        )

    def _eligible_untracked(self, root: Path) -> list[str]:
        if not self.policy.include_untracked:
            return []
        raw = self._git(root, ["ls-files", "--others", "--exclude-standard", "-z"]).stdout
        paths = [
            item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item
        ]
        return [path for path in paths if not self._excluded(path)]

    def _working_tree_oid(self, root: Path) -> str:
        """Hash the complete eligible working state without touching its index.

        A temporary Git index starts from HEAD, absorbs tracked working-tree
        changes, then adds only eligible untracked files.  `write-tree` gives a
        canonical content/mode identity independent of whether a file happened
        to be tracked in the user's source index or in our internal snapshot.
        """

        with tempfile.TemporaryDirectory(prefix="sfh-index-") as temp:
            index_path = Path(temp) / "index"
            env = {"GIT_INDEX_FILE": str(index_path)}
            self._git(root, ["read-tree", "HEAD"], extra_env=env)
            self._git(root, ["add", "-u", "--", "."], extra_env=env)
            untracked = self._eligible_untracked(root)
            for start in range(0, len(untracked), 100):
                chunk = untracked[start : start + 100]
                if chunk:
                    self._git(root, ["add", "--", *chunk], extra_env=env)
            return self._git(root, ["write-tree"], extra_env=env).stdout.decode().strip()

    def _fingerprint(self, root: Path, *, logical_head: str | None = None) -> str:
        head = logical_head or self._git(root, ["rev-parse", "HEAD"]).stdout.decode().strip()
        tree = self._working_tree_oid(root)
        return sha256_bytes(
            canonical_json({"version": 2, "logical_head": head, "working_tree_oid": tree}).encode(
                "utf-8"
            )
        )

    def _copy_untracked(self, source: Path, destination: Path) -> None:
        if not self.policy.include_untracked:
            return
        for relative in self._eligible_untracked(source):
            src = source / relative
            dst = destination / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_symlink():
                target = os.readlink(src)
                try:
                    dst.symlink_to(target, target_is_directory=src.is_dir())
                except (OSError, NotImplementedError):
                    if src.is_file():
                        shutil.copy2(src, dst, follow_symlinks=True)
            elif src.is_file():
                shutil.copy2(src, dst)

    def prepare(self) -> dict[str, Any]:
        if self.metadata_path.exists():
            data = cast(dict[str, Any], json.loads(self.metadata_path.read_text(encoding="utf-8")))
            self.source_root = Path(data["source_root"])
            self.source_head = data["source_head"]
            self.snapshot_commit = data["snapshot_commit"]
            self.source_fingerprint = data["source_fingerprint"]
            self.runtime_excluded_prefix = data.get("runtime_excluded_prefix")
            if not self.seed_repo.exists():
                raise WorkspaceError(
                    "Software snapshot metadata exists but seed repository is missing"
                )
            return data

        if self.workspace is None:
            raise WorkspaceError(
                "The software adapter requires --workspace pointing to a Git repository"
            )
        workspace = self.workspace.expanduser().resolve()
        if shutil.which("git") is None:
            raise WorkspaceError("Git is required for the software adapter")
        top = self._git(workspace, ["rev-parse", "--show-toplevel"]).stdout.decode().strip()
        source_root = Path(top).resolve()
        try:
            self.runtime_excluded_prefix = (
                self.run_dir.resolve().relative_to(source_root).as_posix()
            )
        except ValueError:
            self.runtime_excluded_prefix = None
        self._git(source_root, ["rev-parse", "--verify", "HEAD"])
        tracked_status = self._git(
            source_root, ["status", "--porcelain=v1", "--untracked-files=no"]
        ).stdout
        eligible_untracked = self._eligible_untracked(source_root)
        source_was_dirty = bool(tracked_status or eligible_untracked)
        if source_was_dirty and not self.policy.allow_dirty_source:
            raise WorkspaceError("Source repository is dirty and allow_dirty_source=false")

        self.software_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        clone = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "clone",
                "--local",
                "--no-hardlinks",
                "--no-checkout",
                str(source_root),
                str(self.seed_repo),
            ],
            capture_output=True,
            env=self._git_env(),
            check=False,
        )
        if clone.returncode != 0:
            raise WorkspaceError(
                "Failed to create isolated repository snapshot: "
                + clone.stderr.decode("utf-8", errors="replace")
            )
        head = self._git(source_root, ["rev-parse", "HEAD"]).stdout.decode().strip()
        self._git(self.seed_repo, ["checkout", "--detach", head])
        tracked_diff = self._git(
            source_root, ["diff", "--binary", "--full-index", "HEAD", "--"]
        ).stdout
        if tracked_diff:
            self._git(
                self.seed_repo,
                ["apply", "--binary", "--whitespace=nowarn", "-"],
                input_bytes=tracked_diff,
            )
        self._copy_untracked(source_root, self.seed_repo)
        self._git(self.seed_repo, ["add", "-A"])
        self._git(
            self.seed_repo,
            ["commit", "--allow-empty", "-m", "frontier: immutable starting snapshot"],
        )
        snapshot_commit = self._git(self.seed_repo, ["rev-parse", "HEAD"]).stdout.decode().strip()
        fingerprint = self._fingerprint(source_root)

        self.source_root = source_root
        self.source_head = head
        self.snapshot_commit = snapshot_commit
        self.source_fingerprint = fingerprint
        data = {
            "profile": "software",
            "artifact_kind": self.artifact_kind,
            "source_root": str(source_root),
            "source_head": head,
            "source_fingerprint": fingerprint,
            "fingerprint_version": 2,
            "snapshot_commit": snapshot_commit,
            "seed_repo": str(self.seed_repo),
            "source_was_dirty": source_was_dirty,
            "runtime_excluded_prefix": self.runtime_excluded_prefix,
        }
        atomic_write_text(self.metadata_path, json.dumps(data, indent=2, sort_keys=True))
        return data

    def _require_prepared(self) -> tuple[Path, str]:
        if self.source_root is None or self.snapshot_commit is None:
            self.prepare()
        assert self.source_root is not None
        assert self.snapshot_commit is not None
        return self.source_root, self.snapshot_commit

    def _expected_source_fingerprint(self, artifact: ArtifactRef) -> str:
        source_root, _ = self._require_prepared()
        source_head = (
            self.source_head
            or self._git(source_root, ["rev-parse", "HEAD"]).stdout.decode().strip()
        )
        workspace = self.open_call(
            call_id=new_id("applycheck"),
            call_kind="apply-fingerprint",
            current_artifact=artifact,
        )
        try:
            return self._fingerprint(workspace.cwd, logical_head=source_head)
        finally:
            self.close_call(workspace)

    def _commit_current_state(self, worktree: Path) -> str:
        self._git(worktree, ["add", "-A"])
        self._git(
            worktree,
            ["commit", "--allow-empty", "-m", "frontier: current artifact baseline"],
        )
        return self._git(worktree, ["rev-parse", "HEAD"]).stdout.decode().strip()

    def open_call(
        self,
        *,
        call_id: str,
        call_kind: str,
        current_artifact: ArtifactRef | None,
    ) -> CallWorkspace:
        _, snapshot_commit = self._require_prepared()
        path = self.worktrees_dir / call_id
        if path.exists():
            self._git(self.seed_repo, ["worktree", "remove", "--force", str(path)], check=False)
            shutil.rmtree(path, ignore_errors=True)
        self._git(
            self.seed_repo,
            ["worktree", "add", "--detach", str(path), snapshot_commit],
        )
        if current_artifact is not None:
            if current_artifact.kind != self.artifact_kind:
                raise WorkspaceError(
                    f"Expected {self.artifact_kind} artifact, got {current_artifact.kind}"
                )
            patch = self.blobs.read_bytes(current_artifact.blob)
            if patch.strip():
                self._git(
                    path,
                    ["apply", "--binary", "--whitespace=nowarn", "-"],
                    input_bytes=patch,
                )
            for ref in current_artifact.deliverables:
                if not ref.original_name:
                    continue
                relative = Path(ref.original_name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise WorkspaceError(
                        f"Unsafe durable deliverable path in artifact: {ref.original_name}"
                    )
                destination = (path / relative).resolve()
                try:
                    destination.relative_to(path.resolve())
                except ValueError as exc:
                    raise WorkspaceError(
                        f"Durable deliverable escapes the isolated workspace: {ref.original_name}"
                    ) from exc
                destination.parent.mkdir(parents=True, exist_ok=True)
                self.blobs.materialize(ref, destination)
        baseline_commit = self._commit_current_state(path)
        context = path / ".sfh_context"
        output = path / ".sfh_output"
        context.mkdir(parents=True)
        output.mkdir(parents=True)
        return CallWorkspace(
            call_id=call_id,
            call_kind=call_kind,
            root=path,
            cwd=path,
            context_dir=context,
            output_dir=output,
            expected_artifact_path=output / "artifact-summary.md",
            baseline_commit=baseline_commit,
            metadata={"snapshot_commit": snapshot_commit},
        )

    def _mark_untracked_intent(self, workspace: CallWorkspace) -> None:
        raw = self._git(
            workspace.cwd,
            ["ls-files", "--others", "--exclude-standard", "-z"],
        ).stdout
        paths = [
            item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item
        ]
        paths = [
            path
            for path in paths
            if not path.startswith(".sfh_context/") and not path.startswith(".sfh_output/")
        ]
        for start in range(0, len(paths), 100):
            chunk = paths[start : start + 100]
            if chunk:
                self._git(workspace.cwd, ["add", "-N", "--", *chunk])

    def _diff(self, workspace: CallWorkspace, base_commit: str) -> bytes:
        self._mark_untracked_intent(workspace)
        return self._git(
            workspace.cwd,
            [
                "diff",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                base_commit,
                "--",
                ".",
                ":(exclude).sfh_context/**",
                ":(exclude).sfh_output/**",
            ],
        ).stdout

    def _capture_paths(
        self,
        workspace: CallWorkspace,
        patterns: list[str],
        *,
        label: str,
    ) -> list[BlobRef]:
        refs: list[BlobRef] = []
        seen: set[Path] = set()
        total = 0
        root = workspace.cwd.resolve()
        for pattern in patterns:
            pattern_path = Path(pattern)
            if pattern_path.is_absolute() or ".." in pattern_path.parts:
                raise WorkspaceError(
                    f"Declared {label} path must stay workspace-relative: {pattern}"
                )
            for path in sorted(workspace.cwd.glob(pattern)):
                if not path.is_file():
                    continue
                resolved = path.resolve()
                try:
                    relative = resolved.relative_to(root).as_posix()
                except ValueError as exc:
                    raise WorkspaceError(
                        f"Declared {label} escapes the isolated workspace: {path}"
                    ) from exc
                if resolved in seen:
                    continue
                seen.add(resolved)
                total += resolved.stat().st_size
                if total > self.policy.max_release_artifact_bytes:
                    raise WorkspaceError(
                        f"Declared {label} exceed software.max_release_artifact_bytes"
                    )
                media_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
                refs.append(
                    self.blobs.put_file(
                        resolved,
                        media_type=media_type,
                        original_name=relative,
                    )
                )
        return refs

    def _capture_declared_deliverables(self, workspace: CallWorkspace) -> list[BlobRef]:
        return self._capture_paths(
            workspace,
            self.policy.release_artifacts,
            label="release artifacts",
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
        summary_path = self.resolve_declared_path(workspace, declared_path)
        if not summary_path.is_file():
            raise FileNotFoundError(
                f"Software call did not write its required artifact summary: {declared_path}"
            )
        _, snapshot_commit = self._require_prepared()
        patch = self._diff(workspace, snapshot_commit)
        blob = self.blobs.put_bytes(
            patch,
            media_type="text/x-diff; charset=utf-8",
            original_name=f"artifact-v{version}.patch",
        )
        return ArtifactRef(
            artifact_id=new_id("art"),
            version=version,
            blob=blob,
            kind=self.artifact_kind,
            summary=summary,
            parent_artifact_id=parent.artifact_id if parent else None,
            source_action_ids=source_action_ids,
            deliverables=self._capture_declared_deliverables(workspace),
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
        _, snapshot_commit = self._require_prepared()
        patch = self._diff(workspace, snapshot_commit)
        blob = self.blobs.put_bytes(
            patch,
            media_type="text/x-diff; charset=utf-8",
            original_name=f"{workspace.call_id}-candidate.patch",
        )
        return ArtifactRef(
            artifact_id=new_id("art"),
            version=(parent.version + 1 if parent else 1),
            blob=blob,
            kind=self.artifact_kind,
            summary=summary,
            parent_artifact_id=parent.artifact_id if parent else None,
            source_action_ids=source_action_ids,
            deliverables=self._capture_declared_deliverables(workspace),
            created_at=utc_now(),
        )

    def close_call(self, workspace: CallWorkspace) -> None:
        self._git(
            self.seed_repo,
            ["worktree", "remove", "--force", str(workspace.root)],
            check=False,
        )
        shutil.rmtree(workspace.root, ignore_errors=True)
        self._git(self.seed_repo, ["worktree", "prune"], check=False)

    def materialize_final(self, artifact: ArtifactRef, destination: Path) -> Path:
        return self.blobs.materialize(artifact.blob, destination)

    def _run_checks(
        self,
        artifact: ArtifactRef,
        *,
        commands: list[str],
        stage: str,
    ) -> list[EvidenceRecord]:
        if not commands:
            return []
        workspace = self.open_call(
            call_id=new_id("check"),
            call_kind=f"{stage}-checks",
            current_artifact=artifact,
        )
        evidence: list[EvidenceRecord] = []
        try:
            for index, command in enumerate(commands, start=1):
                try:
                    result = subprocess.run(
                        command,
                        cwd=workspace.cwd,
                        shell=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=self.policy.check_timeout_seconds,
                        env=self._check_env(),
                        check=False,
                    )
                    output = result.stdout.decode("utf-8", errors="replace")
                    passed = result.returncode == 0
                    summary = (
                        f"Configured check passed: {command}"
                        if passed
                        else f"Configured check failed with exit {result.returncode}: {command}"
                    )
                except subprocess.TimeoutExpired as exc:
                    output = (exc.stdout or b"").decode("utf-8", errors="replace")
                    passed = False
                    summary = f"Configured check timed out after {self.policy.check_timeout_seconds}s: {command}"
                blob = self.blobs.put_text(
                    output,
                    media_type="text/plain; charset=utf-8",
                    original_name=f"check-{index}.log",
                )
                evidence.append(
                    EvidenceRecord(
                        evidence_id=new_id("evd"),
                        kind=f"deterministic_{stage}_check",
                        summary=summary,
                        scope=f"Observable behavior exercised by configured command: {command}",
                        artifact_scope=("release" if stage == "release" else "whole_artifact"),
                        independence_class=IndependenceClass.DETERMINISTIC_TOOL,
                        references=[blob.digest],
                        blob=blob,
                        negative_result=not passed,
                        modalities=[EvidenceModality.DETERMINISTIC_TEST],
                        establishes=[f"configured check: {command}"] if passed else [],
                        cannot_establish=[
                            "behavior outside the configured check's exercised scope"
                        ],
                        artifact_digest=artifact.blob.digest,
                    )
                )
        finally:
            self.close_call(workspace)
        return evidence

    def staged_checks(self, artifact: ArtifactRef, *, stage: str) -> list[EvidenceRecord]:
        inferred_preflight = [
            command
            for command in self.policy.checks
            if re.search(r"(?:^|\s)--source-only(?:\s|$)", command)
            and command not in self.policy.preflight_checks
        ]
        commands = {
            "preflight": [*self.policy.preflight_checks, *inferred_preflight],
            "candidate": self.policy.candidate_checks,
        }.get(stage, [])
        return self._run_checks(artifact, commands=commands, stage=stage)

    def deterministic_checks(self, artifact: ArtifactRef) -> list[EvidenceRecord]:
        # Release re-runs every declared check against the exact final artifact.
        commands = [
            *self.policy.preflight_checks,
            *self.policy.candidate_checks,
            *self.policy.checks,
        ]
        return self._run_checks(artifact, commands=commands, stage="release")

    def apply_final_explicit(self, artifact: ArtifactRef) -> dict[str, Any] | None:
        source_root, _ = self._require_prepared()
        current_fingerprint = self._fingerprint(source_root)
        expected_post_fingerprint = self._expected_source_fingerprint(artifact)

        for receipt_path in (self.apply_receipt_path, self.apply_intent_path):
            if not receipt_path.exists():
                continue
            data = json.loads(receipt_path.read_text(encoding="utf-8"))
            if (
                data.get("artifact_id") == artifact.artifact_id
                and data.get("artifact_digest") == artifact.blob.digest
                and data.get("expected_post_fingerprint") == current_fingerprint
                and current_fingerprint == expected_post_fingerprint
            ):
                if receipt_path == self.apply_intent_path:
                    data["completed_at"] = utc_now()
                    atomic_write_text(
                        self.apply_receipt_path,
                        json.dumps(data, indent=2, sort_keys=True),
                    )
                return {
                    "source_root": str(source_root),
                    "applied": False,
                    "reason": "final patch was already applied exactly",
                    "artifact_id": artifact.artifact_id,
                    "post_fingerprint": current_fingerprint,
                }

        if current_fingerprint != self.source_fingerprint:
            raise WorkspaceError(
                "Source repository changed since the immutable snapshot; refusing to apply the final patch"
            )
        patch = self.blobs.read_bytes(artifact.blob)
        if not patch.strip():
            return {
                "source_root": str(source_root),
                "applied": False,
                "reason": "final patch is empty",
            }
        intent = {
            "artifact_id": artifact.artifact_id,
            "artifact_digest": artifact.blob.digest,
            "source_root": str(source_root),
            "starting_fingerprint": self.source_fingerprint,
            "expected_post_fingerprint": expected_post_fingerprint,
            "created_at": utc_now(),
        }
        atomic_write_text(
            self.apply_intent_path,
            json.dumps(intent, indent=2, sort_keys=True),
        )
        self._git(source_root, ["apply", "--binary", "--check", "-"], input_bytes=patch)
        self._git(source_root, ["apply", "--binary", "-"], input_bytes=patch)
        post_fingerprint = self._fingerprint(source_root)
        if post_fingerprint != expected_post_fingerprint:
            raise WorkspaceError(
                "Final patch applied, but the resulting source fingerprint did not match the isolated artifact"
            )
        receipt = {**intent, "completed_at": utc_now(), "post_fingerprint": post_fingerprint}
        atomic_write_text(
            self.apply_receipt_path,
            json.dumps(receipt, indent=2, sort_keys=True),
        )
        return {
            "source_root": str(source_root),
            "applied": True,
            "artifact_id": artifact.artifact_id,
            "starting_fingerprint": self.source_fingerprint,
            "post_fingerprint": post_fingerprint,
        }
