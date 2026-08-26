"""Selective per-action context capsules and source staging."""

from __future__ import annotations

import fnmatch
import json
import mimetypes
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .adapters.base import ArtifactAdapter, CallWorkspace
from .blobs import BlobStore
from .models import (
    ActionContract,
    ActionRecord,
    ActionStatus,
    BlobRef,
    CompletionCase,
    ContextLens,
    EvidenceModality,
    EvidenceRecord,
    GoalContract,
    RunState,
    TaskSource,
)
from .state import state_summary
from .util import atomic_write_text, canonical_json, safe_slug, sha256_text, unique_preserving_order

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
LensPurpose = Literal["bootstrap", "action", "checkpoint", "synthesis", "release", "repair"]


@dataclass(slots=True)
class StagedSource:
    display_name: str
    stored_path: Path
    blob: BlobRef
    original_path: str

    @property
    def is_image(self) -> bool:
        return self.stored_path.suffix.casefold() in _IMAGE_SUFFIXES


@dataclass(slots=True)
class CapsuleSpec:
    """The exact semantic view requested for one provider call."""

    task: str
    assignment: str
    state: RunState | None = None
    goal_contract: GoalContract | None = None
    evidence_action_ids: list[str] = field(default_factory=list)
    extra_notes: str = ""
    task_source: TaskSource | None = None
    action_contract: ActionContract | None = None
    apex_brief: str = ""
    semantic_ci: dict[str, object] | None = None
    completion_case: CompletionCase | None = None
    purpose: LensPurpose = "action"


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
    return _SourceStager(
        run_dir=run_dir,
        blobs=blobs,
        max_total_bytes=max_total_bytes,
        max_files=max_files,
        excluded_patterns=tuple(excluded_globs),
        excluded_roots=tuple(path.resolve() for path in exclude_roots),
    ).stage(paths)


@dataclass(slots=True)
class _SourceStager:
    """Capture one bounded, immutable source snapshot."""

    run_dir: Path
    blobs: BlobStore
    max_total_bytes: int
    max_files: int
    excluded_patterns: tuple[str, ...]
    excluded_roots: tuple[Path, ...]
    staged: list[StagedSource] = field(default_factory=list)
    seen_names: set[str] = field(default_factory=set)
    total_bytes: int = 0

    @property
    def source_root(self) -> Path:
        return self.run_dir / "sources"

    def stage(self, paths: Iterable[Path]) -> list[StagedSource]:
        self.source_root.mkdir(parents=True, exist_ok=True)
        for supplied in paths:
            path = supplied.expanduser().resolve()
            if path.is_file():
                self._capture(path, path.name)
            elif path.is_dir():
                for relative, child in self._walk(path):
                    self._capture(child, f"{path.name}/{relative}")
            else:
                raise FileNotFoundError(path)
        return self.staged

    def _walk(self, root: Path) -> list[tuple[str, Path]]:
        candidates: list[tuple[str, Path]] = []
        for current_root, directories, files in os.walk(root, followlinks=False):
            current = Path(current_root)
            directories[:] = [
                name for name in directories if self._included(current / name, root=root)
            ]
            candidates.extend(
                (child.relative_to(root).as_posix(), child)
                for name in files
                if (child := current / name).is_file() and self._included(child, root=root)
            )
        return sorted(candidates)

    def _included(self, path: Path, *, root: Path) -> bool:
        if path.is_symlink():
            return False
        resolved = path.resolve()
        if any(_is_within(resolved, excluded) for excluded in self.excluded_roots):
            return False
        relative = path.relative_to(root).as_posix()
        return not any(
            fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(f"{relative}/", pattern)
            for pattern in self.excluded_patterns
        )

    def _capture(self, path: Path, display: str) -> None:
        if len(self.staged) >= self.max_files:
            raise ValueError(f"Attachments exceed configured limit of {self.max_files:,} files")
        self._assert_fits(path.stat().st_size)
        data = path.read_bytes()
        self._assert_fits(len(data))
        destination = self.source_root / self._unique_name(path, display)
        blob = self.blobs.put_bytes(
            data,
            media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            original_name=path.name,
        )
        self.blobs.materialize(blob, destination)
        self.total_bytes += len(data)
        self.staged.append(
            StagedSource(
                display_name=display,
                stored_path=destination,
                blob=blob,
                original_path=str(path),
            )
        )

    def _assert_fits(self, size: int) -> None:
        if self.total_bytes + size > self.max_total_bytes:
            raise ValueError(
                f"Attachments exceed configured limit of {self.max_total_bytes:,} bytes"
            )

    def _unique_name(self, path: Path, display: str) -> str:
        base = safe_slug(display, limit=100)
        suffix = path.suffix
        candidate = (
            base if suffix and base.casefold().endswith(suffix.casefold()) else f"{base}{suffix}"
        )
        stem = (
            base[: -len(suffix)].rstrip("-._") or "item"
            if suffix and base.casefold().endswith(suffix.casefold())
            else base
        )
        index = 2
        while candidate.casefold() in self.seen_names:
            candidate = f"{stem}-{index}{suffix}"
            index += 1
        self.seen_names.add(candidate.casefold())
        return candidate


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


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

    def _select_evidence_records(
        self,
        state: RunState,
        *,
        explicit_action_ids: list[str],
        action_contract: ActionContract | None,
    ) -> list[ActionRecord]:
        """Compose a causal evidence view instead of a recency transcript.

        Exact requested actions come first, followed by actions sharing the
        current crux/obligation, prior frontier-changing actions, and finally
        recent completed work.  The lossless ledger remains the zoom target.
        """

        ordered: list[ActionRecord] = []
        seen: set[str] = set()

        def add(action_id: str) -> None:
            if action_id in seen:
                return
            record = state.actions.get(action_id)
            if record is None or record.status != ActionStatus.COMPLETE:
                return
            seen.add(action_id)
            ordered.append(record)

        for action_id in reversed(explicit_action_ids):
            add(action_id)

        if action_contract is not None:
            target_obligations = set(action_contract.obligation_ids)
            target_cruxes = set(action_contract.target_crux_ids)
            for record in reversed(list(state.actions.values())):
                if target_obligations.intersection(
                    record.spec.obligation_ids
                ) or target_cruxes.intersection(record.spec.crux_ids):
                    add(record.spec.action_id)

        for action_id in reversed(state.frontier_advancing_action_ids):
            add(action_id)
        for record in reversed(list(state.actions.values())):
            add(record.spec.action_id)
        return ordered[: self.evidence_limit]

    @staticmethod
    def _observation_contract(
        state: RunState | None,
        *,
        action_contract: ActionContract | None,
        purpose: str,
    ) -> tuple[list[dict[str, object]], list[EvidenceModality]]:
        if state is None:
            return [], []
        if action_contract is not None and action_contract.obligation_ids:
            obligations = [
                state.obligations[item]
                for item in action_contract.obligation_ids
                if item in state.obligations
            ]
        elif purpose in {"synthesis", "release", "repair"}:
            obligations = list(state.release_blocking_obligations)
        else:
            obligations = list(state.open_obligations)
        rows: list[dict[str, object]] = [
            {
                "obligation_id": item.obligation_id,
                "property": item.requirement,
                "acceptance": item.acceptance,
                "artifact_scope": item.required_artifact_scope,
                "required_modalities": [value.value for value in item.required_evidence_modalities],
                "known_evidence_references": list(item.evidence_references),
                "proxy_boundary": (
                    "Evidence outside the required artifact scope or modalities may guide work "
                    "but cannot establish this property."
                ),
            }
            for item in obligations
        ]
        modalities = unique_preserving_order(
            modality for item in obligations for modality in item.required_evidence_modalities
        )
        if action_contract is not None:
            modalities = unique_preserving_order(
                [*modalities, *action_contract.observation_modalities]
            )
        return rows, modalities

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False))

    def _write_anchor_context(
        self,
        workspace: CallWorkspace,
        spec: CapsuleSpec,
    ) -> TaskSource:
        context = workspace.context_dir
        context.mkdir(parents=True, exist_ok=True)
        atomic_write_text(context / "REQUEST.md", f"# Original request\n\n{spec.task.strip()}\n")
        source = spec.task_source or (spec.state.task_source if spec.state is not None else None)
        if source is None:
            from .cognition import capture_task_source

            source = capture_task_source(spec.task)
        self._write_json(context / "TASK_SOURCE.json", source.model_dump(mode="json"))
        atomic_write_text(
            context / "ASSIGNMENT.md",
            f"# Exact assignment\n\n{spec.assignment.strip()}\n",
        )
        self._write_json(
            context / "VERIFICATION_CONTRACT.json",
            self.adapter.verification_contract(),
        )
        if spec.extra_notes:
            atomic_write_text(
                context / "NOTES.md",
                f"# Runtime notes\n\n{spec.extra_notes.strip()}\n",
            )
        if spec.goal_contract is not None:
            self._write_json(
                context / "GOAL_CONTRACT.json",
                spec.goal_contract.model_dump(mode="json"),
            )
        self._write_json(
            context / "ACTION_CONTRACT.json",
            spec.action_contract.model_dump(mode="json") if spec.action_contract else {},
        )
        atomic_write_text(
            context / "APEX_BRIEF.md",
            spec.apex_brief.strip() + "\n"
            if spec.apex_brief
            else "# Apex brief\n\nNot yet constructed.\n",
        )
        self._write_json(context / "SEMANTIC_CI.json", spec.semantic_ci or {})
        completion = spec.completion_case or (
            spec.state.completion_case if spec.state is not None else None
        )
        self._write_json(
            context / "COMPLETION_CASE.json",
            completion.model_dump(mode="json") if completion else {},
        )
        return source

    def _write_state_context(
        self,
        workspace: CallWorkspace,
        state: RunState | None,
    ) -> tuple[Literal["none", "full", "preview_with_full"], list[str]]:
        context = workspace.context_dir
        if state is None:
            for name in (
                "STATE.json",
                "TASK_CHARTER.json",
                "ARTIFACT_SPINE.json",
                "FRONTIER_KERNEL.json",
            ):
                self._write_json(context / name, {})
            return "none", []

        self._write_json(context / "STATE.json", state_summary(state))
        self._write_json(
            context / "TASK_CHARTER.json",
            state.task_charter.model_dump(mode="json") if state.task_charter else {},
        )
        self._write_json(
            context / "ARTIFACT_SPINE.json",
            state.artifact_spine.model_dump(mode="json") if state.artifact_spine else {},
        )
        self._write_json(
            context / "FRONTIER_KERNEL.json",
            state.frontier_kernel.model_dump(mode="json") if state.frontier_kernel else {},
        )
        artifact = state.current_artifact
        if artifact is None:
            return "none", []

        artifact_text = self.adapter.artifact_text(artifact)
        if len(artifact_text) <= self.artifact_char_limit:
            atomic_write_text(context / "CURRENT_ARTIFACT.md", artifact_text)
            artifact_view: Literal["none", "full", "preview_with_full"] = "full"
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
                "the full file whenever the assignment depends on omitted sections.\n\n" + preview,
            )
            artifact_view = "preview_with_full"

        deliverable_dir = context / "deliverables"
        deliverable_dir.mkdir(exist_ok=True)
        deliverable_lines = ["# Captured deliverables", ""]
        image_paths: list[str] = []
        for index, ref in enumerate(artifact.deliverables, start=1):
            safe_name = Path(ref.original_name or f"deliverable-{index}").name
            destination = deliverable_dir / f"{index:03d}-{safe_name}"
            self.blobs.materialize(ref, destination)
            deliverable_lines.append(
                f"- `{self._relative(workspace, destination)}` — {ref.media_type}; "
                f"sha256 `{ref.digest}`"
            )
            if ref.media_type.startswith("image/"):
                image_paths.append(str(destination))
        if not artifact.deliverables:
            deliverable_lines.append("- No sidecar deliverables were declared.")
        atomic_write_text(context / "DELIVERABLES.md", "\n".join(deliverable_lines) + "\n")
        return artifact_view, image_paths

    def _write_source_context(self, workspace: CallWorkspace) -> list[str]:
        source_dir = workspace.context_dir / "sources"
        source_dir.mkdir(exist_ok=True)
        lines = ["# Supplied sources", ""]
        image_paths: list[str] = []
        for source in self.sources:
            destination = source_dir / source.stored_path.name
            # Workers receive copies materialized from authoritative blobs;
            # mutating a capsule cannot mutate retained source evidence.
            self.blobs.materialize(source.blob, destination)
            lines.append(
                f"- `{self._relative(workspace, destination)}` — {source.display_name}; "
                f"sha256 `{source.blob.digest}`"
            )
            if source.is_image:
                image_paths.append(str(destination))
        if not self.sources:
            lines.append("- No external source files were supplied.")
        atomic_write_text(workspace.context_dir / "SOURCES.md", "\n".join(lines) + "\n")
        return image_paths

    def _action_evidence_lines(
        self,
        workspace: CallWorkspace,
        evidence_dir: Path,
        record: ActionRecord,
    ) -> list[str]:
        action_id = record.spec.action_id
        lines = [
            f"## {action_id}: {record.spec.kind.value} — {record.spec.target}",
            "",
            f"Assignment: {record.spec.assignment}",
        ]
        if record.result is not None:
            lines.append("Findings: " + "; ".join(record.result.findings))
            if record.result.unresolved_risks:
                lines.append("Unresolved risks: " + "; ".join(record.result.unresolved_risks))
            if record.result.frame_break:
                lines.append("Frame break: " + record.result.frame_break)
        if record.receipt is not None:
            cost = record.receipt.observed_cost
            channels = (
                ", ".join(item.value for item in record.receipt.observed_evidence_channels)
                or "none observed"
            )
            lines.extend(
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
            lines.append(f"Full result: `{self._relative(workspace, target)}`")
        if record.patch_blob is not None:
            target = evidence_dir / f"{action_id}.patch"
            self.blobs.materialize(record.patch_blob, target)
            lines.append(f"Candidate patch: `{self._relative(workspace, target)}`")
        lines.append("")
        return lines

    def _standalone_evidence_lines(
        self,
        workspace: CallWorkspace,
        evidence_dir: Path,
        evidence: EvidenceRecord,
    ) -> list[str]:
        lines = [
            f"## {evidence.evidence_id}: {evidence.kind}",
            "",
            f"Summary: {evidence.summary}",
            f"Scope: {evidence.scope}",
            f"Independence: {evidence.independence_class.value}",
            "Modalities: "
            + (", ".join(item.value for item in evidence.modalities) or "unspecified"),
        ]
        if evidence.establishes:
            lines.append("Establishes: " + "; ".join(evidence.establishes))
        if evidence.cannot_establish:
            lines.append("Cannot establish: " + "; ".join(evidence.cannot_establish))
        if evidence.blob is not None:
            suffix = Path(evidence.blob.original_name or "evidence.txt").suffix or ".txt"
            target = evidence_dir / f"{evidence.evidence_id}{suffix}"
            self.blobs.materialize(evidence.blob, target)
            lines.append(f"Full evidence: `{self._relative(workspace, target)}`")
        lines.append("")
        return lines

    def _write_evidence_context(
        self,
        workspace: CallWorkspace,
        *,
        state: RunState | None,
        explicit_action_ids: list[str],
        action_contract: ActionContract | None,
    ) -> list[str]:
        evidence_dir = workspace.context_dir / "evidence"
        evidence_dir.mkdir(exist_ok=True)
        lines = ["# Decision-relevant evidence", ""]
        records: list[ActionRecord] = []
        if state is not None:
            records = self._select_evidence_records(
                state,
                explicit_action_ids=explicit_action_ids,
                action_contract=action_contract,
            )
            for record in records:
                lines.extend(self._action_evidence_lines(workspace, evidence_dir, record))
            selected = {record.spec.action_id for record in records}
            remaining = max(0, self.evidence_limit - len(records))
            standalone = [
                evidence
                for evidence in state.evidence.values()
                if evidence.source_action_id not in selected
            ]
            for evidence in standalone[-remaining:] if remaining else []:
                lines.extend(self._standalone_evidence_lines(workspace, evidence_dir, evidence))
        if len(lines) == 2:
            lines.append("No completed targeted actions are available yet.")
        atomic_write_text(workspace.context_dir / "EVIDENCE_INDEX.md", "\n".join(lines) + "\n")
        return [record.spec.action_id for record in records]

    def _write_context_lens(
        self,
        workspace: CallWorkspace,
        *,
        spec: CapsuleSpec,
        source: TaskSource,
        artifact_view: Literal["none", "full", "preview_with_full"],
        selected_action_ids: list[str],
        required_modalities: list[EvidenceModality],
    ) -> ContextLens:
        context = workspace.context_dir
        state = spec.state
        contract = spec.action_contract
        artifact_scope = (
            contract.artifact_scope
            if contract is not None
            else (
                "release"
                if spec.purpose in {"synthesis", "release", "repair"}
                else "whole_artifact"
            )
        )
        included = [
            "exact Task Source and verification contract",
            "compact explicit run state, Task Charter, Artifact Spine, and Frontier Kernel",
            "task-native observation contract",
            "supplied source snapshot",
        ]
        omissions: list[str] = []
        zoom_paths = [
            self._relative(workspace, context / "SOURCES.md"),
            self._relative(workspace, context / "EVIDENCE_INDEX.md"),
        ]
        if state is not None and state.current_artifact is not None:
            included.append("current authoritative artifact")
            zoom_paths.append(self._relative(workspace, context / "CURRENT_ARTIFACT.md"))
            if artifact_view == "preview_with_full":
                omissions.append(
                    "The compact artifact view omits its middle; the complete bytes are "
                    "staged separately."
                )
                zoom_paths.append(self._relative(workspace, context / "CURRENT_ARTIFACT.full.md"))
            completed = sum(
                record.status == ActionStatus.COMPLETE for record in state.actions.values()
            )
            if completed > len(selected_action_ids):
                omissions.append(
                    f"{completed - len(selected_action_ids)} completed action record(s) are "
                    "outside the compact evidence view; the ledger and blob store remain "
                    "authoritative."
                )
        payload = {
            "purpose": spec.purpose,
            "action_id": contract.action_id if contract else None,
            "task_source_digest": source.digest,
            "artifact_digest": (
                state.current_artifact.blob.digest
                if state is not None and state.current_artifact is not None
                else None
            ),
            "artifact_scope": artifact_scope,
            "artifact_view": artifact_view,
            "obligation_ids": list(contract.obligation_ids) if contract else [],
            "crux_ids": list(contract.target_crux_ids) if contract else [],
            "evidence_action_ids": selected_action_ids,
            "required_modalities": [item.value for item in required_modalities],
            "included": included,
            "omissions": omissions,
            "zoom_paths": zoom_paths,
            "state_event_seq": state.last_event_seq if state is not None else 0,
        }
        lens = ContextLens.model_validate(
            {**payload, "digest": sha256_text(canonical_json(payload))}
        )
        self._write_json(context / "CONTEXT_LENS.json", lens.model_dump(mode="json"))
        return lens

    def populate(
        self,
        workspace: CallWorkspace,
        spec: CapsuleSpec,
    ) -> dict[str, str | list[str]]:
        state = spec.state
        evidence_action_ids = spec.evidence_action_ids
        action_contract = spec.action_contract
        lens_purpose = spec.purpose
        context = workspace.context_dir
        source_value = self._write_anchor_context(workspace, spec)
        artifact_view, image_paths = self._write_state_context(workspace, state)
        image_paths.extend(self._write_source_context(workspace))

        observation_rows, required_modalities = self._observation_contract(
            state,
            action_contract=action_contract,
            purpose=lens_purpose,
        )
        atomic_write_text(
            context / "OBSERVATION_CONTRACT.json",
            json.dumps(observation_rows, indent=2, ensure_ascii=False),
        )

        selected_ids = self._write_evidence_context(
            workspace,
            state=state,
            explicit_action_ids=evidence_action_ids,
            action_contract=action_contract,
        )

        lens = self._write_context_lens(
            workspace,
            spec=spec,
            source=source_value,
            artifact_view=artifact_view,
            selected_action_ids=selected_ids,
            required_modalities=required_modalities,
        )

        return {
            "context_dir": self._relative(workspace, context),
            "request": self._relative(workspace, context / "REQUEST.md"),
            "assignment": self._relative(workspace, context / "ASSIGNMENT.md"),
            "verification_contract": self._relative(
                workspace, context / "VERIFICATION_CONTRACT.json"
            ),
            "sources": self._relative(workspace, context / "SOURCES.md"),
            "state": self._relative(workspace, context / "STATE.json") if state is not None else "",
            "frontier_kernel": self._relative(workspace, context / "FRONTIER_KERNEL.json"),
            "context_lens": self._relative(workspace, context / "CONTEXT_LENS.json"),
            "context_lens_digest": lens.digest,
            "artifact": self._relative(workspace, context / "CURRENT_ARTIFACT.md")
            if state is not None and state.current_artifact is not None
            else "",
            "evidence": self._relative(workspace, context / "EVIDENCE_INDEX.md"),
            "image_paths": image_paths,
        }
