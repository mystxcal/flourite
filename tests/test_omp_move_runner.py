from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from frontier_harness.adapters.generic import MarkdownAdapter
from frontier_harness.adapters.profiles import get_profile
from frontier_harness.adapters.software import SoftwareAdapter
from frontier_harness.blobs import BlobStore
from frontier_harness.config import ProviderConfig, SoftwarePolicy
from frontier_harness.core.journal import KernelJournal
from frontier_harness.core.kernel import IntelligenceKernel
from frontier_harness.core.types import ComputeEnvelope, RunResumed, RunStatus
from frontier_harness.errors import ProviderCallError
from frontier_harness.intelligence.omp_runner import OmpMoveRunner
from frontier_harness.ledger import EventLedger
from frontier_harness.models import Usage
from frontier_harness.providers.base import (
    ProviderCallRequest,
    ProviderCallResult,
    ProviderTraceSummary,
)


class FakeOmpProvider:
    def __init__(self) -> None:
        self.config = ProviderConfig(schema_attempts=1)
        self.requests: list[ProviderCallRequest[Any]] = []

    async def run(self, request: ProviderCallRequest[Any]) -> ProviderCallResult[Any]:
        self.requests.append(request)
        if request.call_kind == "lead":
            output = request.cwd / ".sfh_output"
            output.mkdir(parents=True, exist_ok=True)
            assert request.expected_artifact_path is not None
            request.expected_artifact_path.parent.mkdir(parents=True, exist_ok=True)
            request.expected_artifact_path.write_text("# Excellent artifact\n", encoding="utf-8")
            (output / "workspace.md").write_text(
                "# Current best\n\nThe artifact is complete.\n", encoding="utf-8"
            )
            value = {
                "artifact_changed": True,
                "workspace_summary": "Complete first candidate",
                "observations": [
                    {
                        "kind": "artifact",
                        "summary": "The live workspace contains the completed artifact",
                        "evidence_path": str(request.expected_artifact_path),
                    },
                    {
                        "kind": "tool",
                        "summary": "The live application was inspected in a browser",
                    },
                ],
                "finish": {
                    "satisfaction_claims": ["The artifact directly satisfies the objective"],
                    "residual_uncertainty": [],
                },
            }
            thread_id = "thread-lead"
        else:
            evidence = request.cwd / "promotion-assay.txt"
            evidence.write_text("direct inspection passed\n", encoding="utf-8")
            value = {
                "artifact_changed": False,
                "observations": [
                    {
                        "kind": "challenge",
                        "summary": "Direct inspection supports the completion claim",
                        "verdict": "supports",
                        "evidence_path": "promotion-assay.txt",
                    }
                ],
            }
            thread_id = None
        response = request.response_model.model_validate(value)
        return ProviderCallResult(
            call_id=request.call_id,
            response=response,
            usage=Usage(
                calls=1,
                model_requests=2,
                input_tokens=100,
                output_tokens=20,
                wall_seconds=1,
            ),
            duration_seconds=1,
            thread_id=thread_id,
            trace_summary=ProviderTraceSummary(model_turns=2),
        )


class CommitRepairProvider(FakeOmpProvider):
    async def run(self, request: ProviderCallRequest[Any]) -> ProviderCallResult[Any]:
        result = await super().run(request)
        if request.call_kind == "lead" and "-repair-" not in request.call_id:
            value = result.response.model_dump(mode="python")
            value["observations"][0]["evidence_path"] = "/outside-the-live-workspace"
            return result.model_copy(
                update={"response": request.response_model.model_validate(value)}
            )
        return result


class EphemeralChallengeRepairProvider(FakeOmpProvider):
    async def run(self, request: ProviderCallRequest[Any]) -> ProviderCallResult[Any]:
        result = await super().run(request)
        if (
            request.call_kind == "challenge"
            and "promotion gate" not in request.prompt
            and "-repair-" not in request.call_id
        ):
            value = result.response.model_dump(mode="python")
            value["observations"][0]["evidence_path"] = "/outside-the-live-workspace"
            return result.model_copy(
                update={
                    "response": request.response_model.model_validate(value),
                    # OMP can expose this even though preserve_session is false.
                    "thread_id": "ephemeral-challenge-thread",
                }
            )
        return result


class ExploratoryChallengeProvider(FakeOmpProvider):
    async def run(self, request: ProviderCallRequest[Any]) -> ProviderCallResult[Any]:
        self.requests.append(request)
        if request.call_kind == "lead":
            assert request.expected_artifact_path is not None
            request.expected_artifact_path.parent.mkdir(parents=True, exist_ok=True)
            request.expected_artifact_path.write_text("# Candidate\n", encoding="utf-8")
            output = request.cwd / ".sfh_output"
            output.mkdir(parents=True, exist_ok=True)
            (output / "workspace.md").write_text("# Candidate state\n", encoding="utf-8")
            value = {
                "artifact_changed": True,
                "workspace_summary": "Candidate awaiting an exploratory challenge",
                "observations": [],
                "next_move": {
                    "mode": "challenge",
                    "intent": "Challenge the live candidate before claiming completion",
                },
            }
            thread_id = "lead-thread"
        else:
            evidence = request.cwd / "promotion-assay.txt"
            evidence.write_text("direct inspection passed\n", encoding="utf-8")
            value = {
                "artifact_changed": False,
                "observations": [
                    {
                        "kind": "challenge",
                        "summary": "A file-level inspection found a material defect",
                        "verdict": "challenges",
                        "artifact_digest": "a" * 64,
                        "evidence_path": "/tmp/non-durable-challenge-evidence",
                    }
                ],
            }
            thread_id = None
        return ProviderCallResult(
            call_id=request.call_id,
            response=request.response_model.model_validate(value),
            usage=Usage(calls=1, model_requests=1, input_tokens=10, output_tokens=5),
            duration_seconds=0.01,
            thread_id=thread_id,
            trace_summary=ProviderTraceSummary(model_turns=1),
        )


class VanishingSessionProvider(FakeOmpProvider):
    def __init__(self) -> None:
        super().__init__()
        self.lead_calls = 0

    async def run(self, request: ProviderCallRequest[Any]) -> ProviderCallResult[Any]:
        self.requests.append(request)
        if request.call_kind == "lead":
            self.lead_calls += 1
        lead_call = self.lead_calls
        if request.call_kind == "lead" and lead_call == 2:
            assert request.resume_thread_id == "thread-lead"
            raise ProviderCallError(
                "persistent session thread not found",
                usage=Usage(
                    calls=1,
                    model_requests=3,
                    input_tokens=50,
                    output_tokens=5,
                    wall_seconds=2,
                ),
            )
        if request.call_kind == "lead":
            output = request.cwd / ".sfh_output"
            output.mkdir(parents=True, exist_ok=True)
            assert request.expected_artifact_path is not None
            request.expected_artifact_path.parent.mkdir(parents=True, exist_ok=True)
            request.expected_artifact_path.write_text(
                f"# Artifact from Lead call {lead_call}\n", encoding="utf-8"
            )
            (output / "workspace.md").write_text(
                f"# Workspace from Lead call {lead_call}\n", encoding="utf-8"
            )
            value: dict[str, Any] = {
                "artifact_changed": True,
                "workspace_summary": f"Lead call {lead_call}",
                "observations": [],
            }
            if lead_call == 1:
                value["next_move"] = {
                    "mode": "lead",
                    "intent": "Finish the construction",
                }
                thread_id = "thread-lead"
            else:
                value["finish"] = {
                    "satisfaction_claims": ["The reconstructed Lead completed the work"],
                    "residual_uncertainty": [],
                }
                thread_id = "thread-rebuilt"
        else:
            evidence = request.cwd / "promotion-assay.txt"
            evidence.write_text("direct inspection passed\n", encoding="utf-8")
            value = {
                "artifact_changed": False,
                "observations": [
                    {
                        "kind": "challenge",
                        "summary": "Direct inspection supports the reconstructed artifact",
                        "verdict": "supports",
                        "evidence_path": "promotion-assay.txt",
                    }
                ],
            }
            thread_id = None
        response = request.response_model.model_validate(value)
        return ProviderCallResult(
            call_id=request.call_id,
            response=response,
            usage=Usage(
                calls=1,
                model_requests=2,
                input_tokens=100,
                output_tokens=20,
                wall_seconds=1,
            ),
            duration_seconds=1,
            thread_id=thread_id,
            trace_summary=ProviderTraceSummary(model_turns=2),
        )


class PersistentSoftwareProvider(FakeOmpProvider):
    def __init__(self) -> None:
        super().__init__()
        self.lead_cwd: Path | None = None
        self.challenge_cwd: Path | None = None
        self.lead_calls = 0

    async def run(self, request: ProviderCallRequest[Any]) -> ProviderCallResult[Any]:
        self.requests.append(request)
        if request.call_kind == "lead":
            self.lead_calls += 1
            if self.lead_cwd is None:
                self.lead_cwd = request.cwd
            assert request.cwd == self.lead_cwd
            output = request.cwd / ".sfh_output"
            output.mkdir(parents=True, exist_ok=True)
            (output / "workspace.md").write_text(
                f"# Lead epoch {self.lead_calls}\n", encoding="utf-8"
            )
            cache = request.cwd / "build" / "expensive.cache"
            deliverable = request.cwd / "dist" / "film.mp4"
            cache.parent.mkdir(parents=True, exist_ok=True)
            deliverable.parent.mkdir(parents=True, exist_ok=True)
            if self.lead_calls == 1:
                (request.cwd / "app.txt").write_text("epoch one\n", encoding="utf-8")
                cache.write_bytes(b"expensive intermediate")
                deliverable.write_bytes(b"film one")
                value = {
                    "artifact_changed": True,
                    "workspace_summary": "first durable epoch",
                    "observations": [],
                    "next_move": {"mode": "lead", "intent": "refine the same artifact"},
                }
                thread_id = "persistent-thread"
            else:
                assert request.resume_thread_id == "persistent-thread"
                assert cache.read_bytes() == b"expensive intermediate"
                assert deliverable.read_bytes() == b"film one"
                (request.cwd / "app.txt").write_text("epoch two\n", encoding="utf-8")
                deliverable.write_bytes(b"film two")
                value = {
                    "artifact_changed": True,
                    "workspace_summary": "second durable epoch",
                    "observations": [],
                    "finish": {
                        "satisfaction_claims": ["The durable film is complete"],
                        "residual_uncertainty": [],
                    },
                }
                thread_id = "persistent-thread"
        else:
            assert self.lead_cwd is not None
            self.challenge_cwd = request.cwd
            assert request.cwd != self.lead_cwd
            expected_epoch = "epoch one\n" if self.lead_calls == 1 else "epoch two\n"
            expected_film = b"film one" if self.lead_calls == 1 else b"film two"
            assert (request.cwd / "app.txt").read_text(encoding="utf-8") == expected_epoch
            assert (request.cwd / "dist" / "film.mp4").read_bytes() == expected_film
            evidence = request.cwd / "promotion-assay.txt"
            evidence.write_text("direct projection passed\n", encoding="utf-8")
            value = {
                "artifact_changed": False,
                "observations": [
                    {
                        "kind": "challenge",
                        "summary": (
                            "The representative film survives independent projection"
                            if self.lead_calls == 1
                            else "The exact durable film survives independent projection"
                        ),
                        "verdict": "supports",
                        "artifact_digest": hashlib.sha256(expected_film).hexdigest(),
                        "evidence_path": "promotion-assay.txt",
                    }
                ],
            }
            thread_id = None
        return ProviderCallResult(
            call_id=request.call_id,
            response=request.response_model.model_validate(value),
            usage=Usage(calls=1, model_requests=1, input_tokens=10, output_tokens=5),
            duration_seconds=0.01,
            thread_id=thread_id,
            trace_summary=ProviderTraceSummary(model_turns=1),
        )


class InterruptedSoftwareProvider(PersistentSoftwareProvider):
    def __init__(self) -> None:
        super().__init__()
        self.interrupted = False

    async def run(self, request: ProviderCallRequest[Any]) -> ProviderCallResult[Any]:
        if request.call_kind == "lead" and not self.interrupted:
            self.requests.append(request)
            self.interrupted = True
            self.lead_cwd = request.cwd
            partial = request.cwd / "build" / "partial.data"
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"irreplaceable partial work")
            raise ProviderCallError(
                "synthetic transport interruption",
                usage=Usage(calls=1, model_requests=1, input_tokens=3, output_tokens=0),
            )
        return await super().run(request)


class BranchingSoftwareProvider(FakeOmpProvider):
    def __init__(self) -> None:
        super().__init__()
        self.root_cwd: Path | None = None
        self.branch_cwd: Path | None = None

    async def run(self, request: ProviderCallRequest[Any]) -> ProviderCallResult[Any]:
        self.requests.append(request)
        if request.call_kind == "challenge":
            assert self.root_cwd is not None
            assert request.cwd != self.root_cwd
            assert (request.cwd / "base.txt").read_text(encoding="utf-8") == "fork point\n"
            evidence = request.cwd / "promotion-assay.txt"
            evidence.write_text("fork inspection passed\n", encoding="utf-8")
            value = {
                "artifact_changed": False,
                "observations": [
                    {
                        "kind": "challenge",
                        "summary": "The exact fork point survives independent inspection",
                        "verdict": "supports",
                        "evidence_path": "promotion-assay.txt",
                    }
                ],
            }
            thread_id = None
            return ProviderCallResult(
                call_id=request.call_id,
                response=request.response_model.model_validate(value),
                usage=Usage(calls=1, model_requests=1, input_tokens=10, output_tokens=5),
                duration_seconds=0.01,
                thread_id=thread_id,
                trace_summary=ProviderTraceSummary(model_turns=1),
            )
        assert request.expected_artifact_path is not None
        request.expected_artifact_path.parent.mkdir(parents=True, exist_ok=True)
        (request.cwd / ".sfh_output" / "workspace.md").write_text(
            "# Current branch state\n", encoding="utf-8"
        )
        if self.root_cwd is None:
            self.root_cwd = request.cwd
            (request.cwd / "base.txt").write_text("fork point\n", encoding="utf-8")
            request.expected_artifact_path.write_text("# Root\n", encoding="utf-8")
            value = {
                "artifact_changed": True,
                "workspace_summary": "root fork point",
                "observations": [],
                "branches": [
                    {
                        "mode": "lead",
                        "intent": "develop the independent branch",
                        "fork_purpose": "test a materially different construction",
                    }
                ],
            }
        elif request.cwd == self.root_cwd:
            value = {
                "artifact_changed": False,
                "workspace_summary": "root fork point after independent challenge",
                "observations": [],
                "branches": [
                    {
                        "mode": "lead",
                        "intent": "develop the independent branch",
                        "fork_purpose": "test a materially different construction",
                    }
                ],
            }
        else:
            self.branch_cwd = request.cwd
            assert request.cwd != self.root_cwd
            assert (request.cwd / "base.txt").read_text(encoding="utf-8") == "fork point\n"
            (request.cwd / "branch.txt").write_text("branch result\n", encoding="utf-8")
            request.expected_artifact_path.write_text("# Branch\n", encoding="utf-8")
            value = {
                "artifact_changed": True,
                "workspace_summary": "independent branch result",
                "observations": [],
            }
        return ProviderCallResult(
            call_id=request.call_id,
            response=request.response_model.model_validate(value),
            usage=Usage(calls=1, model_requests=1, input_tokens=10, output_tokens=5),
            duration_seconds=0.01,
            thread_id=f"thread-{len(self.requests)}",
            trace_summary=ProviderTraceSummary(model_turns=1),
        )


def test_generic_call_capsule_survives_controller_restart(tmp_path: Path) -> None:
    adapter = MarkdownAdapter(
        profile=get_profile("generic"),
        run_dir=tmp_path,
        blobs=BlobStore(tmp_path / "blobs"),
        workspace=None,
    )
    first = adapter.open_call(call_id="same", call_kind="lead", current_artifact=None)
    (first.cwd / "unfinished.txt").write_text("keep me\n", encoding="utf-8")

    resumed = adapter.open_call(call_id="same", call_kind="lead", current_artifact=None)

    assert resumed.metadata["resumed"] is True
    assert (resumed.cwd / "unfinished.txt").read_text() == "keep me\n"


async def test_omp_runner_connects_transport_adapter_and_kernel(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "blobs")
    adapter = MarkdownAdapter(
        profile=get_profile("generic"),
        run_dir=tmp_path,
        blobs=blobs,
        workspace=None,
    )
    provider = FakeOmpProvider()
    runner = OmpMoveRunner(
        provider=provider,  # type: ignore[arg-type]
        adapter=adapter,
        run_dir=tmp_path,
    )
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_omp"),
            snapshot_path=tmp_path / "state.json",
        ),
        blobs=blobs,
        runner=runner,
        capabilities=["read", "write", "tools"],
    )
    kernel.start("Create an excellent artifact.", envelope=ComputeEnvelope(max_model_turns=10))

    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    assert kernel.state.current_workspace is not None
    assert kernel.state.current_workspace.summary == "Complete first candidate"
    assert len(kernel.state.artifacts) == 1
    assert [request.call_kind for request in provider.requests] == [
        "lead",
        "challenge",
        "lead",
        "challenge",
    ]
    assert provider.requests[0].preserve_session is True
    assert provider.requests[1].preserve_session is False
    assert (tmp_path / "provider-sessions.json").is_file()


async def test_software_lead_keeps_one_live_workspace_and_durable_outputs(
    tmp_path: Path, git_repo: Path
) -> None:
    (git_repo / ".gitignore").write_text("build/\ndist/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(git_repo), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(git_repo), "commit", "-m", "ignore generated data"],
        check=True,
        stdout=subprocess.PIPE,
    )
    run_dir = tmp_path / "run"
    blobs = BlobStore(run_dir / "blobs")
    adapter = SoftwareAdapter(
        run_dir=run_dir,
        blobs=blobs,
        workspace=git_repo,
        policy=SoftwarePolicy(release_artifacts=["dist/*.mp4"]),
    )
    provider = PersistentSoftwareProvider()
    runner = OmpMoveRunner(
        provider=provider,  # type: ignore[arg-type]
        adapter=adapter,
        run_dir=run_dir,
    )
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(run_dir / "ledger.sqlite3", "run_software_lifecycle"),
            snapshot_path=run_dir / "state.json",
        ),
        blobs=blobs,
        runner=runner,
    )
    kernel.start("Build, refine, and verify a rendered film.")

    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    assert provider.lead_cwd is not None and provider.lead_cwd.is_dir()
    assert provider.challenge_cwd is not None and not provider.challenge_cwd.exists()
    assert (provider.lead_cwd / "build" / "expensive.cache").is_file()
    assert [request.cwd for request in provider.requests if request.call_kind == "lead"] == [
        provider.lead_cwd,
        provider.lead_cwd,
    ]
    root = kernel.state.trajectories[kernel.state.root_trajectory_id]
    artifact = kernel.state.artifacts[root.artifact_head_id or ""]
    assert [item.original_name for item in artifact.deliverables] == ["dist/film.mp4"]
    assert blobs.read_bytes(artifact.deliverables[0]) == b"film two"
    support = next(
        item
        for item in kernel.state.observations.values()
        if item.challenge_verdict is not None
        and item.metadata.get("inspected_content_digest")
        == hashlib.sha256(b"film two").hexdigest()
    )
    assert support.artifact_digest == artifact.digest
    assert support.metadata["inspected_content_digest"] == hashlib.sha256(b"film two").hexdigest()


async def test_interrupted_software_move_resumes_same_workspace_without_fake_repair(
    tmp_path: Path, git_repo: Path
) -> None:
    run_dir = tmp_path / "interrupted-run"
    blobs = BlobStore(run_dir / "blobs")
    adapter = SoftwareAdapter(
        run_dir=run_dir,
        blobs=blobs,
        workspace=git_repo,
        policy=SoftwarePolicy(),
    )
    provider = InterruptedSoftwareProvider()
    runner = OmpMoveRunner(
        provider=provider,  # type: ignore[arg-type]
        adapter=adapter,
        run_dir=run_dir,
    )
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(run_dir / "ledger.sqlite3", "run_interrupted_software"),
            snapshot_path=run_dir / "state.json",
        ),
        blobs=blobs,
        runner=runner,
    )
    kernel.start("Finish the software artifact after any transport interruption.")

    await kernel.run()

    assert kernel.state.status == RunStatus.PAUSED
    assert provider.lead_cwd is not None
    assert (provider.lead_cwd / "build" / "partial.data").read_bytes() == (
        b"irreplaceable partial work"
    )
    retry = next(item for item in kernel.state.moves.values() if item.status.value == "proposed")
    assert retry.retry_of_move_id is not None
    assert "repair" not in retry.intent.casefold()

    kernel.journal.append("run.resumed", RunResumed(reason="transport restored"))
    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    assert provider.requests[0].cwd == provider.requests[1].cwd == provider.lead_cwd


async def test_new_software_branch_inherits_the_exact_fork_artifact(
    tmp_path: Path, git_repo: Path
) -> None:
    run_dir = tmp_path / "branch-run"
    blobs = BlobStore(run_dir / "blobs")
    adapter = SoftwareAdapter(
        run_dir=run_dir,
        blobs=blobs,
        workspace=git_repo,
        policy=SoftwarePolicy(),
    )
    provider = BranchingSoftwareProvider()
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(run_dir / "ledger.sqlite3", "run_branch_software"),
            snapshot_path=run_dir / "state.json",
        ),
        blobs=blobs,
        runner=OmpMoveRunner(
            provider=provider,  # type: ignore[arg-type]
            adapter=adapter,
            run_dir=run_dir,
        ),
    )
    kernel.start("Fork a software candidate from the exact current artifact.")

    await kernel.run(max_steps=5)

    assert provider.root_cwd is not None and provider.root_cwd.is_dir()
    assert provider.branch_cwd is not None and provider.branch_cwd.is_dir()
    branch = next(
        trajectory
        for trajectory in kernel.state.trajectories.values()
        if trajectory.parent_trajectory_id is not None
        and trajectory.artifact_head_id is not None
    )
    assert branch.artifact_head_id is not None
    branch_artifact = kernel.state.artifacts[branch.artifact_head_id]
    assert len(branch_artifact.parent_artifact_ids) == 1
    parent = kernel.state.artifacts[branch_artifact.parent_artifact_ids[0]]
    assert parent.trajectory_id == kernel.state.root_trajectory_id


async def test_commit_error_is_repaired_in_the_same_live_codex_workspace(
    tmp_path: Path,
) -> None:
    blobs = BlobStore(tmp_path / "blobs")
    adapter = MarkdownAdapter(
        profile=get_profile("generic"),
        run_dir=tmp_path,
        blobs=blobs,
        workspace=None,
    )
    provider = CommitRepairProvider()
    runner = OmpMoveRunner(
        provider=provider,  # type: ignore[arg-type]
        adapter=adapter,
        run_dir=tmp_path,
    )
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_repair"),
            snapshot_path=tmp_path / "state.json",
        ),
        blobs=blobs,
        runner=runner,
    )
    kernel.start("Build and verify the artifact.", envelope=ComputeEnvelope(max_model_turns=20))

    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    repair = next(item for item in provider.requests if "-repair-" in item.call_id)
    assert repair.resume_thread_id == "thread-lead"
    assert "Exact error:" in repair.prompt


async def test_ephemeral_challenge_keeps_optional_bad_evidence_locator_without_replay(
    tmp_path: Path,
) -> None:
    blobs = BlobStore(tmp_path / "blobs")
    adapter = MarkdownAdapter(
        profile=get_profile("generic"),
        run_dir=tmp_path,
        blobs=blobs,
        workspace=None,
    )
    provider = EphemeralChallengeRepairProvider()
    runner = OmpMoveRunner(
        provider=provider,  # type: ignore[arg-type]
        adapter=adapter,
        run_dir=tmp_path,
    )
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_challenge_repair"),
            snapshot_path=tmp_path / "state.json",
        ),
        blobs=blobs,
        runner=runner,
        capabilities=["read", "write", "tools"],
    )
    kernel.start("Build and verify the artifact.", envelope=ComputeEnvelope(max_model_turns=20))

    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    challenge_requests = [item for item in provider.requests if item.call_kind == "challenge"]
    assert len(challenge_requests) == 2
    challenge = next(
        item
        for item in kernel.state.observations.values()
        if item.kind.value == "challenge"
        and item.metadata.get("evidence_capture") == "unresolved_optional_locator"
    )
    assert challenge.metadata["evidence_capture"] == "unresolved_optional_locator"


async def test_exploratory_challenge_binds_to_frozen_workspace_without_finish_claim(
    tmp_path: Path,
) -> None:
    blobs = BlobStore(tmp_path / "blobs")
    adapter = MarkdownAdapter(
        profile=get_profile("generic"),
        run_dir=tmp_path,
        blobs=blobs,
        workspace=None,
    )
    provider = ExploratoryChallengeProvider()
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_exploratory_challenge"),
            snapshot_path=tmp_path / "state.json",
        ),
        blobs=blobs,
        runner=OmpMoveRunner(
            provider=provider,  # type: ignore[arg-type]
            adapter=adapter,
            run_dir=tmp_path,
        ),
    )
    kernel.start("Build and challenge a candidate before claiming completion.")

    await kernel.run(max_steps=2)

    assert kernel.state.finish_claim is None
    root = kernel.state.trajectories[kernel.state.root_trajectory_id]
    assert root.artifact_head_id is not None
    artifact = kernel.state.artifacts[root.artifact_head_id]
    challenge = next(
        item for item in kernel.state.observations.values() if item.kind.value == "challenge"
    )
    assert challenge.artifact_digest == artifact.digest
    assert challenge.metadata["artifact_binding"] == "single_frozen_target"
    assert challenge.metadata["inspected_content_digest"] == "a" * 64
    assert challenge.metadata["evidence_capture"] == "unresolved_optional_locator"
    assert not any("-repair-" in item.call_id for item in provider.requests)


async def test_vanished_lead_session_reconstructs_from_durable_context(
    tmp_path: Path,
) -> None:
    blobs = BlobStore(tmp_path / "blobs")
    adapter = MarkdownAdapter(
        profile=get_profile("generic"),
        run_dir=tmp_path,
        blobs=blobs,
        workspace=None,
    )
    provider = VanishingSessionProvider()
    runner = OmpMoveRunner(
        provider=provider,  # type: ignore[arg-type]
        adapter=adapter,
        run_dir=tmp_path,
    )
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_reconstruct"),
            snapshot_path=tmp_path / "state.json",
        ),
        blobs=blobs,
        runner=runner,
    )
    kernel.start("Build and verify the artifact.", envelope=ComputeEnvelope(max_model_turns=20))

    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    assert [item.call_kind for item in provider.requests] == [
        "lead",
        "challenge",
        "lead",
        "lead",
        "challenge",
    ]
    assert provider.requests[2].resume_thread_id == "thread-lead"
    assert provider.requests[3].resume_thread_id is None
    assert kernel.state.usage.model_turns == 11
    assert any(
        item.metadata.get("session_reconstructed") is True
        for item in kernel.state.observations.values()
    )
