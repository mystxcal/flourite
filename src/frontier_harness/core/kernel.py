"""The single phase-free transition loop for Flourite."""

from __future__ import annotations

from collections.abc import Sequence

from ..blobs import BlobStore
from ..ids import new_id
from ..intelligence.context import ContextAssembler
from ..intelligence.contracts import (
    MoveDirective,
    MoveExecutionResult,
    MoveRunner,
    ObservationDraft,
)
from ..util import canonical_json, sha256_text, utc_now
from .journal import KernelJournal
from .types import (
    ArtifactVersion,
    ChallengeVerdict,
    ComputeEnvelope,
    FinishClaim,
    Move,
    MoveApplied,
    MoveMode,
    MoveProposed,
    MoveStarted,
    MoveStatus,
    Objective,
    Observation,
    ObservationKind,
    RunStarted,
    RunState,
    RunStatus,
    RunTerminated,
    Trajectory,
    WorkspaceVersion,
)


class IntelligenceKernel:
    """Own semantic transitions; delegate cognition and tools to a MoveRunner."""

    def __init__(
        self,
        *,
        journal: KernelJournal,
        blobs: BlobStore,
        runner: MoveRunner,
        capabilities: Sequence[str] = (),
    ) -> None:
        self.journal = journal
        self.blobs = blobs
        self.runner = runner
        self.context = ContextAssembler(blobs=blobs)
        self.capabilities = list(capabilities)

    @property
    def state(self) -> RunState:
        return self.journal.state

    def start(self, objective_text: str, *, envelope: ComputeEnvelope | None = None) -> None:
        if self.journal.ledger.count():
            raise ValueError("cannot start a non-empty run journal")
        now = utc_now()
        text_ref = self.blobs.put_text(
            objective_text,
            media_type="text/markdown; charset=utf-8",
            original_name="objective.md",
        )
        objective = Objective(
            objective_id=new_id("obj"),
            original_text_ref=text_ref,
            original_text_digest=text_ref.digest,
            envelope=envelope or ComputeEnvelope(),
            created_at=now,
        )
        root = Trajectory(
            trajectory_id=new_id("traj"),
            purpose="Solve the exact objective",
            created_at=now,
        )
        self.journal.append(
            "run.started",
            RunStarted(objective=objective, root_trajectory=root),
        )
        self._propose(
            MoveDirective(
                mode=MoveMode.LEAD,
                intent="Establish the strongest live solution and decision map",
                instructions=(
                    "Work directly on the objective. Build a current-best result, inspect what "
                    "matters, and return an honest workspace checkpoint plus the highest-value "
                    "next move or a finish claim."
                ),
                trajectory_id=root.trajectory_id,
            )
        )

    async def run(self, *, max_steps: int | None = None) -> None:
        steps = 0
        while not self.state.status.terminal and self.state.status != RunStatus.PAUSED:
            if max_steps is not None and steps >= max_steps:
                return
            progressed = await self.step()
            if not progressed:
                return
            steps += 1

    async def step(self) -> bool:
        state = self.state
        if state.status.terminal or state.status == RunStatus.PAUSED:
            return False
        exhausted = state.usage.exhausted(state.objective.envelope)
        if exhausted:
            self.journal.append(
                "run.exhausted",
                RunTerminated(status="exhausted", reason="; ".join(exhausted)),
            )
            return True

        running = [state.moves[item] for item in state.active_move_ids]
        if running:
            await self._execute(running[0], recovering=True)
            return True
        proposed = [move for move in state.moves.values() if move.status == MoveStatus.PROPOSED]
        if proposed:
            await self._execute(proposed[0], recovering=False)
            return True

        if state.finish_claim is not None:
            return self._advance_finish_claim()

        last_move = max(state.moves.values(), key=lambda item: item.proposed_at, default=None)
        if last_move is not None and last_move.mode == MoveMode.NAVIGATE:
            directive = MoveDirective(
                mode=MoveMode.LEAD,
                intent="Broaden or reframe the search after navigation found no live move",
                instructions=(
                    "The prior navigation produced no continuation. Return to the exact objective, "
                    "open the full artifact/evidence as needed, change the representation or "
                    "hypothesis class, and make a decision-changing move. Do not infer completion "
                    "from an empty queue."
                ),
            )
        else:
            directive = MoveDirective(
                mode=MoveMode.NAVIGATE,
                intent="Reconstruct the global frontier and find the strongest next move",
                instructions=(
                    "Use a fresh context to detect drift, repetition, hidden assumptions, missing "
                    "alternatives, and the highest-value next move. You are advisory: do not "
                    "declare completion."
                ),
            )
        if self._propose(directive) is None:
            directive.instructions += f"\nThis is a fresh reframe at event {state.last_event_seq}."
            self._propose(directive)
        return True

    def _advance_finish_claim(self) -> bool:
        state = self.state
        claim = state.finish_claim
        assert claim is not None
        relevant = [
            obs
            for obs in state.observations.values()
            if obs.kind == ObservationKind.CHALLENGE
            and obs.metadata.get("claim_id") == claim.claim_id
        ]
        challenged = [
            obs
            for obs in relevant
            if obs.challenge_verdict in {ChallengeVerdict.CHALLENGES, ChallengeVerdict.UNCERTAIN}
            and obs.metadata.get("material_to_claim", True) is not False
        ]
        if challenged:
            findings = "\n".join(f"- {obs.summary}" for obs in challenged)
            self._propose(
                MoveDirective(
                    mode=MoveMode.LEAD,
                    intent="Learn from the completion challenge and improve the live artifact",
                    instructions=(
                        "The current finish claim was not established. Treat these findings as "
                        "new evidence, inspect their direct sources, and reconstruct at the "
                        f"earliest falsified boundary:\n{findings}"
                    ),
                )
            )
            return True
        support = [obs for obs in relevant if obs.challenge_verdict == ChallengeVerdict.SUPPORTS]
        if support:
            claimed_digests = {
                state.artifacts[item].digest for item in claim.artifact_head_ids
            }
            supported_digests = {
                item.artifact_digest for item in support if item.artifact_digest is not None
            }
            missing_digests = claimed_digests - supported_digests
            if missing_digests:
                self._propose(
                    MoveDirective(
                        mode=MoveMode.CHALLENGE,
                        intent="Challenge the unverified artifact heads in the finish claim",
                        instructions=(
                            "Directly inspect the claimed artifact heads whose digests still lack "
                            "independent support: " + ", ".join(sorted(missing_digests))
                        ),
                    )
                )
                return True
            self.journal.append(
                "run.satisfied",
                RunTerminated(
                    status="satisfied",
                    reason="completion claim survived direct independent challenge",
                    claim_id=claim.claim_id,
                    supporting_observation_ids=[obs.observation_id for obs in support],
                ),
            )
            return True
        self._propose(
            MoveDirective(
                mode=MoveMode.CHALLENGE,
                intent="Directly test the current finish claim against the exact objective",
                instructions=(
                    "Inspect the actual artifact, evidence, and objective. Seek concrete "
                    "falsification. Report supports, challenges, or uncertain with direct scope."
                ),
            )
        )
        return True

    def _propose(self, directive: MoveDirective) -> Move | None:
        move = self._build_move(directive, based_on_workspace_id=self.state.current_workspace_id)
        if move is None:
            return None
        self.journal.append(
            "move.proposed",
            MoveProposed(move=move),
            actor="kernel",
            action_id=move.move_id,
        )
        return move

    def _build_move(
        self,
        directive: MoveDirective,
        *,
        based_on_workspace_id: str | None,
        additional_trajectory_ids: set[str] | None = None,
    ) -> Move | None:
        state = self.state
        trajectory_id = directive.trajectory_id or state.root_trajectory_id
        if trajectory_id not in state.trajectories and trajectory_id not in (
            additional_trajectory_ids or set()
        ):
            raise ValueError(f"move trajectory does not exist: {trajectory_id}")
        basis = {
            "workspace": based_on_workspace_id,
            "trajectory": trajectory_id,
            "mode": directive.mode,
            "intent": directive.intent,
            "instructions": directive.instructions,
        }
        idempotency_key = sha256_text(canonical_json(basis))
        for existing in state.moves.values():
            if existing.idempotency_key == idempotency_key:
                return None
        move = Move(
            move_id=new_id("move"),
            based_on_workspace_id=based_on_workspace_id,
            trajectory_id=trajectory_id,
            mode=directive.mode,
            intent=directive.intent,
            instructions=directive.instructions,
            declared_ceiling=directive.declared_ceiling,
            idempotency_key=idempotency_key,
            proposed_at=utc_now(),
        )
        return move

    async def _execute(self, move: Move, *, recovering: bool) -> None:
        if move.status == MoveStatus.PROPOSED:
            self.journal.append(
                "move.started",
                MoveStarted(move_id=move.move_id, started_at=utc_now()),
                actor="runtime",
                action_id=move.move_id,
            )
            move = self.state.moves[move.move_id]
        frame = self.context.build(
            self.state,
            mode=move.mode,
            workspace_id=move.based_on_workspace_id,
            capabilities=self.capabilities,
        )
        try:
            result = await self.runner.run(
                move=move,
                state=self.state,
                context=frame,
                recovering=recovering,
            )
        except Exception as exc:
            result = MoveExecutionResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                observations=[
                    ObservationDraft(
                        kind=ObservationKind.ERROR,
                        summary=f"Move execution failed: {type(exc).__name__}: {exc}",
                        source="runtime",
                    )
                ],
            )
        self._commit_result(move, frame.observations, result)

    def _commit_result(
        self,
        move: Move,
        visible_observations: list[Observation],
        result: MoveExecutionResult,
    ) -> None:
        new_artifact: ArtifactVersion | None = None
        if result.artifact is not None:
            artifact_draft = result.artifact
            new_artifact = ArtifactVersion(
                artifact_id=new_id("art"),
                content_ref=artifact_draft.content_ref,
                digest=artifact_draft.content_ref.digest,
                parent_artifact_ids=artifact_draft.parent_artifact_ids,
                trajectory_id=move.trajectory_id,
                created_by_move_id=move.move_id,
                deliverables=artifact_draft.deliverables,
                metadata=dict(artifact_draft.metadata),
                created_at=utc_now(),
            )

        observations: list[Observation] = []
        for observation_draft in result.observations:
            metadata = dict(observation_draft.metadata)
            if move.mode == MoveMode.CHALLENGE and self.state.finish_claim is not None:
                metadata.setdefault("claim_id", self.state.finish_claim.claim_id)
            observation = Observation(
                observation_id=new_id("obs"),
                kind=observation_draft.kind,
                summary=observation_draft.summary,
                source=observation_draft.source,
                created_at=utc_now(),
                move_id=move.move_id,
                trajectory_id=move.trajectory_id,
                artifact_digest=(
                    observation_draft.artifact_digest
                    or (
                        new_artifact.digest
                        if observation_draft.bind_to_new_artifact and new_artifact is not None
                        else None
                    )
                ),
                raw_ref=observation_draft.raw_ref,
                confidence=observation_draft.confidence,
                challenge_verdict=observation_draft.challenge_verdict,
                metadata=metadata,
            )
            observations.append(observation)

        low_information = (
            move.mode == MoveMode.LEAD
            and move.based_on_workspace_id is not None
            and new_artifact is None
            and not any(
                item.raw_ref is not None
                or item.kind
                in {
                    ObservationKind.TOOL,
                    ObservationKind.TEST,
                    ObservationKind.SOURCE,
                    ObservationKind.ARTIFACT,
                }
                for item in observations
            )
        )
        repeated_low_information = low_information and self._last_move_was_low_information(
            move.trajectory_id
        )
        if low_information:
            observations.append(
                Observation(
                    observation_id=new_id("obs"),
                    kind=ObservationKind.RESOURCE,
                    summary=(
                        "This Lead move changed neither the artifact nor decision-relevant "
                        "external evidence."
                    ),
                    source="kernel",
                    created_at=utc_now(),
                    move_id=move.move_id,
                    trajectory_id=move.trajectory_id,
                    metadata={
                        "kernel_signal": "low_information",
                        "repeated": repeated_low_information,
                    },
                )
            )

        new_workspace: WorkspaceVersion | None = None
        if result.workspace is not None:
            workspace_draft = result.workspace
            document_ref = self.blobs.put_text(
                workspace_draft.document,
                media_type="text/markdown; charset=utf-8",
                original_name="workspace.md",
            )
            base_workspace = self.state.workspaces.get(move.based_on_workspace_id or "")
            current_heads = list(base_workspace.artifact_head_ids if base_workspace else [])
            if new_artifact is not None:
                replaced = {
                    artifact_id
                    for artifact_id in current_heads
                    if self.state.artifacts[artifact_id].trajectory_id == move.trajectory_id
                }
                current_heads = [item for item in current_heads if item not in replaced]
                current_heads.append(new_artifact.artifact_id)
            heads = workspace_draft.artifact_head_ids or current_heads
            trajectories = workspace_draft.active_trajectory_ids or [
                item.trajectory_id
                for item in self.state.trajectories.values()
                if item.status.value == "active"
            ]
            consumed = list(
                dict.fromkeys(
                    [obs.observation_id for obs in visible_observations]
                    + workspace_draft.consumed_observation_ids
                )
            )
            new_workspace = WorkspaceVersion(
                workspace_id=new_id("ws"),
                parent_workspace_id=move.based_on_workspace_id,
                document_ref=document_ref,
                summary=workspace_draft.summary,
                based_on_event_seq=self.state.last_event_seq,
                artifact_head_ids=heads,
                active_trajectory_ids=trajectories,
                consumed_observation_ids=consumed,
                created_by_move_id=move.move_id,
                created_at=utc_now(),
            )
        resulting_workspace = (
            new_workspace
            if new_workspace is not None and result.workspace is not None and result.workspace.activate
            else self.state.current_workspace
        )
        finish_claim: FinishClaim | None = None
        if result.success and result.finish is not None:
            if resulting_workspace is None:
                raise ValueError("finish claim requires a live workspace")
            finish_draft = result.finish
            finish_claim = FinishClaim(
                claim_id=new_id("claim"),
                workspace_id=resulting_workspace.workspace_id,
                artifact_head_ids=(
                    finish_draft.artifact_head_ids or resulting_workspace.artifact_head_ids
                ),
                satisfaction_claims=finish_draft.satisfaction_claims,
                evidence_refs=(
                    finish_draft.evidence_refs
                    or [item.observation_id for item in observations]
                ),
                residual_uncertainty=finish_draft.residual_uncertainty,
                created_at=utc_now(),
            )

        next_moves: list[Move] = []
        new_trajectories: list[Trajectory] = []
        directives = (
            result.next_moves
            if result.next_moves
            else [result.next_move]
            if result.next_move is not None
            else []
        )
        if repeated_low_information and directives and finish_claim is None:
            directives = [
                MoveDirective(
                    mode=MoveMode.NAVIGATE,
                    intent="Escape a repeated low-information trajectory",
                    instructions=(
                        "Two consecutive Lead moves changed neither the artifact nor direct "
                        "evidence. Reconstruct the frontier from fresh context, identify the "
                        "repeated assumption or missing representation, and return a materially "
                        "different next move."
                    ),
                    trajectory_id=move.trajectory_id,
                )
            ]
        continuation_workspace_id = (
            new_workspace.workspace_id
            if new_workspace is not None
            else resulting_workspace.workspace_id
            if resulting_workspace is not None
            else None
        )
        if result.success and directives and finish_claim is None:
            for directive in directives:
                effective = directive
                if directive.fork_purpose is not None:
                    trajectory = Trajectory(
                        trajectory_id=new_id("traj"),
                        purpose=directive.fork_purpose,
                        base_workspace_id=continuation_workspace_id,
                        parent_trajectory_id=move.trajectory_id,
                        created_at=utc_now(),
                    )
                    new_trajectories.append(trajectory)
                    effective = directive.model_copy(
                        update={
                            "trajectory_id": trajectory.trajectory_id,
                            "fork_purpose": None,
                        }
                    )
                candidate = self._build_move(
                    effective,
                    based_on_workspace_id=continuation_workspace_id,
                    additional_trajectory_ids={
                        item.trajectory_id for item in new_trajectories
                    },
                )
                if candidate is not None:
                    next_moves.append(candidate)

        if new_workspace is not None and new_trajectories:
            new_workspace.active_trajectory_ids = list(
                dict.fromkeys(
                    new_workspace.active_trajectory_ids
                    + [item.trajectory_id for item in new_trajectories]
                )
            )

        blocker_reason = result.blocker.reason if result.blocker is not None else None
        blocker_refs = result.blocker.evidence_refs if result.blocker is not None else []
        self.journal.append(
            "move.applied",
            MoveApplied(
                move_id=move.move_id,
                success=result.success,
                finished_at=utc_now(),
                usage_delta=result.usage,
                observations=observations,
                artifacts=[new_artifact] if new_artifact is not None else [],
                new_trajectories=new_trajectories,
                workspace=new_workspace,
                activate_workspace=(
                    result.workspace.activate if result.workspace is not None else True
                ),
                finish_claim=finish_claim,
                next_moves=next_moves,
                blocked_reason=blocker_reason,
                blocker_evidence_refs=blocker_refs,
                error=result.error,
            ),
            actor="runtime",
            action_id=move.move_id,
        )

    def _last_move_was_low_information(self, trajectory_id: str) -> bool:
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
        previous = completed[-1]
        return any(
            self.state.observations[item].metadata.get("kernel_signal") == "low_information"
            for item in previous.observation_ids
            if item in self.state.observations
        )
