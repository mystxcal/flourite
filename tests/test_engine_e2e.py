from __future__ import annotations

import asyncio
import sqlite3
import stat
import zipfile
from pathlib import Path

import pytest

from frontier_harness.engine import FrontierEngine
from frontier_harness.errors import FrontierError, LedgerIntegrityError, ProviderCallError
from frontier_harness.exporter import export_run
from frontier_harness.models import (
    ArtifactRef,
    BootstrapOutput,
    CheckpointOutput,
    FinalOutput,
    ReleaseFinding,
    ReleaseOutput,
    RepairOutput,
    Usage,
    WorkerEnvelope,
)
from frontier_harness.providers.fake import FakeProvider


class BootstrapFailsOnce(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False
        self.recovered = False
        self.resume_thread_id: str | None = None

    async def run(self, request):  # type: ignore[no-untyped-def]
        if not self.failed:
            self.failed = True
            request.expected_artifact_path.write_text(
                "partial generic artifact\n", encoding="utf-8"
            )
            raise ProviderCallError(
                "synthetic bootstrap failure",
                usage=Usage(calls=1),
                thread_id="session-generic-partial",
            )
        if request.response_model is BootstrapOutput:
            self.recovered = (
                request.expected_artifact_path.read_text(encoding="utf-8")
                == "partial generic artifact\n"
            )
            self.resume_thread_id = request.resume_thread_id
        return await super().run(request)


class SoftwareBootstrapFailsAfterWriting(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False
        self.recovered = False
        self.resume_thread_id: str | None = None

    async def run(self, request):  # type: ignore[no-untyped-def]
        if request.response_model is BootstrapOutput and not self.failed:
            self.failed = True
            (request.cwd / "app.txt").write_text("base\npartial-bootstrap\n", encoding="utf-8")
            raw_events = request.output_path.parent / "provider-events.jsonl"
            raw_events.write_text(
                '{"attempt":1,"event":{"type":"session","id":"session-partial"}}\n',
                encoding="utf-8",
            )
            raise ProviderCallError(
                "synthetic interrupted bootstrap",
                usage=Usage(calls=1),
                raw_events_path=raw_events,
            )
        if request.response_model is BootstrapOutput:
            self.recovered = (request.cwd / "app.txt").read_text(
                encoding="utf-8"
            ) == "base\npartial-bootstrap\n"
            self.resume_thread_id = request.resume_thread_id
        return await super().run(request)


class CheckpointFails(FakeProvider):
    async def run(self, request):  # type: ignore[no-untyped-def]
        if request.response_model is CheckpointOutput:
            raise ProviderCallError("synthetic checkpoint failure", usage=Usage(calls=1))
        return await super().run(request)


class WorkerFails(FakeProvider):
    async def run(self, request):  # type: ignore[no-untyped-def]
        if request.response_model is WorkerEnvelope:
            raise ProviderCallError("synthetic worker failure", usage=Usage(calls=1))
        return await super().run(request)


class SoftwareWorkerFailsAfterWriting(FakeProvider):
    async def run(self, request):  # type: ignore[no-untyped-def]
        if request.response_model is WorkerEnvelope:
            (request.cwd / "app.txt").write_text("base\npartial-worker\n", encoding="utf-8")
            raise ProviderCallError(
                "synthetic interrupted worker",
                usage=Usage(calls=1),
                thread_id="session-worker-partial",
            )
        return await super().run(request)


class WorkerBlocks(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.worker_started = asyncio.Event()

    async def run(self, request):  # type: ignore[no-untyped-def]
        if request.response_model is WorkerEnvelope:
            self.worker_started.set()
            await asyncio.Event().wait()
        return await super().run(request)


def test_clean_fresh_release_can_clear_only_model_semantic_false_positives() -> None:
    release = ReleaseOutput(
        releaseable=True,
        requires_repair=False,
        task_fidelity_passed=True,
        completion_case_valid=True,
        strongest_alternative_addressed=True,
    )
    assert FrontierEngine._release_can_adjudicate_model_semantic_findings(
        semantic_ci_passed=False,
        completion_gaps=[],
        deterministic_failures=[],
        checks=[],
        release=release,
    )
    assert not FrontierEngine._release_can_adjudicate_model_semantic_findings(
        semantic_ci_passed=False,
        completion_gaps=[],
        deterministic_failures=["lost public API"],
        checks=[],
        release=release,
    )
    assert not FrontierEngine._release_can_adjudicate_model_semantic_findings(
        semantic_ci_passed=False,
        completion_gaps=[],
        deterministic_failures=None,
        checks=[],
        release=release,
    )


def test_fake_end_to_end_resume_verify_and_export(tmp_path: Path, fake_config) -> None:
    source = tmp_path / "requirements.md"
    source.write_text("Preserve the hard requirement.\n", encoding="utf-8")
    config = fake_config()
    engine = FrontierEngine.create(
        "Produce a rigorous example answer.",
        config=config,
        sources=[source],
    )
    run_dir = engine.run_dir
    try:
        output = asyncio.run(engine.execute())
        assert output.exists()
        text = output.read_text(encoding="utf-8")
        assert "Baseline artifact" in text
        assert "Evidence-backed refinement" in text
        assert "Final synthesis" in text
        assert engine.state.phase.value == "complete"
        assert engine.state.usage.calls == 5
        assert len(engine.state.probes) == 1
        assert engine.state.release is not None
        assert engine.state.release.releaseable
        report = engine.verify_integrity()
        assert report["sealed"] is True
        assert report["verified_blob_count"] >= 10

        diagnostic = export_run(engine, tmp_path / "diagnostic.zip", mode="diagnostic")
        audit = export_run(engine, tmp_path / "audit.zip", mode="audit")
        with zipfile.ZipFile(diagnostic) as bundle:
            names = bundle.namelist()
            assert any(name.endswith("events.jsonl") for name in names)
            assert any(name.endswith("final.md") for name in names)
            assert not any(name.endswith("ledger.sqlite3") for name in names)
        with zipfile.ZipFile(audit) as bundle:
            names = bundle.namelist()
            assert any(name.endswith("ledger.sqlite3") for name in names)
            assert any("blobs/sha256/" in name for name in names)
    finally:
        engine.close()

    loaded = FrontierEngine.load(run_dir)
    try:
        before = loaded.ledger.count()
        second = tmp_path / "second.md"
        returned = asyncio.run(loaded.execute(output_path=second))
        assert returned == second.resolve()
        assert second.read_bytes() == output.read_bytes()
        assert loaded.ledger.count() == before
    finally:
        loaded.close()


def test_completion_seal_detects_tail_deletion(tmp_path: Path, fake_config) -> None:
    engine = FrontierEngine.create("Run to completion.", config=fake_config())
    run_dir = engine.run_dir
    try:
        asyncio.run(engine.execute())
    finally:
        engine.close()

    ledger_path = run_dir / "ledger.sqlite3"
    connection = sqlite3.connect(ledger_path)
    try:
        connection.execute("DROP TRIGGER events_no_delete")
        connection.execute("DELETE FROM events WHERE seq=(SELECT MAX(seq) FROM events)")
        connection.commit()
    finally:
        connection.close()

    loaded = FrontierEngine.load(run_dir)
    try:
        with pytest.raises(LedgerIntegrityError, match="Completion seal"):
            loaded.verify_integrity()
    finally:
        loaded.close()


def test_bootstrap_failure_is_resumable(tmp_path: Path, fake_config) -> None:
    provider = BootstrapFailsOnce()
    engine = FrontierEngine.create("Recover this run.", config=fake_config(), provider=provider)
    run_dir = engine.run_dir
    try:
        with pytest.raises(ProviderCallError):
            asyncio.run(engine.execute())
        assert engine.state.phase.value == "created"
        assert "bootstrap_error" in engine.state.metadata
        assert engine.state.metadata["bootstrap_recovery_artifact"]["kind"] == "markdown"
        assert engine.state.metadata["bootstrap_recovery_thread_id"] == "session-generic-partial"
    finally:
        engine.close()

    resumed = FrontierEngine.load(run_dir, provider=provider)
    try:
        output = asyncio.run(resumed.execute())
        assert output.exists()
        assert provider.recovered is True
        assert provider.resume_thread_id == "session-generic-partial"
        assert resumed.state.phase.value == "complete"
        assert resumed.state.usage.calls == 6
    finally:
        resumed.close()


def test_software_bootstrap_failure_preserves_patch_and_provider_session(
    tmp_path: Path, fake_config, git_repo: Path
) -> None:
    provider = SoftwareBootstrapFailsAfterWriting()
    config = fake_config(software={"apply_final_patch": False, "checks": []})
    engine = FrontierEngine.create(
        "Recover partial software work.",
        config=config,
        adapter_name="software",
        workspace=git_repo,
        provider=provider,
    )
    run_dir = engine.run_dir
    try:
        with pytest.raises(ProviderCallError):
            asyncio.run(engine.execute())
        assert engine.state.phase.value == "created"
        assert engine.state.metadata["bootstrap_recovery_artifact"]["kind"] == "git-patch"
        assert "bootstrap_recovery_thread_id" not in engine.state.metadata
    finally:
        engine.close()

    resumed = FrontierEngine.load(run_dir, provider=provider)
    try:
        output = asyncio.run(resumed.execute())
        assert output.exists()
        assert provider.recovered is True
        assert provider.resume_thread_id == "session-partial"
        assert resumed.state.phase.value == "complete"
        assert "bootstrap_recovery_artifact" not in resumed.state.metadata
    finally:
        resumed.close()


def test_worker_failure_is_retained_and_run_can_integrate(tmp_path: Path, fake_config) -> None:
    engine = FrontierEngine.create(
        "Retain failed worker evidence.",
        config=fake_config(),
        provider=WorkerFails(),
    )
    try:
        output = asyncio.run(engine.execute())
        assert output.exists()
        failures = [record for record in engine.state.actions.values() if record.error]
        assert len(failures) == 1
        assert "synthetic worker failure" in (failures[0].error or "")
        assert failures[0].receipt is not None
        assert failures[0].receipt.integration_status == "failed"
        assert failures[0].receipt.evidence_strength == "none"
        assert engine.state.phase.value == "complete"
    finally:
        engine.close()


def test_failed_worker_preserves_its_workspace_patch(
    tmp_path: Path, fake_config, git_repo: Path
) -> None:
    engine = FrontierEngine.create(
        "Retain interrupted worker changes.",
        config=fake_config(software={"apply_final_patch": False, "checks": []}),
        adapter_name="software",
        workspace=git_repo,
        provider=SoftwareWorkerFailsAfterWriting(),
    )
    try:
        output = asyncio.run(engine.execute())
        assert output.exists()
        failed = [
            event
            for event in engine.events()
            if event.event_type == "action.failed"
            and event.payload.get("error", "").endswith("synthetic interrupted worker")
        ]
        assert len(failed) == 1
        recovery = ArtifactRef.model_validate(failed[0].payload["recovery_artifact"])
        patch = engine.blobs.read_text(recovery.blob)
        assert "partial-worker" in patch
        assert failed[0].payload["provider_thread_id"] == "session-worker-partial"
    finally:
        engine.close()


def test_cancellation_is_durable_and_resume_continues_from_checkpoint(
    tmp_path: Path, fake_config
) -> None:
    provider = WorkerBlocks()
    engine = FrontierEngine.create(
        "Resume safely after a process interruption.",
        config=fake_config(),
        provider=provider,
    )
    run_dir = engine.run_dir

    async def interrupt() -> None:
        task = asyncio.create_task(engine.execute())
        await asyncio.wait_for(provider.worker_started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(interrupt())
        failed = [
            record for record in engine.state.actions.values() if record.status.value == "failed"
        ]
        assert len(failed) == 1
        assert failed[0].error == "worker cancelled before durable completion"
        assert engine.state.phase.value == "active"
    finally:
        engine.close()

    resumed = FrontierEngine.load(run_dir, provider=FakeProvider())
    try:
        output = asyncio.run(resumed.execute())
        assert output.is_file()
        assert resumed.state.phase.value == "complete"
        assert len(resumed.state.actions) == 1
        assert resumed.verify_integrity()["sealed"] is True
    finally:
        resumed.close()


def test_checkpoint_failure_promotes_current_artifact(tmp_path: Path, fake_config) -> None:
    engine = FrontierEngine.create(
        "Survive a checkpoint failure.",
        config=fake_config(),
        provider=CheckpointFails(),
    )
    try:
        output = asyncio.run(engine.execute())
        assert output.exists()
        assert "checkpoint_error" in engine.state.metadata
        assert engine.state.phase.value == "complete"
    finally:
        engine.close()


class SoftwarePatchProvider(FakeProvider):
    """Fake semantic responses plus a real isolated source-tree change."""

    async def run(self, request):  # type: ignore[no-untyped-def]
        result = await super().run(request)
        if request.response_model in {
            BootstrapOutput,
            CheckpointOutput,
            FinalOutput,
            RepairOutput,
        }:
            target = request.cwd / "app.txt"
            target.write_text("base\nmodel-change\n", encoding="utf-8")
        return result


class ReleaseFailsForSoftware(SoftwarePatchProvider):
    async def run(self, request):  # type: ignore[no-untyped-def]
        if request.response_model is ReleaseOutput:
            raise ProviderCallError(
                "synthetic release failure",
                usage=Usage(calls=1),
            )
        return await super().run(request)


def test_required_release_failure_blocks_automatic_and_explicit_apply(
    tmp_path: Path,
    fake_config,
    git_repo: Path,
) -> None:
    config = fake_config(
        software={"apply_final_patch": True, "checks": []},
    )
    engine = FrontierEngine.create(
        "Change the software safely.",
        config=config,
        adapter_name="software",
        workspace=git_repo,
        provider=ReleaseFailsForSoftware(),
    )
    try:
        patch = asyncio.run(engine.execute())
        assert patch.exists()
        assert "model-change" in patch.read_text(encoding="utf-8")
        assert (git_repo / "app.txt").read_text(encoding="utf-8") == "base\n"
        assert engine.state.metadata["release_required"] is True
        assert engine.state.metadata["release_gate_succeeded"] is False
        assert engine.state.metadata["mutation_gate_passed"] is False
        assert "did not complete" in engine.state.metadata["mutation_gate_block_reason"]
        with pytest.raises(FrontierError, match="Refusing to apply final patch"):
            engine.apply_final_patch()
    finally:
        engine.close()


def test_failed_deterministic_check_blocks_apply_even_without_release_gate(
    tmp_path: Path,
    fake_config,
    git_repo: Path,
) -> None:
    config = fake_config(
        run={"release_gate": "never"},
        software={
            "apply_final_patch": True,
            "checks": ['python -c "raise SystemExit(7)"'],
        },
    )
    engine = FrontierEngine.create(
        "Change the software but require the configured check.",
        config=config,
        adapter_name="software",
        workspace=git_repo,
        provider=SoftwarePatchProvider(),
    )
    try:
        patch = asyncio.run(engine.execute())
        assert patch.exists()
        assert (git_repo / "app.txt").read_text(encoding="utf-8") == "base\n"
        assert engine.state.metadata["release_required"] is False
        assert engine.state.metadata["deterministic_checks_passed"] is False
        assert engine.state.metadata["mutation_gate_passed"] is False
        assert "deterministic release checks failed" in engine.state.stop_reason
        with pytest.raises(FrontierError, match="deterministic release checks failed"):
            engine.apply_final_patch()
    finally:
        engine.close()


def test_semantic_failure_blocks_mutation_even_when_release_gate_is_disabled(
    tmp_path: Path, fake_config
) -> None:
    engine = FrontierEngine.create(
        "Do not mutate through a failed semantic gate.",
        config=fake_config(run={"release_gate": "never"}),
    )
    try:
        engine.state.metadata["semantic_ci_passed"] = False
        engine.state.metadata["semantic_ci_gaps"] = ["missing obligation evidence"]
        decision = engine._evaluate_mutation_gate(
            checks=[],
            release_required=False,
            release=None,
            repair_completed=False,
        )
        assert decision.mutation_gate_passed is False
        assert decision.block_reason == "semantic regression checks did not pass"
    finally:
        engine.close()


def test_diagnostic_export_redacts_artifact_content_but_audit_is_lossless(
    tmp_path: Path,
    fake_config,
) -> None:
    secret = "sk-proj-abcdefghijklmnopqrstuvwx"
    engine = FrontierEngine.create(
        f"Explain this task without leaking {secret}.",
        config=fake_config(),
    )
    try:
        asyncio.run(engine.execute())
        diagnostic = export_run(engine, tmp_path / "redacted.zip", mode="diagnostic")
        audit = export_run(engine, tmp_path / "lossless.zip", mode="audit")

        with zipfile.ZipFile(diagnostic) as bundle:
            diagnostic_text = "\n".join(
                bundle.read(name).decode("utf-8", errors="ignore")
                for name in bundle.namelist()
                if not name.endswith("ledger.sqlite3")
            )
        assert secret not in diagnostic_text
        assert "[REDACTED_OPENAI_KEY]" in diagnostic_text

        with zipfile.ZipFile(audit) as bundle:
            audit_text = "\n".join(
                bundle.read(name).decode("utf-8", errors="ignore")
                for name in bundle.namelist()
                if name.endswith((".md", ".json", ".jsonl"))
            )
        assert secret in audit_text
    finally:
        engine.close()


class NonReleaseableRepairProvider(SoftwarePatchProvider):
    def __init__(self) -> None:
        super().__init__()
        self.release_calls = 0

    async def run(self, request):  # type: ignore[no-untyped-def]
        result = await super().run(request)
        if request.response_model is RepairOutput:
            (request.cwd / "app.txt").write_text(
                "base\nmodel-change\nrelease-repair\n", encoding="utf-8"
            )
        if request.response_model is ReleaseOutput:
            self.release_calls += 1
            if self.release_calls > 1:
                return result
            release = ReleaseOutput(
                findings=[
                    ReleaseFinding(
                        severity="high",
                        title="Synthetic blocking defect",
                        explanation="The test requires one bounded repair.",
                        repair_instruction="Repair the isolated source artifact.",
                    )
                ],
                requires_repair=False,
                releaseable=False,
                rationale="Not releaseable before repair.",
            )
            request.output_path.write_text(release.model_dump_json(), encoding="utf-8")
            return result.model_copy(update={"response": release})
        return result


def test_repaired_artifact_requires_a_fresh_release_verdict_before_apply(
    tmp_path: Path,
    fake_config,
    git_repo: Path,
) -> None:
    config = fake_config(
        run={
            "budget": {
                "max_rounds": 3,
                "max_calls": 12,
                "max_parallel": 2,
                "synthesis_reserve_calls": 4,
            }
        },
        software={"apply_final_patch": True, "checks": []},
    )
    provider = NonReleaseableRepairProvider()
    engine = FrontierEngine.create(
        "Repair a blocking release finding before applying.",
        config=config,
        adapter_name="software",
        workspace=git_repo,
        provider=provider,
    )
    try:
        asyncio.run(engine.execute())
        assert (git_repo / "app.txt").read_text(encoding="utf-8") == (
            "base\nmodel-change\nrelease-repair\n"
        )
        assert provider.release_calls == 2
        assert engine.state.release is not None
        assert engine.state.release.artifact_digest == engine.state.final_artifact.blob.digest
        assert engine.state.metadata["release_report_releaseable"] is True
        assert engine.state.metadata["repair_completed"] is True
        assert engine.state.metadata["release_gate_passed"] is True
        assert engine.state.metadata["mutation_gate_passed"] is True
        assert engine.state.metadata["apply_result"]["applied"] is True
    finally:
        engine.close()


class RepeatingReleaseDefectProvider(SoftwarePatchProvider):
    def __init__(self) -> None:
        super().__init__()
        self.release_calls = 0
        self.repair_calls = 0

    async def run(self, request):  # type: ignore[no-untyped-def]
        result = await super().run(request)
        if request.response_model is RepairOutput:
            self.repair_calls += 1
            (request.cwd / "app.txt").write_text(
                f"base\nmodel-change\nrepair-{self.repair_calls}\n", encoding="utf-8"
            )
        if request.response_model is ReleaseOutput:
            self.release_calls += 1
            release = ReleaseOutput(
                findings=[
                    ReleaseFinding(
                        severity="high",
                        title="Persistent structural defect",
                        explanation="The same material defect remains.",
                        repair_instruction="Correct the structural defect.",
                    )
                ],
                requires_repair=True,
                releaseable=False,
            )
            request.output_path.write_text(release.model_dump_json(), encoding="utf-8")
            return result.model_copy(update={"response": release})
        return result


def test_repair_loop_stops_when_fresh_challenge_repeats_same_defect(
    tmp_path: Path,
    fake_config,
    git_repo: Path,
) -> None:
    provider = RepeatingReleaseDefectProvider()
    engine = FrontierEngine.create(
        "Do not churn on an unchanged release diagnosis.",
        config=fake_config(
            run={
                "budget": {
                    "max_rounds": 3,
                    "max_calls": 16,
                    "max_parallel": 2,
                    "synthesis_reserve_calls": 4,
                }
            },
            cognition={"max_material_repairs": 3},
            software={"apply_final_patch": True, "checks": []},
        ),
        adapter_name="software",
        workspace=git_repo,
        provider=provider,
    )
    try:
        asyncio.run(engine.execute())

        assert provider.repair_calls == 1
        assert provider.release_calls == 2
        assert len(engine.state.metadata["release_rejection_fingerprints"]) == 2
        assert engine.state.metadata["repair_loop_stop"]["reason"] == (
            "fresh challenge repeated the same blocking findings"
        )
        assert engine.state.metadata["mutation_gate_passed"] is False
        assert (git_repo / "app.txt").read_text(encoding="utf-8") == "base\n"
    finally:
        engine.close()


class TwoStageReleaseDefectProvider(SoftwarePatchProvider):
    def __init__(self) -> None:
        super().__init__()
        self.release_calls = 0
        self.repair_calls = 0

    async def run(self, request):  # type: ignore[no-untyped-def]
        result = await super().run(request)
        if request.response_model is RepairOutput:
            self.repair_calls += 1
            (request.cwd / "app.txt").write_text(
                f"base\nmodel-change\nrepair-{self.repair_calls}\n", encoding="utf-8"
            )
        if request.response_model is ReleaseOutput:
            self.release_calls += 1
            if self.release_calls <= 2:
                defect = "contract" if self.release_calls == 1 else "boundary"
                release = ReleaseOutput(
                    findings=[
                        ReleaseFinding(
                            severity="high",
                            title=f"{defect.title()} defect",
                            explanation=f"The {defect} remains unresolved.",
                            repair_instruction=f"Correct the {defect}.",
                        )
                    ],
                    requires_repair=True,
                    releaseable=False,
                )
                request.output_path.write_text(release.model_dump_json(), encoding="utf-8")
                return result.model_copy(update={"response": release})
        return result


def test_distinct_material_findings_can_earn_multiple_repairs(
    tmp_path: Path,
    fake_config,
    git_repo: Path,
) -> None:
    provider = TwoStageReleaseDefectProvider()
    engine = FrontierEngine.create(
        "Repair distinct material defects until a fresh challenger passes.",
        config=fake_config(
            run={
                "budget": {
                    "max_rounds": 3,
                    "max_calls": 18,
                    "max_parallel": 2,
                    "synthesis_reserve_calls": 4,
                }
            },
            cognition={"max_material_repairs": 3},
            software={"apply_final_patch": True, "checks": []},
        ),
        adapter_name="software",
        workspace=git_repo,
        provider=provider,
    )
    try:
        asyncio.run(engine.execute())

        assert provider.repair_calls == 2
        assert provider.release_calls == 3
        assert engine.state.metadata["repair_count"] == 2
        assert engine.state.metadata["mutation_gate_passed"] is True
        assert "repair-2" in (git_repo / "app.txt").read_text(encoding="utf-8")
    finally:
        engine.close()


def test_failed_creation_removes_partial_run_directory(
    tmp_path: Path,
    fake_config,
) -> None:
    source = tmp_path / "oversized.txt"
    source.write_text("x" * 64, encoding="utf-8")
    run_root = tmp_path / "runs"
    config = fake_config(
        run={"run_root": str(run_root), "max_attachment_bytes": 8},
    )

    with pytest.raises(ValueError, match="configured limit"):
        FrontierEngine.create(
            "This creation must fail atomically.",
            config=config,
            sources=[source],
        )

    assert run_root.exists()
    assert list(run_root.iterdir()) == []


def test_audit_export_preserves_symlink_without_reading_external_target(
    tmp_path: Path,
    fake_config,
) -> None:
    secret = "external-content-must-not-be-followed"
    external = tmp_path / "outside.txt"
    external.write_text(secret, encoding="utf-8")
    engine = FrontierEngine.create("Export safely.", config=fake_config())
    try:
        asyncio.run(engine.execute())
        link = engine.run_dir / "capsules" / "external-link"
        try:
            link.symlink_to(external)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation is unavailable on this platform")

        audit = export_run(engine, tmp_path / "symlink-audit.zip", mode="audit")
        with zipfile.ZipFile(audit) as bundle:
            link_name = next(name for name in bundle.namelist() if name.endswith("external-link"))
            info = bundle.getinfo(link_name)
            mode = info.external_attr >> 16
            assert stat.S_ISLNK(mode)
            assert bundle.read(link_name).decode("utf-8") == str(external)
            decoded = "\n".join(
                bundle.read(name).decode("utf-8", errors="ignore") for name in bundle.namelist()
            )
        assert secret not in decoded
    finally:
        engine.close()


def test_loading_restores_staged_source_from_authoritative_blob(
    tmp_path: Path,
    fake_config,
) -> None:
    source = tmp_path / "requirements.md"
    original = b"immutable source evidence\n"
    source.write_bytes(original)
    engine = FrontierEngine.create(
        "Preserve the supplied evidence.",
        config=fake_config(),
        sources=[source],
    )
    run_dir = engine.run_dir
    staged_path = engine.sources[0].stored_path
    engine.close()

    staged_path.write_bytes(b"same-sized corruption!!\n")
    loaded = FrontierEngine.load(run_dir)
    try:
        assert loaded.sources[0].stored_path.read_bytes() == original
        assert loaded.blobs.read_bytes(loaded.sources[0].blob) == original
    finally:
        loaded.close()


def test_run_lookup_prefers_flourite_and_falls_back_to_legacy_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    legacy_run = tmp_path / ".frontier" / "runs" / "run_legacy"
    legacy_run.mkdir(parents=True)
    (legacy_run.parent / "LATEST").write_text("run_legacy\n", encoding="utf-8")

    assert FrontierEngine.resolve_run_dir("latest") == legacy_run

    flourite_run = tmp_path / ".flourite" / "runs" / "run_flourite"
    flourite_run.mkdir(parents=True)
    (flourite_run.parent / "LATEST").write_text("run_flourite\n", encoding="utf-8")

    assert FrontierEngine.resolve_run_dir("latest") == flourite_run


def test_software_preflight_runs_before_the_first_worker_wave(
    fake_config, git_repo: Path
) -> None:
    config = fake_config(
        run={"adapter": "software"},
        software={"preflight_checks": ["true"], "checks": []},
    )
    engine = FrontierEngine.create(
        "Make one verified repository improvement.",
        config=config,
        workspace=git_repo,
    )
    try:
        asyncio.run(engine.execute())
        event_types = [item.event_type for item in engine.ledger.verified_events()]
        bootstrap = event_types.index("bootstrap.completed")
        staged = event_types.index("check.stage_completed")
        selected = event_types.index("action.selected")
        assert bootstrap < staged < selected
    finally:
        engine.close()


def test_persistent_preflight_failure_stops_instead_of_replanning_forever(
    fake_config, git_repo: Path
) -> None:
    config = fake_config(
        run={"adapter": "software"},
        software={"preflight_checks": ["false"], "checks": []},
    )
    engine = FrontierEngine.create(
        "Make one verified repository improvement.",
        config=config,
        workspace=git_repo,
    )
    try:
        with pytest.raises(FrontierError, match="no corrective action was proposed"):
            asyncio.run(engine.execute())

        event_types = [item.event_type for item in engine.ledger.verified_events()]
        assert event_types.count("checkpoint.completed") == 1
        assert event_types.count("check.replan_decided") == 1
        assert engine.state.metadata["verification_replan_decision"] == "dead_end"
        assert engine.state.metadata["verification_replan_pending"] is False
    finally:
        engine.close()
