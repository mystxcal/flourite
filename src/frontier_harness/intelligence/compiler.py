"""Compile one model/tool result into Flourite's canonical atomic event."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ..blobs import BlobStore
from ..core.promotion import decide_promotion, lease_matches, root_artifact_digest
from ..core.types import (
    ArtifactVersion,
    FinishClaim,
    Move,
    MoveApplied,
    MoveMode,
    Observation,
    ObservationKind,
    PromotionGate,
    RunState,
    Trajectory,
    TrajectoryStatus,
    WorkspaceVersion,
)
from ..ids import new_id
from ..util import utc_now
from .contracts import MoveDirective, MoveExecutionResult, ObservationDraft


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
        visible_observations: Sequence[Observation],
        result: MoveExecutionResult,
    ) -> MoveApplied:
        artifact = self._artifact(move, result)
        observations = self._observations(move, result.observations, artifact)
        promotion_decision = None
        promotion_lease = None
        if (
            result.success
            and move.promotion_gate is not None
            and move.promotion_gate.role == "challenge"
        ):
            promotion_decision, promotion_lease = decide_promotion(
                gate=move.promotion_gate,
                challenge_move_id=move.move_id,
                observations=observations,
                decision_id=new_id("promotion-decision"),
                lease_id=new_id("promotion-lease"),
                created_at=self.now,
            )
        repeated_low_information = self._record_information_signal(move, artifact, observations)
        workspace = self._workspace(move, result, artifact, observations=visible_observations)
        resulting_workspace = self._resulting_workspace(result, workspace)
        promotion = self._promotion_continuation(
            move,
            result,
            artifact,
        )
        requested_finish = self._finish_claim(result, resulting_workspace, observations)
        defer_until_promotion = (
            promotion is not None
            and promotion.promotion_gate is not None
            and promotion.promotion_gate.role == "challenge"
        )
        deferred_finish = requested_finish if defer_until_promotion else None
        finish_claim = requested_finish if promotion is None else None
        clear_deferred_finish = False
        if promotion_decision is not None:
            clear_deferred_finish = True
            pending = self.state.pending_promotion_finish_claim
            if (
                promotion_lease is not None
                and pending is not None
                and self._claim_contains_digest(pending, promotion_lease.artifact_digest)
            ):
                finish_claim = pending
        continuations = self._continuations(
            move,
            result,
            workspace=resulting_workspace,
            repeated_low_information=repeated_low_information,
            has_finish_claim=finish_claim is not None,
            forced_directive=promotion,
        )
        workspace = self._include_new_trajectories(workspace, continuations.trajectories)
        blocker = result.blocker
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
            promotion_decision=promotion_decision,
            promotion_lease=promotion_lease,
            deferred_finish_claim=deferred_finish,
            clear_deferred_finish_claim=clear_deferred_finish,
            finish_claim=finish_claim,
            next_moves=list(continuations.moves),
            blocked_reason=blocker.reason if blocker is not None else None,
            blocker_evidence_refs=blocker.evidence_refs if blocker is not None else [],
            error=result.error,
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
        if move.mode == MoveMode.CHALLENGE and self.state.finish_claim is not None:
            metadata.setdefault("claim_id", self.state.finish_claim.claim_id)
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
            metadata=metadata,
        )

    def _record_information_signal(
        self,
        move: Move,
        artifact: ArtifactVersion | None,
        observations: list[Observation],
    ) -> bool:
        if not self._is_low_information(move, artifact, observations):
            return False
        repeated = self._previous_move_was_low_information(move.trajectory_id)
        observations.append(
            Observation(
                observation_id=new_id("obs"),
                kind=ObservationKind.RESOURCE,
                summary=(
                    "This Lead move changed neither the artifact nor decision-relevant "
                    "external evidence."
                ),
                source="kernel",
                created_at=self.now,
                move_id=move.move_id,
                trajectory_id=move.trajectory_id,
                metadata={"kernel_signal": "low_information", "repeated": repeated},
            )
        )
        return repeated

    @staticmethod
    def _is_low_information(
        move: Move,
        artifact: ArtifactVersion | None,
        observations: Sequence[Observation],
    ) -> bool:
        direct_kinds = {
            ObservationKind.TOOL,
            ObservationKind.TEST,
            ObservationKind.SOURCE,
            ObservationKind.ARTIFACT,
        }
        has_direct_evidence = any(
            item.raw_ref is not None or item.kind in direct_kinds for item in observations
        )
        return (
            move.mode == MoveMode.LEAD
            and move.based_on_workspace_id is not None
            and artifact is None
            and not has_direct_evidence
        )

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
        observations: Sequence[Observation],
    ) -> WorkspaceVersion | None:
        draft = result.workspace
        if draft is None:
            return None
        document_ref = self.blobs.put_text(
            draft.document,
            media_type="text/markdown; charset=utf-8",
            original_name="workspace.md",
        )
        heads = draft.artifact_head_ids or self._artifact_heads(move, artifact)
        trajectories = draft.active_trajectory_ids or [
            item.trajectory_id
            for item in self.state.trajectories.values()
            if item.status == TrajectoryStatus.ACTIVE
        ]
        consumed = list(
            dict.fromkeys(
                [item.observation_id for item in observations] + draft.consumed_observation_ids
            )
        )
        return WorkspaceVersion(
            workspace_id=new_id("ws"),
            parent_workspace_id=move.based_on_workspace_id,
            document_ref=document_ref,
            summary=draft.summary,
            based_on_event_seq=self.state.last_event_seq,
            artifact_head_ids=heads,
            active_trajectory_ids=trajectories,
            consumed_observation_ids=consumed,
            created_by_move_id=move.move_id,
            created_at=self.now,
        )

    def _artifact_heads(self, move: Move, artifact: ArtifactVersion | None) -> list[str]:
        base = self.state.workspaces.get(move.based_on_workspace_id or "")
        heads = list(base.artifact_head_ids if base is not None else [])
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
        return FinishClaim(
            claim_id=new_id("claim"),
            workspace_id=workspace.workspace_id,
            artifact_head_ids=draft.artifact_head_ids or workspace.artifact_head_ids,
            satisfaction_claims=draft.satisfaction_claims,
            evidence_refs=draft.evidence_refs or [item.observation_id for item in observations],
            residual_uncertainty=draft.residual_uncertainty,
            created_at=self.now,
        )

    def _claim_contains_digest(self, claim: FinishClaim, digest: str) -> bool:
        return any(
            self.state.artifacts[item].digest == digest
            for item in claim.artifact_head_ids
            if item in self.state.artifacts
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
            [forced_directive]
            if forced_directive is not None
            else self._directives(result)
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

    def _promotion_continuation(
        self,
        move: Move,
        result: MoveExecutionResult,
        artifact: ArtifactVersion | None,
    ) -> MoveDirective | None:
        if (
            not result.success
            or result.blocker is not None
            or move.mode != MoveMode.LEAD
            or move.trajectory_id != self.state.root_trajectory_id
        ):
            return None
        if move.promotion_gate is not None and move.promotion_gate.role == "revision":
            if (
                artifact is not None
                and artifact.digest != move.promotion_gate.target_artifact_digest
            ):
                return self._promotion_challenge(
                    move,
                    artifact,
                    predecessor_digest=move.promotion_gate.target_artifact_digest,
                )
            return self._required_revision(move)
        if artifact is None or lease_matches(self.state, artifact.digest):
            return None
        return self._promotion_challenge(
            move,
            artifact,
            predecessor_digest=root_artifact_digest(self.state),
        )

    @staticmethod
    def _promotion_challenge(
        move: Move,
        artifact: ArtifactVersion,
        *,
        predecessor_digest: str | None,
    ) -> MoveDirective:
        return MoveDirective(
            mode=MoveMode.CHALLENGE,
            trajectory_id=move.trajectory_id,
            intent="Falsify the exact representative artifact before promotion",
            instructions=(
                "Independently inspect and stress the exact frozen artifact with canonical "
                f"digest {artifact.digest}. Find the strongest concrete reason its governing "
                "premise would fail the objective. Execute the smallest discriminating "
                "artifact-native assay or matched counterexample available and attach its "
                "durable evidence. A support verdict requires direct evidence; absence of a "
                "criticism is not support. Return a scoped supports, challenges, or uncertain "
                "disposition. Do not edit or elaborate the artifact."
            ),
            fork_purpose=(
                "Independently falsify the exact representative artifact before promotion"
            ),
            promotion_gate=PromotionGate(
                role="challenge",
                target_artifact_digest=artifact.digest,
                predecessor_artifact_digest=predecessor_digest,
            ),
        )

    @staticmethod
    def _required_revision(move: Move) -> MoveDirective:
        gate = move.promotion_gate
        assert gate is not None and gate.role == "revision"
        return MoveDirective(
            mode=MoveMode.LEAD,
            trajectory_id=move.trajectory_id,
            intent="Produce a distinct artifact head before promotion",
            instructions=(
                "The attempted revision in move "
                f"{move.move_id} did not produce an artifact digest distinct from the blocked "
                f"head {gate.target_artifact_digest}. Continue the substantive revision now. "
                "Scale-out and finish remain blocked until a distinct head is challenged and "
                "passes on direct evidence."
            ),
            promotion_gate=gate,
        )

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
                "Two consecutive Lead moves changed neither the artifact nor direct evidence. "
                "Reconstruct the frontier from fresh context, identify the repeated assumption "
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
