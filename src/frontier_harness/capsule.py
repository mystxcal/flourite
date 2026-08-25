"""Selective per-action context capsules and source staging."""

from __future__ import annotations

import fnmatch
import json
import mimetypes
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .adapters.base import ArtifactAdapter, CallWorkspace
from .blobs import BlobStore
from .models import (
    ActionContract,
    ActionRecord,
    ActionStatus,
    BlobRef,
    CompletionCase,
    GoalContract,
    RunState,
    TaskSource,
)
from .state import state_summary
from .util import atomic_write_text, safe_slug

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@dataclass(slots=True)
class StagedSource:
    display_name: str
    stored_path: Path
    blob: BlobRef
    original_path: str

    @property
    def is_image(self) -> bool:
        return self.stored_path.suffix.casefold() in _IMAGE_SUFFIXES


def stage_sources(
    paths: Iterable[Path],
    *,
    run_dir: Path,
    blobs: BlobStore,
    max_total_bytes: int,
    max_files: int = 2_000,
    excluded_globs: Iterable[str] = (),
    exclude_roots: Iterable[Path] = (),
) -> list[StagedSource]:
    source_root = run_dir / "sources"
    source_root.mkdir(parents=True, exist_ok=True)
    staged: list[StagedSource] = []
    total = 0
    seen_names: set[str] = set()
    excluded_patterns = tuple(excluded_globs)
    resolved_exclude_roots = tuple(path.resolve() for path in exclude_roots)

    def is_excluded(path: Path, *, relative: str) -> bool:
        resolved = path.resolve()
        for root in resolved_exclude_roots:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                pass
        normalized = relative.replace("\\", "/")
        return any(
            fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(f"{normalized}/", pattern)
            for pattern in excluded_patterns
        )

    def add_file(path: Path, display: str) -> None:
        nonlocal total
        if len(staged) >= max_files:
            raise ValueError(f"Attachments exceed configured limit of {max_files:,} files")

        # Reject obviously oversized inputs before reading, then enforce the
        # limit again against the bytes actually captured. The source may be
        # changing concurrently; one read defines the immutable snapshot.
        estimated_size = path.stat().st_size
        if total + estimated_size > max_total_bytes:
            raise ValueError(f"Attachments exceed configured limit of {max_total_bytes:,} bytes")
        data = path.read_bytes()
        if total + len(data) > max_total_bytes:
            raise ValueError(f"Attachments exceed configured limit of {max_total_bytes:,} bytes")

        base = safe_slug(display, limit=100)
        suffix = path.suffix
        if suffix and base.casefold().endswith(suffix.casefold()):
            stem = base[: -len(suffix)].rstrip("-._") or "item"
            candidate = base
        else:
            stem = base
            candidate = f"{base}{suffix}"
        index = 2
        while candidate.casefold() in seen_names:
            candidate = f"{stem}-{index}{suffix}"
            index += 1
        seen_names.add(candidate.casefold())

        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        blob = blobs.put_bytes(
            data,
            media_type=media_type,
            original_name=path.name,
        )
        destination = source_root / candidate
        blobs.materialize(blob, destination)
        total += len(data)
        staged.append(
            StagedSource(
                display_name=display,
                stored_path=destination,
                blob=blob,
                original_path=str(path),
            )
        )

    for supplied in paths:
        path = supplied.expanduser().resolve()
        if path.is_file():
            add_file(path, path.name)
        elif path.is_dir():
            candidates: list[tuple[str, Path]] = []
            for current_root, directories, files in os.walk(path, followlinks=False):
                current = Path(current_root)
                kept_directories: list[str] = []
                for directory in directories:
                    child = current / directory
                    relative = child.relative_to(path).as_posix()
                    if child.is_symlink():
                        continue
                    if not is_excluded(child, relative=relative):
                        kept_directories.append(directory)
                directories[:] = kept_directories
                for filename in files:
                    child = current / filename
                    relative = child.relative_to(path).as_posix()
                    if child.is_symlink():
                        continue
                    if not is_excluded(child, relative=relative):
                        candidates.append((relative, child))
            for relative, child in sorted(candidates):
                if child.is_file():
                    add_file(child, f"{path.name}/{relative}")
        else:
            raise FileNotFoundError(path)
    return staged


class CapsuleBuilder:
    def __init__(
        self,
        *,
        adapter: ArtifactAdapter,
        blobs: BlobStore,
        sources: list[StagedSource],
        evidence_limit: int = 12,
        artifact_char_limit: int = 200_000,
    ) -> None:
        self.adapter = adapter
        self.blobs = blobs
        self.sources = sources
        self.evidence_limit = evidence_limit
        self.artifact_char_limit = artifact_char_limit

    @staticmethod
    def _relative(workspace: CallWorkspace, path: Path) -> str:
        return path.relative_to(workspace.cwd).as_posix()

    def populate(
        self,
        workspace: CallWorkspace,
        *,
        task: str,
        state: RunState | None,
        assignment: str,
        goal_contract: GoalContract | None,
        evidence_action_ids: list[str] | None = None,
        extra_notes: str = "",
        task_source: TaskSource | None = None,
        action_contract: ActionContract | None = None,
        apex_brief: str = "",
        semantic_ci: dict[str, object] | None = None,
        completion_case: CompletionCase | None = None,
    ) -> dict[str, str | list[str]]:
        context = workspace.context_dir
        context.mkdir(parents=True, exist_ok=True)
        atomic_write_text(context / "REQUEST.md", f"# Original request\n\n{task.strip()}\n")
        source_value = task_source or (state.task_source if state is not None else None)
        if source_value is not None:
            atomic_write_text(
                context / "TASK_SOURCE.json",
                json.dumps(source_value.model_dump(mode="json"), indent=2, ensure_ascii=False),
            )
        else:
            from .cognition import capture_task_source

            captured = capture_task_source(task)
            atomic_write_text(
                context / "TASK_SOURCE.json",
                json.dumps(captured.model_dump(mode="json"), indent=2, ensure_ascii=False),
            )
        atomic_write_text(
            context / "ASSIGNMENT.md", f"# Exact assignment\n\n{assignment.strip()}\n"
        )
        atomic_write_text(
            context / "VERIFICATION_CONTRACT.json",
            json.dumps(self.adapter.verification_contract(), indent=2, ensure_ascii=False),
        )
        if extra_notes:
            atomic_write_text(context / "NOTES.md", f"# Runtime notes\n\n{extra_notes.strip()}\n")

        if goal_contract is not None:
            atomic_write_text(
                context / "GOAL_CONTRACT.json",
                json.dumps(goal_contract.model_dump(mode="json"), indent=2, ensure_ascii=False),
            )
        if action_contract is not None:
            atomic_write_text(
                context / "ACTION_CONTRACT.json",
                json.dumps(action_contract.model_dump(mode="json"), indent=2, ensure_ascii=False),
            )
        else:
            atomic_write_text(context / "ACTION_CONTRACT.json", "{}\n")
        if apex_brief:
            atomic_write_text(context / "APEX_BRIEF.md", apex_brief.strip() + "\n")
        else:
            atomic_write_text(context / "APEX_BRIEF.md", "# Apex brief\n\nNot yet constructed.\n")
        if semantic_ci is not None:
            atomic_write_text(
                context / "SEMANTIC_CI.json",
                json.dumps(semantic_ci, indent=2, ensure_ascii=False),
            )
        else:
            atomic_write_text(context / "SEMANTIC_CI.json", "{}\n")
        case_value = completion_case or (state.completion_case if state is not None else None)
        if case_value is not None:
            atomic_write_text(
                context / "COMPLETION_CASE.json",
                json.dumps(case_value.model_dump(mode="json"), indent=2, ensure_ascii=False),
            )
        else:
            atomic_write_text(context / "COMPLETION_CASE.json", "{}\n")
        if state is not None:
            atomic_write_text(
                context / "STATE.json",
                json.dumps(state_summary(state), indent=2, ensure_ascii=False),
            )
            atomic_write_text(
                context / "TASK_CHARTER.json",
                json.dumps(
                    state.task_charter.model_dump(mode="json") if state.task_charter else {},
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            atomic_write_text(
                context / "ARTIFACT_SPINE.json",
                json.dumps(
                    state.artifact_spine.model_dump(mode="json") if state.artifact_spine else {},
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            if state.current_artifact is not None:
                artifact_text = self.adapter.artifact_text(state.current_artifact)
                if len(artifact_text) <= self.artifact_char_limit:
                    atomic_write_text(context / "CURRENT_ARTIFACT.md", artifact_text)
                else:
                    full_path = context / "CURRENT_ARTIFACT.full.md"
                    atomic_write_text(full_path, artifact_text)
                    preview_budget = max(2_000, self.artifact_char_limit // 2)
                    preview = (
                        artifact_text[:preview_budget]
                        + "\n\n[... deterministic middle compaction ...]\n\n"
                        + artifact_text[-preview_budget:]
                    )
                    atomic_write_text(
                        context / "CURRENT_ARTIFACT.md",
                        "# Current artifact preview\n\n"
                        "The artifact exceeded the capsule preview threshold. The complete, "
                        "lossless artifact is available at `CURRENT_ARTIFACT.full.md`; inspect "
                        "the full file whenever the assignment depends on omitted sections.\n\n"
                        + preview,
                    )

                deliverable_dir = context / "deliverables"
                deliverable_dir.mkdir(exist_ok=True)
                deliverable_lines = ["# Captured deliverables", ""]
                for index, ref in enumerate(state.current_artifact.deliverables, start=1):
                    safe_name = Path(ref.original_name or f"deliverable-{index}").name
                    destination = deliverable_dir / f"{index:03d}-{safe_name}"
                    self.blobs.materialize(ref, destination)
                    deliverable_lines.append(
                        f"- `{self._relative(workspace, destination)}` — {ref.media_type}; "
                        f"sha256 `{ref.digest}`"
                    )
                if not state.current_artifact.deliverables:
                    deliverable_lines.append("- No sidecar deliverables were declared.")
                atomic_write_text(context / "DELIVERABLES.md", "\n".join(deliverable_lines) + "\n")

        if state is None:
            atomic_write_text(context / "STATE.json", "{}\n")
            atomic_write_text(context / "TASK_CHARTER.json", "{}\n")
            atomic_write_text(context / "ARTIFACT_SPINE.json", "{}\n")
        source_dir = context / "sources"
        source_dir.mkdir(exist_ok=True)
        source_lines = ["# Supplied sources", ""]
        image_paths: list[str] = []
        if state is not None and state.current_artifact is not None:
            deliverable_dir = context / "deliverables"
            for index, ref in enumerate(state.current_artifact.deliverables, start=1):
                if not ref.media_type.startswith("image/"):
                    continue
                safe_name = Path(ref.original_name or f"deliverable-{index}").name
                image_paths.append(str(deliverable_dir / f"{index:03d}-{safe_name}"))
        for source in self.sources:
            destination = source_dir / source.stored_path.name
            # Materialize from the authoritative blob rather than linking the
            # durable staged copy. A worker may freely modify its capsule
            # without changing evidence retained by the run.
            self.blobs.materialize(source.blob, destination)
            relative = self._relative(workspace, destination)
            source_lines.append(
                f"- `{relative}` — {source.display_name}; sha256 `{source.blob.digest}`"
            )
            if source.is_image:
                image_paths.append(str(destination))
        if not self.sources:
            source_lines.append("- No external source files were supplied.")
        atomic_write_text(context / "SOURCES.md", "\n".join(source_lines) + "\n")

        evidence_dir = context / "evidence"
        evidence_dir.mkdir(exist_ok=True)
        evidence_lines = ["# Decision-relevant evidence", ""]
        if state is not None:
            records: list[ActionRecord] = []
            if evidence_action_ids:
                for action_id in evidence_action_ids:
                    record = state.actions.get(action_id)
                    if record is not None:
                        records.append(record)
            for record in reversed(list(state.actions.values())):
                if len(records) >= self.evidence_limit:
                    break
                if record in records or record.status != ActionStatus.COMPLETE:
                    continue
                records.append(record)
            for record in records[: self.evidence_limit]:
                action_id = record.spec.action_id
                evidence_lines.extend(
                    [
                        f"## {action_id}: {record.spec.kind.value} — {record.spec.target}",
                        "",
                        f"Assignment: {record.spec.assignment}",
                    ]
                )
                if record.result is not None:
                    evidence_lines.append("Findings: " + "; ".join(record.result.findings))
                    if record.result.unresolved_risks:
                        evidence_lines.append(
                            "Unresolved risks: " + "; ".join(record.result.unresolved_risks)
                        )
                    if record.result.frame_break:
                        evidence_lines.append("Frame break: " + record.result.frame_break)
                if record.receipt is not None:
                    cost = record.receipt.observed_cost
                    channels = (
                        ", ".join(item.value for item in record.receipt.observed_evidence_channels)
                        or "none observed"
                    )
                    evidence_lines.extend(
                        [
                            (
                                "Receipt: "
                                f"{record.receipt.integration_status}; "
                                f"outcome {record.receipt.outcome_match}; "
                                f"channels {channels}; "
                                f"channel confirmed {record.receipt.evidence_channel_confirmed}"
                            ),
                            (
                                "Observed cost: "
                                f"{cost.model_turns} model turns, "
                                f"{cost.tool_calls} tool calls, "
                                f"{cost.tool_errors} tool errors, "
                                f"{cost.wall_seconds:.2f}s"
                            ),
                        ]
                    )
                if record.result_blob is not None:
                    suffix = Path(record.result_blob.original_name or "result.txt").suffix or ".txt"
                    target = evidence_dir / f"{action_id}{suffix}"
                    self.blobs.materialize(record.result_blob, target)
                    evidence_lines.append(f"Full result: `{self._relative(workspace, target)}`")
                if record.patch_blob is not None:
                    target = evidence_dir / f"{action_id}.patch"
                    self.blobs.materialize(record.patch_blob, target)
                    evidence_lines.append(f"Candidate patch: `{self._relative(workspace, target)}`")
                evidence_lines.append("")
            retained_records = records[: self.evidence_limit]
            selected_action_ids = {record.spec.action_id for record in retained_records}
            remaining = max(0, self.evidence_limit - len(retained_records))
            standalone_candidates = [
                evidence
                for evidence in state.evidence.values()
                if evidence.source_action_id not in selected_action_ids
            ]
            standalone = standalone_candidates[-remaining:] if remaining else []
            for evidence in standalone:
                evidence_lines.extend(
                    [
                        f"## {evidence.evidence_id}: {evidence.kind}",
                        "",
                        f"Summary: {evidence.summary}",
                        f"Scope: {evidence.scope}",
                        f"Independence: {evidence.independence_class.value}",
                        "Modalities: "
                        + (", ".join(item.value for item in evidence.modalities) or "unspecified"),
                    ]
                )
                if evidence.establishes:
                    evidence_lines.append("Establishes: " + "; ".join(evidence.establishes))
                if evidence.cannot_establish:
                    evidence_lines.append(
                        "Cannot establish: " + "; ".join(evidence.cannot_establish)
                    )
                if evidence.blob is not None:
                    suffix = Path(evidence.blob.original_name or "evidence.txt").suffix or ".txt"
                    target = evidence_dir / f"{evidence.evidence_id}{suffix}"
                    self.blobs.materialize(evidence.blob, target)
                    evidence_lines.append(f"Full evidence: `{self._relative(workspace, target)}`")
                evidence_lines.append("")
        if len(evidence_lines) == 2:
            evidence_lines.append("No completed targeted actions are available yet.")
        atomic_write_text(context / "EVIDENCE_INDEX.md", "\n".join(evidence_lines) + "\n")

        return {
            "context_dir": self._relative(workspace, context),
            "request": self._relative(workspace, context / "REQUEST.md"),
            "assignment": self._relative(workspace, context / "ASSIGNMENT.md"),
            "verification_contract": self._relative(
                workspace, context / "VERIFICATION_CONTRACT.json"
            ),
            "sources": self._relative(workspace, context / "SOURCES.md"),
            "state": self._relative(workspace, context / "STATE.json") if state is not None else "",
            "artifact": self._relative(workspace, context / "CURRENT_ARTIFACT.md")
            if state is not None and state.current_artifact is not None
            else "",
            "evidence": self._relative(workspace, context / "EVIDENCE_INDEX.md"),
            "image_paths": image_paths,
        }
