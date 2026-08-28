from __future__ import annotations

from collections import deque
from pathlib import Path

from frontier_harness.blobs import BlobStore
from frontier_harness.core.journal import KernelJournal
from frontier_harness.core.kernel import IntelligenceKernel
from frontier_harness.core.types import (
    ChallengeVerdict,
    ComputeEnvelope,
    ComputeUsage,
    ContentRef,
    Move,
    MoveMode,
    MoveStatus,
    ObservationKind,
    RunResumed,
    RunState,
    RunStatus,
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
    def __init__(self, outcomes: list[MoveExecutionResult]) -> None:
        self.outcomes = deque(outcomes)
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
        return self.outcomes.popleft()


def kernel_for(
    tmp_path: Path,
    outcomes: list[MoveExecutionResult],
    *,
    envelope: ComputeEnvelope | None = None,
) -> tuple[IntelligenceKernel, ScriptedRunner]:
    runner = ScriptedRunner(outcomes)
    blobs = BlobStore(tmp_path / "blobs")
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
                ),
                ObservationDraft(
                    kind=ObservationKind.CHALLENGE,
                    summary="The executable acceptance check fails",
                    source="artifact-check",
                    challenge_verdict=ChallengeVerdict.CHALLENGES,
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
                ),
                ObservationDraft(
                    kind=ObservationKind.CHALLENGE,
                    summary="An earlier evidence label was imprecise but output is correct",
                    source="fresh-challenger",
                    challenge_verdict=ChallengeVerdict.CHALLENGES,
                    metadata={"material_to_claim": False},
                ),
            ]
        ),
    ]
    kernel, _ = kernel_for(tmp_path, outcomes)

    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    assert any(
        item.metadata.get("material_to_claim") is False
        for item in kernel.state.observations.values()
    )


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


async def test_first_root_artifact_is_falsified_on_its_exact_fork_before_scale(
    tmp_path: Path,
) -> None:
    text = "# Representative artifact"
    encoded = text.encode()
    candidate = ArtifactDraft(
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
            artifact=candidate,
            workspace=workspace("Representative"),
            next_move=MoveDirective(
                mode=MoveMode.LEAD,
                intent="Scale the untested premise",
            ),
        ),
        MoveExecutionResult(
            observations=[
                ObservationDraft(
                    kind=ObservationKind.CHALLENGE,
                    summary="A matched counterexample falsifies the first premise",
                    source="fresh-challenger",
                    artifact_digest=candidate.content_ref.digest,
                    challenge_verdict=ChallengeVerdict.CHALLENGES,
                )
            ]
        ),
        MoveExecutionResult(workspace=workspace("Revised from the counterexample")),
    ]
    kernel, runner = kernel_for(tmp_path, outcomes)

    await kernel.run(max_steps=4)

    assert [mode for mode, _, _ in runner.calls] == [
        MoveMode.LEAD,
        MoveMode.CHALLENGE,
        MoveMode.LEAD,
    ]
    root_id = kernel.state.root_trajectory_id
    fork = next(
        item
        for item in kernel.state.trajectories.values()
        if item.parent_trajectory_id == root_id
    )
    first_move = next(item for item in kernel.state.moves.values() if item.mode == MoveMode.LEAD)
    first_workspace = kernel.state.workspaces[first_move.workspace_id or ""]
    assert fork.base_workspace_id == first_workspace.workspace_id
    assert runner.calls[1][1].artifact_heads[0].digest == candidate.content_ref.digest
    assert any(
        "matched counterexample" in item.summary
        for item in runner.calls[2][1].observations
    )
    assert all(item.intent != "Scale the untested premise" for item in kernel.state.moves.values())


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


async def test_completion_requires_direct_support_for_every_claimed_head(
    tmp_path: Path,
) -> None:
    class MultiHeadRunner:
        def __init__(self) -> None:
            self.calls: list[MoveMode] = []
            self.challenge_count = 0
            self.root_reentered = False

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
            if move.mode == MoveMode.CHALLENGE and state.finish_claim is None:
                root = state.trajectories[state.root_trajectory_id]
                assert root.artifact_head_id is not None
                return MoveExecutionResult(
                    observations=[
                        ObservationDraft(
                            kind=ObservationKind.CHALLENGE,
                            summary="The representative root survives early falsification",
                            source="fresh-challenger",
                            artifact_digest=state.artifacts[root.artifact_head_id].digest,
                            challenge_verdict=ChallengeVerdict.SUPPORTS,
                        )
                    ]
                )
            if move.mode == MoveMode.LEAD and move.trajectory_id == state.root_trajectory_id:
                if not self.root_reentered:
                    self.root_reentered = True
                    return MoveExecutionResult(
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
                        ]
                    )
                heads = list(state.artifacts)
                return MoveExecutionResult(
                    workspace=WorkspaceDraft(
                        document="# Integrated",
                        summary="Integrated",
                        artifact_head_ids=heads,
                    ),
                    finish=FinishDraft(
                        satisfaction_claims=["Both claimed heads satisfy their scope"],
                        artifact_head_ids=heads,
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
                    )
                ]
            )

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
    assert runner.challenge_count == 2
    assert runner.calls == [
        MoveMode.LEAD,
        MoveMode.CHALLENGE,
        MoveMode.LEAD,
        MoveMode.LEAD,
        MoveMode.LEAD,
        MoveMode.CHALLENGE,
        MoveMode.CHALLENGE,
    ]


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


async def test_failed_move_pauses_and_preserves_the_original_move(
    tmp_path: Path,
) -> None:
    kernel, _runner = kernel_for(
        tmp_path,
        [
            MoveExecutionResult(
                success=False,
                error="ProviderError: transport unavailable",
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
            MoveExecutionResult(success=False, error="ProviderError: transport unavailable"),
            MoveExecutionResult(success=False, error="ProviderError: transport unavailable"),
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
