"""One-way translation from pre-canonical journal payloads.

Legacy promotion records remain immutable in their original ledgers.  They are
translated at replay into ordinary finish claims and exact challenges; none of
their ceremony enters the live RunState.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .types import AssayStatus, MoveMode, RunState

_PROMOTION_RESULT_FIELDS = {
    "promotion_decision",
    "promotion_lease",
    "clear_deferred_finish_claim",
}


def canonical_payload(
    event_type: str,
    payload: dict[str, Any],
    state: RunState | None,
) -> dict[str, Any]:
    """Return a canonical copy; current payloads pass through unchanged."""

    value = deepcopy(payload)
    if event_type == "move.proposed":
        _canonical_move(value.get("move"))
        return value
    if event_type != "move.applied":
        return value

    for field in _PROMOTION_RESULT_FIELDS:
        value.pop(field, None)
    deferred = value.pop("deferred_finish_claim", None)
    if deferred is not None and value.get("finish_claim") is None:
        value["finish_claim"] = deferred
    for move in value.get("next_moves", []):
        _canonical_move(move)

    running = state.moves.get(value.get("move_id", "")) if state is not None else None
    challenge = running is not None and running.mode == MoveMode.CHALLENGE
    claim_id = state.finish_claim.claim_id if state is not None and state.finish_claim else None
    for observation in value.get("observations", []):
        metadata = observation.get("metadata") or {}
        for field in (
            "claim_id",
            "assay_status",
            "assay_coverage",
            "material_to_claim",
            "direct_inspection",
            "quality_delta",
        ):
            if field in metadata and field not in observation:
                observation[field] = metadata.pop(field)
        observation["metadata"] = metadata
        if challenge and observation.get("challenge_verdict") is not None:
            observation.setdefault("claim_id", claim_id)
            observation.setdefault("assay_status", AssayStatus.VALID.value)
            observation.setdefault("direct_inspection", True)
    return value


def _canonical_move(move: object) -> None:
    if isinstance(move, dict):
        move.pop("promotion_gate", None)
