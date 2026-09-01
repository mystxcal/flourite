"""Deterministic offline execution for the real intelligence kernel.

The fake runner exists to exercise the same journal, reducer, completion,
control, and materialization paths as a live run.  It is deliberately small;
it is not a second controller and it does not simulate model intelligence.
"""

from __future__ import annotations

from ..core.types import (
    AssayStatus,
    ChallengeVerdict,
    ComputeUsage,
    Move,
    MoveMode,
    ObservationKind,
    RunState,
)
from .context import ContextFrame
from .contracts import (
    FinishDraft,
    MoveExecutionResult,
    ObservationDraft,
    WorkspaceDraft,
)


class DeterministicMoveRunner:
    """Complete one tiny artifact and challenge it through the canonical path."""

    async def run(
        self,
        *,
        move: Move,
        state: RunState,
        context: ContextFrame,
        recovering: bool,
    ) -> MoveExecutionResult:
        del state, recovering
        if move.mode == MoveMode.CHALLENGE:
            return MoveExecutionResult(
                observations=[
                    ObservationDraft(
                        kind=ObservationKind.CHALLENGE,
                        summary="Direct deterministic inspection supports the completion claim",
                        source="fresh-challenger",
                        challenge_verdict=ChallengeVerdict.SUPPORTS,
                        confidence=1.0,
                        assay_status=AssayStatus.VALID,
                        material_to_claim=True,
                        direct_inspection=True,
                    )
                ],
                usage=ComputeUsage(model_turns=1),
            )

        objective = " ".join(context.objective_text.split())
        document = (
            "# Flourite offline demonstration\n\n"
            f"Objective: {objective}\n\n"
            "The canonical intelligence kernel created this artifact, committed it "
            "atomically, and sent its completion claim to a fresh challenger.\n"
        )
        return MoveExecutionResult(
            workspace=WorkspaceDraft(
                document=document,
                summary="Deterministic canonical-kernel artifact",
                consumed_observation_ids=[
                    item.observation_id
                    for item in context.observations
                    if item.kind == ObservationKind.STEERING
                ],
            ),
            finish=FinishDraft(
                satisfaction_claims=[
                    "The offline artifact was created through the production kernel path"
                ]
            ),
            usage=ComputeUsage(model_turns=1),
        )
