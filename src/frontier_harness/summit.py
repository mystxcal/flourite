"""Bounded Summit capability archive for upper-tail search.

The archive is intentionally subordinate to the one-task sparse controller. It
preserves developmentally useful lineages and falsification residue without
forcing a founder population or a fixed Summit phase sequence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .models import (
    SummitLineage,
    SummitLineageStatus,
    Uncertainty,
    ValueBand,
)
from .util import normalize_key, unique_preserving_order

_VALUE = {ValueBand.NONE: 0, ValueBand.LOW: 1, ValueBand.MEDIUM: 2, ValueBand.HIGH: 3}
_UNCERTAINTY = {Uncertainty.LOW: 0, Uncertainty.MEDIUM: 1, Uncertainty.HIGH: 2}


@dataclass(slots=True)
class ArchiveDecision:
    accepted: list[str] = field(default_factory=list)
    replaced: dict[str, str] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)
    demoted: list[str] = field(default_factory=list)


def _niche(lineage: SummitLineage) -> str:
    if lineage.behavioral_descriptors:
        return normalize_key(lineage.behavioral_descriptors[0]) or "general"
    return normalize_key(lineage.mechanism.split(".", 1)[0]) or "general"


def _duplicate_key(lineage: SummitLineage) -> tuple[str, tuple[str, ...]]:
    return (
        normalize_key(lineage.mechanism),
        tuple(sorted(normalize_key(item) for item in lineage.assumptions)),
    )


def _rank(lineage: SummitLineage) -> tuple[int, int, int, int, int, int, int, int, str]:
    protected = int(
        lineage.status == SummitLineageStatus.PROTECTED
        and lineage.unlock_contract is not None
        and (
            lineage.unlock_contract.probes_used < lineage.unlock_contract.probe_allowance
            or lineage.unlock_contract.development_steps_used
            < lineage.unlock_contract.development_allowance
        )
    )
    return (
        -int(lineage.status == SummitLineageStatus.ELITE),
        -protected,
        -_VALUE[lineage.quality],
        -_VALUE[lineage.potential],
        -_VALUE[lineage.leverage],
        -_VALUE[lineage.robustness],
        -_VALUE[lineage.novelty],
        _UNCERTAINTY[lineage.uncertainty],
        lineage.lineage_id,
    )


def _dominates(left: SummitLineage, right: SummitLineage) -> bool:
    weakly = (
        _VALUE[left.quality] >= _VALUE[right.quality]
        and _VALUE[left.potential] >= _VALUE[right.potential]
        and _VALUE[left.leverage] >= _VALUE[right.leverage]
        and _VALUE[left.robustness] >= _VALUE[right.robustness]
        and _UNCERTAINTY[left.uncertainty] <= _UNCERTAINTY[right.uncertainty]
    )
    strict = (
        _VALUE[left.quality] > _VALUE[right.quality]
        or _VALUE[left.potential] > _VALUE[right.potential]
        or _VALUE[left.leverage] > _VALUE[right.leverage]
        or _VALUE[left.robustness] > _VALUE[right.robustness]
        or _UNCERTAINTY[left.uncertainty] < _UNCERTAINTY[right.uncertainty]
    )
    return weakly and strict


class SummitArchive:
    def __init__(
        self,
        *,
        max_lineages: int,
        max_active: int,
        max_per_niche: int,
        preserve_falsification_residue: bool = True,
    ) -> None:
        self.max_lineages = max_lineages
        self.max_active = max_active
        self.max_per_niche = max_per_niche
        self.preserve_falsification_residue = preserve_falsification_residue

    def admit(
        self,
        existing: dict[str, SummitLineage],
        candidates: Sequence[SummitLineage],
    ) -> tuple[dict[str, SummitLineage], ArchiveDecision]:
        projected = {key: value.model_copy(deep=True) for key, value in existing.items()}
        decision = ArchiveDecision()
        duplicate_index = {_duplicate_key(item): key for key, item in projected.items()}

        for raw in candidates:
            item = raw.model_copy(deep=True)
            duplicate_id = duplicate_index.get(_duplicate_key(item))
            if duplicate_id:
                incumbent = projected[duplicate_id]
                if item.lineage_id == incumbent.lineage_id:
                    # A same-identity return is a state transition, not a new
                    # candidate.  Preserve accumulated evidence/history while
                    # accepting deliberate downgrades and falsifications.
                    item.evidence_for = unique_preserving_order(
                        [*incumbent.evidence_for, *item.evidence_for]
                    )
                    item.evidence_against = unique_preserving_order(
                        [*incumbent.evidence_against, *item.evidence_against]
                    )
                    item.development_history = unique_preserving_order(
                        [*incumbent.development_history, *item.development_history]
                    )
                    item.falsification_residue = unique_preserving_order(
                        [*incumbent.falsification_residue, *item.falsification_residue]
                    )
                    item.parent_lineage_ids = unique_preserving_order(
                        [*incumbent.parent_lineage_ids, *item.parent_lineage_ids]
                    )
                    item.behavioral_descriptors = unique_preserving_order(
                        [*incumbent.behavioral_descriptors, *item.behavioral_descriptors]
                    )
                    projected[duplicate_id] = item
                    decision.accepted.append(item.lineage_id)
                elif _rank(item) < _rank(incumbent):
                    if self.preserve_falsification_residue:
                        item.falsification_residue = unique_preserving_order(
                            [*incumbent.falsification_residue, *item.falsification_residue]
                        )
                    projected.pop(duplicate_id)
                    projected[item.lineage_id] = item
                    duplicate_index[_duplicate_key(item)] = item.lineage_id
                    decision.replaced[duplicate_id] = item.lineage_id
                else:
                    incumbent.evidence_for = unique_preserving_order(
                        [*incumbent.evidence_for, *item.evidence_for]
                    )
                    incumbent.evidence_against = unique_preserving_order(
                        [*incumbent.evidence_against, *item.evidence_against]
                    )
                    incumbent.falsification_residue = unique_preserving_order(
                        [*incumbent.falsification_residue, *item.falsification_residue]
                    )
                    decision.rejected[item.lineage_id] = f"near-duplicate of {duplicate_id}"
                continue
            projected[item.lineage_id] = item
            duplicate_index[_duplicate_key(item)] = item.lineage_id
            decision.accepted.append(item.lineage_id)

        # Enforce per-niche capacity, but never discard residue. Falsified items
        # may remain dormant if they carry reusable residue.
        by_niche: dict[str, list[SummitLineage]] = {}
        for item in projected.values():
            by_niche.setdefault(_niche(item), []).append(item)
        for niche_items in by_niche.values():
            ordered = sorted(niche_items, key=_rank)
            for item in ordered[self.max_per_niche :]:
                if self.preserve_falsification_residue and item.falsification_residue:
                    item.status = SummitLineageStatus.DORMANT
                    decision.demoted.append(item.lineage_id)
                else:
                    projected.pop(item.lineage_id, None)
                    decision.rejected[item.lineage_id] = "niche capacity exceeded"

        if len(projected) > self.max_lineages:
            # Preserve one strong representative per distinct mechanism niche
            # before spending remaining capacity on near-neighbour refinement.
            ordered_all = sorted(projected.values(), key=_rank)
            niche_first: list[SummitLineage] = []
            seen_niches: set[str] = set()
            for item in ordered_all:
                niche = _niche(item)
                if niche in seen_niches:
                    continue
                niche_first.append(item)
                seen_niches.add(niche)
            selected_ids = {item.lineage_id for item in niche_first[: self.max_lineages]}
            for item in ordered_all:
                if len(selected_ids) >= self.max_lineages:
                    break
                selected_ids.add(item.lineage_id)
            ordered = [
                *[item for item in ordered_all if item.lineage_id in selected_ids],
                *[item for item in ordered_all if item.lineage_id not in selected_ids],
            ]
            for item in ordered[self.max_lineages :]:
                if self.preserve_falsification_residue and item.falsification_residue:
                    item.status = SummitLineageStatus.DORMANT
                    decision.demoted.append(item.lineage_id)
                else:
                    projected.pop(item.lineage_id, None)
                    decision.rejected[item.lineage_id] = "global archive capacity exceeded"

        active = [
            item
            for item in projected.values()
            if item.status
            in {
                SummitLineageStatus.SEED,
                SummitLineageStatus.ACTIVE,
                SummitLineageStatus.PROTECTED,
                SummitLineageStatus.ELITE,
            }
        ]
        for item in sorted(active, key=_rank)[self.max_active :]:
            item.status = SummitLineageStatus.DORMANT
            decision.demoted.append(item.lineage_id)
        return projected, decision

    def select_development_batch(
        self,
        archive: dict[str, SummitLineage],
        *,
        limit: int,
    ) -> list[SummitLineage]:
        viable: list[SummitLineage] = []
        for item in archive.values():
            if item.status in {
                SummitLineageStatus.FALSIFIED,
                SummitLineageStatus.MERGED,
                SummitLineageStatus.DORMANT,
            }:
                continue
            if item.status == SummitLineageStatus.PROTECTED and item.unlock_contract:
                contract = item.unlock_contract
                exhausted = (
                    contract.probes_used >= contract.probe_allowance
                    and contract.development_steps_used >= contract.development_allowance
                )
                if exhausted:
                    continue
            viable.append(item)
        # One top quality lineage, then mechanismally distinct high-potential
        # lineages. This keeps the batch sparse while preserving upper-tail reach.
        selected: list[SummitLineage] = []
        seen_niches: set[str] = set()
        for item in sorted(viable, key=_rank):
            niche = _niche(item)
            if niche in seen_niches and len(selected) + 1 < limit:
                continue
            selected.append(item)
            seen_niches.add(niche)
            if len(selected) >= limit:
                break
        return selected
