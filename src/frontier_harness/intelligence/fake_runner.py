"""Deterministic offline execution for the real intelligence kernel.

The fake runner exists to exercise the same journal, reducer, completion,
control, and materialization paths as a live run.  It is deliberately small;
it is not a second controller and it does not simulate model intelligence.
"""

from __future__ import annotations

from ..blobs import BlobStore
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
    ArtifactDraft,
    FinishDraft,
    MoveExecutionResult,
    ObservationDraft,
    WorkspaceDraft,
)


class DeterministicMoveRunner:
    """Complete one tiny artifact and challenge it through the canonical path."""

    def __init__(self, blobs: BlobStore) -> None:
        self.blobs = blobs

    async def run(
        self,
        *,
        move: Move,
        state: RunState,
        context: ContextFrame,
        recovering: bool,
    ) -> MoveExecutionResult:
        del recovering
        if move.mode == MoveMode.CHALLENGE:
            claim = state.finish_claim
            assert claim is not None
            artifact = state.artifacts[claim.artifact_head_ids[0]]
            return MoveExecutionResult(
                observations=[
                    ObservationDraft(
                        kind=ObservationKind.CHALLENGE,
                        summary="Direct deterministic inspection supports the completion claim",
                        source="fresh-challenger",
                        challenge_verdict=ChallengeVerdict.SUPPORTS,
                        confidence=1.0,
                        artifact_digest=artifact.digest,
                        assay_status=AssayStatus.VALID,
                        assay_coverage="the complete deterministic demonstration artifact",
                        covered_claims=(
                            state.finish_claim.satisfaction_claims
                            if state.finish_claim is not None
                            else []
                        ),
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
            artifact=ArtifactDraft(
                content_ref=self.blobs.put_text(
                    document,
                    media_type="text/markdown; charset=utf-8",
                    original_name="offline-demonstration.md",
                )
            ),
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
