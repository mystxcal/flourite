"""Conservative v3.5 cognitive control primitives.

This module is deliberately deterministic.  Models propose semantic objects;
these helpers decide whether they are admissible, resolve local keys, reopen
invalidated dependants, enforce small active sets, and validate continuity and
release coverage.  It does not replace the sparse frontier or Summit search.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from .ids import new_id
from .models import (
    ActionContract,
    ActionOutcome,
    ActionProposal,
    ActionReceipt,
    ArtifactRef,
    ArtifactSpine,
    CeilingSensitivityScan,
    CharterAssertion,
    CharterProvenance,
    CompletionCase,
    Crux,
    CruxDraft,
    CruxStatus,
    CruxUpdate,
    EliminatedDirection,
    EvidenceModality,
    FrontierKernel,
    GoalContract,
    Impact,
    IndependenceClass,
    InvariantRevision,
    LeadContinuityAck,
    LeadContinuityStatus,
    Obligation,
    ObligationDraft,
    ObligationStatus,
    ObligationUpdate,
    ObservedActionCost,
    OverlayStatus,
    ReframeWitness,
    RequirementTrace,
    RunState,
    SpeculativeOverlay,
    SubstrateEntry,
    TaskCharter,
    TaskSource,
    Usage,
)
from .providers.base import ProviderTraceSummary
from .util import normalize_key, sha256_text, unique_preserving_order, utc_now


@dataclass(slots=True)
class AdmissionNotes:
    accepted_ids: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FrontierKernelNotes:
    advanced: bool = False
    reasons: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def capture_task_source(text: str) -> TaskSource:
    normalized = text.strip()
    if not normalized:
        raise ValueError("task source cannot be empty")
    return TaskSource(
        original_text=normalized,
        digest=sha256_text(normalized),
        created_at=utc_now(),
    )


def fallback_charter(source: TaskSource, contract: GoalContract) -> TaskCharter:
    assertions = [
        CharterAssertion(
            key="deliverable",
            statement=contract.deliverable,
            provenance=CharterProvenance.EXPLICIT,
            rationale="Derived from the baseline goal contract and subordinate to the immutable task source.",
        )
    ]
    assertions.extend(
        CharterAssertion(
            key=f"hard_constraint_{index + 1}",
            statement=value,
            provenance=CharterProvenance.EXPLICIT,
        )
        for index, value in enumerate(contract.hard_constraints)
    )
    assertions.extend(
        CharterAssertion(
            key=f"assumption_{index + 1}",
            statement=value,
            provenance=CharterProvenance.TENTATIVE,
        )
        for index, value in enumerate(contract.assumptions)
    )
    return TaskCharter(
        source_digest=source.digest,
        deliverable=contract.deliverable,
        assertions=assertions,
        hard_constraints=list(contract.hard_constraints),
        soft_objectives=list(contract.soft_objectives),
        unacceptable_failures=list(contract.exclusions),
        requirement_traces=compile_requirement_traces(source.original_text),
    )


_REQUIREMENT_SIGNAL = re.compile(
    r"\b(must|need(?:s)?\s+to|required|requirement|do\s+not|don't|never|cannot|can't|"
    r"should|unacceptable|not\s+a\s+request|final\s+artifact)\b",
    re.IGNORECASE,
)
_PROHIBITION_SIGNAL = re.compile(
    r"\b(do\s+not|don't|never|cannot|can't|unacceptable|must\s+not)\b", re.IGNORECASE
)


def _requirement_modalities(text: str) -> list[EvidenceModality]:
    lowered = text.lower()
    modalities: list[EvidenceModality] = []
    if any(word in lowered for word in ("video", "animation", "motion", "temporal", "watch")):
        modalities.append(EvidenceModality.TEMPORAL_VISUAL)
    if any(word in lowered for word in ("audio", "voice", "sound", "music")):
        modalities.append(EvidenceModality.AUDIO)
    if any(word in lowered for word in ("visual", "image", "layout", "frame", "design")):
        modalities.append(EvidenceModality.STATIC_VISUAL)
    if any(word in lowered for word in ("interactive", "click", "steer", "live ui")):
        modalities.append(EvidenceModality.INTERACTIVE)
    if any(word in lowered for word in ("test", "verify", "check", "benchmark")):
        modalities.append(EvidenceModality.DETERMINISTIC_TEST)
    return unique_preserving_order(modalities)


def compile_requirement_traces(text: str) -> list[RequirementTrace]:
    """Extract exact high-signal clauses without mistaking wrapped Markdown for clauses.

    Task prompts are often formatted documents. Splitting at every newline
    turned headings and 80-column continuations into release requirements. We
    first recover Markdown paragraphs/list items, then split complete sentences.
    """

    blocks: list[str] = []
    current: list[str] = []
    fenced = False

    def flush() -> None:
        if current:
            blocks.append(" ".join(current))
            current.clear()

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        if not stripped:
            flush()
            continue
        if stripped.startswith("#"):
            flush()
            continue
        list_match = re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)(.*)$", stripped)
        if list_match:
            flush()
            current.append(list_match.group(1).strip())
            continue
        current.append(stripped)
    flush()

    pieces: list[str] = []
    for block in blocks:
        pieces.extend(re.split(r"(?<=[.!?])\s+(?=[A-Z0-9`])", block))
    traces: list[RequirementTrace] = []
    seen: set[str] = set()
    for raw in pieces:
        source_text = re.sub(r"\s+", " ", raw).strip(" -*\t")
        if (
            len(source_text) < 8
            or source_text.endswith(":")
            or not _REQUIREMENT_SIGNAL.search(source_text)
        ):
            continue
        normalized = normalize_key(source_text)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        prohibited = bool(_PROHIBITION_SIGNAL.search(source_text))
        merely_preferred = bool(re.search(r"\bshould\b", source_text, re.I)) and not bool(
            re.search(
                r"\b(must|required|need(?:s)?\s+to|cannot|can't|do\s+not|don't|never)\b",
                source_text,
                re.I,
            )
        )
        category: Literal["requirement", "prohibition", "preference", "hypothesis", "process"] = (
            "prohibition" if prohibited else ("preference" if merely_preferred else "requirement")
        )
        digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:16]
        traces.append(
            RequirementTrace(
                requirement_id=f"req_{digest}",
                source_text=source_text,
                category=category,
                release_blocking=not merely_preferred,
                evidence_modalities=_requirement_modalities(source_text),
            )
        )
    return traces


def _semantic_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[\w]+", normalize_key(text))
        if len(token) >= 4
        and token
        not in {
            "that",
            "this",
            "with",
            "from",
            "must",
            "should",
            "final",
            "artifact",
            "requirement",
        }
    }


def _same_requirement(left: str, right: str) -> bool:
    """Conservative deterministic overlap used only to suppress guard duplicates."""

    left_key = normalize_key(left)
    right_key = normalize_key(right)
    if not left_key or not right_key:
        return False
    if min(len(left_key), len(right_key)) >= 24 and (
        left_key in right_key or right_key in left_key
    ):
        return True
    left_tokens = _semantic_tokens(left)
    right_tokens = _semantic_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    return overlap / min(len(left_tokens), len(right_tokens)) >= 0.82


def _same_frontier_statement(left: str, right: str) -> bool:
    """Suppress obvious semantic churn without pretending to be an oracle."""

    if _same_requirement(left, right):
        return True
    left_tokens = _semantic_tokens(left)
    right_tokens = _semantic_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    return overlap / min(len(left_tokens), len(right_tokens)) >= 0.75


def _append_semantic_unique(items: list[str], candidate: str) -> bool:
    candidate = " ".join(candidate.split()).strip()
    if not candidate or any(_same_frontier_statement(candidate, item) for item in items):
        return False
    items.append(candidate)
    return True


def _same_semantic_set(left: Sequence[str], right: Sequence[str]) -> bool:
    clean_left = [" ".join(item.split()).strip() for item in left if item.strip()]
    clean_right = [" ".join(item.split()).strip() for item in right if item.strip()]
    if len(clean_left) != len(clean_right):
        return False
    return all(
        any(_same_frontier_statement(item, candidate) for candidate in clean_right)
        for item in clean_left
    )


def reconcile_frontier_kernel(
    current: FrontierKernel | None,
    proposed: FrontierKernel | None,
    *,
    cruxes: Sequence[Crux],
    spine: ArtifactSpine | None,
    next_actions: Sequence[ActionProposal],
    round_index: int,
    eligible_action_ids: Sequence[str] = (),
) -> tuple[FrontierKernel, FrontierKernelNotes]:
    """Reconcile the solver/keeper handoff without rewarding paraphrase churn.

    The immutable ledger remains the lossless record.  This kernel is only the
    compact navigational state.  Invariants and killed search families are
    monotone here so a fresh keeper cannot erase a hard-won lesson by omission;
    a genuinely reopened direction remains expressible as a new live family
    whose novelty basis names the reopening evidence.
    """

    notes = FrontierKernelNotes()
    active = [item for item in cruxes if item.status == CruxStatus.ACTIVE]
    if current is None:
        seed = proposed.model_copy(deep=True) if proposed is not None else FrontierKernel()
        if not seed.bottleneck and active:
            seed.bottleneck = active[0].uncertainty or active[0].title
        if not seed.invariants and spine is not None:
            seed.invariants = list(spine.hard_invariants)
        if not seed.live_hypotheses and active:
            seed.live_hypotheses = unique_preserving_order(
                possibility
                for crux in active
                for possibility in crux.competing_possibilities
                if possibility.strip()
            )
        if not seed.next_move and next_actions:
            seed.next_move = next_actions[0].assignment
        has_content = bool(
            seed.bottleneck
            or seed.invariants
            or seed.live_hypotheses
            or seed.eliminated_directions
            or seed.next_move
        )
        seed.revision = 1 if has_content else 0
        seed.last_advance_round = round_index if has_content else 0
        seed.stagnant_rounds = 0
        seed.source_action_ids = []
        seed.invariant_revisions = []
        notes.advanced = has_content
        if has_content:
            notes.reasons.append("frontier kernel established")
        return seed, notes

    candidate = proposed.model_copy(deep=True) if proposed is not None else current.model_copy(deep=True)
    merged_invariants = list(current.invariants)
    merged_revisions = [item.model_copy(deep=True) for item in current.invariant_revisions]
    protected_invariants = list(spine.hard_invariants) if spine is not None else []
    for revision in candidate.invariant_revisions:
        statement = " ".join(revision.statement.split()).strip()
        failure = " ".join(revision.failure_mechanism.split()).strip()
        replacement = " ".join(revision.replacement.split()).strip()
        if not statement or not failure:
            notes.rejected.append("invariant revision lacked a statement or failure mechanism")
            continue
        if any(
            _same_frontier_statement(statement, item.statement) for item in merged_revisions
        ):
            continue
        incumbent = next(
            (
                item
                for item in merged_invariants
                if _same_frontier_statement(statement, item)
            ),
            None,
        )
        if incumbent is None:
            notes.rejected.append("invariant revision did not match an active invariant")
            continue
        if any(
            _same_frontier_statement(incumbent, protected)
            for protected in protected_invariants
        ):
            notes.rejected.append(
                "kernel cannot retire an Artifact Spine hard invariant; revise the spine first"
            )
            continue
        merged_invariants.remove(incumbent)
        merged_revisions.append(
            InvariantRevision(
                statement=incumbent,
                failure_mechanism=failure,
                replacement=replacement,
            )
        )
        if replacement:
            _append_semantic_unique(merged_invariants, replacement)
        notes.reasons.append("working invariant revised with a causal failure")

    retired_invariants = [item.statement for item in merged_revisions]
    for invariant in candidate.invariants:
        if any(
            _same_frontier_statement(invariant, retired) for retired in retired_invariants
        ):
            continue
        if _append_semantic_unique(merged_invariants, invariant):
            notes.reasons.append("new invariant")

    merged_eliminations = [item.model_copy(deep=True) for item in current.eliminated_directions]
    for elimination in candidate.eliminated_directions:
        family = " ".join(elimination.family.split()).strip()
        mechanism = " ".join(elimination.failure_mechanism.split()).strip()
        if not family or not mechanism:
            notes.rejected.append("eliminated direction lacked a family or failure mechanism")
            continue
        if any(_same_frontier_statement(family, item.family) for item in merged_eliminations):
            continue
        merged_eliminations.append(
            EliminatedDirection(
                family=family,
                failure_mechanism=mechanism,
                reopen_if=" ".join(elimination.reopen_if.split()).strip(),
            )
        )
        notes.reasons.append("search family eliminated with a reusable cause")

    bottleneck = " ".join((candidate.bottleneck or current.bottleneck).split()).strip()
    live = unique_preserving_order(
        " ".join(item.split()).strip() for item in candidate.live_hypotheses if item.strip()
    )
    if not live:
        live = list(current.live_hypotheses)
    reopened_families = [
        action.hypothesis_family
        for action in next_actions
        if action.hypothesis_family.strip() and action.novelty_basis.strip()
    ]
    live = [
        hypothesis
        for hypothesis in live
        if not any(
            _same_frontier_statement(hypothesis, elimination.family)
            and not any(
                _same_frontier_statement(elimination.family, reopened)
                for reopened in reopened_families
            )
            for elimination in merged_eliminations
        )
    ]
    next_move = " ".join((candidate.next_move or current.next_move).split()).strip()
    if not next_move and next_actions:
        next_move = next_actions[0].assignment

    if (
        bottleneck
        and current.bottleneck
        and not _same_frontier_statement(bottleneck, current.bottleneck)
    ):
        notes.reasons.append("controlling bottleneck changed")
    if not _same_semantic_set(current.live_hypotheses, live):
        notes.reasons.append("live hypothesis frontier changed")

    eligible = set(eligible_action_ids)
    requested_sources = candidate.source_action_ids if proposed is not None else []
    sources = [item for item in requested_sources if item in eligible]
    invalid_sources = [item for item in requested_sources if item not in eligible]
    if invalid_sources:
        notes.rejected.append(
            "frontier sources were not completed in this checkpoint: "
            + ", ".join(invalid_sources)
        )
    advanced = bool(notes.reasons)
    if advanced and eligible and not sources:
        notes.rejected.append(
            "semantic frontier update lacked a completed source action; prior kernel preserved"
        )
        preserved = current.model_copy(deep=True)
        preserved.source_action_ids = []
        preserved.stagnant_rounds += 1
        notes.advanced = False
        notes.reasons.clear()
        return preserved, notes
    updated = FrontierKernel(
        bottleneck=bottleneck,
        invariants=merged_invariants,
        invariant_revisions=merged_revisions,
        live_hypotheses=live,
        eliminated_directions=merged_eliminations,
        next_move=next_move,
        source_action_ids=sources if advanced else [],
        revision=current.revision + int(advanced),
        last_advance_round=round_index if advanced else current.last_advance_round,
        stagnant_rounds=0 if advanced else current.stagnant_rounds + 1,
    )
    notes.advanced = advanced
    return updated, notes


def _requirement_recall(needle: str, haystack: str) -> float:
    needle_tokens = _semantic_tokens(needle)
    if not needle_tokens:
        return 0.0
    return len(needle_tokens & _semantic_tokens(haystack)) / len(needle_tokens)


def compile_guard_obligations(
    source: TaskSource,
    contract: GoalContract,
    charter: TaskCharter,
    *,
    existing_drafts: Sequence[ObligationDraft] = (),
) -> tuple[TaskCharter, list[ObligationDraft]]:
    """Compile a *small, lossless* runtime guard around model obligations.

    Exact source clauses stay in the charter. They do not each become a second
    obligation when the Lead already mapped them into a coherent obligation.
    Any genuinely uncovered clauses are bundled by kind, preserving every trace
    ID without flooding the controller with compiler debris.
    """

    traces = list(charter.requirement_traces)
    known_trace_text = [item.source_text for item in traces]
    for trace in compile_requirement_traces(source.original_text):
        if not any(_same_requirement(trace.source_text, known) for known in known_trace_text):
            traces.append(trace)
            known_trace_text.append(trace.source_text)
    charter = charter.model_copy(update={"requirement_traces": traces})

    trace_by_id = {item.requirement_id: item for item in traces}
    covered_trace_ids = {
        requirement_id
        for draft in existing_drafts
        for requirement_id in draft.source_requirement_ids
    }
    for trace in traces:
        if not trace.release_blocking or trace.requirement_id in covered_trace_ids:
            continue
        best_draft: ObligationDraft | None = None
        best_score = 0.0
        for draft in existing_drafts:
            linked = " ".join(
                trace_by_id[requirement_id].source_text
                for requirement_id in draft.source_requirement_ids
                if requirement_id in trace_by_id
            )
            candidate_text = " ".join(
                [draft.title, draft.requirement, draft.acceptance, linked]
            )
            score = _requirement_recall(trace.source_text, candidate_text)
            if score > best_score:
                best_score = score
                best_draft = draft
        # A fallback assignment changes no semantics; it only links an exact
        # source clause to an already coherent obligation whose language covers
        # most of that clause. Ambiguous residue remains a runtime guard below.
        if best_draft is not None and best_score >= 0.58:
            best_draft.source_requirement_ids = unique_preserving_order(
                [*best_draft.source_requirement_ids, trace.requirement_id]
            )
            best_draft.required_evidence_modalities = unique_preserving_order(
                [*best_draft.required_evidence_modalities, *trace.evidence_modalities]
            )
            covered_trace_ids.add(trace.requirement_id)

    drafts: list[ObligationDraft] = []
    if not any(item.kind == "deliverable" for item in existing_drafts):
        drafts.append(
            ObligationDraft(
                local_key="deliverable",
                title="Deliver the requested artifact",
                requirement=contract.deliverable,
                kind="deliverable",
                acceptance="The final artifact directly fulfills the immutable Task Source.",
                impact=Impact.FATAL,
                release_blocking=True,
                required_artifact_scope="release",
                tags=["runtime-guard"],
            )
        )

    covered_trace_ids = {
        requirement_id
        for draft in [*existing_drafts, *drafts]
        for requirement_id in draft.source_requirement_ids
    }
    uncovered = [
        trace
        for trace in traces
        if trace.release_blocking and trace.requirement_id not in covered_trace_ids
    ]
    grouped: dict[str, list[RequirementTrace]] = defaultdict(list)
    for trace in uncovered:
        grouped[trace.category].append(trace)
    for category, items in grouped.items():
        ids = [item.requirement_id for item in items]
        modalities = unique_preserving_order(
            modality for item in items for modality in item.evidence_modalities
        )
        drafts.append(
            ObligationDraft(
                local_key=f"guard_unmapped_{category}",
                title=f"Preserve uncovered {category} traces",
                requirement=(
                    "Satisfy every exact Task Charter trace in this bundle: " + ", ".join(ids)
                ),
                kind="constraint" if category == "prohibition" else "construction",
                acceptance=(
                    "The Completion Case maps each bundled trace to an artifact location and "
                    "appropriately scoped evidence."
                ),
                impact=Impact.FATAL,
                release_blocking=True,
                source_requirement_ids=ids,
                required_evidence_modalities=modalities,
                required_artifact_scope="whole_artifact",
                tags=["runtime-guard", "trace-bundle"],
            )
        )
    return charter, drafts


def fallback_spine(contract: GoalContract, summary: str) -> ArtifactSpine:
    thesis = summary.strip() or contract.deliverable
    return ArtifactSpine(
        central_thesis=thesis,
        architecture=["One integrated artifact answering the immutable task."],
        key_decisions=[],
        hard_invariants=list(contract.hard_constraints),
        must_preserve=[contract.deliverable],
        tradeoffs=[],
        residual_uncertainty=list(contract.assumptions),
    )


def _local_key_map(items: Iterable[Obligation | Crux], *, prefix: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in items:
        item_id = item.obligation_id if isinstance(item, Obligation) else item.crux_id
        mapping[item_id] = item_id
        for tag in item.tags:
            marker = f"{prefix}:"
            if tag.startswith(marker):
                mapping[tag.removeprefix(marker)] = item_id
    return mapping


def instantiate_obligations(
    drafts: Sequence[ObligationDraft],
    *,
    existing: Iterable[Obligation] = (),
    capacity: int = 24,
    created_seq: int = 0,
    charter: TaskCharter | None = None,
    human_evidence_available: bool = False,
) -> tuple[list[Obligation], dict[str, str], AdmissionNotes]:
    existing_list = list(existing)
    keymap = _local_key_map(existing_list, prefix="local-key")
    notes = AdmissionNotes()
    created_pairs: list[tuple[ObligationDraft, Obligation]] = []
    room = max(0, capacity - len(existing_list))
    ordered = sorted(
        enumerate(drafts),
        key=lambda item: (
            0 if item[1].release_blocking else 1,
            {Impact.FATAL: 0, Impact.HIGH: 1, Impact.MEDIUM: 2, Impact.LOW: 3}[item[1].impact],
            item[0],
        ),
    )
    for _, draft in ordered[:room]:
        key = normalize_key(draft.local_key) or normalize_key(draft.title)
        if key in keymap:
            notes.rejected.append(f"duplicate obligation local key: {draft.local_key}")
            continue
        obligation_id = new_id("obl")
        keymap[draft.local_key] = obligation_id
        keymap[key] = obligation_id
        keymap[obligation_id] = obligation_id
        required_modalities = list(draft.required_evidence_modalities)
        if (
            EvidenceModality.HUMAN_OBSERVATION in required_modalities
            and not human_evidence_available
        ):
            linked_text = " ".join(
                trace.source_text
                for trace in (charter.requirement_traces if charter else [])
                if trace.requirement_id in draft.source_requirement_ids
            )
            explicit_human = bool(
                re.search(r"\b(human|person|people|user|operator|panel|manual review)\b", linked_text, re.I)
            )
            if not explicit_human:
                required_modalities.remove(EvidenceModality.HUMAN_OBSERVATION)
                notes.warnings.append(
                    f"removed model-invented unavailable human evidence requirement: {draft.title}"
                )
        required_scope = draft.required_artifact_scope
        if draft.kind == "deliverable":
            required_scope = "release"
        elif draft.kind in {"verification", "coherence"} and required_scope == "targeted":
            required_scope = "whole_artifact"
        obligation = Obligation(
            obligation_id=obligation_id,
            title=draft.title,
            requirement=draft.requirement,
            kind=draft.kind,
            acceptance=draft.acceptance,
            impact=draft.impact,
            assumptions=list(draft.assumption_keys),
            release_blocking=draft.release_blocking,
            artifact_location=draft.artifact_location_hint,
            source_requirement_ids=list(draft.source_requirement_ids),
            required_evidence_modalities=required_modalities,
            required_artifact_scope=required_scope,
            tags=unique_preserving_order([f"local-key:{key}", *draft.tags]),
            created_seq=created_seq,
            updated_seq=created_seq,
        )
        created_pairs.append((draft, obligation))
        notes.accepted_ids.append(obligation_id)

    for draft, obligation in created_pairs:
        resolved: list[str] = []
        for raw in draft.depends_on_keys:
            target = keymap.get(raw) or keymap.get(normalize_key(raw))
            if target and target != obligation.obligation_id:
                resolved.append(target)
        obligation.depends_on = unique_preserving_order(resolved)
    if len(ordered) > room:
        notes.rejected.extend(
            f"obligation capacity exceeded: {draft.title}" for _, draft in ordered[room:]
        )
    return [item for _, item in created_pairs], keymap, notes


def instantiate_cruxes(
    drafts: Sequence[CruxDraft],
    *,
    obligations: Iterable[Obligation],
    existing: Iterable[Crux] = (),
    active_limit: int = 3,
    total_limit: int = 12,
    created_seq: int = 0,
) -> tuple[list[Crux], dict[str, str], AdmissionNotes]:
    existing_list = list(existing)
    obligation_map = _local_key_map(obligations, prefix="local-key")
    crux_map = _local_key_map(existing_list, prefix="local-key")
    notes = AdmissionNotes()
    room = max(0, total_limit - len(existing_list))
    active_existing = sum(item.status == CruxStatus.ACTIVE for item in existing_list)
    active_room = max(0, active_limit - active_existing)
    ordered = sorted(
        enumerate(drafts),
        key=lambda item: (
            {Impact.FATAL: 0, Impact.HIGH: 1, Impact.MEDIUM: 2, Impact.LOW: 3}[
                item[1].unlock_value
            ],
            item[0],
        ),
    )
    created: list[Crux] = []
    for index, (_, draft) in enumerate(ordered[:room]):
        key = normalize_key(draft.local_key) or normalize_key(draft.title)
        if key in crux_map:
            notes.rejected.append(f"duplicate crux local key: {draft.local_key}")
            continue
        crux_id = new_id("crx")
        crux_map[draft.local_key] = crux_id
        crux_map[key] = crux_id
        crux_map[crux_id] = crux_id
        obligation_ids = []
        for raw in draft.obligation_keys:
            target = obligation_map.get(raw) or obligation_map.get(normalize_key(raw))
            if target:
                obligation_ids.append(target)
        status = CruxStatus.ACTIVE if index < active_room else CruxStatus.DORMANT
        item = Crux(
            crux_id=crux_id,
            title=draft.title,
            uncertainty=draft.uncertainty,
            decision_controlled=draft.decision_controlled,
            competing_possibilities=list(draft.competing_possibilities),
            why_it_matters=draft.why_it_matters,
            obligation_ids=unique_preserving_order(obligation_ids),
            discriminating_evidence=list(draft.discriminating_evidence),
            unlock_value=draft.unlock_value,
            status=status,
            tags=[*draft.tags, f"local-key:{key}"],
            created_seq=created_seq,
            updated_seq=created_seq,
        )
        created.append(item)
        notes.accepted_ids.append(crux_id)
    if len(ordered) > room:
        notes.rejected.extend(
            f"crux capacity exceeded: {draft.title}" for _, draft in ordered[room:]
        )
    return created, crux_map, notes


def apply_obligation_updates(
    obligations: dict[str, Obligation],
    updates: Sequence[ObligationUpdate],
    *,
    updated_seq: int,
) -> tuple[dict[str, Obligation], list[str]]:
    projected = {key: value.model_copy(deep=True) for key, value in obligations.items()}
    notes: list[str] = []
    invalidated_roots: set[str] = set()
    for update in updates:
        item = projected.get(update.obligation_id)
        if item is None:
            notes.append(f"unknown obligation update: {update.obligation_id}")
            continue
        if update.status is not None:
            item.status = update.status
            if update.status == ObligationStatus.INVALIDATED or update.invalidate_dependents:
                invalidated_roots.add(item.obligation_id)
        if update.acceptance is not None:
            item.acceptance = update.acceptance
        if update.artifact_location is not None:
            item.artifact_location = update.artifact_location
        if update.residual_uncertainty is not None:
            item.residual_uncertainty = update.residual_uncertainty
        if update.reopen_condition is not None:
            item.reopen_condition = update.reopen_condition
        if update.resolution is not None:
            item.resolution = update.resolution
        if update.required_artifact_scope is not None:
            item.required_artifact_scope = update.required_artifact_scope
        item.evidence_references = unique_preserving_order(
            [*item.evidence_references, *update.evidence_references]
        )
        item.updated_seq = updated_seq

    if invalidated_roots:
        dependants: dict[str, list[str]] = defaultdict(list)
        for item in projected.values():
            for dependency in item.depends_on:
                dependants[dependency].append(item.obligation_id)
        queue: deque[str] = deque(invalidated_roots)
        seen = set(invalidated_roots)
        while queue:
            root = queue.popleft()
            for dependant_id in dependants.get(root, []):
                if dependant_id in seen:
                    continue
                seen.add(dependant_id)
                dependant = projected[dependant_id]
                dependant.status = ObligationStatus.OPEN
                dependant.resolution = None
                dependant.residual_uncertainty = (
                    f"Reopened because dependency {root} was invalidated."
                )
                dependant.updated_seq = updated_seq
                queue.append(dependant_id)
                notes.append(f"reopened dependent obligation: {dependant_id}")
    return projected, notes


def apply_crux_updates(
    cruxes: dict[str, Crux],
    updates: Sequence[CruxUpdate],
    *,
    updated_seq: int,
    active_limit: int,
) -> tuple[dict[str, Crux], list[str]]:
    projected = {key: value.model_copy(deep=True) for key, value in cruxes.items()}
    notes: list[str] = []
    for update in updates:
        item = projected.get(update.crux_id)
        if item is None:
            notes.append(f"unknown crux update: {update.crux_id}")
            continue
        if update.status is not None:
            item.status = update.status
        if update.uncertainty is not None:
            item.uncertainty = update.uncertainty
        if update.resolution is not None:
            item.resolution = update.resolution
        item.evidence_references = unique_preserving_order(
            [*item.evidence_references, *update.evidence_references]
        )
        item.updated_seq = updated_seq

    active = [item for item in projected.values() if item.status == CruxStatus.ACTIVE]
    if len(active) > active_limit:
        ordered = sorted(
            active,
            key=lambda item: (
                -{Impact.FATAL: 4, Impact.HIGH: 3, Impact.MEDIUM: 2, Impact.LOW: 1}[
                    item.unlock_value
                ],
                item.created_seq,
            ),
        )
        for item in ordered[active_limit:]:
            item.status = CruxStatus.DORMANT
            notes.append(f"dormant due to active crux limit: {item.crux_id}")
    return projected, notes


def charter_change_requires_witness(
    current: TaskCharter | None, proposed: TaskCharter | None
) -> bool:
    """Return whether a charter revision changes the task's destination.

    Clarifying provenance, evidence requirements, audience detail, or unresolved
    questions is ordinary task-model refinement. Changing the deliverable, real-
    world purpose, hard constraints, or declared unacceptable failures can alter
    what counts as success and therefore requires an equivalence witness.
    """

    if current is None or proposed is None:
        return False
    if normalize_key(current.deliverable) != normalize_key(proposed.deliverable):
        return True
    if normalize_key(current.real_world_purpose) != normalize_key(proposed.real_world_purpose):
        return True
    for before, after in (
        (current.hard_constraints, proposed.hard_constraints),
        (current.unacceptable_failures, proposed.unacceptable_failures),
    ):
        if {normalize_key(item) for item in before} != {normalize_key(item) for item in after}:
            return True
    return False


def reactivate_cruxes_for_open_obligations(
    cruxes: dict[str, Crux],
    obligations: dict[str, Obligation],
    *,
    updated_seq: int,
    active_limit: int,
) -> tuple[dict[str, Crux], list[str]]:
    """Reopen the most valuable cruxes controlling newly open obligations.

    Invalidating an upstream premise can reopen downstream obligations. A crux
    that controlled one of those obligations must not remain silently resolved.
    The active limit is still respected; excess candidates become dormant.
    """

    projected = {key: value.model_copy(deep=True) for key, value in cruxes.items()}
    open_ids = {
        item.obligation_id
        for item in obligations.values()
        if item.status in {ObligationStatus.OPEN, ObligationStatus.BLOCKED}
    }
    notes: list[str] = []
    candidates: list[Crux] = []
    for item in projected.values():
        if not open_ids.intersection(item.obligation_ids):
            continue
        if item.status == CruxStatus.RESOLVED:
            item.status = CruxStatus.DORMANT
            item.resolution = None
            item.updated_seq = updated_seq
            notes.append(f"reopened crux because a controlled obligation reopened: {item.crux_id}")
        if item.status in {CruxStatus.ACTIVE, CruxStatus.DORMANT}:
            candidates.append(item)

    ordered = sorted(
        candidates,
        key=lambda item: (
            -{Impact.FATAL: 4, Impact.HIGH: 3, Impact.MEDIUM: 2, Impact.LOW: 1}[item.unlock_value],
            item.created_seq,
            item.crux_id,
        ),
    )
    active_ids = {item.crux_id for item in ordered[:active_limit]}
    for item in ordered:
        desired = CruxStatus.ACTIVE if item.crux_id in active_ids else CruxStatus.DORMANT
        if item.status != desired:
            item.status = desired
            item.updated_seq = updated_seq
            notes.append(
                f"set crux {item.crux_id} to {desired.value} after obligation recompilation"
            )
    return projected, notes


def validate_reframe(
    witness: ReframeWitness,
    *,
    charter: TaskCharter,
) -> tuple[bool, list[str]]:
    problems: list[str] = []
    if not witness.mapping_back.strip():
        problems.append("missing mapping back to the original deliverable")
    if not witness.preserved_constraints:
        problems.append("no preserved constraints declared")
    normalized_preserved = {normalize_key(item) for item in witness.preserved_constraints}
    for constraint in charter.hard_constraints:
        if normalize_key(constraint) not in normalized_preserved:
            # Exact restatement is not mandatory, but omission is a warning that
            # must be handled by the semantic controller before admission.
            problems.append(f"hard constraint not explicitly preserved: {constraint}")
    if not witness.drift_risks:
        problems.append("no task-drift risk analysis supplied")
    return not problems, problems


def ceiling_trigger_reasons(scan: CeilingSensitivityScan | None) -> list[str]:
    if scan is None:
        return []
    reasons: list[str] = []
    for label, values in (
        ("hidden assumption", scan.hidden_assumptions),
        ("alternative mechanism", scan.alternative_mechanisms),
        ("weak observation channel", scan.weak_observation_channels),
        ("holistic trade-off", scan.holistic_tradeoffs),
        ("representation failure", scan.representation_failures),
    ):
        reasons.extend(f"{label}: {item}" for item in values if item.strip())
    if scan.concrete_trigger and not reasons and scan.rationale.strip():
        reasons.append(scan.rationale.strip())
    return unique_preserving_order(reasons)


def build_action_contract(
    proposal: ActionProposal,
    *,
    action_id: str,
    obligation_ids: Sequence[str],
    crux_ids: Sequence[str],
) -> ActionContract:
    outcomes = list(proposal.outcome_branches)
    if not outcomes:
        outcomes = [
            ActionOutcome(
                outcome="The targeted uncertainty is materially resolved.",
                decision_effect=proposal.expected_decision_effect,
                obligation_effect=proposal.expected_decision_effect,
            ),
            ActionOutcome(
                outcome="The result is negative, ambiguous, or invalid.",
                decision_effect="Preserve the current decision, record the scoped negative result, and replan only if the failure exposes a new crux.",
            ),
        ]
    return ActionContract(
        action_id=action_id,
        target_crux_ids=list(crux_ids),
        question=proposal.assignment,
        possible_outcomes=outcomes,
        obligation_ids=list(obligation_ids),
        evidence_channel=proposal.independence_class,
        expected_cost=proposal.cost,
        stop_condition=proposal.stop_condition,
        failure_handling=proposal.failure_handling,
        expected_unlock=proposal.expected_decision_effect,
        artifact_scope=proposal.artifact_scope,
        causal_hypothesis=proposal.causal_hypothesis,
        intervention=proposal.intervention,
        potency_check=proposal.potency_check,
        decision_rule=proposal.decision_rule,
        observation_modalities=list(proposal.observation_modalities),
        continuation=proposal.continuation,
        substantive=proposal.substantive,
    )


def derive_action_receipt(
    *,
    action_id: str,
    findings: Sequence[str],
    decision_effect: str,
    scope: str,
    evidence_strength: str = "moderate",
) -> ActionReceipt:
    strength = (
        evidence_strength
        if evidence_strength in {"none", "weak", "moderate", "strong", "decisive"}
        else "moderate"
    )
    return ActionReceipt(
        action_id=action_id,
        observed_result=" ".join(item.strip() for item in findings if item.strip())
        or "No substantive finding was returned.",
        state_changes=[decision_effect] if decision_effect.strip() else [],
        decisions_changed=[decision_effect] if decision_effect.strip() else [],
        evidence_strength=strength,  # type: ignore[arg-type]
        evidence_scope=scope,
    )


_EVIDENCE_STRENGTH_RANK = {
    "none": 0,
    "weak": 1,
    "moderate": 2,
    "strong": 3,
    "decisive": 4,
}

_EXTERNAL_TOOLS = {"browser", "web_search"}
_DETERMINISTIC_TOOLS = {
    "bash",
    "debug",
    "eval",
    "lsp",
}

_MODALITY_TOOLS: dict[EvidenceModality, set[str]] = {
    EvidenceModality.SOURCE: {"read", "grep", "glob", "github", "browser", "web_search"},
    EvidenceModality.STRUCTURED_DATA: {"read", "grep", "glob", "bash", "eval", "debug"},
    EvidenceModality.DETERMINISTIC_TEST: {"bash", "debug", "eval", "lsp"},
    EvidenceModality.STATIC_VISUAL: {"inspect_image", "browser", "computer"},
    EvidenceModality.TEMPORAL_VISUAL: {"browser", "computer", "bash"},
    EvidenceModality.AUDIO: {"browser", "computer", "bash"},
    EvidenceModality.INTERACTIVE: {"browser", "computer"},
    EvidenceModality.EXTERNAL_OBSERVATION: {"browser", "web_search", "github"},
    EvidenceModality.HUMAN_OBSERVATION: set(),
}


def observed_modalities_from_trace(
    requested: Sequence[EvidenceModality],
    trace: ProviderTraceSummary,
) -> list[EvidenceModality]:
    """Retain only requested modalities whose observation tool actually ran."""

    successful_tools = {item.name for item in trace.tool_calls if item.success is True}
    return unique_preserving_order(
        modality
        for modality in requested
        if _MODALITY_TOOLS[modality].intersection(successful_tools)
    )


def finalize_action_receipt(
    receipt: ActionReceipt,
    *,
    contract: ActionContract | None,
    trace: ProviderTraceSummary,
    usage: Usage,
) -> ActionReceipt:
    """Replace self-certified receipt claims with provider-observed facts.

    Semantic outcome matching remains a worker claim, but its branch index is
    checked against the pre-registered contract. Tool channel and cost come
    only from the provider trace.
    """

    successful_tools = {item.name for item in trace.tool_calls if item.success is True}
    channels: list[IndependenceClass] = []
    if successful_tools & _EXTERNAL_TOOLS:
        channels.append(IndependenceClass.EXTERNAL_EVIDENCE)
    if successful_tools & _DETERMINISTIC_TOOLS:
        channels.append(IndependenceClass.DETERMINISTIC_TOOL)
    if not channels:
        channels.append(IndependenceClass.SAME_MODEL)

    outcome_index = receipt.matched_outcome_index
    if outcome_index is None:
        outcome_match = (
            receipt.outcome_match
            if receipt.outcome_match in {"ambiguous", "unmapped"}
            else "unmapped"
        )
    elif contract is None or outcome_index >= len(contract.possible_outcomes):
        outcome_match = "invalid"
        outcome_index = None
    else:
        outcome_match = "matched"

    confirmed = bool(contract and contract.evidence_channel in channels)
    maximum_strength = (
        "strong"
        if any(
            channel
            in {
                IndependenceClass.DETERMINISTIC_TOOL,
                IndependenceClass.EXTERNAL_EVIDENCE,
            }
            for channel in channels
        )
        else "moderate"
    )
    claimed_strength = receipt.evidence_strength
    strength = min(
        (claimed_strength, maximum_strength),
        key=lambda item: _EVIDENCE_STRENGTH_RANK[item],
    )

    return receipt.model_copy(
        update={
            "matched_outcome_index": outcome_index,
            "outcome_match": outcome_match,
            "observed_evidence_channels": channels,
            "evidence_channel_confirmed": confirmed,
            "evidence_strength": strength,
            "observed_cost": ObservedActionCost(
                model_turns=trace.model_turns,
                provider_calls=usage.calls,
                tool_calls=len(trace.tool_calls),
                tool_errors=trace.tool_errors,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_output_tokens=usage.reasoning_output_tokens,
                wall_seconds=usage.wall_seconds,
            ),
            "forecast_was_useful": outcome_match == "matched",
        }
    )


def admit_substrate_entries(
    entries: Sequence[SubstrateEntry],
    *,
    existing: dict[str, SubstrateEntry],
) -> tuple[dict[str, SubstrateEntry], AdmissionNotes]:
    projected = {key: value.model_copy(deep=True) for key, value in existing.items()}
    notes = AdmissionNotes()
    statement_index = {
        (normalize_key(item.statement), normalize_key(item.scope)): key
        for key, item in projected.items()
    }
    for raw in entries:
        item = raw.model_copy(deep=True)
        if not item.entry_id:
            item.entry_id = new_id("sub")
        key = (normalize_key(item.statement), normalize_key(item.scope))
        duplicate = statement_index.get(key)
        if duplicate:
            incumbent = projected[duplicate]
            incumbent.evidence_references = unique_preserving_order(
                [*incumbent.evidence_references, *item.evidence_references]
            )
            if item.confidence == "verified":
                incumbent.confidence = "verified"
            notes.rejected.append(f"merged duplicate substrate entry into {duplicate}")
            continue
        # Branch-local or ungrounded material may be retained, but nothing is
        # admitted to the global substrate without a provenance handle. The
        # handle can point to the immutable Task Source, a source snapshot, an
        # executable result, or another ledger-backed evidence object.
        if item.global_admission and not item.evidence_references:
            item.global_admission = False
            notes.warnings.append(
                f"global admission removed from ungrounded substrate entry: {item.entry_id}"
            )
        projected[item.entry_id] = item
        statement_index[key] = item.entry_id
        notes.accepted_ids.append(item.entry_id)
    return projected, notes


def admit_overlays(
    candidates: Sequence[SpeculativeOverlay],
    *,
    existing: dict[str, SpeculativeOverlay],
    normal_limit: int,
    hard_limit: int,
    require_behavioral_difference: bool,
) -> tuple[dict[str, SpeculativeOverlay], AdmissionNotes]:
    projected = {key: value.model_copy(deep=True) for key, value in existing.items()}
    notes = AdmissionNotes()
    mechanism_index = {
        normalize_key(item.mechanism): key
        for key, item in projected.items()
        if item.status in {OverlayStatus.PROPOSED, OverlayStatus.ACTIVE}
    }
    active_count = sum(
        item.status in {OverlayStatus.PROPOSED, OverlayStatus.ACTIVE} for item in projected.values()
    )
    for raw in candidates:
        item = raw.model_copy(deep=True)
        if not item.overlay_id:
            item.overlay_id = new_id("ovr")
        if require_behavioral_difference and not item.behavioral_difference.strip():
            notes.rejected.append(f"overlay lacks consequential behavioral difference: {item.name}")
            continue
        mechanism_key = normalize_key(item.mechanism)
        duplicate = mechanism_index.get(mechanism_key)
        if duplicate:
            notes.rejected.append(f"overlay mechanism duplicates {duplicate}: {item.name}")
            continue
        if active_count >= hard_limit:
            notes.rejected.append(f"hard overlay limit reached: {item.name}")
            continue
        if active_count >= normal_limit and item.unlock_contract is None:
            item.status = OverlayStatus.DORMANT
        else:
            item.status = OverlayStatus.ACTIVE
            active_count += 1
        projected[item.overlay_id] = item
        mechanism_index[mechanism_key] = item.overlay_id
        notes.accepted_ids.append(item.overlay_id)
    return projected, notes


def validate_lead_ack(
    ack: LeadContinuityAck | None,
    *,
    state: RunState,
    artifact: ArtifactRef | None,
) -> tuple[LeadContinuityStatus, list[str]]:
    if ack is None:
        return LeadContinuityStatus.DEGRADED, ["lead response omitted continuity acknowledgement"]
    problems: list[str] = []
    if state.task_source and ack.task_source_digest != state.task_source.digest:
        problems.append("task source digest mismatch")
    if artifact and ack.current_artifact_digest not in {None, artifact.blob.digest}:
        problems.append("artifact digest mismatch")
    expected_obligations = {item.obligation_id for item in state.open_obligations}
    if expected_obligations and not expected_obligations.issubset(set(ack.active_obligation_ids)):
        missing = sorted(expected_obligations - set(ack.active_obligation_ids))
        problems.append("missing active obligations: " + ", ".join(missing))
    expected_cruxes = {item.crux_id for item in state.active_cruxes}
    if expected_cruxes and not expected_cruxes.issubset(set(ack.active_crux_ids)):
        missing = sorted(expected_cruxes - set(ack.active_crux_ids))
        problems.append("missing active cruxes: " + ", ".join(missing))
    if state.artifact_spine and ack.artifact_spine_revision not in {
        None,
        state.artifact_spine.revision,
    }:
        problems.append("artifact spine revision mismatch")
    return (
        LeadContinuityStatus.CONTINUOUS if not problems else LeadContinuityStatus.DEGRADED,
        problems,
    )


def completion_case_gaps(state: RunState, completion: CompletionCase | None) -> list[str]:
    if completion is None:
        return ["completion case is missing"]
    gaps: list[str] = []
    if state.task_source and completion.task_source_digest != state.task_source.digest:
        gaps.append("completion case task digest mismatch")
    if (
        state.final_artifact
        and completion.artifact_digest
        and completion.artifact_digest != state.final_artifact.blob.digest
    ):
        gaps.append("completion case artifact digest mismatch")
    covered = {claim.obligation_id: claim for claim in completion.claims}
    for obligation in state.obligations.values():
        if not obligation.release_blocking:
            continue
        claim = covered.get(obligation.obligation_id)
        if claim is None:
            gaps.append(f"release-blocking obligation not covered: {obligation.obligation_id}")
            continue
        if obligation.status != ObligationStatus.SATISFIED:
            gaps.append(f"release-blocking obligation unresolved: {obligation.obligation_id}")
        if claim.status != "satisfied":
            gaps.append(f"release-blocking completion claim unresolved: {obligation.obligation_id}")
        if claim.status == "satisfied" and not claim.artifact_location.strip():
            gaps.append(
                f"satisfied completion claim lacks artifact location: {obligation.obligation_id}"
            )
        if (
            claim.status == "satisfied"
            and not claim.evidence_or_test
            and obligation.kind in {"claim", "verification", "construction"}
        ):
            gaps.append(
                f"satisfied completion claim lacks evidence/test: {obligation.obligation_id}"
            )
        if claim.status == "satisfied" and obligation.required_evidence_modalities:
            evidence_ids = unique_preserving_order(
                [*obligation.evidence_references, *claim.evidence_or_test]
            )
            if state.final_artifact is not None:
                evidence_ids = unique_preserving_order(
                    [
                        *evidence_ids,
                        *(
                            evidence_id
                            for evidence_id, evidence in state.evidence.items()
                            if evidence.artifact_digest == state.final_artifact.blob.digest
                        ),
                    ]
                )
            observed: set[EvidenceModality] = set()
            artifact_bound = {
                EvidenceModality.DETERMINISTIC_TEST,
                EvidenceModality.STATIC_VISUAL,
                EvidenceModality.TEMPORAL_VISUAL,
                EvidenceModality.AUDIO,
                EvidenceModality.INTERACTIVE,
            }
            final_digest = state.final_artifact.blob.digest if state.final_artifact else None
            for evidence_id in evidence_ids:
                evidence = state.evidence.get(evidence_id)
                if evidence is None:
                    continue
                for modality in evidence.modalities:
                    if modality in artifact_bound and evidence.artifact_digest != final_digest:
                        continue
                    observed.add(modality)
            missing = [
                modality.value
                for modality in obligation.required_evidence_modalities
                if modality not in observed
            ]
            if missing:
                gaps.append(
                    "release-blocking obligation lacks required evidence modalities "
                    f"({', '.join(missing)}): {obligation.obligation_id}"
                )
        if claim.status == "satisfied" and obligation.required_artifact_scope != "targeted":
            scope_rank = {
                "targeted": 0,
                "sequence": 1,
                "whole_artifact": 2,
                "release": 3,
            }
            evidence_ids = unique_preserving_order(
                [*obligation.evidence_references, *claim.evidence_or_test]
            )
            if state.final_artifact is not None:
                evidence_ids = unique_preserving_order(
                    [
                        *evidence_ids,
                        *(
                            evidence_id
                            for evidence_id, evidence in state.evidence.items()
                            if evidence.artifact_digest == state.final_artifact.blob.digest
                        ),
                    ]
                )
            observed_rank = max(
                (
                    scope_rank[evidence.artifact_scope]
                    for evidence_id in evidence_ids
                    if (evidence := state.evidence.get(evidence_id)) is not None
                    and not evidence.negative_result
                ),
                default=-1,
            )
            if (
                obligation.kind == "deliverable"
                and claim.artifact_location.strip()
                and state.final_artifact is not None
            ):
                observed_rank = max(observed_rank, scope_rank["release"])
            required_rank = scope_rank[obligation.required_artifact_scope]
            if observed_rank < required_rank:
                gaps.append(
                    "release-blocking obligation lacks evidence at required artifact scope "
                    f"({obligation.required_artifact_scope}): {obligation.obligation_id}"
                )
    for risk in completion.unresolved_high_impact_risks:
        gaps.append(
            "completion case retains unresolved high-impact risk: "
            + (risk.strip() or "(unspecified)")
        )
    return gaps


def task_source_fingerprint(source: TaskSource) -> str:
    payload = (
        source.original_text
        + "\n"
        + "\n".join(f"{item.amendment_id}:{item.digest}:{item.text}" for item in source.amendments)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
