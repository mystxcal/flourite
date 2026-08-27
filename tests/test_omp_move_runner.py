from __future__ import annotations

from pathlib import Path
from typing import Any

from frontier_harness.adapters.generic import MarkdownAdapter
from frontier_harness.adapters.profiles import get_profile
from frontier_harness.blobs import BlobStore
from frontier_harness.config import ProviderConfig
from frontier_harness.core.journal import KernelJournal
from frontier_harness.core.kernel import IntelligenceKernel
from frontier_harness.core.types import ComputeEnvelope, RunStatus
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
                "workspace_path": ".sfh_output/workspace.md",
                "workspace_summary": "Complete first candidate",
                "observations": [
                    {
                        "kind": "artifact",
                        "summary": "The live workspace contains the completed artifact",
                        "evidence_path": str(request.cwd),
                    },
                    {
                        "kind": "tool",
                        "summary": "The live application was inspected in a browser",
                        "evidence_path": "http://127.0.0.1:8080/",
                    },
                ],
                "finish": {
                    "satisfaction_claims": ["The artifact directly satisfies the objective"],
                    "residual_uncertainty": [],
                },
            }
            thread_id = "thread-lead"
        else:
            value = {
                "artifact_changed": False,
                "observations": [
                    {
                        "kind": "challenge",
                        "summary": "Direct inspection supports the completion claim",
                        "verdict": "supports",
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


class VanishingSessionProvider(FakeOmpProvider):
    async def run(self, request: ProviderCallRequest[Any]) -> ProviderCallResult[Any]:
        self.requests.append(request)
        call = len(self.requests)
        if call == 2:
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
                f"# Artifact from call {call}\n", encoding="utf-8"
            )
            (output / "workspace.md").write_text(
                f"# Workspace from call {call}\n", encoding="utf-8"
            )
            value: dict[str, Any] = {
                "artifact_changed": True,
                "workspace_path": ".sfh_output/workspace.md",
                "workspace_summary": f"Lead call {call}",
                "observations": [],
            }
            if call == 1:
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
            value = {
                "artifact_changed": False,
                "observations": [
                    {
                        "kind": "challenge",
                        "summary": "Direct inspection supports the reconstructed artifact",
                        "verdict": "supports",
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
    assert [request.call_kind for request in provider.requests] == ["lead", "challenge"]
    assert provider.requests[0].preserve_session is True
    assert provider.requests[1].preserve_session is False
    assert (tmp_path / "provider-sessions.json").is_file()


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
