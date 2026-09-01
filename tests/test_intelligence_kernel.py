from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

import pytest

from frontier_harness.blobs import BlobStore
from frontier_harness.core.journal import KernelJournal
from frontier_harness.core.kernel import IntelligenceKernel
from frontier_harness.core.types import (
    AssayStatus,
    ChallengeVerdict,
    ComputeEnvelope,
    ComputeUsage,
    ContentRef,
    FailureDomain,
    Move,
    MoveMode,
    MoveStatus,
    Observation,
    ObservationKind,
    RunResumed,
    RunState,
    RunStatus,
    SteeringReceived,
)
from frontier_harness.intelligence.context import ContextFrame
from frontier_harness.intelligence.contracts import (
    ArtifactDraft,
    FinishDraft,
    MoveDirective,
    MoveExecutionResult,
    ObservationDraft,
    WorkspaceDraft,
)
from frontier_harness.ledger import EventLedger
from frontier_harness.util import sha256_text


class ScriptedRunner:
    def __init__(self, outcomes: list[MoveExecutionResult], blobs: BlobStore) -> None:
        self.outcomes = deque(outcomes)
        self.blobs = blobs
        self.calls: list[tuple[MoveMode, ContextFrame, bool]] = []

    async def run(
        self,
        *,
        move: Move,
        state: RunState,
        context: ContextFrame,
        recovering: bool,
    ) -> MoveExecutionResult:
        self.calls.append((move.mode, context, recovering))
        result = self.outcomes.popleft()
        update: dict[str, object] = {}
        if result.finish is not None and result.artifact is None:
            document = result.workspace.document if result.workspace is not None else "artifact"
            update["artifact"] = ArtifactDraft(
                content_ref=self.blobs.put_text(
                    document,
                    media_type="text/markdown; charset=utf-8",
                    original_name="scripted-artifact.md",
                )
            )
        if move.mode == MoveMode.CHALLENGE:
            claim = state.finish_claim
            target_digest = (
                state.artifacts[claim.artifact_head_ids[0]].digest
                if claim is not None and len(claim.artifact_head_ids) == 1
                else None
            )
            update["observations"] = [
                item.model_copy(
                    update={
                        "assay_coverage": item.assay_coverage or "the exact claimed artifact",
                        "artifact_digest": item.artifact_digest or target_digest,
                        "covered_claims": (
                            item.covered_claims
                            or (claim.satisfaction_claims if claim is not None else [])
                        ),
                    }
                )
                if item.challenge_verdict is not None
                else item
                for item in result.observations
            ]
        return result.model_copy(update=update)


def test_finish_claims_must_be_distinct_and_nonempty() -> None:
    with pytest.raises(ValueError, match="empty satisfaction claim"):
        FinishDraft(satisfaction_claims=[" "])
    with pytest.raises(ValueError, match="repeats a satisfaction claim"):
        FinishDraft(satisfaction_claims=["same", " same "])


def kernel_for(
    tmp_path: Path,
    outcomes: list[MoveExecutionResult],
    *,
    envelope: ComputeEnvelope | None = None,
) -> tuple[IntelligenceKernel, ScriptedRunner]:
    blobs = BlobStore(tmp_path / "blobs")
    runner = ScriptedRunner(outcomes, blobs)
    journal = KernelJournal(
        ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_kernel"),
        snapshot_path=tmp_path / "state.json",
    )
    kernel = IntelligenceKernel(journal=journal, blobs=blobs, runner=runner)
    kernel.start("Build an excellent artifact.", envelope=envelope)
    return kernel, runner


def workspace(name: str) -> WorkspaceDraft:
    return WorkspaceDraft(document=f"# {name}\n\nCurrent best.", summary=name)


async def test_rejected_finish_reenters_normal_construction(tmp_path: Path) -> None:
    outcomes = [
        MoveExecutionResult(
            workspace=workspace("First candidate"),
            finish=FinishDraft(satisfaction_claims=["The artifact is excellent"]),
            usage=ComputeUsage(model_turns=5),
        ),
        MoveExecutionResult(
            observations=[
                ObservationDraft(
                    kind=ObservationKind.CHALLENGE,
                    summary="The artifact is structurally valid but conceptually sparse",
                    source="fresh-challenger",
                    challenge_verdict=ChallengeVerdict.CHALLENGES,
                    assay_status=AssayStatus.VALID,
                    direct_inspection=True,
                )
            ],
            usage=ComputeUsage(model_turns=1),
        ),
        MoveExecutionResult(
            workspace=workspace("Reconstructed candidate"),
            finish=FinishDraft(satisfaction_claims=["The rebuilt artifact is excellent"]),
            usage=ComputeUsage(model_turns=5),
        ),
        MoveExecutionResult(
            observations=[
                ObservationDraft(
                    kind=ObservationKind.CHALLENGE,
                    summary="Direct inspection supports the rebuilt artifact",
                    source="fresh-challenger",
                    challenge_verdict=ChallengeVerdict.SUPPORTS,
                    assay_status=AssayStatus.VALID,
                    direct_inspection=True,
                )
            ],
            usage=ComputeUsage(model_turns=1),
        ),
    ]
    kernel, runner = kernel_for(
        tmp_path,
        outcomes,
        envelope=ComputeEnvelope(max_model_turns=48),
    )

    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    assert kernel.state.usage.model_turns == 12
    assert [mode for mode, _, _ in runner.calls] == [
        MoveMode.LEAD,
        MoveMode.CHALLENGE,
        MoveMode.LEAD,
        MoveMode.CHALLENGE,
    ]
    assert kernel.state.current_workspace is not None
    assert kernel.state.current_workspace.summary == "Reconstructed candidate"
    assert any(
        "conceptually sparse" in call[1].observations[0].summary
        for call in runner.calls
        if call[0] == MoveMode.LEAD and call[1].observations
    )


async def test_empty_move_set_broadens_instead_of_completing(tmp_path: Path) -> None:
    outcomes = [
        MoveExecutionResult(workspace=workspace("Unfinished"), usage=ComputeUsage(model_turns=1)),
        MoveExecutionResult(
            observations=[
                ObservationDraft(
                    kind=ObservationKind.MODEL,
                    summary="Current representation is too narrow",
                    source="navigator",
                )
            ],
            usage=ComputeUsage(model_turns=1),
        ),
        MoveExecutionResult(
            workspace=workspace("Broader attempt"),
            next_move=MoveDirective(
                mode=MoveMode.LEAD,
                intent="Continue the broader construction",
            ),
            usage=ComputeUsage(model_turns=1),
        ),
    ]
    kernel, runner = kernel_for(tmp_path, outcomes)

    await kernel.run(max_steps=5)

    assert kernel.state.status == RunStatus.ACTIVE
    assert [mode for mode, _, _ in runner.calls] == [
        MoveMode.LEAD,
        MoveMode.NAVIGATE,
        MoveMode.LEAD,
    ]
    assert kernel.state.finish_claim is None


async def test_hard_envelope_is_the_only_compute_stop(tmp_path: Path) -> None:
    outcomes = [
        MoveExecutionResult(
            workspace=workspace("Still incomplete"),
            usage=ComputeUsage(model_turns=2),
        )
    ]
    kernel, _ = kernel_for(
        tmp_path,
        outcomes,
        envelope=ComputeEnvelope(max_model_turns=2),
    )

    await kernel.run()

    assert kernel.state.status == RunStatus.EXHAUSTED
    assert "model turns" in (kernel.state.terminal_reason or "")


async def test_final_supported_claim_settles_at_the_exact_compute_boundary(
    tmp_path: Path,
) -> None:
    kernel, _ = kernel_for(
        tmp_path,
        [
            MoveExecutionResult(
                workspace=workspace("Complete at the boundary"),
                finish=FinishDraft(satisfaction_claims=["The artifact is complete"]),
                usage=ComputeUsage(model_turns=1),
            ),
            MoveExecutionResult(
                observations=[
                    ObservationDraft(
                        kind=ObservationKind.CHALLENGE,
                        summary="The exact artifact and claim are directly supported",
                        source="fresh-challenger",
                        challenge_verdict=ChallengeVerdict.SUPPORTS,
                        assay_status=AssayStatus.VALID,
                        assay_coverage="the complete artifact",
                        direct_inspection=True,
                    )
                ],
                usage=ComputeUsage(model_turns=1),
            ),
        ],
        envelope=ComputeEnvelope(max_model_turns=2),
    )

    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    assert kernel.state.usage.model_turns == 2


async def test_finish_claim_cannot_ignore_pending_operator_steering(tmp_path: Path) -> None:
    kernel, _ = kernel_for(
        tmp_path,
        [
            MoveExecutionResult(
                workspace=workspace("Candidate that ignored steering"),
                finish=FinishDraft(satisfaction_claims=["The old objective is complete"]),
            )
        ],
    )
    steering_ref = kernel.blobs.put_text(
        "Also satisfy the newly added constraint.",
        media_type="text/plain; charset=utf-8",
        original_name="steering.txt",
    )
    kernel.journal.append(
        "steering.received",
        SteeringReceived(
            observation=Observation(
                observation_id="obs_pending_steering",
                kind=ObservationKind.STEERING,
                summary="Also satisfy the newly added constraint.",
                source="operator",
                raw_ref=steering_ref,
                created_at="2026-09-01T00:00:00Z",
            )
        ),
    )

    await kernel.run(max_steps=1)

    assert kernel.state.status == RunStatus.PAUSED
    assert kernel.state.failure_domain == FailureDomain.PROVIDER
    assert kernel.state.finish_claim is None


async def test_running_move_is_recovered_without_a_second_start(tmp_path: Path) -> None:
    outcomes = [
        MoveExecutionResult(
            workspace=workspace("Recovered"),
            next_move=MoveDirective(mode=MoveMode.NAVIGATE, intent="Review recovery"),
        )
    ]
    kernel, runner = kernel_for(tmp_path, outcomes)
    move = next(iter(kernel.state.moves.values()))
    kernel.journal.append(
        "move.started",
        {"move_id": move.move_id, "started_at": "2026-08-26T00:00:00Z"},
        action_id=move.move_id,
    )
    count_before = kernel.journal.ledger.count()

    await kernel.step()

    event_types = [event.event_type for event in kernel.journal.events()]
    assert event_types.count("move.started") == 1
    assert kernel.journal.ledger.count() > count_before
    assert runner.calls[0][2] is True


async def test_challenge_contradiction_outranks_support(tmp_path: Path) -> None:
    outcomes = [
        MoveExecutionResult(
            workspace=workspace("Apparently finished"),
            finish=FinishDraft(satisfaction_claims=["Everything works"]),
        ),
        MoveExecutionResult(
            observations=[
                ObservationDraft(
                    kind=ObservationKind.CHALLENGE,
                    summary="Visual review supports the artifact",
                    source="fresh-challenger",
                    challenge_verdict=ChallengeVerdict.SUPPORTS,
                    assay_status=AssayStatus.VALID,
                    direct_inspection=True,
                ),
                ObservationDraft(
                    kind=ObservationKind.CHALLENGE,
                    summary="The executable acceptance check fails",
                    source="artifact-check",
                    challenge_verdict=ChallengeVerdict.CHALLENGES,
                    assay_status=AssayStatus.VALID,
                    direct_inspection=True,
                ),
            ]
        ),
        MoveExecutionResult(workspace=workspace("Correcting the failed check")),
    ]
    kernel, runner = kernel_for(tmp_path, outcomes)

    await kernel.run(max_steps=5)

    assert kernel.state.status == RunStatus.ACTIVE
    assert kernel.state.finish_claim is None
    assert [mode for mode, _, _ in runner.calls] == [
        MoveMode.LEAD,
        MoveMode.CHALLENGE,
        MoveMode.LEAD,
    ]
    assert "executable acceptance check fails" in runner.calls[-1][1].observations[-1].summary


async def test_non_material_criticism_is_preserved_without_vetoing_completion(
    tmp_path: Path,
) -> None:
    outcomes = [
        MoveExecutionResult(
            workspace=workspace("Finished"),
            finish=FinishDraft(satisfaction_claims=["The exact objective is satisfied"]),
        ),
        MoveExecutionResult(
            observations=[
                ObservationDraft(
                    kind=ObservationKind.CHALLENGE,
                    summary="Direct artifact inspection supports the objective",
                    source="fresh-challenger",
                    challenge_verdict=ChallengeVerdict.SUPPORTS,
                    assay_status=AssayStatus.VALID,
                    direct_inspection=True,
                ),
                ObservationDraft(
                    kind=ObservationKind.CHALLENGE,
                    summary="An earlier evidence label was imprecise but output is correct",
                    source="fresh-challenger",
                    challenge_verdict=ChallengeVerdict.CHALLENGES,
                    assay_status=AssayStatus.VALID,
                    material_to_claim=False,
                    direct_inspection=True,
                ),
            ]
        ),
    ]
    kernel, _ = kernel_for(tmp_path, outcomes)

    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    assert any(item.material_to_claim is False for item in kernel.state.observations.values())


async def test_non_material_support_cannot_close_a_semantic_claim(tmp_path: Path) -> None:
    outcomes = [
        MoveExecutionResult(
            workspace=workspace("Looks finished"),
            finish=FinishDraft(satisfaction_claims=["The semantic objective is satisfied"]),
        ),
        MoveExecutionResult(
            observations=[
                ObservationDraft(
                    kind=ObservationKind.CHALLENGE,
                    summary="The file opens and its checksum is stable",
                    source="artifact-check",
                    challenge_verdict=ChallengeVerdict.SUPPORTS,
                    assay_status=AssayStatus.VALID,
                    material_to_claim=False,
                    direct_inspection=True,
                )
            ]
        ),
        MoveExecutionResult(
            workspace=workspace("Strengthened semantic result"),
            finish=FinishDraft(satisfaction_claims=["The revised result is understandable"]),
        ),
        MoveExecutionResult(
            observations=[
                ObservationDraft(
                    kind=ObservationKind.CHALLENGE,
                    summary="Whole-result inspection directly supports understandability",
                    source="fresh-challenger",
                    challenge_verdict=ChallengeVerdict.SUPPORTS,
                    assay_status=AssayStatus.VALID,
                    direct_inspection=True,
                )
            ]
        ),
    ]
    kernel, runner = kernel_for(tmp_path, outcomes)

    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    assert [mode for mode, _, _ in runner.calls] == [
        MoveMode.LEAD,
        MoveMode.CHALLENGE,
        MoveMode.LEAD,
        MoveMode.CHALLENGE,
    ]


async def test_post_move_evidence_cannot_be_consumed_by_the_earlier_workspace(
    tmp_path: Path,
) -> None:
    outcomes = [
        MoveExecutionResult(
            workspace=workspace("First"),
            observations=[
                ObservationDraft(
                    kind=ObservationKind.TEST,
                    summary="A post-construction check found a boundary flaw",
                    source="artifact-check",
                )
            ],
            next_move=MoveDirective(mode=MoveMode.LEAD, intent="Use the check"),
        ),
        MoveExecutionResult(workspace=workspace("Corrected")),
    ]
    kernel, runner = kernel_for(tmp_path, outcomes)

    await kernel.run(max_steps=2)

    assert len(runner.calls) == 2
    assert any(
        item.summary == "A post-construction check found a boundary flaw"
        for item in runner.calls[1][1].observations
    )


async def test_evidence_stays_live_until_the_lead_explicitly_integrates_it(
    tmp_path: Path,
) -> None:
    outcomes = [
        MoveExecutionResult(
            workspace=workspace("First"),
            observations=[
                ObservationDraft(
                    kind=ObservationKind.TEST,
                    summary="A decision-changing boundary flaw remains",
                    source="artifact-check",
                )
            ],
            next_move=MoveDirective(mode=MoveMode.LEAD, intent="Consider the flaw"),
        ),
        MoveExecutionResult(
            workspace=workspace("Lead saw but did not integrate the flaw"),
            next_move=MoveDirective(mode=MoveMode.LEAD, intent="Reconsider the flaw"),
        ),
        MoveExecutionResult(workspace=workspace("Still working")),
    ]
    kernel, runner = kernel_for(tmp_path, outcomes)

    await kernel.run(max_steps=3)

    assert all(
        any(
            item.summary == "A decision-changing boundary flaw remains"
            for item in frame.observations
        )
        for _, frame, _ in runner.calls[1:]
    )


async def test_integrated_evidence_stays_consumed_through_workspace_lineage(
    tmp_path: Path,
) -> None:
    blobs = BlobStore(tmp_path / "blobs")

    class IntegratingRunner:
        def __init__(self) -> None:
            self.calls = 0
            self.evidence_id: str | None = None

        async def run(
            self,
            *,
            move: Move,
            state: RunState,
            context: ContextFrame,
            recovering: bool,
        ) -> MoveExecutionResult:
            del move, state, recovering
            self.calls += 1
            if self.calls == 1:
                return MoveExecutionResult(
                    workspace=workspace("Evidence appears"),
                    observations=[
                        ObservationDraft(
                            kind=ObservationKind.SOURCE,
                            summary="A durable source changes the decision",
                            source="lead",
                            raw_ref=blobs.put_text("source evidence"),
                        )
                    ],
                    next_move=MoveDirective(mode=MoveMode.LEAD, intent="Integrate evidence"),
                )
            if self.calls == 2:
                self.evidence_id = next(
                    item.observation_id
                    for item in context.observations
                    if item.summary == "A durable source changes the decision"
                )
                return MoveExecutionResult(
                    workspace=WorkspaceDraft(
                        document="# Integrated",
                        summary="Integrated",
                        consumed_observation_ids=[self.evidence_id],
                    ),
                    next_move=MoveDirective(mode=MoveMode.LEAD, intent="Continue cleanly"),
                )
            assert self.evidence_id is not None
            assert self.evidence_id not in {item.observation_id for item in context.observations}
            return MoveExecutionResult(workspace=workspace("Later workspace"))

    runner = IntegratingRunner()
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_consumption_lineage"),
            snapshot_path=tmp_path / "state.json",
        ),
        blobs=blobs,
        runner=runner,
    )
    kernel.start("Preserve evidence integration across workspace lineage.")

    await kernel.run(max_steps=3)

    assert runner.evidence_id is not None
    assert kernel.state.current_workspace is not None
    assert runner.evidence_id in kernel.state.current_workspace.consumed_observation_ids


async def test_finish_challenger_receives_the_integrated_raw_evidence(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "blobs")

    class EvidenceBoundFinishRunner:
        def __init__(self) -> None:
            self.calls = 0
            self.evidence_id: str | None = None

        async def run(
            self,
            *,
            move: Move,
            state: RunState,
            context: ContextFrame,
            recovering: bool,
        ) -> MoveExecutionResult:
            del recovering
            self.calls += 1
            if self.calls == 1:
                return MoveExecutionResult(
                    artifact=ArtifactDraft(content_ref=blobs.put_text("candidate")),
                    workspace=workspace("Evidence appears"),
                    observations=[
                        ObservationDraft(
                            kind=ObservationKind.SOURCE,
                            summary="The decisive raw source",
                            source="lead",
                            raw_ref=blobs.put_text("decisive source bytes"),
                        )
                    ],
                    next_move=MoveDirective(mode=MoveMode.LEAD, intent="Integrate the source"),
                )
            if self.calls == 2:
                self.evidence_id = next(
                    item.observation_id
                    for item in context.observations
                    if item.summary == "The decisive raw source"
                )
                return MoveExecutionResult(
                    workspace=WorkspaceDraft(
                        document="# Integrated evidence",
                        summary="Integrated evidence",
                        consumed_observation_ids=[self.evidence_id],
                    ),
                    finish=FinishDraft(satisfaction_claims=["The evidence supports the result"]),
                )
            assert move.mode == MoveMode.CHALLENGE
            assert self.evidence_id is not None
            evidence = next(
                item for item in context.observations if item.observation_id == self.evidence_id
            )
            assert evidence.raw_ref is not None
            claim = state.finish_claim
            assert claim is not None
            artifact_id = claim.artifact_head_ids[0]
            return MoveExecutionResult(
                observations=[
                    ObservationDraft(
                        kind=ObservationKind.CHALLENGE,
                        summary="The raw source and exact artifact directly support the claim",
                        source="fresh-challenger",
                        artifact_digest=state.artifacts[artifact_id].digest,
                        challenge_verdict=ChallengeVerdict.SUPPORTS,
                        assay_status=AssayStatus.VALID,
                        assay_coverage="artifact plus decisive raw source",
                        covered_claims=claim.satisfaction_claims,
                        direct_inspection=True,
                    )
                ]
            )

    runner = EvidenceBoundFinishRunner()
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_evidence_bound_finish"),
            snapshot_path=tmp_path / "state.json",
        ),
        blobs=blobs,
        runner=runner,
    )
    kernel.start("Keep decisive evidence visible through independent evaluation.")

    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    assert runner.evidence_id in (
        kernel.state.finish_claim.evidence_refs if kernel.state.finish_claim else []
    )


def test_challenge_verdict_cannot_impersonate_direct_inspection() -> None:
    with pytest.raises(ValueError, match="direct inspection"):
        Observation(
            observation_id="obs_fake",
            kind=ObservationKind.CHALLENGE,
            summary="Looks good from the summary",
            source="fresh-challenger",
            created_at="2026-09-01T00:00:00Z",
            challenge_verdict=ChallengeVerdict.SUPPORTS,
            assay_status=AssayStatus.VALID,
            assay_coverage="only the summary",
            direct_inspection=False,
        )


async def test_problem_selected_branches_remain_isolated_then_rejoin_context(
    tmp_path: Path,
) -> None:
    def artifact(text: str) -> ArtifactDraft:
        encoded = text.encode()
        return ArtifactDraft(
            content_ref=ContentRef(
                digest=sha256_text(text),
                size=len(encoded),
                media_type="text/markdown",
                relative_path=f"sha256/{sha256_text(text)}",
                original_name="artifact.md",
            )
        )

    outcomes = [
        MoveExecutionResult(
            workspace=workspace("Root hypothesis"),
            next_moves=[
                MoveDirective(
                    mode=MoveMode.LEAD,
                    intent="Test solution family A",
                    fork_purpose="Solution family A predicts a simpler construction",
                ),
                MoveDirective(
                    mode=MoveMode.LEAD,
                    intent="Test solution family B",
                    fork_purpose="Solution family B predicts stronger edge-case behavior",
                ),
                MoveDirective(
                    mode=MoveMode.LEAD,
                    intent="Integrate the strongest branch evidence",
                ),
            ],
        ),
        MoveExecutionResult(
            workspace=WorkspaceDraft(
                document="# Family A",
                summary="Family A result",
                activate=False,
            ),
            artifact=artifact("# Family A artifact"),
        ),
        MoveExecutionResult(
            workspace=WorkspaceDraft(
                document="# Family B",
                summary="Family B result",
                activate=False,
            ),
            artifact=artifact("# Family B artifact"),
        ),
        MoveExecutionResult(workspace=workspace("Integrated result")),
    ]
    kernel, runner = kernel_for(tmp_path, outcomes)

    await kernel.run(max_steps=4)

    assert kernel.state.current_workspace is not None
    assert kernel.state.current_workspace.summary == "Integrated result"
    assert len(kernel.state.trajectories) == 3
    assert len(kernel.state.workspaces) == 4
    assert [call[0] for call in runner.calls] == [
        MoveMode.LEAD,
        MoveMode.LEAD,
        MoveMode.LEAD,
        MoveMode.LEAD,
    ]
    integration_context = runner.calls[-1][1]
    assert len(integration_context.artifact_heads) == 2
    assert {item.trajectory_id for item in integration_context.artifact_heads} == (
        set(kernel.state.trajectories) - {kernel.state.root_trajectory_id}
    )


async def test_completion_requires_branches_to_rejoin_one_integrated_artifact(
    tmp_path: Path,
) -> None:
    class MultiHeadRunner:
        def __init__(self) -> None:
            self.calls: list[MoveMode] = []
            self.challenge_count = 0

        async def run(
            self,
            *,
            move: Move,
            state: RunState,
            context: ContextFrame,
            recovering: bool,
        ) -> MoveExecutionResult:
            del context, recovering
            self.calls.append(move.mode)
            if len(self.calls) == 1:
                return MoveExecutionResult(
                    artifact=artifact("# Root candidate"),
                    workspace=workspace("Root candidate"),
                    next_moves=[
                        MoveDirective(
                            mode=MoveMode.LEAD,
                            intent="Develop the independent alternative",
                            fork_purpose="A materially different solution family",
                        ),
                        MoveDirective(
                            mode=MoveMode.LEAD,
                            intent="Integrate all viable heads",
                        ),
                    ],
                )
            if move.mode == MoveMode.LEAD and move.trajectory_id == state.root_trajectory_id:
                return MoveExecutionResult(
                    artifact=artifact("# Integrated root artifact"),
                    workspace=WorkspaceDraft(
                        document="# Integrated",
                        summary="Integrated",
                        active_trajectory_ids=[state.root_trajectory_id],
                    ),
                    finish=FinishDraft(
                        satisfaction_claims=["The integrated root artifact satisfies the task"],
                    ),
                )
            if move.mode == MoveMode.LEAD:
                return MoveExecutionResult(
                    artifact=artifact("# Alternative candidate"),
                    workspace=WorkspaceDraft(
                        document="# Alternative",
                        summary="Alternative",
                        activate=False,
                    ),
                )
            claim = state.finish_claim
            assert claim is not None
            artifact_id = claim.artifact_head_ids[self.challenge_count]
            self.challenge_count += 1
            return MoveExecutionResult(
                observations=[
                    ObservationDraft(
                        kind=ObservationKind.CHALLENGE,
                        summary=f"Direct support for {artifact_id}",
                        source="fresh-challenger",
                        artifact_digest=state.artifacts[artifact_id].digest,
                        challenge_verdict=ChallengeVerdict.SUPPORTS,
                        assay_status=AssayStatus.VALID,
                        assay_coverage="the complete integrated root artifact",
                        covered_claims=claim.satisfaction_claims,
                        direct_inspection=True,
                    )
                ]
            )

    def artifact(text: str) -> ArtifactDraft:
        return ArtifactDraft(
            content_ref=blobs.put_text(
                text,
                media_type="text/markdown",
                original_name="artifact.md",
            )
        )

    runner = MultiHeadRunner()
    blobs = BlobStore(tmp_path / "blobs")
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_multi_head"),
            snapshot_path=tmp_path / "state.json",
        ),
        blobs=blobs,
        runner=runner,
    )
    kernel.start("Build and directly verify a multi-head result.")

    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    assert runner.challenge_count == 1
    assert runner.calls == [
        MoveMode.LEAD,
        MoveMode.LEAD,
        MoveMode.LEAD,
        MoveMode.CHALLENGE,
    ]
    assert kernel.state.current_workspace is not None
    assert len(kernel.state.current_workspace.artifact_head_ids) == 1
    assert all(
        trajectory.status.value == "merged"
        for trajectory_id, trajectory in kernel.state.trajectories.items()
        if trajectory_id != kernel.state.root_trajectory_id
    )


async def test_repeated_low_information_lead_moves_trigger_fresh_navigation(
    tmp_path: Path,
) -> None:
    outcomes = [
        MoveExecutionResult(
            workspace=workspace("Initial map"),
            next_move=MoveDirective(mode=MoveMode.LEAD, intent="Try once"),
        ),
        MoveExecutionResult(
            workspace=workspace("Narrative churn one"),
            next_move=MoveDirective(mode=MoveMode.LEAD, intent="Try twice"),
        ),
        MoveExecutionResult(
            workspace=workspace("Narrative churn two"),
            next_move=MoveDirective(mode=MoveMode.LEAD, intent="Try a third time"),
        ),
    ]
    kernel, _ = kernel_for(tmp_path, outcomes)

    await kernel.run(max_steps=3)

    proposed = [item for item in kernel.state.moves.values() if item.status.value == "proposed"]
    assert len(proposed) == 1
    assert proposed[0].mode == MoveMode.NAVIGATE
    signals = [
        item
        for item in kernel.state.observations.values()
        if item.metadata.get("kernel_signal") == "low_information"
    ]
    assert len(signals) == 2
    assert signals[-1].metadata["repeated"] is True


async def test_new_decision_boundaries_count_as_thought_space_progress(
    tmp_path: Path,
) -> None:
    outcomes = [
        MoveExecutionResult(
            workspace=WorkspaceDraft(
                document="# Initial",
                summary="Initial",
                decision_boundary="Which representation exposes the causal structure?",
            ),
            next_move=MoveDirective(mode=MoveMode.LEAD, intent="Eliminate representation A"),
        ),
        MoveExecutionResult(
            workspace=WorkspaceDraft(
                document="# A eliminated",
                summary="A eliminated",
                decision_boundary="Whether representation B preserves the invariant",
            ),
            next_move=MoveDirective(mode=MoveMode.LEAD, intent="Test representation B in thought"),
        ),
        MoveExecutionResult(
            workspace=WorkspaceDraft(
                document="# B understood",
                summary="B understood",
                decision_boundary="How to turn representation B into the artifact",
            ),
            next_move=MoveDirective(mode=MoveMode.LEAD, intent="Construct from B"),
        ),
    ]
    kernel, _ = kernel_for(tmp_path, outcomes)

    await kernel.run(max_steps=3)

    proposed = [item for item in kernel.state.moves.values() if item.status == MoveStatus.PROPOSED]
    assert len(proposed) == 1
    assert proposed[0].mode == MoveMode.LEAD
    assert not any(
        item.metadata.get("kernel_signal") == "low_information"
        for item in kernel.state.observations.values()
    )


async def test_completion_requires_every_exact_semantic_claim_to_be_covered(
    tmp_path: Path,
) -> None:
    claims = ["The artifact is correct", "The artifact is understandable"]
    outcomes = [
        MoveExecutionResult(
            workspace=workspace("Complete candidate"),
            finish=FinishDraft(satisfaction_claims=claims),
        ),
        MoveExecutionResult(
            observations=[
                ObservationDraft(
                    kind=ObservationKind.CHALLENGE,
                    summary="Correctness is directly supported",
                    source="fresh-challenger",
                    challenge_verdict=ChallengeVerdict.SUPPORTS,
                    assay_status=AssayStatus.VALID,
                    assay_coverage="all correctness behavior",
                    covered_claims=[claims[0]],
                    direct_inspection=True,
                )
            ]
        ),
        MoveExecutionResult(
            observations=[
                ObservationDraft(
                    kind=ObservationKind.CHALLENGE,
                    summary="Understandability is directly supported",
                    source="fresh-challenger",
                    challenge_verdict=ChallengeVerdict.SUPPORTS,
                    assay_status=AssayStatus.VALID,
                    assay_coverage="the complete reader-facing artifact",
                    covered_claims=[claims[1]],
                    direct_inspection=True,
                )
            ]
        ),
    ]
    kernel, runner = kernel_for(tmp_path, outcomes)

    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    assert [mode for mode, _, _ in runner.calls] == [
        MoveMode.LEAD,
        MoveMode.CHALLENGE,
        MoveMode.CHALLENGE,
    ]


async def test_wall_envelope_interrupts_the_live_move_and_exhausts_cleanly(
    tmp_path: Path,
) -> None:
    class SlowRunner:
        async def run(
            self,
            *,
            move: Move,
            state: RunState,
            context: ContextFrame,
            recovering: bool,
        ) -> MoveExecutionResult:
            del move, state, context, recovering
            await asyncio.sleep(1)
            raise AssertionError("the wall envelope should cancel the move")

    blobs = BlobStore(tmp_path / "blobs")
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_wall_limit"),
            snapshot_path=tmp_path / "state.json",
        ),
        blobs=blobs,
        runner=SlowRunner(),
    )
    kernel.start(
        "Stop this live move at the operator boundary.",
        envelope=ComputeEnvelope(max_wall_seconds=0.02),
    )

    await kernel.run()

    assert kernel.state.status == RunStatus.EXHAUSTED
    assert kernel.state.usage.wall_seconds >= 0.02
    assert kernel.state.terminal_evidence_refs
    terminal = kernel.state.observations[kernel.state.terminal_evidence_refs[0]]
    assert terminal.kind == ObservationKind.RESOURCE
    assert terminal.raw_ref == kernel.state.objective.original_text_ref
    assert not any(item.status == MoveStatus.PROPOSED for item in kernel.state.moves.values())


async def test_invalid_external_boundary_becomes_a_preserved_retry_not_a_runtime_crash(
    tmp_path: Path,
) -> None:
    class InvalidBoundaryRunner:
        async def run(
            self,
            *,
            move: Move,
            state: RunState,
            context: ContextFrame,
            recovering: bool,
        ) -> MoveExecutionResult:
            del move, state, context, recovering
            return MoveExecutionResult(
                workspace=workspace("Claim with no artifact"),
                finish=FinishDraft(satisfaction_claims=["A nonexistent artifact is done"]),
            )

    blobs = BlobStore(tmp_path / "blobs")
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_invalid_boundary"),
            snapshot_path=tmp_path / "state.json",
        ),
        blobs=blobs,
        runner=InvalidBoundaryRunner(),
    )
    kernel.start("Reject an invalid external move boundary.")

    await kernel.run(max_steps=1)

    assert kernel.state.status == RunStatus.PAUSED
    assert kernel.state.failure_domain == FailureDomain.PROVIDER
    failed = [item for item in kernel.state.moves.values() if item.status == MoveStatus.FAILED]
    retries = [item for item in kernel.state.moves.values() if item.status == MoveStatus.PROPOSED]
    assert len(failed) == len(retries) == 1
    assert retries[0].retry_of_move_id == failed[0].move_id


async def test_failed_move_pauses_and_preserves_the_original_move(
    tmp_path: Path,
) -> None:
    kernel, _runner = kernel_for(
        tmp_path,
        [
            MoveExecutionResult(
                success=False,
                error="ProviderError: transport unavailable",
                failure_domain=FailureDomain.PROVIDER,
            )
        ],
    )

    await kernel.run(max_steps=1)

    assert kernel.state.status == RunStatus.PAUSED
    proposed = [item for item in kernel.state.moves.values() if item.status == MoveStatus.PROPOSED]
    assert len(proposed) == 1
    assert proposed[0].intent == "Establish the strongest live solution and decision map"
    assert "Repair the failed operation" not in proposed[0].intent


async def test_resumed_infrastructure_failure_retries_without_semantic_reframing(
    tmp_path: Path,
) -> None:
    kernel, _runner = kernel_for(
        tmp_path,
        [
            MoveExecutionResult(
                success=False,
                error="ProviderError: transport unavailable",
                failure_domain=FailureDomain.PROVIDER,
            ),
            MoveExecutionResult(
                success=False,
                error="ProviderError: transport unavailable",
                failure_domain=FailureDomain.PROVIDER,
            ),
        ],
    )

    await kernel.run()
    assert kernel.state.status == RunStatus.PAUSED
    kernel.journal.append("run.resumed", RunResumed(reason="transport restored"))
    await kernel.run()

    assert kernel.state.status == RunStatus.PAUSED
    proposed = [item for item in kernel.state.moves.values() if item.status == MoveStatus.PROPOSED]
    assert len(proposed) == 1
    assert proposed[0].intent == "Establish the strongest live solution and decision map"
