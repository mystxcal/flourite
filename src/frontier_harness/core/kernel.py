"""The single phase-free transition loop for Flourite."""

from __future__ import annotations

from collections.abc import Sequence

from ..blobs import BlobStore
from ..ids import new_id
from ..intelligence.compiler import MoveResultCompiler
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
    ChallengeVerdict,
    ComputeEnvelope,
    Move,
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
            return self._continue_after_challenge(challenged)
        support = [obs for obs in relevant if obs.challenge_verdict == ChallengeVerdict.SUPPORTS]
        if support:
            return self._resolve_support(support)
        return self._request_finish_challenge()

    def _continue_after_challenge(self, challenged: list[Observation]) -> bool:
        findings = "\n".join(f"- {observation.summary}" for observation in challenged)
        self._propose(
            MoveDirective(
                mode=MoveMode.LEAD,
                intent="Learn from the completion challenge and improve the live artifact",
                instructions=(
                    "The current finish claim was not established. Treat these findings as new "
                    "evidence, inspect their direct sources, and reconstruct at the earliest "
                    f"falsified boundary:\n{findings}"
                ),
            )
        )
        return True

    def _resolve_support(self, support: list[Observation]) -> bool:
        state = self.state
        claim = state.finish_claim
        assert claim is not None
        claimed = {state.artifacts[item].digest for item in claim.artifact_head_ids}
        supported = {
            observation.artifact_digest
            for observation in support
            if observation.artifact_digest is not None
        }
        missing = claimed - supported
        if missing:
            self._request_artifact_challenge(missing)
            return True
        self.journal.append(
            "run.satisfied",
            RunTerminated(
                status="satisfied",
                reason="completion claim survived direct independent challenge",
                claim_id=claim.claim_id,
                supporting_observation_ids=[item.observation_id for item in support],
            ),
        )
        return True

    def _request_artifact_challenge(self, missing_digests: set[str]) -> None:
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

    def _request_finish_challenge(self) -> bool:
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
        payload = MoveResultCompiler(
            state=self.state,
            blobs=self.blobs,
            build_move=self._build_move,
        ).compile(move, visible_observations, result)
        self.journal.append(
            "move.applied",
            payload,
            actor="runtime",
            action_id=move.move_id,
        )
        if not result.success and not self.state.status.terminal:
            self.journal.append(
                "run.failed",
                RunTerminated(
                    status="failed",
                    reason=result.error or "move execution failed without a reason",
                ),
                actor="runtime",
                action_id=move.move_id,
            )
