from __future__ import annotations

from frontier_harness.core.legacy import canonical_payload
from frontier_harness.core.types import MoveApplied


def test_deferred_promotion_payload_becomes_plain_finish_claim() -> None:
    claim = {
        "claim_id": "claim_old",
        "workspace_id": "ws_old",
        "artifact_head_ids": [],
        "satisfaction_claims": ["the exact objective is satisfied"],
        "evidence_refs": [],
        "residual_uncertainty": [],
        "created_at": "2026-01-01T00:00:00Z",
    }
    old = {
        "move_id": "move_old",
        "success": True,
        "finished_at": "2026-01-01T00:00:00Z",
        "deferred_finish_claim": claim,
        "promotion_decision": None,
        "promotion_lease": None,
        "clear_deferred_finish_claim": False,
        "next_moves": [],
    }

    current = canonical_payload("move.applied", old, None)

    assert current["finish_claim"] == claim
    assert "deferred_finish_claim" not in current
    assert "promotion_decision" not in current
    MoveApplied.model_validate(current)


def test_legacy_evidence_control_metadata_is_promoted_to_typed_fields() -> None:
    old = {
        "move_id": "move_old",
        "success": True,
        "finished_at": "2026-01-01T00:00:00Z",
        "observations": [
            {
                "observation_id": "obs_old",
                "kind": "challenge",
                "summary": "direct inspection supports the claim",
                "source": "fresh-challenger",
                "created_at": "2026-01-01T00:00:00Z",
                "challenge_verdict": "supports",
                "metadata": {
                    "assay_status": "valid",
                    "material_to_claim": True,
                    "direct_inspection": True,
                    "diagnostic": "preserved",
                },
            }
        ],
        "next_moves": [],
    }

    current = canonical_payload("move.applied", old, None)
    observation = current["observations"][0]

    assert observation["assay_status"] == "valid"
    assert observation["material_to_claim"] is True
    assert observation["direct_inspection"] is True
    assert observation["metadata"] == {"diagnostic": "preserved"}
    MoveApplied.model_validate(current)
