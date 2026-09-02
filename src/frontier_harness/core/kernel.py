"""The single phase-free transition loop for Flourite."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import cast

from ..blobs import BlobStore
from ..errors import LedgerIntegrityError
from ..ids import new_id
from ..intelligence.compiler import MoveResultCompiler
from ..intelligence.context import ContextAssembler
from ..intelligence.contracts import (
    AdmissionRejection,
    MoveDirective,
    MoveExecutionResult,
    MoveRunner,
    ObservationDraft,
    SemanticRepairRunner,
)
from ..util import canonical_json, sha256_text, utc_now
from .journal import KernelJournal
from .transition import CompletionValidator
from .types import (
    AssayStatus,
    ChallengeVerdict,
    ComputeEnvelope,
    ComputeUsage,
    FailureDomain,
    FinishClaim,
    Move,
    MoveMode,
    MoveProposed,
    MoveStarted,
    MoveStatus,
    Objective,
    Observation,
    ObservationKind,
    PauseKind,
    RunPaused,
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
                    "Work directly on the objective. First make the smallest complete, "
                    "end-to-end representative artifact that exposes the governing quality "
                    "or performance bet; do not scale an untested premise. Inspect what matters, "
                    "and return an honest workspace checkpoint plus the highest-value next move "
                    "or a finish claim."
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
            if self._settle_supported_finish():
                return True
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
        if last_move is not None and self._is_fork_challenge(last_move):
            return self._continue_after_fork_challenge(last_move)
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

    def _is_fork_challenge(self, move: Move) -> bool:
        trajectory = self.state.trajectories[move.trajectory_id]
        return move.mode == MoveMode.CHALLENGE and trajectory.parent_trajectory_id is not None

    def _continue_after_fork_challenge(self, move: Move) -> bool:
        trajectory = self.state.trajectories[move.trajectory_id]
        assert trajectory.parent_trajectory_id is not None
        findings = [
            self.state.observations[item]
            for item in move.observation_ids
            if item in self.state.observations
            and self.state.observations[item].challenge_verdict is not None
        ]
        disposition = (
            "\n".join(
                f"- {item.challenge_verdict or ChallengeVerdict.UNCERTAIN}: {item.summary}"
                for item in findings
            )
            or "- uncertain: the fork returned no challenge disposition"
        )
        self._propose(
            MoveDirective(
                mode=MoveMode.LEAD,
                trajectory_id=trajectory.parent_trajectory_id,
                intent="Act on the representative artifact's independent falsification",
                instructions=(
                    "A fresh Challenger inspected the exact representative artifact at its "
                    "frozen fork boundary. Read its direct evidence. Revise, replace, or retain "
                    "the governing premise according to that evidence before scaling; do not "
                    "continue the pre-fork plan by inertia.\n" + disposition
                ),
            )
        )
        return True

    def _advance_finish_claim(self) -> bool:
        state = self.state
        claim = state.finish_claim
        assert claim is not None
        relevant = [
            obs
            for obs in state.observations.values()
            if obs.challenge_verdict is not None
            and obs.claim_id == claim.claim_id
            and obs.assay_status == AssayStatus.VALID
        ]
        lens_deltas = [
            obs
            for obs in state.observations.values()
            if obs.claim_id == claim.claim_id and obs.quality_delta
        ]
        if lens_deltas:
            findings = "\n".join(f"- {item.summary}" for item in lens_deltas)
            self._propose(
                MoveDirective(
                    mode=MoveMode.LEAD,
                    intent="Integrate a newly exposed quality distinction",
                    instructions=(
                        "The fresh inspection exposed a material distinction missing from the "
                        "quality lens. Integrate it, determine which claims it reopens, and "
                        "improve the artifact where needed before making a new finish claim:\n"
                        + findings
                    ),
                )
            )
            return True
        challenged = [
            obs
            for obs in relevant
            if obs.challenge_verdict in {ChallengeVerdict.CHALLENGES, ChallengeVerdict.UNCERTAIN}
            and obs.material_to_claim
        ]
        if challenged:
            return self._continue_after_challenge(challenged)
        support = [
            obs
            for obs in relevant
            if obs.challenge_verdict == ChallengeVerdict.SUPPORTS
            and obs.material_to_claim
            and obs.direct_inspection
            and bool((obs.assay_coverage or "").strip())
        ]
        if support:
            covered_claims = {
                covered for observation in support for covered in observation.covered_claims
            }
            missing_claims = set(claim.satisfaction_claims) - covered_claims
            if missing_claims:
                self._request_claim_challenge(missing_claims)
                return True
            return self._resolve_support(support)
        if relevant:
            summaries = "\n".join(f"- {item.summary}" for item in relevant)
            directive = MoveDirective(
                mode=MoveMode.LEAD,
                intent="Establish material evidence for the finish claim",
                instructions=(
                    "The prior assay was valid but produced no material support for the exact "
                    "finish claim. Do not treat proxy or non-material checks as completion. "
                    "Strengthen the artifact, narrow the claim, or obtain a decision-relevant "
                    f"observation. Prior findings:\n{summaries}"
                ),
            )
            if self._propose(directive) is None:
                directive.instructions += f"\nRe-evaluate at event {state.last_event_seq}."
                self._propose(directive)
            return True
        return self._request_finish_challenge()

    def _settle_supported_finish(self) -> bool:
        """Apply an already-complete proof without spending another unit of compute."""

        state = self.state
        claim = state.finish_claim
        if claim is None:
            return False
        relevant = [
            item
            for item in state.observations.values()
            if item.challenge_verdict is not None
            and item.claim_id == claim.claim_id
            and item.assay_status == AssayStatus.VALID
        ]
        if any(
            item.claim_id == claim.claim_id and item.quality_delta
            for item in state.observations.values()
        ):
            return False
        support = [
            item
            for item in relevant
            if item.challenge_verdict == ChallengeVerdict.SUPPORTS
            and item.material_to_claim
            and item.direct_inspection
            and bool((item.assay_coverage or "").strip())
        ]
        proposed = RunTerminated(
            status="satisfied",
            reason="completion claim survived direct independent challenge",
            claim_id=claim.claim_id,
            supporting_observation_ids=[item.observation_id for item in support],
        )
        try:
            CompletionValidator(state, proposed).validate()
        except LedgerIntegrityError:
            return False
        self._resolve_support(support)
        return self.state.status in {RunStatus.SATISFIED, RunStatus.PAUSED}

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
        try:
            self._verify_completion_material(claim)
        except (OSError, ValueError) as exc:
            self.journal.append(
                "run.paused",
                RunPaused(
                    reason=f"completion material is not readable: {type(exc).__name__}: {exc}",
                    kind=PauseKind.EXECUTION,
                    failure_domain=FailureDomain.ASSAY,
                ),
                actor="kernel",
            )
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

    def _verify_completion_material(self, claim: FinishClaim) -> None:
        workspace = self.state.workspaces[claim.workspace_id]
        if workspace.quality_ref is None:
            raise ValueError("quality lens is missing")
        self.blobs.verify(workspace.quality_ref)
        for artifact_id in claim.artifact_head_ids:
            artifact = self.state.artifacts[artifact_id]
            self.blobs.verify(artifact.content_ref)
            for deliverable in artifact.deliverables:
                self.blobs.verify(deliverable)

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

    def _request_claim_challenge(self, missing_claims: set[str]) -> None:
        self._propose(
            MoveDirective(
                mode=MoveMode.CHALLENGE,
                intent="Challenge the uncovered semantic claims in the finish claim",
                instructions=(
                    "Directly test these exact satisfaction claims, then copy each tested "
                    "claim verbatim into covered_claims: " + " | ".join(sorted(missing_claims))
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
            "event_seq": state.last_event_seq,
            "workspace": based_on_workspace_id,
            "trajectory": trajectory_id,
            "mode": directive.mode,
            "intent": directive.intent,
            "instructions": directive.instructions,
            "causal_checkpoint": directive.causal_checkpoint,
            "retry_of_move_id": directive.retry_of_move_id,
        }
        idempotency_key = sha256_text(canonical_json(basis))
        for existing in state.moves.values():
            if existing.idempotency_key == idempotency_key:
                return None
        move = Move(
            move_id=new_id("move"),
            retry_of_move_id=directive.retry_of_move_id,
            based_on_workspace_id=based_on_workspace_id,
            based_on_event_seq=state.last_event_seq,
            trajectory_id=trajectory_id,
            mode=directive.mode,
            intent=directive.intent,
            instructions=directive.instructions,
            causal_checkpoint=directive.causal_checkpoint,
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
        started = time.monotonic()
        wall_limit = self.state.objective.envelope.max_wall_seconds
        wall_remaining = (
            max(0.0, wall_limit - self.state.usage.wall_seconds) if wall_limit is not None else None
        )
        try:
            async with asyncio.timeout(wall_remaining):
                result = await self.runner.run(
                    move=move,
                    state=self.state,
                    context=frame,
                    recovering=recovering,
                )
        except TimeoutError:
            elapsed = time.monotonic() - started
            checkpoint_ref = (
                self.state.current_workspace.document_ref
                if self.state.current_workspace is not None
                else self.state.objective.original_text_ref
            )
            result = MoveExecutionResult(
                success=False,
                error="ComputeEnvelope: wall-time boundary reached during the live move",
                failure_domain=FailureDomain.EXTERNAL,
                usage=ComputeUsage(wall_seconds=elapsed),
                observations=[
                    ObservationDraft(
                        kind=ObservationKind.RESOURCE,
                        summary="The operator-owned wall-time envelope stopped the live move.",
                        source="kernel",
                        raw_ref=checkpoint_ref,
                        metadata={
                            "current_workspace_id": self.state.current_workspace_id,
                            "causal_checkpoint": move.causal_checkpoint,
                        },
                    )
                ],
            )
        except Exception as exc:
            elapsed = time.monotonic() - started
            result = MoveExecutionResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                failure_domain=FailureDomain.COMPONENT,
                usage=ComputeUsage(wall_seconds=elapsed),
                observations=[
                    ObservationDraft(
                        kind=ObservationKind.ERROR,
                        summary=f"Move execution failed: {type(exc).__name__}: {exc}",
                        source="runtime",
                    )
                ],
            )
        else:
            elapsed = time.monotonic() - started
            result = result.model_copy(
                update={
                    "usage": result.usage.model_copy(
                        update={"wall_seconds": max(result.usage.wall_seconds, elapsed)}
                    )
                }
            )
        visible_observation_ids = {item.observation_id for item in frame.observations}
        rejected_signatures: set[str] = set()
        rejection_attempt = 0
        while True:
            try:
                result = self._commit_result(
                    move,
                    result,
                    visible_observation_ids=visible_observation_ids,
                )
            except (LedgerIntegrityError, ValueError) as exc:
                if self.state.moves[move.move_id].status != MoveStatus.RUNNING:
                    raise
                rejection_attempt += 1
                candidate_digest = sha256_text(canonical_json(result.model_dump(mode="json")))
                signature = sha256_text(
                    canonical_json(
                        {
                            "candidate": candidate_digest,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )
                )
                repairable = (
                    isinstance(self.runner, SemanticRepairRunner)
                    and signature not in rejected_signatures
                    and not self.state.usage.plus(result.usage).exhausted(
                        self.state.objective.envelope
                    )
                )
                if repairable:
                    rejected_signatures.add(signature)
                    repair_runner = cast(SemanticRepairRunner, self.runner)
                    try:
                        result = await repair_runner.repair_rejected_result(
                            move=move,
                            state=self.state,
                            context=frame,
                            rejected=result,
                            rejection=AdmissionRejection(
                                error_type=type(exc).__name__,
                                error=str(exc),
                                candidate_digest=candidate_digest,
                                attempt=rejection_attempt,
                            ),
                        )
                    except Exception as repair_error:
                        result = self._admission_failure(
                            result,
                            "semantic correction failed while preserving the candidate: "
                            f"{type(repair_error).__name__}: {repair_error}",
                        )
                    else:
                        continue
                else:
                    reason = f"{type(exc).__name__}: {exc}"
                    if signature in rejected_signatures:
                        reason += "; the same unchanged candidate repeated the same rejection"
                    result = self._admission_failure(result, reason)
                result = self._commit_result(
                    move,
                    result,
                    visible_observation_ids=visible_observation_ids,
                )
                if isinstance(self.runner, SemanticRepairRunner):
                    self.runner.accept_result(move.move_id, result)
            else:
                if isinstance(self.runner, SemanticRepairRunner):
                    self.runner.accept_result(move.move_id, result)
            break

    @staticmethod
    def _admission_failure(
        candidate: MoveExecutionResult,
        reason: str,
    ) -> MoveExecutionResult:
        """Preserve a rejected candidate as evidence instead of deleting it."""

        return MoveExecutionResult(
            success=False,
            error=f"Invalid move boundary: {reason}",
            failure_domain=FailureDomain.COMPONENT,
            usage=candidate.usage,
            observations=[
                ObservationDraft(
                    kind=ObservationKind.ERROR,
                    summary=(
                        "The candidate remains durably retained, but authoritative admission "
                        f"could not reconcile it: {reason}"
                    ),
                    source="kernel",
                    metadata={
                        "candidate_digest": sha256_text(
                            canonical_json(candidate.model_dump(mode="json"))
                        ),
                        "candidate_retained": True,
                    },
                )
            ],
        )

    def _commit_result(
        self,
        move: Move,
        result: MoveExecutionResult,
        *,
        visible_observation_ids: set[str],
    ) -> MoveExecutionResult:
        exhausted = self.state.usage.plus(result.usage).exhausted(self.state.objective.envelope)
        if not result.success and not exhausted:
            result = result.model_copy(
                update={
                    "next_move": MoveDirective(
                        mode=move.mode,
                        intent=move.intent,
                        instructions=move.instructions,
                        causal_checkpoint=move.causal_checkpoint,
                        trajectory_id=move.trajectory_id,
                        retry_of_move_id=move.move_id,
                    )
                }
            )
        payload = MoveResultCompiler(
            state=self.state,
            blobs=self.blobs,
            build_move=self._build_move,
        ).compile(move, result, visible_observation_ids=visible_observation_ids)
        self.journal.append(
            "move.applied",
            payload,
            actor="runtime",
            action_id=move.move_id,
        )
        if self.state.status.terminal:
            return result
        if exhausted:
            if self._settle_supported_finish():
                return result
            self.journal.append(
                "run.exhausted",
                RunTerminated(
                    status="exhausted",
                    reason="; ".join(exhausted),
                    supporting_observation_ids=list(self.state.moves[move.move_id].observation_ids),
                ),
                actor="kernel",
                action_id=move.move_id,
            )
            return result
        if not result.success and not self.state.status.terminal:
            self.journal.append(
                "run.paused",
                RunPaused(
                    reason=(
                        "execution paused with the original move preserved: "
                        + (result.error or "move execution failed without a reason")
                    ),
                    kind=PauseKind.EXECUTION,
                    failure_domain=result.failure_domain,
                ),
                actor="runtime",
                action_id=move.move_id,
            )
        return result
