from __future__ import annotations

from frontier_harness.models import (
    SummitLineage,
    SummitLineageStatus,
    Uncertainty,
    UnlockContract,
    ValueBand,
)
from frontier_harness.summit import SummitArchive


def _lineage(
    lineage_id: str,
    *,
    mechanism: str,
    niche: str,
    status: SummitLineageStatus = SummitLineageStatus.SEED,
    quality: ValueBand = ValueBand.MEDIUM,
    potential: ValueBand = ValueBand.MEDIUM,
    residue: list[str] | None = None,
    unlock: UnlockContract | None = None,
) -> SummitLineage:
    return SummitLineage(
        lineage_id=lineage_id,
        name=lineage_id,
        thesis=f"Thesis for {lineage_id}",
        mechanism=mechanism,
        behavioral_descriptors=[niche],
        status=status,
        quality=quality,
        potential=potential,
        leverage=potential,
        robustness=ValueBand.MEDIUM,
        uncertainty=Uncertainty.MEDIUM,
        falsification_residue=residue or [],
        unlock_contract=unlock,
    )


def test_archive_replaces_near_duplicate_with_stronger_lineage_and_keeps_residue() -> None:
    archive = SummitArchive(max_lineages=4, max_active=3, max_per_niche=2)
    old = _lineage(
        "lin_old",
        mechanism="Causal mechanism A",
        niche="causal",
        quality=ValueBand.LOW,
        residue=["Old counterexample"],
    )
    stronger = _lineage(
        "lin_new",
        mechanism="Causal mechanism A",
        niche="causal",
        quality=ValueBand.HIGH,
        potential=ValueBand.HIGH,
        residue=["New boundary result"],
    )
    projected, decision = archive.admit({old.lineage_id: old}, [stronger])
    assert old.lineage_id not in projected
    assert stronger.lineage_id in projected
    assert decision.replaced == {old.lineage_id: stronger.lineage_id}
    assert projected[stronger.lineage_id].falsification_residue == [
        "Old counterexample",
        "New boundary result",
    ]


def test_protected_stepping_stone_survives_bounded_archive_pressure() -> None:
    archive = SummitArchive(max_lineages=2, max_active=2, max_per_niche=1)
    unlock = UnlockContract(
        potential_unlock="Expose a discontinuous improvement",
        blocking_dependency="One missing discriminator",
        next_probe="Run the discriminator",
        continuation_evidence="Distinct prediction survives",
        kill_condition="Prediction fails",
    )
    protected = _lineage(
        "lin_protected",
        mechanism="Representation shift",
        niche="alternate-representation",
        status=SummitLineageStatus.PROTECTED,
        quality=ValueBand.LOW,
        potential=ValueBand.HIGH,
        unlock=unlock,
    )
    incumbent = _lineage(
        "lin_incumbent",
        mechanism="Reliable conventional mechanism",
        niche="conventional",
        quality=ValueBand.HIGH,
        potential=ValueBand.MEDIUM,
    )
    excess = _lineage(
        "lin_excess",
        mechanism="Weak third mechanism",
        niche="third",
        quality=ValueBand.LOW,
        potential=ValueBand.LOW,
    )
    projected, decision = archive.admit({}, [protected, incumbent, excess])
    assert protected.lineage_id in projected
    assert incumbent.lineage_id in projected
    assert excess.lineage_id not in projected
    assert decision.rejected[excess.lineage_id] == "global archive capacity exceeded"


def test_development_batch_excludes_falsified_and_dormant_lineages() -> None:
    archive = SummitArchive(max_lineages=6, max_active=4, max_per_niche=2)
    active = _lineage(
        "lin_active",
        mechanism="Active mechanism",
        niche="active",
        status=SummitLineageStatus.ACTIVE,
        quality=ValueBand.HIGH,
    )
    falsified = _lineage(
        "lin_false",
        mechanism="False mechanism",
        niche="false",
        status=SummitLineageStatus.FALSIFIED,
    )
    dormant = _lineage(
        "lin_dormant",
        mechanism="Dormant mechanism",
        niche="dormant",
        status=SummitLineageStatus.DORMANT,
    )
    batch = archive.select_development_batch(
        {item.lineage_id: item for item in [active, falsified, dormant]},
        limit=3,
    )
    assert [item.lineage_id for item in batch] == [active.lineage_id]


def test_same_identity_update_preserves_history_and_accepts_falsification() -> None:
    archive = SummitArchive(max_lineages=4, max_active=3, max_per_niche=2)
    original = _lineage(
        "lin_same",
        mechanism="Mechanism under test",
        niche="causal",
        quality=ValueBand.HIGH,
        residue=["old boundary"],
    )
    original.development_history = ["first experiment"]
    update = original.model_copy(
        update={
            "status": SummitLineageStatus.FALSIFIED,
            "quality": ValueBand.LOW,
            "development_history": ["decisive falsifier"],
            "falsification_residue": ["new counterexample"],
        }
    )
    projected, decision = archive.admit({original.lineage_id: original}, [update])
    retained = projected[original.lineage_id]
    assert decision.accepted == [original.lineage_id]
    assert retained.status == SummitLineageStatus.FALSIFIED
    assert retained.quality == ValueBand.LOW
    assert retained.development_history == ["first experiment", "decisive falsifier"]
    assert retained.falsification_residue == ["old boundary", "new counterexample"]
