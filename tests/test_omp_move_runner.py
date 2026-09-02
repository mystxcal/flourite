from __future__ import annotations

import json
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
from frontier_harness.core.types import (
    AssayStatus,
    ComputeEnvelope,
    FailureDomain,
    RunResumed,
    RunStatus,
)
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
                "decision_boundary": "Whether the candidate satisfies the objective",
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
            evidence = request.cwd / "challenge-assay.txt"
            evidence.write_text("direct inspection passed\n", encoding="utf-8")
            value = {
                "artifact_changed": False,
                "assay": {"status": "valid", "coverage": "whole artifact"},
                "observations": [
                    {
                        "kind": "challenge",
                        "summary": "Direct inspection supports the completion claim",
                        "verdict": "supports",
                        "covered_claims": ["The artifact directly satisfies the objective"],
                        "evidence_path": "challenge-assay.txt",
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


class IncompleteClaimCoverageProvider(FakeOmpProvider):
    async def run(self, request: ProviderCallRequest[Any]) -> ProviderCallResult[Any]:
        result = await super().run(request)
        if request.call_kind != "challenge":
            return result
        value = result.response.model_dump(mode="python")
        value["observations"][0]["covered_claims"] = []
        return result.model_copy(
            update={"response": request.response_model.model_validate(value)}
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


class UnrepairableBoundaryProvider(FakeOmpProvider):
    async def run(self, request: ProviderCallRequest[Any]) -> ProviderCallResult[Any]:
        result = await super().run(request)
        if request.call_kind != "lead":
            return result
        value = result.response.model_dump(mode="python")
        value["observations"][0]["evidence_path"] = "/outside-the-live-workspace"
        return result.model_copy(
            update={"response": request.response_model.model_validate(value)}
        )


class EphemeralChallengeRepairProvider(FakeOmpProvider):
    async def run(self, request: ProviderCallRequest[Any]) -> ProviderCallResult[Any]:
        result = await super().run(request)
        if (
            request.call_kind == "challenge"
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


class AssayHandshakeProvider(FakeOmpProvider):
    def __init__(self) -> None:
        super().__init__()
        self.challenge_calls = 0

    async def run(self, request: ProviderCallRequest[Any]) -> ProviderCallResult[Any]:
        if request.call_kind != "challenge":
            return await super().run(request)
        self.requests.append(request)
        self.challenge_calls += 1
        if self.challenge_calls == 1:
            index = json.loads(
                (request.cwd / ".sfh_context" / "index.json").read_text(encoding="utf-8")
            )
            (request.cwd / index["artifact_heads"][0]["local_path"]).unlink()
            value = {
                "artifact_changed": False,
                "assay": {
                    "status": "invalid",
                    "reason": "the target path was copied incorrectly",
                    "missing_material": [".sfh_context/artifacts/target.md"],
                },
                "observations": [],
            }
        else:
            index = json.loads(
                (request.cwd / ".sfh_context" / "index.json").read_text(encoding="utf-8")
            )
            assert (request.cwd / index["artifact_heads"][0]["local_path"]).is_file()
            value = {
                "artifact_changed": False,
                "assay": {
                    "status": "valid",
                    "coverage": "read the complete artifact from the relative manifest path",
                },
                "observations": [
                    {
                        "kind": "challenge",
                        "summary": "Direct whole-artifact inspection supports the claim",
                        "verdict": "supports",
                        "covered_claims": ["The artifact directly satisfies the objective"],
                    }
                ],
                "quality_delta": (
                    ["A complete artifact must remain understandable without hidden context."]
                    if self.challenge_calls == 2
                    else []
                ),
            }
        return ProviderCallResult(
            call_id=request.call_id,
            response=request.response_model.model_validate(value),
            usage=Usage(calls=1, model_requests=1, input_tokens=10, output_tokens=5),
            duration_seconds=0.01,
            thread_id=None,
            trace_summary=ProviderTraceSummary(model_turns=1),
        )


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
                "decision_boundary": "Whether the representative premise survives challenge",
                "observations": [],
                "next_move": {
                    "mode": "challenge",
                    "intent": "Challenge the live candidate before claiming completion",
                },
            }
            thread_id = "lead-thread"
        else:
            evidence = request.cwd / "challenge-assay.txt"
            evidence.write_text("direct inspection passed\n", encoding="utf-8")
            index = json.loads(
                (request.cwd / ".sfh_context" / "index.json").read_text(encoding="utf-8")
            )
            artifact_digest = index["artifact_heads"][0]["digest"]
            value = {
                "artifact_changed": False,
                "assay": {"status": "valid", "coverage": "whole artifact"},
                "observations": [
                    {
                        "kind": "challenge",
                        "summary": "A file-level inspection found a material defect",
                        "verdict": "challenges",
                        "artifact_digest": artifact_digest,
                        "covered_claims": ["A provisional objective criterion"],
                        "evidence_path": "challenge-assay.txt",
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
                "decision_boundary": f"Decision boundary {lead_call}",
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
            evidence = request.cwd / "challenge-assay.txt"
            evidence.write_text("direct inspection passed\n", encoding="utf-8")
            value = {
                "artifact_changed": False,
                "assay": {"status": "valid", "coverage": "whole artifact"},
                "observations": [
                    {
                        "kind": "challenge",
                        "summary": "Direct inspection supports the reconstructed artifact",
                        "verdict": "supports",
                        "covered_claims": [
                            "The reconstructed Lead completed the work"
                        ],
                        "evidence_path": "challenge-assay.txt",
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
                    "decision_boundary": "Whether the first epoch is complete",
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
                    "decision_boundary": "Whether the second epoch is complete",
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
            evidence = request.cwd / "challenge-assay.txt"
            evidence.write_text("direct projection passed\n", encoding="utf-8")
            value = {
                "artifact_changed": False,
                "assay": {"status": "valid", "coverage": "whole artifact"},
                "observations": [
                    {
                        "kind": "challenge",
                        "summary": (
                            "The representative film survives independent projection"
                            if self.lead_calls == 1
                            else "The exact durable film survives independent projection"
                        ),
                        "verdict": "supports",
                        "covered_claims": ["The durable film is complete"],
                        "artifact_digest": json.loads(
                            (request.cwd / ".sfh_context" / "index.json").read_text(
                                encoding="utf-8"
                            )
                        )["artifact_heads"][0]["digest"],
                        "evidence_path": "challenge-assay.txt",
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
            evidence = request.cwd / "challenge-assay.txt"
            evidence.write_text("fork inspection passed\n", encoding="utf-8")
            value = {
                "artifact_changed": False,
                "assay": {"status": "valid", "coverage": "whole artifact"},
                "observations": [
                    {
                        "kind": "challenge",
                        "summary": "The exact fork point survives independent inspection",
                        "verdict": "supports",
                        "evidence_path": "challenge-assay.txt",
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
                "decision_boundary": "Which fork has the stronger governing premise",
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
                "decision_boundary": "How to integrate the challenged fork evidence",
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
                "decision_boundary": "Whether the independent branch should be integrated",
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
    ]
    assert provider.requests[0].preserve_session is True
    assert provider.requests[1].preserve_session is False
    assert (tmp_path / "provider-sessions.json").is_file()


async def test_new_evidence_can_repeat_a_semantic_challenge_without_spinning(
    tmp_path: Path,
) -> None:
    blobs = BlobStore(tmp_path / "blobs")
    provider = IncompleteClaimCoverageProvider()
    runner = OmpMoveRunner(
        provider=provider,  # type: ignore[arg-type]
        adapter=MarkdownAdapter(
            profile=get_profile("generic"),
            run_dir=tmp_path,
            blobs=blobs,
            workspace=None,
        ),
        run_dir=tmp_path,
    )
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_incomplete_claims"),
            snapshot_path=tmp_path / "state.json",
        ),
        blobs=blobs,
        runner=runner,
    )
    kernel.start("Create an excellent artifact.", envelope=ComputeEnvelope(max_model_turns=6))

    await kernel.run()

    assert kernel.state.status == RunStatus.EXHAUSTED
    assert [request.call_kind for request in provider.requests] == [
        "lead",
        "challenge",
        "challenge",
    ]


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
    )
    assert support.artifact_digest == artifact.digest
    assert support.direct_inspection is True


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


async def test_repeated_invalid_external_boundary_is_not_misclassified_as_code_fault(
    tmp_path: Path,
) -> None:
    blobs = BlobStore(tmp_path / "blobs")
    provider = UnrepairableBoundaryProvider()
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_bad_boundary"),
            snapshot_path=tmp_path / "state.json",
        ),
        blobs=blobs,
        runner=OmpMoveRunner(
            provider=provider,  # type: ignore[arg-type]
            adapter=MarkdownAdapter(
                profile=get_profile("generic"),
                run_dir=tmp_path,
                blobs=blobs,
                workspace=None,
            ),
            run_dir=tmp_path,
        ),
    )
    kernel.start("Build an artifact with a valid boundary.")

    await kernel.run(max_steps=1)

    assert kernel.state.status == RunStatus.PAUSED
    assert kernel.state.failure_domain == FailureDomain.PROVIDER


async def test_ephemeral_challenge_repairs_bad_evidence_path_before_admission(
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
    assert challenge_requests[0].cwd == challenge_requests[1].cwd
    assert "AssayInvalidError" in challenge_requests[1].prompt
    challenge = next(
        item
        for item in kernel.state.observations.values()
        if item.kind.value == "challenge"
    )
    assert challenge.metadata["evidence_capture"] == "durable"


async def test_invalid_assay_repairs_same_capsule_before_any_verdict(
    tmp_path: Path,
) -> None:
    blobs = BlobStore(tmp_path / "blobs")
    adapter = MarkdownAdapter(
        profile=get_profile("generic"),
        run_dir=tmp_path,
        blobs=blobs,
        workspace=None,
    )
    provider = AssayHandshakeProvider()
    kernel = IntelligenceKernel(
        journal=KernelJournal(
            ledger=EventLedger(tmp_path / "ledger.sqlite3", "run_assay_handshake"),
            snapshot_path=tmp_path / "state.json",
        ),
        blobs=blobs,
        runner=OmpMoveRunner(
            provider=provider,  # type: ignore[arg-type]
            adapter=adapter,
            run_dir=tmp_path,
        ),
    )
    kernel.start("Build and verify an artifact.")

    await kernel.run()

    assert kernel.state.status == RunStatus.SATISFIED
    challenges = [item for item in provider.requests if item.call_kind == "challenge"]
    assert len(challenges) == 3
    assert challenges[0].cwd == challenges[1].cwd
    assert "AssayInvalidError" in challenges[1].prompt
    verdicts = [
        item
        for item in kernel.state.observations.values()
        if item.challenge_verdict is not None
    ]
    assert len(verdicts) == 2
    assert all(item.assay_status == AssayStatus.VALID for item in verdicts)
    assert any(
        item.quality_delta
        for item in kernel.state.observations.values()
    )
    assert kernel.state.current_workspace is not None
    assert kernel.state.current_workspace.quality_ref is not None
    quality = blobs.read_text(kernel.state.current_workspace.quality_ref)
    assert "understandable without hidden context" in quality
    assert kernel.state.finish_claim is not None
    assert kernel.state.finish_claim.quality_digest == kernel.state.current_workspace.quality_ref.digest


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

    assert kernel.state.status == RunStatus.ACTIVE
    assert kernel.state.finish_claim is None
    root = kernel.state.trajectories[kernel.state.root_trajectory_id]
    assert root.artifact_head_id is not None
    artifact = kernel.state.artifacts[root.artifact_head_id]
    challenge = next(
        item for item in kernel.state.observations.values() if item.kind.value == "challenge"
    )
    assert challenge.artifact_digest == artifact.digest
    assert challenge.claim_id is None
    assert challenge.covered_claims == []
    assert challenge.metadata["evidence_capture"] == "durable"
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
        "lead",
        "lead",
        "challenge",
    ]
    assert provider.requests[1].resume_thread_id == "thread-lead"
    assert provider.requests[2].resume_thread_id is None
    assert kernel.state.usage.model_turns == 9
    assert any(
        item.metadata.get("session_reconstructed") is True
        for item in kernel.state.observations.values()
    )
