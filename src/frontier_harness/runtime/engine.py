"""Creation, resume, execution, and materialization for kernel runs."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .. import __version__
from ..adapters import create_adapter
from ..adapters.base import ArtifactAdapter
from ..blobs import BlobStore
from ..config import HarnessConfig
from ..control import CommandKind, CommandStatus, RunControlPlane, RuntimeStatus
from ..core.journal import KernelJournal
from ..core.kernel import IntelligenceKernel
from ..core.types import (
    ArtifactVersion,
    ComputeEnvelope,
    Observation,
    ObservationKind,
    RunPaused,
    RunResumed,
    RunState,
    RunStatus,
    RunTerminated,
    SteeringReceived,
)
from ..errors import LedgerIntegrityError, RunNotFoundError
from ..ids import new_id
from ..intelligence.contracts import MoveRunner
from ..intelligence.fake_runner import DeterministicMoveRunner
from ..intelligence.omp_runner import OmpMoveRunner
from ..ledger import EventLedger
from ..locking import RunLock
from ..models import ArtifactRef
from ..providers import OmpCodexProvider, build_provider
from ..util import atomic_write_text, utc_now
from .activity import ProviderActivity
from .sources import StagedInput, load_sources, stage_sources


class KernelEngine:
    """Thin host shell around the canonical IntelligenceKernel."""

    ARCHITECTURE = "intelligence-kernel-v2"
    CONFIG_FILE = "config.snapshot.json"
    MANIFEST_FILE = "run.json"
    STATE_FILE = "state.json"
    LEDGER_FILE = "ledger.sqlite3"
    SOURCES_FILE = "sources.json"
    CONTROL_FILE = "control.sqlite3"

    def __init__(
        self,
        *,
        run_dir: Path,
        config: HarnessConfig,
        blobs: BlobStore,
        journal: KernelJournal,
        runner: MoveRunner,
        adapter: ArtifactAdapter,
        adapter_name: str,
        workspace: Path | None,
        sources: list[StagedInput],
    ) -> None:
        self.run_dir = run_dir
        self.config = config
        self.blobs = blobs
        self.journal = journal
        self.runner = runner
        self.adapter = adapter
        self.adapter_name = adapter_name
        self.workspace = workspace
        self.sources = sources
        self.lock = RunLock(run_dir / ".run.lock")
        self.control = RunControlPlane(
            run_dir / self.CONTROL_FILE,
            journal.ledger.run_id,
            busy_timeout_ms=config.runtime.sqlite_busy_timeout_ms,
        )
        self.kernel = IntelligenceKernel(
            journal=journal,
            blobs=blobs,
            runner=runner,
            capabilities=config.provider.capabilities.tools,
        )

    @property
    def state(self) -> RunState:
        return self.kernel.state

    @classmethod
    def create(
        cls,
        task: str,
        *,
        config: HarnessConfig,
        adapter_name: str,
        workspace: Path | None = None,
        source_paths: list[Path] | None = None,
        runner: MoveRunner | None = None,
    ) -> KernelEngine:
        if not task.strip():
            raise ValueError("task must not be empty")
        run_id = new_id("run")
        run_root = config.run.run_root.expanduser().resolve()
        run_dir = run_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        journal: KernelJournal | None = None
        try:
            atomic_write_text(
                run_dir / cls.CONFIG_FILE,
                json.dumps(config.model_dump(mode="json"), indent=2, ensure_ascii=False),
            )
            blobs = BlobStore(run_dir / "blobs")
            resolved_workspace = workspace.expanduser().resolve() if workspace else None
            adapter = create_adapter(
                adapter_name,
                run_dir=run_dir,
                blobs=blobs,
                workspace=resolved_workspace,
                config=config,
            )
            adapter.prepare()
            sources = stage_sources(
                list(source_paths or []),
                blobs=blobs,
                manifest_path=run_dir / cls.SOURCES_FILE,
                max_files=config.run.max_attachment_files,
                max_bytes=config.run.max_attachment_bytes,
                excluded_globs=config.run.excluded_source_globs,
            )
            if runner is None:
                provider = build_provider(config.provider)
                if config.provider.kind == "fake":
                    runner = DeterministicMoveRunner()
                else:
                    if not isinstance(provider, OmpCodexProvider):
                        raise ValueError("the intelligence kernel currently requires omp-codex")
                    runner = OmpMoveRunner(
                        provider=provider,
                        adapter=adapter,
                        run_dir=run_dir,
                        sources=sources,
                    )
            ledger = EventLedger(
                run_dir / cls.LEDGER_FILE,
                run_id,
                busy_timeout_ms=config.runtime.sqlite_busy_timeout_ms,
            )
            journal = KernelJournal(
                ledger=ledger,
                snapshot_path=run_dir / cls.STATE_FILE,
                max_event_payload_bytes=config.kernel.max_event_payload_bytes,
            )
            engine = cls(
                run_dir=run_dir,
                config=config,
                blobs=blobs,
                journal=journal,
                runner=runner,
                adapter=adapter,
                adapter_name=adapter_name,
                workspace=resolved_workspace,
                sources=sources,
            )
            if isinstance(runner, OmpMoveRunner):
                runner.activity_callback = engine._record_provider_activity
            atomic_write_text(
                run_dir / cls.MANIFEST_FILE,
                json.dumps(
                    {
                        "run_id": run_id,
                        "created_at": utc_now(),
                        "architecture": cls.ARCHITECTURE,
                        "engine_version": __version__,
                        "adapter": adapter_name,
                        "workspace": str(resolved_workspace) if resolved_workspace else None,
                        "ledger": cls.LEDGER_FILE,
                        "config": cls.CONFIG_FILE,
                        "sources": cls.SOURCES_FILE,
                    },
                    indent=2,
                    sort_keys=True,
                ),
            )
            envelope = ComputeEnvelope(
                max_wall_seconds=config.kernel.max_wall_seconds,
                max_input_tokens=config.kernel.max_input_tokens,
                max_output_tokens=config.kernel.max_output_tokens,
                max_model_turns=config.kernel.max_model_turns,
                max_cost_usd=config.kernel.max_cost_usd,
                max_parallel=config.kernel.max_parallel,
            )
            engine.kernel.start(task, envelope=envelope)
            atomic_write_text(run_root / "LATEST", run_id + "\n")
            return engine
        except BaseException:
            if journal is not None:
                journal.close()
            shutil.rmtree(run_dir, ignore_errors=True)
            raise

    @classmethod
    def load(
        cls,
        reference: str | Path,
        *,
        run_root: Path | None = None,
    ) -> KernelEngine:
        run_dir = cls.resolve_run_dir(reference, run_root=run_root)
        try:
            manifest = json.loads((run_dir / cls.MANIFEST_FILE).read_text(encoding="utf-8"))
            config = HarnessConfig.model_validate_json(
                (run_dir / cls.CONFIG_FILE).read_text(encoding="utf-8")
            )
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            raise RunNotFoundError(f"incomplete kernel run: {run_dir}") from exc
        if manifest.get("architecture") != cls.ARCHITECTURE:
            raise RunNotFoundError(f"run is not a {cls.ARCHITECTURE} run: {run_dir}")
        run_id = str(manifest["run_id"])
        blobs = BlobStore(run_dir / "blobs")
        workspace_value = manifest.get("workspace")
        workspace = Path(workspace_value) if isinstance(workspace_value, str) else None
        adapter_name = str(manifest["adapter"])
        sources = load_sources(run_dir / cls.SOURCES_FILE)
        adapter = create_adapter(
            adapter_name,
            run_dir=run_dir,
            blobs=blobs,
            workspace=workspace,
            config=config,
        )
        adapter.prepare()
        provider = build_provider(config.provider)
        if config.provider.kind == "fake":
            runner: MoveRunner = DeterministicMoveRunner()
        else:
            if not isinstance(provider, OmpCodexProvider):
                raise ValueError("the intelligence kernel currently requires omp-codex")
            runner = OmpMoveRunner(
                provider=provider,
                adapter=adapter,
                run_dir=run_dir,
                sources=sources,
            )
        journal = KernelJournal(
            ledger=EventLedger(
                run_dir / cls.LEDGER_FILE,
                run_id,
                busy_timeout_ms=config.runtime.sqlite_busy_timeout_ms,
            ),
            snapshot_path=run_dir / cls.STATE_FILE,
            max_event_payload_bytes=config.kernel.max_event_payload_bytes,
        )
        journal.refresh()
        engine = cls(
            run_dir=run_dir,
            config=config,
            blobs=blobs,
            journal=journal,
            runner=runner,
            adapter=adapter,
            adapter_name=adapter_name,
            workspace=workspace,
            sources=sources,
        )
        if isinstance(runner, OmpMoveRunner):
            runner.activity_callback = engine._record_provider_activity
        return engine

    @classmethod
    def resolve_run_dir(
        cls,
        reference: str | Path,
        *,
        run_root: Path | None = None,
    ) -> Path:
        candidate = Path(reference).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
        root = (run_root or Path(".flourite/runs")).expanduser().resolve()
        if str(reference).casefold() == "latest":
            try:
                reference = (root / "LATEST").read_text(encoding="utf-8").strip()
            except FileNotFoundError as exc:
                raise RunNotFoundError(f"no latest run under {root}") from exc
        candidate = root / str(reference)
        if not candidate.is_dir():
            raise RunNotFoundError(f"run not found: {reference}")
        return candidate.resolve()

    async def execute(self, *, max_steps: int | None = None) -> RunState:
        with self.lock:
            self.control.set_runtime(
                status=RuntimeStatus.RUNNING,
                phase="active",
                pid=os.getpid(),
                started_at=utc_now(),
                detail="advancing the live objective",
            )
            steps = 0
            published_seq = self.state.last_event_seq
            try:
                while not self.state.status.terminal:
                    self._apply_commands()
                    self._publish_events(after_seq=published_seq)
                    published_seq = self.state.last_event_seq
                    if self.state.status == RunStatus.PAUSED:
                        break
                    if max_steps is not None and steps >= max_steps:
                        break
                    progressed = await self.kernel.step()
                    if not progressed:
                        break
                    steps += 1
            finally:
                self._publish_events(after_seq=published_seq)
                if self.state.status == RunStatus.PAUSED:
                    runtime_status = RuntimeStatus.PAUSED
                elif self.state.status == RunStatus.FAILED:
                    runtime_status = RuntimeStatus.FAILED
                elif self.state.status.terminal:
                    runtime_status = RuntimeStatus.COMPLETE
                else:
                    runtime_status = RuntimeStatus.IDLE
                self.control.set_runtime(
                    status=runtime_status,
                    phase=self.state.status.value,
                    pid=None,
                    detail=self.state.terminal_reason or self.state.status.value,
                )
        return self.state

    def _apply_commands(self) -> None:
        for command in self.control.commands(pending_only=True):
            try:
                if command.kind == CommandKind.STEER:
                    text_ref = self.blobs.put_text(
                        command.text,
                        media_type="text/plain; charset=utf-8",
                        original_name="steering.txt",
                    )
                    observation = Observation(
                        observation_id=new_id("obs"),
                        kind=ObservationKind.STEERING,
                        summary=command.text,
                        source="operator",
                        raw_ref=text_ref,
                        created_at=utc_now(),
                        metadata={"command_id": command.command_id},
                    )
                    self.journal.append(
                        "steering.received",
                        SteeringReceived(observation=observation),
                        actor="operator",
                    )
                    detail = "steering admitted to the next workspace transition"
                elif command.kind == CommandKind.PAUSE:
                    if self.state.status != RunStatus.ACTIVE:
                        raise ValueError(f"run is {self.state.status.value}")
                    self.journal.append(
                        "run.paused",
                        RunPaused(reason=command.text or "operator pause"),
                        actor="operator",
                    )
                    detail = "paused at a safe move boundary"
                elif command.kind == CommandKind.RESUME:
                    if self.state.status != RunStatus.PAUSED:
                        raise ValueError(f"run is {self.state.status.value}")
                    self.journal.append(
                        "run.resumed",
                        RunResumed(reason=command.text or "operator resume"),
                        actor="operator",
                    )
                    detail = "resumed"
                else:
                    if self.state.status.terminal:
                        raise ValueError(f"run is {self.state.status.value}")
                    self.journal.append(
                        "run.stopped",
                        RunTerminated(status="stopped", reason=command.text or "operator stop"),
                        actor="operator",
                    )
                    detail = "stopped at a safe move boundary"
                self.control.mark_command(command.command_id, CommandStatus.APPLIED, detail)
            except (LedgerIntegrityError, ValueError) as exc:
                self.control.mark_command(command.command_id, CommandStatus.REJECTED, str(exc))

    def _record_provider_activity(self, event: dict[str, object]) -> None:
        activity = ProviderActivity.from_event(event)
        if activity is None:
            return
        self.control.record_activity(
            kind=f"provider.{activity.kind}",
            label=activity.label,
            message=activity.message,
            state=activity.state,
            action_id=activity.action_id,
        )

    def _publish_events(self, *, after_seq: int) -> None:
        labels = {
            "move.proposed": "next",
            "move.started": "working",
            "observation.recorded": "learned",
            "artifact.committed": "artifact",
            "workspace.committed": "workspace",
            "move.finished": "checkpoint",
            "move.applied": "checkpoint",
            "finish.claimed": "claim",
            "run.satisfied": "satisfied",
            "run.exhausted": "exhausted",
            "run.blocked": "blocked",
            "run.stopped": "stopped",
            "run.failed": "failed",
            "steering.received": "steering",
        }
        for event in self.journal.ledger.events(after_seq=after_seq):
            label = labels.get(event.event_type)
            if label is None:
                continue
            message = event.event_type.replace(".", " ")
            if event.event_type == "observation.recorded":
                value = event.payload.get("observation", {})
                message = str(value.get("summary") or message)
            elif event.event_type == "move.proposed":
                value = event.payload.get("move", {})
                message = str(value.get("intent") or message)
            elif event.event_type == "move.applied":
                value = event.payload
                workspace = value.get("workspace") or {}
                message = str(
                    workspace.get("summary")
                    or value.get("error")
                    or "move result committed atomically"
                )
            elif event.event_type.startswith("run."):
                message = str(event.payload.get("reason") or message)
            self.control.record_activity(
                kind=event.event_type,
                label=label,
                message=message,
                state=(
                    "warn"
                    if event.event_type in {"run.exhausted", "run.blocked", "run.failed"}
                    else "done"
                    if event.event_type in {"move.finished", "run.satisfied"}
                    else "active"
                ),
                action_id=event.action_id,
            )

    def materialize_current(self, destination: Path | None = None) -> Path:
        workspace = self.state.current_workspace
        if workspace is None:
            raise ValueError("run has no current workspace")
        root_trajectory = self.state.trajectories[self.state.root_trajectory_id]
        artifact_id = root_trajectory.artifact_head_id
        if artifact_id is None:
            path = destination or (self.run_dir / "current.md")
            self.blobs.materialize(workspace.document_ref, path)
            return path
        legacy = self._legacy_artifact(self.state.artifacts[artifact_id])
        path = destination or (self.run_dir / f"current{self.adapter.final_suffix}")
        return self.adapter.materialize_final(legacy, path)

    def _legacy_artifact(self, artifact: ArtifactVersion) -> ArtifactRef:
        workspace = self.state.current_workspace
        return ArtifactRef(
            artifact_id=artifact.artifact_id,
            version=int(artifact.metadata.get("legacy_version", len(self.state.artifacts))),
            blob=artifact.content_ref,
            kind=str(artifact.metadata.get("kind", self.adapter.artifact_kind)),
            summary=workspace.summary if workspace is not None else "current artifact",
            parent_artifact_id=(
                artifact.parent_artifact_ids[0] if artifact.parent_artifact_ids else None
            ),
            source_action_ids=[artifact.created_by_move_id],
            deliverables=artifact.deliverables,
            created_at=artifact.created_at,
        )

    def apply_current_explicit(self) -> dict[str, object] | None:
        if self.state.status != RunStatus.SATISFIED:
            raise ValueError("only a satisfied run can mutate the original workspace")
        root = self.state.trajectories[self.state.root_trajectory_id]
        if root.artifact_head_id is None:
            raise ValueError("run has no root artifact to apply")
        return self.adapter.apply_final_explicit(
            self._legacy_artifact(self.state.artifacts[root.artifact_head_id])
        )

    def verify(self) -> tuple[int, str]:
        events, replayed = self.journal.verified_projection()
        if replayed.model_dump(mode="json") != self.state.model_dump(mode="json"):
            raise ValueError("live state differs from verified replay")
        references = [self.state.objective.original_text_ref]
        references.extend(item.text_ref for item in self.state.objective.amendments)
        references.extend(item.document_ref for item in self.state.workspaces.values())
        for artifact in self.state.artifacts.values():
            references.append(artifact.content_ref)
            references.extend(artifact.deliverables)
        references.extend(
            item.raw_ref for item in self.state.observations.values() if item.raw_ref is not None
        )
        references.extend(item.content_ref for item in self.sources)
        verified: set[str] = set()
        for reference in references:
            if reference.digest in verified:
                continue
            self.blobs.verify(reference)
            verified.add(reference.digest)
        return len(events), events[-1].event_hash

    def close(self) -> None:
        self.control.close()
        self.journal.close()
