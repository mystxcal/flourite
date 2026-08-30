"""One exact, replayable promotion boundary for material root artifacts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from .types import (
    ChallengeVerdict,
    Move,
    MoveMode,
    Observation,
    ObservationKind,
    PromotionDecision,
    PromotionGate,
    PromotionLease,
    RunState,
)


def root_artifact_digest(state: RunState) -> str | None:
    trajectory = state.trajectories[state.root_trajectory_id]
    if trajectory.artifact_head_id is None:
        return None
    return state.artifacts[trajectory.artifact_head_id].digest


def lease_matches(state: RunState, digest: str | None = None) -> bool:
    target = digest if digest is not None else root_artifact_digest(state)
    return (
        target is not None
        and state.promotion_lease is not None
        and state.promotion_lease.artifact_digest == target
    )


def move_allowed_at_boundary(
    move: Move,
    *,
    root_digest: str | None,
    lease: PromotionLease | None,
) -> bool:
    """Whether a move can legally cross the current root promotion boundary."""

    if root_digest is None or (
        lease is not None and lease.artifact_digest == root_digest
    ):
        return True
    if move.mode in {MoveMode.NAVIGATE, MoveMode.ENVIRONMENT}:
        return True
    gate = move.promotion_gate
    if gate is None or gate.target_artifact_digest != root_digest:
        return False
    return (move.mode, gate.role) in {
        (MoveMode.CHALLENGE, "challenge"),
        (MoveMode.LEAD, "revision"),
    }


def direct_evidence(observation: Observation) -> bool:
    return observation.raw_ref is not None or observation.source == "artifact-check"


def decide_promotion(
    *,
    gate: PromotionGate,
    challenge_move_id: str,
    observations: Sequence[Observation],
    decision_id: str,
    lease_id: str,
    created_at: str,
) -> tuple[PromotionDecision, PromotionLease | None]:
    """Derive the only legal decision from observations on the challenged digest."""

    relevant = [
        item
        for item in observations
        if item.kind == ObservationKind.CHALLENGE
        and item.artifact_digest == gate.target_artifact_digest
    ]
    direct = [item for item in relevant if direct_evidence(item)]
    blocked = any(
        item.challenge_verdict
        in {ChallengeVerdict.CHALLENGES, ChallengeVerdict.UNCERTAIN}
        and item.metadata.get("material_to_claim", True) is not False
        for item in relevant
    )
    supported = any(
        item.challenge_verdict == ChallengeVerdict.SUPPORTS for item in direct
    )
    disposition: Literal["granted", "denied"] = (
        "granted" if supported and not blocked else "denied"
    )
    decision = PromotionDecision(
        decision_id=decision_id,
        artifact_digest=gate.target_artifact_digest,
        predecessor_artifact_digest=gate.predecessor_artifact_digest,
        challenge_move_id=challenge_move_id,
        disposition=disposition,
        evidence_observation_ids=[item.observation_id for item in relevant],
        direct_evidence_observation_ids=[item.observation_id for item in direct],
        created_at=created_at,
    )
    lease = (
        PromotionLease(
            lease_id=lease_id,
            artifact_digest=gate.target_artifact_digest,
            decision_id=decision_id,
            issued_at=created_at,
        )
        if disposition == "granted"
        else None
    )
    return decision, lease
