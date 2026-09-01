"""One-way translation from pre-canonical journal payloads.

Legacy promotion records remain immutable in their original ledgers.  They are
translated at replay into ordinary finish claims and exact challenges; none of
their ceremony enters the live RunState.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .types import AssayStatus, FailureDomain, MoveMode, PauseKind, RunState

_PROMOTION_RESULT_FIELDS = {
    "promotion_decision",
    "promotion_lease",
    "clear_deferred_finish_claim",
}
_OBSERVATION_METADATA_FIELDS = (
    "claim_id",
    "assay_status",
    "assay_coverage",
    "material_to_claim",
    "direct_inspection",
    "quality_delta",
)


def canonical_payload(
    event_type: str,
    payload: dict[str, Any],
    state: RunState | None,
) -> dict[str, Any]:
    """Return a canonical copy; current payloads pass through unchanged."""

    value = deepcopy(payload)
    if event_type == "run.paused":
        return _canonical_pause(value)
    if event_type == "move.proposed":
        _canonical_move(value.get("move"))
        return value
    if event_type != "move.applied":
        return value

    return _canonical_move_result(value, state)


def _canonical_pause(value: dict[str, Any]) -> dict[str, Any]:
    if (
        value.get("kind") == PauseKind.EXECUTION.value
        and value.get("failure_domain") is None
    ):
        value["failure_domain"] = FailureDomain.EXTERNAL.value
    return value


def _canonical_move_result(
    value: dict[str, Any],
    state: RunState | None,
) -> dict[str, Any]:

    for field in _PROMOTION_RESULT_FIELDS:
        value.pop(field, None)
    deferred = value.pop("deferred_finish_claim", None)
    if (
        deferred is not None
        and deferred.get("artifact_head_ids")
        and value.get("finish_claim") is None
    ):
        value["finish_claim"] = deferred
    for move in value.get("next_moves", []):
        _canonical_move(move)

    running = state.moves.get(value.get("move_id", "")) if state is not None else None
    challenge = running is not None and running.mode == MoveMode.CHALLENGE
    claim_id = state.finish_claim.claim_id if state is not None and state.finish_claim else None
    for observation in value.get("observations", []):
        _canonical_observation(
            observation,
            challenge=challenge,
            claim_id=claim_id,
            satisfaction_claims=(
                state.finish_claim.satisfaction_claims
                if state is not None and state.finish_claim is not None
                else []
            ),
        )
    return value


def _canonical_observation(
    observation: dict[str, Any],
    *,
    challenge: bool,
    claim_id: str | None,
    satisfaction_claims: list[str],
) -> None:
    metadata = observation.get("metadata") or {}
    for field in _OBSERVATION_METADATA_FIELDS:
        if field in metadata and field not in observation:
            observation[field] = metadata.pop(field)
    observation["metadata"] = metadata
    if observation.get("challenge_verdict") is None:
        return
    observation.setdefault("assay_coverage", "legacy direct challenge")
    if not challenge:
        return
    observation.setdefault("claim_id", claim_id)
    observation.setdefault("assay_status", AssayStatus.VALID.value)
    observation.setdefault("direct_inspection", True)
    observation.setdefault("covered_claims", satisfaction_claims)


def _canonical_move(move: object) -> None:
    if isinstance(move, dict):
        move.pop("promotion_gate", None)
        move.pop("declared_ceiling", None)
        move.pop("input_refs", None)
