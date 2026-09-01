"""Compile one model/tool result into Flourite's canonical atomic event."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ..blobs import BlobStore
from ..core.types import (
    ArtifactVersion,
    FinishClaim,
    Move,
    MoveApplied,
    MoveMode,
    Observation,
    ObservationKind,
    RunState,
    Trajectory,
    TrajectoryStatus,
    WorkspaceVersion,
)
from ..ids import new_id
from ..util import utc_now
from .budget import CausalBoundarySignal, causal_boundary_signal
from .contracts import MoveDirective, MoveExecutionResult, ObservationDraft

DEFAULT_QUALITY_LENS = """# Quality lens

- Satisfy the exact objective, including explicit constraints and amendments.
- Judge the real artifact with task-native evidence, not activity or proxy checks.
- Preserve correctness, completeness, coherence, usability, and robustness where they matter.
- Record newly discovered success signatures, failure signatures, and blind spots here.
"""


class MoveBuilder(Protocol):
    def __call__(
        self,
        directive: MoveDirective,
        *,
        based_on_workspace_id: str | None,
        additional_trajectory_ids: set[str] | None = None,
    ) -> Move | None: ...


@dataclass(frozen=True, slots=True)
class CompiledContinuations:
    moves: tuple[Move, ...] = ()
    trajectories: tuple[Trajectory, ...] = ()


class MoveResultCompiler:
    """Create the event payload without changing authoritative run state."""

    def __init__(
        self,
        *,
        state: RunState,
        blobs: BlobStore,
        build_move: MoveBuilder,
    ) -> None:
        self.state = state
        self.blobs = blobs
        self.build_move = build_move
        self.now = utc_now()

    def compile(
        self,
        move: Move,
        result: MoveExecutionResult,
        *,
        visible_observation_ids: set[str],
    ) -> MoveApplied:
        artifact = self._artifact(move, result)
        observations = self._observations(move, result.observations, artifact)
        workspace = self._workspace(
            move,
            result,
            artifact,
            visible_observation_ids=visible_observation_ids,
        )
        repeated_low_information = self._record_information_signal(
            move, artifact, observations, workspace
        )
        resulting_workspace = self._resulting_workspace(result, workspace)
        finish_claim = self._finish_claim(result, resulting_workspace, observations)
        hard_exhausted = bool(
            self.state.usage.plus(result.usage).exhausted(self.state.objective.envelope)
        )
        signal = causal_boundary_signal(
            self.state,
            prospective_usage=result.usage,
            prospective_lead_seconds=(
                result.usage.wall_seconds
                if result.success
                and move.mode == MoveMode.LEAD
                and move.trajectory_id == self.state.root_trajectory_id
                else None
            ),
        )
        checkpoint_terminal = (
            move.causal_checkpoint
            and result.success
            and not hard_exhausted
            and finish_claim is None
            and result.blocker is None
        )
        forced_directive = (
            self._causal_checkpoint(signal, result)
            if signal is not None
            and result.success
            and not move.causal_checkpoint
            and finish_claim is None
            and result.blocker is None
            and self.state.finish_claim is None
            else None
        )
        continuations = (
            CompiledContinuations()
            if checkpoint_terminal
            else self._continuations(
                move,
                result,
                workspace=resulting_workspace,
                repeated_low_information=repeated_low_information,
                has_finish_claim=finish_claim is not None,
                forced_directive=forced_directive,
            )
        )
        workspace = self._include_new_trajectories(workspace, continuations.trajectories)
        blocker = result.blocker
        if checkpoint_terminal:
            checkpoint_observation = self._checkpoint_observation(
                move,
                resulting_workspace,
                signal,
            )
            observations.append(checkpoint_observation)
        blocker_evidence = (
            list(
                dict.fromkeys(
                    [*blocker.evidence_refs]
                    + [item.observation_id for item in observations if item.raw_ref is not None]
                )
            )
            if blocker is not None
            else ([checkpoint_observation.observation_id] if checkpoint_terminal else [])
        )
        return MoveApplied(
            move_id=move.move_id,
            success=result.success,
            finished_at=self.now,
            usage_delta=result.usage,
            observations=observations,
            artifacts=[artifact] if artifact is not None else [],
            new_trajectories=list(continuations.trajectories),
            workspace=workspace,
            activate_workspace=result.workspace.activate if result.workspace is not None else True,
            finish_claim=finish_claim,
            next_moves=list(continuations.moves),
            blocked_reason=(
                blocker.reason
                if blocker is not None
                else (
                    "The adaptive causal-boundary checkpoint preserved the current "
                    "artifact and unresolved decision before the hard envelope."
                    if checkpoint_terminal
                    else None
                )
            ),
            blocker_evidence_refs=blocker_evidence,
            error=result.error,
        )

    def _causal_checkpoint(
        self,
        signal: CausalBoundarySignal,
        result: MoveExecutionResult,
    ) -> MoveDirective:
        requested = self._directives(result)
        requested_text = (
            " | ".join(f"{item.intent}: {item.instructions}" for item in requested)
            if requested
            else "No continuation was proposed."
        )
        return MoveDirective(
            mode=MoveMode.LEAD,
            intent="Settle the earliest causal boundary before the hard envelope",
            instructions=(
                "The controller has admitted the one adaptive causal-boundary checkpoint. "
                f"About {signal.remaining_wall_seconds:.0f}s remain; the empirical upper-"
                f"quartile Lead move is {signal.empirical_move_seconds:.0f}s across "
                f"{signal.completed_lead_moves} completed Lead moves. Do not spend this move "
                "on a downstream symptom, local polish, or another plan. Inspect the current "
                "artifact and evidence, identify the earliest accessible cause behind the live "
                "decision boundary, and directly execute one complete intervention only if you "
                "can inspect and commit it inside this move. Otherwise preserve the current head, "
                "attach the workspace or artifact as durable evidence, and return a blocker whose "
                "reason names the exact unfinished causal boundary. Do not return another next "
                "move or branch. The prior continuation was: " + requested_text
            ),
            trajectory_id=self.state.root_trajectory_id,
            causal_checkpoint=True,
        )

    def _checkpoint_observation(
        self,
        move: Move,
        workspace: WorkspaceVersion | None,
        signal: CausalBoundarySignal | None,
    ) -> Observation:
        snapshot = (
            workspace.document_ref
            if workspace is not None
            else self.state.objective.original_text_ref
        )
        boundary = workspace.decision_boundary if workspace is not None else None
        return Observation(
            observation_id=new_id("obs"),
            kind=ObservationKind.RESOURCE,
            summary=(
                "The adaptive causal-boundary checkpoint ended without a finish claim or "
                "evidenced external blocker; the current workspace was preserved instead of "
                "starting another move."
            ),
            source="kernel",
            created_at=self.now,
            move_id=move.move_id,
            trajectory_id=move.trajectory_id,
            raw_ref=snapshot,
            metadata={
                "kernel_signal": "causal_boundary_settled",
                "decision_boundary": boundary,
                "remaining_wall_seconds": (
                    signal.remaining_wall_seconds if signal is not None else None
                ),
            },
        )

    def _artifact(self, move: Move, result: MoveExecutionResult) -> ArtifactVersion | None:
        draft = result.artifact
        if draft is None:
            return None
        return ArtifactVersion(
            artifact_id=new_id("art"),
            content_ref=draft.content_ref,
            digest=draft.content_ref.digest,
            parent_artifact_ids=draft.parent_artifact_ids,
            trajectory_id=move.trajectory_id,
            created_by_move_id=move.move_id,
            deliverables=draft.deliverables,
            metadata=dict(draft.metadata),
            created_at=self.now,
        )

    def _observations(
        self,
        move: Move,
        drafts: Sequence[ObservationDraft],
        artifact: ArtifactVersion | None,
    ) -> list[Observation]:
        return [self._observation(move, draft, artifact) for draft in drafts]

    def _observation(
        self,
        move: Move,
        draft: ObservationDraft,
        artifact: ArtifactVersion | None,
    ) -> Observation:
        metadata = dict(draft.metadata)
        claim_id = draft.claim_id
        if claim_id is None and move.mode == MoveMode.CHALLENGE and self.state.finish_claim:
            claim_id = self.state.finish_claim.claim_id
        bound_digest = draft.artifact_digest
        if bound_digest is None and draft.bind_to_new_artifact and artifact is not None:
            bound_digest = artifact.digest
        return Observation(
            observation_id=new_id("obs"),
            kind=draft.kind,
            summary=draft.summary,
            source=draft.source,
            created_at=self.now,
            move_id=move.move_id,
            trajectory_id=move.trajectory_id,
            artifact_digest=bound_digest,
            raw_ref=draft.raw_ref,
            confidence=draft.confidence,
            challenge_verdict=draft.challenge_verdict,
            claim_id=claim_id,
            assay_status=draft.assay_status,
            assay_coverage=draft.assay_coverage,
            covered_claims=draft.covered_claims,
            material_to_claim=draft.material_to_claim,
            direct_inspection=draft.direct_inspection,
            quality_delta=draft.quality_delta,
            metadata=metadata,
        )

    def _record_information_signal(
        self,
        move: Move,
        artifact: ArtifactVersion | None,
        observations: list[Observation],
        workspace: WorkspaceVersion | None,
    ) -> bool:
        if not self._is_low_information(move, artifact, observations, workspace):
            return False
        repeated = self._previous_move_was_low_information(move.trajectory_id)
        observations.append(
            Observation(
                observation_id=new_id("obs"),
                kind=ObservationKind.RESOURCE,
                summary=(
                    "This Lead move revisited the same decision boundary without changing "
                    "the artifact or adding durable evidence."
                ),
                source="kernel",
                created_at=self.now,
                move_id=move.move_id,
                trajectory_id=move.trajectory_id,
                metadata={"kernel_signal": "low_information", "repeated": repeated},
            )
        )
        return repeated

    def _is_low_information(
        self,
        move: Move,
        artifact: ArtifactVersion | None,
        observations: Sequence[Observation],
        workspace: WorkspaceVersion | None,
    ) -> bool:
        # Model-selected labels are not evidence. Information gain here means
        # durable inspected bytes or a changed artifact, not calling prose a
        # tool/test/source observation.
        has_direct_evidence = any(item.raw_ref is not None for item in observations)
        if (
            move.mode != MoveMode.LEAD
            or move.based_on_workspace_id is None
            or artifact is not None
            or has_direct_evidence
            or workspace is None
        ):
            return False
        base = self.state.workspaces[move.based_on_workspace_id]
        current_boundary = self._normalize_boundary(workspace.decision_boundary)
        previous_boundary = self._normalize_boundary(base.decision_boundary)
        return current_boundary is None or current_boundary == previous_boundary

    @staticmethod
    def _normalize_boundary(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.casefold().split())
        return normalized or None

    def _previous_move_was_low_information(self, trajectory_id: str) -> bool:
        completed = sorted(
            (
                item
                for item in self.state.moves.values()
                if item.trajectory_id == trajectory_id
                and item.mode == MoveMode.LEAD
                and item.status.terminal
            ),
            key=lambda item: item.proposed_at,
        )
        if not completed:
            return False
        return any(
            self.state.observations[item].metadata.get("kernel_signal") == "low_information"
            for item in completed[-1].observation_ids
            if item in self.state.observations
        )

    def _workspace(
        self,
        move: Move,
        result: MoveExecutionResult,
        artifact: ArtifactVersion | None,
        *,
        visible_observation_ids: set[str],
    ) -> WorkspaceVersion | None:
        draft = result.workspace
        if draft is None:
            return None
        document_ref = self.blobs.put_text(
            draft.document,
            media_type="text/markdown; charset=utf-8",
            original_name="workspace.md",
        )
        base = self.state.workspaces.get(move.based_on_workspace_id or "")
        quality_ref = (
            self.blobs.put_text(
                draft.quality_document,
                media_type="text/markdown; charset=utf-8",
                original_name="quality.md",
            )
            if draft.quality_document is not None
            else (
                base.quality_ref
                if base is not None and base.quality_ref is not None
                else self.blobs.put_text(
                    DEFAULT_QUALITY_LENS,
                    media_type="text/markdown; charset=utf-8",
                    original_name="quality.md",
                )
            )
        )
        trajectories = (
            list(draft.active_trajectory_ids)
            if draft.active_trajectory_ids is not None
            else (
                list(base.active_trajectory_ids)
                if base is not None
                else [
                    item.trajectory_id
                    for item in self.state.trajectories.values()
                    if item.status == TrajectoryStatus.ACTIVE
                ]
            )
        )
        heads = (
            list(draft.artifact_head_ids)
            if draft.artifact_head_ids is not None
            else self._artifact_heads(move, artifact, active_trajectory_ids=set(trajectories))
        )
        inherited_consumed = list(base.consumed_observation_ids if base is not None else [])
        requested_consumed = list(dict.fromkeys(draft.consumed_observation_ids))
        unseen = set(requested_consumed) - set(inherited_consumed) - visible_observation_ids
        if unseen:
            raise ValueError(
                "workspace cannot consume evidence absent from its context: "
                + ", ".join(sorted(unseen))
            )
        consumed = list(dict.fromkeys([*inherited_consumed, *requested_consumed]))
        return WorkspaceVersion(
            workspace_id=new_id("ws"),
            parent_workspace_id=move.based_on_workspace_id,
            document_ref=document_ref,
            quality_ref=quality_ref,
            summary=draft.summary,
            decision_boundary=draft.decision_boundary,
            based_on_event_seq=self.state.last_event_seq,
            artifact_head_ids=heads,
            active_trajectory_ids=trajectories,
            consumed_observation_ids=consumed,
            created_by_move_id=move.move_id,
            created_at=self.now,
        )

    def _artifact_heads(
        self,
        move: Move,
        artifact: ArtifactVersion | None,
        *,
        active_trajectory_ids: set[str],
    ) -> list[str]:
        base = self.state.workspaces.get(move.based_on_workspace_id or "")
        heads = [
            artifact_id
            for artifact_id in (base.artifact_head_ids if base is not None else [])
            if self.state.artifacts[artifact_id].trajectory_id in active_trajectory_ids
        ]
        if artifact is None:
            return heads
        heads = [
            artifact_id
            for artifact_id in heads
            if self.state.artifacts[artifact_id].trajectory_id != move.trajectory_id
        ]
        return [*heads, artifact.artifact_id]

    def _resulting_workspace(
        self,
        result: MoveExecutionResult,
        workspace: WorkspaceVersion | None,
    ) -> WorkspaceVersion | None:
        if workspace is not None and result.workspace is not None and result.workspace.activate:
            return workspace
        return self.state.current_workspace

    def _finish_claim(
        self,
        result: MoveExecutionResult,
        workspace: WorkspaceVersion | None,
        observations: Sequence[Observation],
    ) -> FinishClaim | None:
        draft = result.finish
        if not result.success or draft is None:
            return None
        if workspace is None:
            raise ValueError("finish claim requires a live workspace")
        if workspace.quality_ref is None:
            raise ValueError("finish claim requires a task-native quality lens")
        artifact_head_ids = draft.artifact_head_ids or workspace.artifact_head_ids
        if not artifact_head_ids:
            raise ValueError("finish claim requires an actual artifact head")
        return FinishClaim(
            claim_id=new_id("claim"),
            workspace_id=workspace.workspace_id,
            quality_digest=workspace.quality_ref.digest,
            artifact_head_ids=artifact_head_ids,
            satisfaction_claims=draft.satisfaction_claims,
            evidence_refs=list(
                dict.fromkeys(
                    [
                        *workspace.consumed_observation_ids,
                        *draft.evidence_refs,
                        *(item.observation_id for item in observations),
                    ]
                )
            ),
            residual_uncertainty=draft.residual_uncertainty,
            created_at=self.now,
        )

    def _continuations(
        self,
        move: Move,
        result: MoveExecutionResult,
        *,
        workspace: WorkspaceVersion | None,
        repeated_low_information: bool,
        has_finish_claim: bool,
        forced_directive: MoveDirective | None,
    ) -> CompiledContinuations:
        directives = (
            [forced_directive] if forced_directive is not None else self._directives(result)
        )
        if repeated_low_information and directives and not has_finish_claim:
            directives = [self._navigation_escape(move)]
        if not directives or has_finish_claim:
            return CompiledContinuations()
        moves: list[Move] = []
        trajectories: list[Trajectory] = []
        reserved_keys: set[str] = set()
        workspace_id = workspace.workspace_id if workspace is not None else None
        for directive in directives:
            effective = directive
            if directive.fork_purpose is not None:
                trajectory = self._fork(move, directive, workspace_id)
                trajectories.append(trajectory)
                effective = directive.model_copy(
                    update={"trajectory_id": trajectory.trajectory_id, "fork_purpose": None}
                )
            candidate = self.build_move(
                effective,
                based_on_workspace_id=workspace_id,
                additional_trajectory_ids={item.trajectory_id for item in trajectories},
            )
            if candidate is not None and candidate.idempotency_key not in reserved_keys:
                moves.append(candidate)
                reserved_keys.add(candidate.idempotency_key)
        return CompiledContinuations(tuple(moves), tuple(trajectories))

    @staticmethod
    def _directives(result: MoveExecutionResult) -> list[MoveDirective]:
        if result.next_moves:
            return list(result.next_moves)
        return [result.next_move] if result.next_move is not None else []

    @staticmethod
    def _navigation_escape(move: Move) -> MoveDirective:
        return MoveDirective(
            mode=MoveMode.NAVIGATE,
            intent="Escape a repeated low-information trajectory",
            instructions=(
                "Two consecutive Lead moves stayed on the same decision boundary while changing "
                "neither the artifact nor direct evidence. Reconstruct the frontier from fresh "
                "context, identify the repeated assumption "
                "or missing representation, and return a materially different next move."
            ),
            trajectory_id=move.trajectory_id,
        )

    def _fork(
        self,
        move: Move,
        directive: MoveDirective,
        workspace_id: str | None,
    ) -> Trajectory:
        assert directive.fork_purpose is not None
        return Trajectory(
            trajectory_id=new_id("traj"),
            purpose=directive.fork_purpose,
            base_workspace_id=workspace_id,
            parent_trajectory_id=move.trajectory_id,
            created_at=self.now,
        )

    @staticmethod
    def _include_new_trajectories(
        workspace: WorkspaceVersion | None,
        trajectories: Sequence[Trajectory],
    ) -> WorkspaceVersion | None:
        if workspace is None or not trajectories:
            return workspace
        active_ids = list(
            dict.fromkeys(
                workspace.active_trajectory_ids + [item.trajectory_id for item in trajectories]
            )
        )
        return workspace.model_copy(update={"active_trajectory_ids": active_ids})
