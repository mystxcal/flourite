"""Event-sourced Flourite runtime.

The engine keeps one integrated artifact, a small unresolved frontier, and a
small adaptive probe portfolio.  Semantic model calls happen only at meaningful
checkpoints; deterministic code owns persistence, budgets, scheduling,
recovery, isolation, and integrity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from . import __version__
from . import events as et
from .adapters import ArtifactAdapter, create_adapter
from .adapters.base import CallWorkspace
from .adapters.profiles import PROFILES, AdapterProfile, combine_profiles
from .blobs import BlobStore
from .capsule import CapsuleBuilder, StagedSource, stage_sources
from .cognition import (
    admit_overlays,
    build_action_contract,
    capture_task_source,
    ceiling_trigger_reasons,
    compile_guard_obligations,
    completion_case_gaps,
    fallback_charter,
    fallback_spine,
    instantiate_cruxes,
    instantiate_obligations,
    reconcile_artifact_spine,
    reconcile_frontier_kernel,
    validate_lead_ack,
)
from .config import HarnessConfig
from .control import (
    CommandKind,
    CommandStatus,
    RunControlPlane,
    RuntimeStatus,
)
from .discovery import ExperimentalFrontier
from .errors import (
    FrontierError,
    LedgerIntegrityError,
    OperatorStop,
    ProviderCallError,
    ProviderError,
    RunNotFoundError,
)
from .execution import ActionExecutor, CallTrace, RunJournal
from .ids import new_id
from .ledger import EventLedger, LedgerEvent
from .locking import RunLock
from .models import (
    ActionContract,
    ActionKind,
    ActionProposal,
    ActionRecord,
    ActionSpec,
    ActionStatus,
    ArtifactRef,
    BlobRef,
    BootstrapOutput,
    BudgetContract,
    CognitiveTopology,
    CompletionCase,
    CompletionClaim,
    CostBand,
    Crux,
    CruxDraft,
    CruxStatus,
    EpistemicMode,
    EvidenceModality,
    EvidenceRecord,
    FinalOutput,
    FrontierKernel,
    GoalContract,
    Impact,
    IndependenceClass,
    Issue,
    IssueDraft,
    IssueStatus,
    IssueUpdate,
    LeadContinuityStatus,
    Obligation,
    ObligationDraft,
    ObligationStatus,
    OverlayStatus,
    ReleaseOutput,
    ReleaseRecovery,
    RepairOutput,
    ResourceDecisionKind,
    Role,
    RunPhase,
    RunState,
    SummitLineage,
    TaskAmendment,
    TaskCharter,
    TaskSource,
    Usage,
    ValueBand,
    WorkerEnvelope,
)
from .observability import LiveObserver
from .orchestration import (
    CheckpointExecutor,
    FrontierLoop,
    MutationGateDecision,
    ReleasePipeline,
    ReleasePolicy,
    RunCoordinator,
)
from .prompts import (
    bootstrap_prompt,
    final_prompt,
    release_prompt,
    repair_prompt,
)
from .providers import (
    ModelProvider,
    ProviderCallRequest,
    ProviderCallResult,
    ProviderDoctorResult,
    build_provider,
)
from .resources import ResourceGovernor
from .scheduler import ActionScheduler, SelectionResult
from .semantic_ci import run_semantic_ci
from .state import StateReducer
from .summit import SummitArchive
from .util import (
    atomic_write_text,
    canonical_json,
    normalize_key,
    safe_slug,
    sha256_text,
    unique_preserving_order,
    utc_now,
)

ResponseT = TypeVar("ResponseT", bound=BaseModel)
EventCallback = Callable[[LedgerEvent, RunState], None]

DEFAULT_RUN_ROOT = Path(".flourite/runs")
LEGACY_RUN_ROOT = Path(".frontier/runs")

_IMPACT_RANK = {Impact.LOW: 1, Impact.MEDIUM: 2, Impact.HIGH: 3, Impact.FATAL: 4}
_UNCERTAINTY_RANK = {"low": 1, "medium": 2, "high": 3}
_TERMINAL_ACTION_STATUSES = {
    ActionStatus.COMPLETE,
    ActionStatus.FAILED,
    ActionStatus.CANCELLED,
}


class FrontierEngine:
    """Create, execute, resume, inspect, and verify one sparse-frontier run."""

    CONFIG_FILE = "config.snapshot.json"
    MANIFEST_FILE = "run.json"
    STATE_FILE = "state.json"
    LEDGER_FILE = "ledger.sqlite3"
    SEAL_FILE = "seal.json"
    EXTENSION_INTENT_FILE = "extension.intent.json"
    CONTROL_FILE = "control.sqlite3"
    _detached_state: RunState

    def __init__(
        self,
        *,
        run_dir: Path,
        config: HarnessConfig,
        provider: ModelProvider,
        adapter: ArtifactAdapter,
        ledger: EventLedger,
        blobs: BlobStore,
        state: RunState,
        sources: list[StagedSource],
        on_event: EventCallback | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.config = config
        self.provider = provider
        self.adapter = adapter
        self.blobs = blobs
        self.sources = sources
        self.on_event = on_event
        self.journal = RunJournal(
            ledger=ledger,
            snapshot_path=run_dir / self.STATE_FILE,
            max_event_payload_bytes=config.runtime.max_event_payload_bytes,
            state=state,
        )
        self.scheduler = ActionScheduler(config.frontier)
        self.summit_archive = SummitArchive(
            max_lineages=config.summit.max_archive_lineages,
            max_active=config.summit.max_active_lineages,
            max_per_niche=config.summit.max_per_niche,
            preserve_falsification_residue=config.summit.preserve_falsification_residue,
        )
        self.experimental_frontier = ExperimentalFrontier(
            stagnation_before_mutation=config.summit.stagnation_before_mutation,
            enable_mutation=config.summit.enable_semantic_mutation,
            enable_crossover=config.summit.enable_semantic_crossover,
        )
        self.resource_governor = ResourceGovernor(
            policy=config.resource,
            budget=config.run.budget,
            release_expected=config.run.release_gate != "never",
            max_material_repairs=config.cognition.max_material_repairs,
        )
        self.lock = RunLock(run_dir / ".run.lock")
        self.control = RunControlPlane(
            run_dir / self.CONTROL_FILE,
            state.run_id,
            busy_timeout_ms=config.runtime.sqlite_busy_timeout_ms,
        )
        self.logger = logging.getLogger(f"frontier_harness.{state.run_id}")
        self.observer = LiveObserver(self.control, self.logger)
        self.action_executor = ActionExecutor()
        self.checkpoint_executor = CheckpointExecutor()
        self.frontier_loop = FrontierLoop()
        self.release_pipeline = ReleasePipeline()
        self._capsules = CapsuleBuilder(
            adapter=adapter,
            blobs=blobs,
            sources=sources,
            evidence_limit=config.runtime.evidence_per_capsule,
            artifact_char_limit=config.runtime.capsule_artifact_char_limit,
        )

    @property
    def state(self) -> RunState:
        if "journal" not in self.__dict__:
            # A few pure unit tests construct a detached engine shell to probe
            # scheduling logic without I/O. Production engines always own a
            # RunJournal.
            return self._detached_state
        return self.journal.state

    @state.setter
    def state(self, value: RunState) -> None:
        if hasattr(self, "journal"):
            raise AttributeError("Run state is owned by RunJournal")
        self._detached_state = value

    @property
    def ledger(self) -> EventLedger:
        """Compatibility view for legacy readers and audit exporters.

        Authoritative writes are intentionally unavailable through Flourite's
        own engine paths; all of them pass through RunJournal.
        """

        return self.journal.compatibility_ledger

    # ------------------------------------------------------------------
    # Construction and loading
    # ------------------------------------------------------------------
    @staticmethod
    def _create_provider(config: HarnessConfig) -> ModelProvider:
        return build_provider(config.provider)

    @staticmethod
    def _serialize_source(source: StagedSource, run_dir: Path) -> dict[str, Any]:
        return {
            "display_name": source.display_name,
            "stored_path": source.stored_path.relative_to(run_dir).as_posix(),
            "blob": source.blob.model_dump(mode="json"),
            "original_path": source.original_path,
        }

    @staticmethod
    def _restore_sources(
        run_dir: Path, blobs: BlobStore, metadata: dict[str, Any]
    ) -> list[StagedSource]:
        restored: list[StagedSource] = []
        for item in metadata.get("sources", []):
            blob = BlobRef.model_validate(item["blob"])
            stored = run_dir / item["stored_path"]
            blobs.materialize(blob, stored)
            restored.append(
                StagedSource(
                    display_name=item["display_name"],
                    stored_path=stored,
                    blob=blob,
                    original_path=item.get("original_path", ""),
                )
            )
        return restored

    @classmethod
    def create(
        cls,
        task: str,
        *,
        config: HarnessConfig | None = None,
        adapter_name: str | None = None,
        workspace: Path | None = None,
        sources: Sequence[Path] = (),
        provider: ModelProvider | None = None,
        on_event: EventCallback | None = None,
    ) -> FrontierEngine:
        if not task.strip():
            raise ValueError("The task must not be empty")
        config = config or HarnessConfig()
        adapter_name = adapter_name or config.run.adapter
        run_id = new_id("run")
        run_root = config.run.run_root.expanduser().resolve()
        run_dir = run_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        created_at = utc_now()
        ledger: EventLedger | None = None
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
            adapter_metadata = adapter.prepare()
            staged = stage_sources(
                sources,
                run_dir=run_dir,
                blobs=blobs,
                max_total_bytes=config.run.max_attachment_bytes,
                max_files=config.run.max_attachment_files,
                excluded_globs=config.run.excluded_source_globs,
                exclude_roots=[run_dir],
            )
            ledger = EventLedger(
                run_dir / cls.LEDGER_FILE,
                run_id,
                busy_timeout_ms=config.runtime.sqlite_busy_timeout_ms,
            )
            metadata = {
                "engine_version": __version__,
                "adapter": adapter_metadata,
                "sources": [cls._serialize_source(item, run_dir) for item in staged],
                "config_snapshot": cls.CONFIG_FILE,
            }
            event = ledger.append(
                et.RUN_CREATED,
                {
                    "created_at": created_at,
                    "source_prompt": task,
                    "adapter": adapter_name,
                    "workspace": str(resolved_workspace) if resolved_workspace else None,
                    "metadata": metadata,
                },
                actor="runtime",
            )
            state = StateReducer().apply(None, event)
            task_source = capture_task_source(task)
            task_event = ledger.append(
                et.TASK_SOURCE_CAPTURED,
                {"task_source": task_source.model_dump(mode="json")},
                actor="runtime",
            )
            state = StateReducer().apply(state, task_event)
            atomic_write_text(
                run_dir / "task-source.json",
                json.dumps(task_source.model_dump(mode="json"), indent=2, ensure_ascii=False),
            )
            atomic_write_text(
                run_dir / cls.MANIFEST_FILE,
                json.dumps(
                    {
                        "run_id": run_id,
                        "created_at": created_at,
                        "adapter": adapter_name,
                        "engine_version": __version__,
                        "ledger": cls.LEDGER_FILE,
                        "config": cls.CONFIG_FILE,
                    },
                    indent=2,
                    sort_keys=True,
                ),
            )
            atomic_write_text(
                run_dir / cls.STATE_FILE,
                json.dumps(state.model_dump(mode="json"), indent=2, ensure_ascii=False),
            )
            engine = cls(
                run_dir=run_dir,
                config=config,
                provider=provider or cls._create_provider(config),
                adapter=adapter,
                ledger=ledger,
                blobs=blobs,
                state=state,
                sources=staged,
                on_event=on_event,
            )
            if on_event:
                on_event(event, state)
                on_event(task_event, state)
            atomic_write_text(run_root / "LATEST", run_id + "\n")
            return engine
        except BaseException:
            if ledger is not None:
                ledger.close()
            shutil.rmtree(run_dir, ignore_errors=True)
            raise

    @classmethod
    def resolve_run_dir(cls, reference: Path | str, *, run_root: Path | None = None) -> Path:
        candidate = Path(reference).expanduser()
        if candidate.exists() and candidate.is_dir():
            return candidate.resolve()
        roots = [(run_root or DEFAULT_RUN_ROOT).expanduser().resolve()]
        if run_root is None:
            legacy = LEGACY_RUN_ROOT.expanduser().resolve()
            if legacy not in roots:
                roots.append(legacy)
        for root in roots:
            resolved_reference = reference
            if str(reference).casefold() == "latest":
                latest = root / "LATEST"
                if latest.is_file():
                    resolved_reference = latest.read_text(encoding="utf-8").strip()
            by_id = root / str(resolved_reference)
            if by_id.is_dir():
                return by_id
        raise RunNotFoundError(f"Run not found: {reference}")

    @classmethod
    def load(
        cls,
        reference: Path | str,
        *,
        run_root: Path | None = None,
        provider: ModelProvider | None = None,
        on_event: EventCallback | None = None,
    ) -> FrontierEngine:
        run_dir = cls.resolve_run_dir(reference, run_root=run_root)
        try:
            manifest = json.loads((run_dir / cls.MANIFEST_FILE).read_text(encoding="utf-8"))
            raw_config = json.loads((run_dir / cls.CONFIG_FILE).read_text(encoding="utf-8"))
            legacy_resource_policy = "resource" not in raw_config
            config = HarnessConfig.model_validate(raw_config)
            if legacy_resource_policy:
                # Runs created before the resource governor retain their exact
                # full-envelope behavior when resumed.  New runs snapshot an
                # explicit adaptive policy.
                config.resource.mode = "static"
        except FileNotFoundError as exc:
            raise RunNotFoundError(f"Incomplete run directory: {run_dir}") from exc
        run_id = str(manifest["run_id"])
        ledger = EventLedger(
            run_dir / cls.LEDGER_FILE,
            run_id,
            busy_timeout_ms=config.runtime.sqlite_busy_timeout_ms,
        )
        event_snapshot = ledger.verified_events()
        state = StateReducer().replay(event_snapshot)
        extension = state.runtime.extension.last_event
        if isinstance(extension, dict) and extension.get("new_budget"):
            # The extension event is authoritative even if a process ended
            # between appending it and replacing the config snapshot.
            config.run.budget = BudgetContract.model_validate(extension["new_budget"])
        if state.run_id != run_id:
            ledger.close()
            raise LedgerIntegrityError(
                f"Run manifest says {run_id}, ledger reconstructs {state.run_id}"
            )
        blobs = BlobStore(run_dir / "blobs")
        workspace = Path(state.workspace) if state.workspace else None
        adapter = create_adapter(
            state.adapter,
            run_dir=run_dir,
            blobs=blobs,
            workspace=workspace,
            config=config,
        )
        adapter.prepare()
        sources = cls._restore_sources(run_dir, blobs, state.metadata)
        return cls(
            run_dir=run_dir,
            config=config,
            provider=provider or cls._create_provider(config),
            adapter=adapter,
            ledger=ledger,
            blobs=blobs,
            state=state,
            sources=sources,
            on_event=on_event,
        )

    async def doctor(self) -> ProviderDoctorResult:
        return await self.provider.doctor()

    def events(self) -> list[LedgerEvent]:
        return list(self.journal.events())

    async def extend(
        self,
        *,
        additional_calls: int,
        additional_rounds: int | None = None,
        output_path: Path | None = None,
        reason: str = "operator requested additional exact-task development",
    ) -> Path:
        """Reopen a sealed completed run without discarding accumulated research.

        The previous completion seal is archived and committed as a blob. The
        extension event invalidates release evidence, preserves the current
        artifact and all semantic state, expands the budget, and requires a
        fresh Lead checkpoint before additional work can proceed.
        """

        if additional_calls <= 0:
            raise ValueError("additional_calls must be positive")
        if additional_rounds is not None and additional_rounds <= 0:
            raise ValueError("additional_rounds must be positive when supplied")
        intent_path = self.run_dir / self.EXTENSION_INTENT_FILE
        with self.lock:
            self._refresh_state_from_ledger()
            if self.state.phase == RunPhase.ACTIVE and self.state.runtime.extension.replan_pending:
                intent_path.unlink(missing_ok=True)
            elif self.state.phase != RunPhase.COMPLETE:
                raise FrontierError("Only a completed run can be extended")
            else:
                intent: dict[str, Any] | None = None
                if intent_path.exists():
                    intent = json.loads(intent_path.read_text(encoding="utf-8"))
                if intent is None:
                    report = self.verify_integrity()
                    if not report.get("sealed"):
                        raise FrontierError(
                            "Completed run is not sealed; verify or repair its integrity before extension"
                        )
                    old_budget = self.config.run.budget
                    new_rounds = (
                        None
                        if old_budget.max_rounds is None
                        else old_budget.max_rounds
                        + (additional_rounds if additional_rounds is not None else additional_calls)
                    )
                    new_max_calls = old_budget.max_calls + additional_calls

                    def scaled_cap(value: int | None) -> int | None:
                        if value is None:
                            return None
                        return max(
                            value,
                            (value * new_max_calls + old_budget.max_calls - 1)
                            // old_budget.max_calls,
                        )

                    new_budget = BudgetContract(
                        max_rounds=new_rounds,
                        max_calls=new_max_calls,
                        max_parallel=old_budget.max_parallel,
                        max_input_tokens=scaled_cap(old_budget.max_input_tokens),
                        max_output_tokens=scaled_cap(old_budget.max_output_tokens),
                        max_wall_seconds=scaled_cap(old_budget.max_wall_seconds),
                        synthesis_reserve_calls=old_budget.synthesis_reserve_calls,
                    )
                    remaining = new_budget.max_calls - self.state.usage.calls
                    minimum = new_budget.synthesis_reserve_calls + 3
                    if remaining < minimum:
                        raise FrontierError(
                            "Extension budget is too small for one replan, one development action, "
                            f"its integration checkpoint, and the protected release reserve; need at least {minimum} remaining calls"
                        )
                    seal_path = self.run_dir / self.SEAL_FILE
                    seal_blob = self.blobs.put_file(
                        seal_path,
                        media_type="application/json",
                        original_name=f"seal-before-extension-{self.state.runtime.extension.count + 1}.json",
                    )
                    history_dir = self.run_dir / "seal-history"
                    history_dir.mkdir(parents=True, exist_ok=True)
                    history_name = f"seal-{self.state.runtime.extension.count + 1:03d}.json"
                    atomic_write_text(
                        history_dir / history_name,
                        seal_path.read_text(encoding="utf-8"),
                    )
                    intent = {
                        "requested_at": utc_now(),
                        "reason": reason,
                        "additional_calls": additional_calls,
                        "additional_rounds": (
                            0
                            if new_rounds is None or old_budget.max_rounds is None
                            else new_rounds - old_budget.max_rounds
                        ),
                        "old_budget": old_budget.model_dump(mode="json"),
                        "new_budget": new_budget.model_dump(mode="json"),
                        "previous_seal_blob": seal_blob.model_dump(mode="json"),
                        "previous_seal_history_path": f"seal-history/{history_name}",
                    }
                    atomic_write_text(intent_path, json.dumps(intent, indent=2, ensure_ascii=False))
                    seal_path.unlink()
                new_budget = BudgetContract.model_validate(intent["new_budget"])
                self.config.run.budget = new_budget
                atomic_write_text(
                    self.run_dir / self.CONFIG_FILE,
                    json.dumps(self.config.model_dump(mode="json"), indent=2, ensure_ascii=False),
                )
                self._append(et.RUN_EXTENDED, intent, actor="operator")
                self.resource_governor = ResourceGovernor(
                    policy=self.config.resource,
                    budget=self.config.run.budget,
                    release_expected=self.config.run.release_gate != "never",
                    max_material_repairs=self.config.cognition.max_material_repairs,
                )
                intent_path.unlink(missing_ok=True)

        return await self.execute(output_path=output_path)

    def apply_final_patch(self) -> dict[str, Any]:
        """Explicitly apply a completed, mutation-gate-approved software patch.

        The CLI boundary is deliberate, but not an override of failed checks or
        unresolved release findings.  Repository fingerprint and idempotency
        checks remain owned by the software adapter.
        """

        if not self.adapter.supports_explicit_apply:
            raise FrontierError("Only software runs have an applicable Git patch")
        with self.lock:
            self._refresh_state_from_ledger()
            self.verify_integrity()
            if self.state.phase != RunPhase.COMPLETE:
                raise FrontierError(
                    "The software run must complete before its patch can be applied"
                )
            if self.state.final_artifact is None:
                raise FrontierError("Run has no final software artifact")
            if self.state.runtime.completion.mutation_gate_passed is not True:
                reason = (
                    self.state.runtime.completion.mutation_gate_block_reason
                    or "the run has no affirmative mutation-gate decision"
                )
                raise FrontierError(f"Refusing to apply final patch: {reason}")

            result = self.adapter.apply_final_explicit(self.state.final_artifact)
            if result is None:
                raise FrontierError("Software adapter did not produce an apply result")
            self._append(et.PATCH_APPLIED, result, actor="user")
            self._write_seal()
            return result

    def close(self) -> None:
        self.control.close()
        self.journal.close()

    def __enter__(self) -> FrontierEngine:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Durable state and integrity
    # ------------------------------------------------------------------
    def _save_state(self) -> None:
        # Kept as a compatibility shim for callers that explicitly request a
        # snapshot refresh. RunJournal owns normal snapshot writes.
        atomic_write_text(
            self.run_dir / self.STATE_FILE,
            json.dumps(self.state.model_dump(mode="json"), indent=2, ensure_ascii=False),
        )

    def _refresh_state_from_ledger(self) -> None:
        """Reconstruct the authoritative projection after acquiring the run lock."""

        self.journal.refresh(expected_run_id=self.state.run_id)

    def _append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: str = "runtime",
        action_id: str | None = None,
    ) -> LedgerEvent:
        event = self.journal.append(
            event_type,
            payload,
            actor=actor,
            action_id=action_id,
        )
        self.observer.ledger_event(event, self.state)
        if self.on_event:
            self.on_event(event, self.state)
        return event

    def _processed_control_ids(self) -> set[str]:
        return set(self.state.runtime.control.processed_command_ids)

    def _admit_steering(self, command_id: str, text: str) -> None:
        amendment = TaskAmendment(
            amendment_id=new_id("amendment"),
            text=text,
            created_at=utc_now(),
            source="operator",
            digest=sha256_text(text),
        )
        self._append(
            et.TASK_SOURCE_AMENDED,
            {
                "amendment": amendment.model_dump(mode="json"),
                "command_id": command_id,
            },
            actor="operator",
        )

    def _mark_reconciled_controls(self) -> None:
        processed = self._processed_control_ids()
        for command in self.control.commands(pending_only=True):
            if command.command_id in processed:
                self.control.mark_command(
                    command.command_id,
                    CommandStatus.APPLIED,
                    "reconciled from the authoritative event ledger",
                )

    async def _wait_while_paused(self) -> bool:
        steered = False
        self.observer.set_runtime(
            RuntimeStatus.PAUSED,
            phase=self.state.phase.value,
            detail="waiting for operator",
        )
        while True:
            await asyncio.sleep(0.2)
            for command in self.control.commands(pending_only=True):
                if command.command_id in self._processed_control_ids():
                    self.control.mark_command(
                        command.command_id,
                        CommandStatus.APPLIED,
                        "reconciled from the authoritative event ledger",
                    )
                elif command.kind == CommandKind.STEER:
                    self._admit_steering(command.command_id, command.text)
                    self.control.mark_command(
                        command.command_id,
                        CommandStatus.APPLIED,
                        "admitted while paused; replanning on resume",
                    )
                    steered = True
                elif command.kind == CommandKind.PAUSE:
                    self.control.mark_command(
                        command.command_id,
                        CommandStatus.REJECTED,
                        "the run is already paused",
                    )
                elif command.kind == CommandKind.STOP:
                    self._append(
                        et.RUN_STOPPED,
                        {
                            "command_id": command.command_id,
                            "detail": "operator stopped the paused run",
                        },
                        actor="operator",
                    )
                    self.control.mark_command(
                        command.command_id,
                        CommandStatus.APPLIED,
                        "stopped while paused",
                    )
                    self.observer.set_runtime(
                        RuntimeStatus.STOPPED,
                        phase=self.state.phase.value,
                        detail="stopped by operator",
                    )
                    raise OperatorStop("Run stopped at a safe boundary; it remains resumable")
                else:
                    self._append(
                        et.RUN_RESUMED,
                        {
                            "command_id": command.command_id,
                            "detail": "operator resumed the paused run",
                        },
                        actor="operator",
                    )
                    self.control.mark_command(
                        command.command_id,
                        CommandStatus.APPLIED,
                        "resumed",
                    )
                    self.observer.set_runtime(
                        RuntimeStatus.RUNNING,
                        phase=self.state.phase.value,
                        detail="resumed",
                    )
                    return steered

    async def _control_boundary(self) -> bool:
        """Admit commands only while no model call or integration is in flight."""

        steered = False
        self._mark_reconciled_controls()
        for command in self.control.commands(pending_only=True):
            if command.kind == CommandKind.STEER:
                self._admit_steering(command.command_id, command.text)
                self.control.mark_command(
                    command.command_id,
                    CommandStatus.APPLIED,
                    "admitted to the Task Source at a safe boundary",
                )
                steered = True
            elif command.kind == CommandKind.RESUME:
                self.control.mark_command(
                    command.command_id,
                    CommandStatus.REJECTED,
                    "the run was not paused",
                )
            elif command.kind == CommandKind.STOP:
                self._append(
                    et.RUN_STOPPED,
                    {
                        "command_id": command.command_id,
                        "detail": "operator requested a resumable stop",
                    },
                    actor="operator",
                )
                self.control.mark_command(
                    command.command_id,
                    CommandStatus.APPLIED,
                    "stopped at a safe boundary",
                )
                self.observer.set_runtime(
                    RuntimeStatus.STOPPED,
                    phase=self.state.phase.value,
                    detail="stopped by operator",
                )
                raise OperatorStop("Run stopped at a safe boundary; it remains resumable")
            else:
                self._append(
                    et.RUN_PAUSED,
                    {
                        "command_id": command.command_id,
                        "detail": "operator requested a safe-boundary pause",
                    },
                    actor="operator",
                )
                self.control.mark_command(
                    command.command_id,
                    CommandStatus.APPLIED,
                    "paused at a safe boundary",
                )
                steered = await self._wait_while_paused() or steered
        return steered

    def _write_seal(self) -> None:
        count, last_hash = self.journal.verify()
        final_digest = self.state.final_artifact.blob.digest if self.state.final_artifact else None
        state_hash = sha256_text(canonical_json(self.state.model_dump(mode="json")))
        atomic_write_text(
            self.run_dir / self.SEAL_FILE,
            json.dumps(
                {
                    "run_id": self.state.run_id,
                    "event_count": count,
                    "last_event_hash": last_hash,
                    "final_artifact_digest": final_digest,
                    "state_hash": state_hash,
                    "sealed_at": utc_now(),
                },
                indent=2,
                sort_keys=True,
            ),
        )

    @staticmethod
    def _blob_dicts(value: Any) -> Iterable[dict[str, Any]]:
        if isinstance(value, dict):
            keys = set(value)
            if {"digest", "size", "relative_path"}.issubset(keys):
                yield value
            for child in value.values():
                yield from FrontierEngine._blob_dicts(child)
        elif isinstance(value, list):
            for child in value:
                yield from FrontierEngine._blob_dicts(child)

    def verify_integrity(self) -> dict[str, Any]:
        events, replayed = self.journal.verified_projection()
        event_count = len(events)
        last_hash = events[-1].event_hash if events else "0" * 64
        if replayed.model_dump(mode="json") != self.state.model_dump(mode="json"):
            raise LedgerIntegrityError("In-memory state differs from ledger replay")

        verified: set[str] = set()
        for event in events:
            for raw in self._blob_dicts(event.payload):
                ref = BlobRef.model_validate(raw)
                if ref.digest not in verified:
                    self.blobs.verify(ref)
                    verified.add(ref.digest)

        seal_path = self.run_dir / self.SEAL_FILE
        sealed = False
        if seal_path.exists():
            seal = json.loads(seal_path.read_text(encoding="utf-8"))
            if int(seal["event_count"]) != event_count or seal["last_event_hash"] != last_hash:
                raise LedgerIntegrityError("Completion seal does not match the current ledger")
            if self.state.final_artifact and (
                seal.get("final_artifact_digest") != self.state.final_artifact.blob.digest
            ):
                raise LedgerIntegrityError("Completion seal final artifact digest mismatch")
            expected_state_hash = sha256_text(canonical_json(replayed.model_dump(mode="json")))
            if seal.get("state_hash") not in {None, expected_state_hash}:
                raise LedgerIntegrityError("Completion seal state hash mismatch")
            sealed = True
        return {
            "run_id": self.state.run_id,
            "event_count": event_count,
            "last_event_hash": last_hash,
            "verified_blob_count": len(verified),
            "sealed": sealed,
            "phase": self.state.phase.value,
        }

    # ------------------------------------------------------------------
    # Small object construction helpers
    # ------------------------------------------------------------------
    @property
    def _profile(self) -> AdapterProfile | None:
        names: list[str] = []
        if self.adapter.profile is not None and self.adapter.profile.name != "generic":
            names.append(self.adapter.profile.name)
        names.extend(self.config.run.semantic_profiles)
        if self.state.contract:
            names.extend(self.state.contract.semantic_profiles)
        names = [name for name in unique_preserving_order(names) if name in PROFILES]
        return (
            combine_profiles(names)
            if names
            else self.adapter.profile
        )

    @property
    def _software(self) -> bool:
        return self.adapter.is_software

    def _calls_remaining(self) -> int:
        return max(0, self.config.run.budget.max_calls - self.state.usage.calls)

    def _ensure_resource_state(self) -> None:
        if self.state.resource_state is not None:
            return
        resource = self.resource_governor.initial_state(self.state)
        self._append(
            et.RESOURCE_INITIALIZED,
            {"resource_state": resource.model_dump(mode="json")},
            actor="resource-governor",
        )

    def _active_calls_remaining(self) -> int:
        self._ensure_resource_state()
        assert self.state.resource_state is not None
        return max(0, self.state.resource_state.active_call_limit - self.state.usage.calls)

    def _completion_reserve_calls(self) -> int:
        return self.resource_governor.completion_reserve(self.state)

    def _actionable_selection(self, proposals: Sequence[ActionSpec]) -> SelectionResult:
        return self.scheduler.select(
            list(proposals),
            max_parallel=self.config.run.budget.max_parallel,
            available_calls=self.config.run.budget.max_parallel,
            obligations=self.state.obligations,
            target_stalls=self._target_stalls(),
            human_evidence_available=self.config.cognition.human_evidence_available,
            require_execution_trigger=self.config.cognition.require_execution_trigger,
            frontier_kernel=self.state.frontier_kernel,
            action_records=self.state.actions,
            frontier_advancing_action_ids=set(self.state.frontier_advancing_action_ids),
        )

    def _actionable_count(self, proposals: Sequence[ActionSpec]) -> int:
        return len(self._actionable_selection(proposals).selected)

    def _resource_boundary(self, proposals: Sequence[ActionSpec]) -> bool:
        """Return true only when the governor grants another work horizon."""

        self._ensure_resource_state()
        assert self.state.resource_state is not None
        selection = self._actionable_selection(proposals)
        decision, resource = self.resource_governor.decide(
            self.state,
            self.state.resource_state,
            actionable_actions=len(selection.selected),
            active_commitments=sum(item.continuation is not None for item in selection.selected),
        )
        self._append(
            et.RESOURCE_DECIDED,
            {
                "decision": decision.model_dump(mode="json"),
                "resource_state": resource.model_dump(mode="json"),
            },
            actor="resource-governor",
        )
        return decision.kind == ResourceDecisionKind.GRANT

    def _budget_limit_reason(self, *, calls: bool = True) -> str | None:
        budget = self.config.run.budget
        usage = self.state.usage
        if calls and usage.calls >= budget.max_calls:
            return "model-call budget exhausted"
        if budget.max_input_tokens is not None and usage.input_tokens >= budget.max_input_tokens:
            return "input-token budget exhausted"
        if budget.max_output_tokens is not None and usage.output_tokens >= budget.max_output_tokens:
            return "output-token budget exhausted"
        if budget.max_wall_seconds is not None and usage.wall_seconds >= budget.max_wall_seconds:
            return "model wall-time budget exhausted"
        return None

    def _can_call(self) -> bool:
        return self._budget_limit_reason() is None

    def _target_stalls(self) -> dict[str, int]:
        """Count consecutive no-information attempts per semantic target."""

        stalls: dict[str, int] = {}
        ordered = sorted(
            self.state.actions.values(),
            key=lambda record: (record.spec.round_index, record.spec.action_id),
        )
        for record in ordered:
            if record.status not in _TERMINAL_ACTION_STATUSES:
                continue
            key = self.scheduler._target_key(record.spec)
            receipt = record.receipt
            keeper_advanced = bool(
                record.spec.action_id in self.state.frontier_advancing_action_ids
            )
            informative = (
                bool(record.objective_measurement and record.objective_measurement.valid)
                or bool(
                    receipt
                    and receipt.evidence_channel_confirmed
                    and (
                        receipt.state_changes
                        or receipt.decisions_changed
                        or receipt.obligations_unlocked
                        or receipt.obligations_invalidated
                        or receipt.forecast_was_useful
                        or receipt.evidence_strength in {"strong", "decisive"}
                    )
                )
                or keeper_advanced
            )
            stalls[key] = 0 if informative else stalls.get(key, 0) + 1
        return stalls

    @staticmethod
    def _issue_sort_key(item: tuple[int, IssueDraft]) -> tuple[int, int, int]:
        index, draft = item
        return (
            -_IMPACT_RANK[draft.impact],
            -_UNCERTAINTY_RANK[draft.uncertainty.value],
            index,
        )

    @staticmethod
    def _issue_local_keys(issues: Iterable[Issue]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for issue in issues:
            mapping[issue.issue_id] = issue.issue_id
            for tag in issue.tags:
                if tag.startswith("local-key:"):
                    key = tag.removeprefix("local-key:")
                    mapping[key] = issue.issue_id
        return mapping

    def _instantiate_issue_drafts(
        self,
        drafts: Sequence[IssueDraft],
        *,
        existing: Iterable[Issue] = (),
        active_capacity: int | None = None,
    ) -> tuple[list[Issue], dict[str, str], list[str]]:
        existing_list = list(existing)
        keymap = self._issue_local_keys(existing_list)
        if active_capacity is None:
            active_capacity = self.config.frontier.max_open_issues
        selected = sorted(enumerate(drafts), key=self._issue_sort_key)[: max(0, active_capacity)]
        dropped = [
            draft.title
            for index, draft in enumerate(drafts)
            if index not in {item[0] for item in selected}
        ]
        created: list[tuple[IssueDraft, Issue]] = []
        next_seq = self.journal.count() + 1
        for _, draft in selected:
            normalized = normalize_key(draft.local_key)
            if not normalized:
                normalized = safe_slug(draft.title)
            if normalized in keymap:
                # A repeated local key means the issue already exists; do not
                # create a duplicate frontier node.
                continue
            issue_id = new_id("iss")
            keymap[draft.local_key] = issue_id
            keymap[normalized] = issue_id
            tags = unique_preserving_order([*draft.tags, f"local-key:{normalized}"])
            issue = Issue(
                issue_id=issue_id,
                title=draft.title,
                description=draft.description,
                impact=draft.impact,
                uncertainty=draft.uncertainty,
                decision_sensitivity=draft.decision_sensitivity,
                tags=tags,
                created_seq=next_seq,
                updated_seq=next_seq,
            )
            created.append((draft, issue))

        for draft, issue in created:
            dependencies: list[str] = []
            for key in draft.depends_on_keys:
                resolved = keymap.get(key) or keymap.get(normalize_key(key))
                if resolved and resolved != issue.issue_id:
                    dependencies.append(resolved)
            issue.depends_on = unique_preserving_order(dependencies)
        return [issue for _, issue in created], keymap, dropped

    @staticmethod
    def _obligation_local_keys(obligations: Iterable[Obligation]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for item in obligations:
            mapping[item.obligation_id] = item.obligation_id
            for tag in item.tags:
                if tag.startswith("local-key:"):
                    mapping[tag.removeprefix("local-key:")] = item.obligation_id
        return mapping

    @staticmethod
    def _crux_local_keys(cruxes: Iterable[Crux]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for item in cruxes:
            mapping[item.crux_id] = item.crux_id
            for tag in item.tags:
                if tag.startswith("local-key:"):
                    mapping[tag.removeprefix("local-key:")] = item.crux_id
        return mapping

    def _compile_epistemic_action(self, proposal: ActionProposal) -> ActionProposal:
        """Keep unambiguously conceptual work in one continuous solver thread."""

        if not self.config.cognition.thought_first:
            return proposal
        if proposal.epistemic_mode == EpistemicMode.THINK:
            if proposal.topology == CognitiveTopology.SUMMIT:
                return proposal
            return proposal.model_copy(update={"topology": CognitiveTopology.LEAD})
        if proposal.epistemic_mode != EpistemicMode.AUTO:
            return proposal
        conceptual = proposal.kind in {
            ActionKind.EXPLORE,
            ActionKind.DISCRIMINATE,
            ActionKind.REFRAME,
            ActionKind.CEILING_AUDIT,
        }
        has_execution_surface = bool(
            proposal.observation_modalities
            or proposal.instrument is not None
            or proposal.intervention.strip()
            or proposal.network
            or proposal.topology == CognitiveTopology.SUMMIT
            or proposal.independence_class
            in {
                IndependenceClass.DETERMINISTIC_TOOL,
                IndependenceClass.EXTERNAL_EVIDENCE,
                IndependenceClass.HUMAN,
                IndependenceClass.REAL_WORLD,
            }
        )
        if proposal.topology == CognitiveTopology.SUMMIT:
            return proposal.model_copy(update={"epistemic_mode": EpistemicMode.THINK})
        if proposal.kind == ActionKind.ACQUIRE:
            return proposal.model_copy(update={"epistemic_mode": EpistemicMode.RETRIEVE})
        if proposal.kind == ActionKind.INSTRUMENT or proposal.instrument is not None:
            return proposal.model_copy(update={"epistemic_mode": EpistemicMode.BUILD})
        if has_execution_surface:
            return proposal.model_copy(update={"epistemic_mode": EpistemicMode.EXECUTE})
        if proposal.kind in {
            ActionKind.EXPLOIT,
            ActionKind.REPAIR,
            ActionKind.TOOL,
            ActionKind.INTEGRATE,
            ActionKind.RECONSTRUCT,
        }:
            return proposal.model_copy(update={"epistemic_mode": EpistemicMode.BUILD})
        if conceptual or proposal.kind == ActionKind.MECHANISM_GRAFT:
            return proposal.model_copy(
                update={
                    "epistemic_mode": EpistemicMode.THINK,
                    "topology": CognitiveTopology.LEAD,
                }
            )
        return proposal

    def _instantiate_actions(
        self,
        proposals: Sequence[ActionProposal],
        *,
        issue_keymap: dict[str, str],
        obligation_keymap: dict[str, str] | None = None,
        crux_keymap: dict[str, str] | None = None,
        round_index: int,
    ) -> tuple[list[ActionSpec], list[ActionContract], list[str]]:
        actions: list[ActionSpec] = []
        contracts: list[ActionContract] = []
        dropped: list[str] = []
        obligation_keymap = obligation_keymap or {}
        crux_keymap = crux_keymap or {}
        for proposal in proposals:
            proposal = self._compile_epistemic_action(proposal)
            resolved_issues, resolved_obligations, resolved_cruxes = (
                self._resolve_action_links(
                    proposal,
                    issue_keymap=issue_keymap,
                    obligation_keymap=obligation_keymap,
                    crux_keymap=crux_keymap,
                )
            )
            rejection = self._action_rejection(
                proposal,
                action_count=len(actions),
                obligation_ids=resolved_obligations,
                crux_ids=resolved_cruxes,
            )
            if rejection is not None:
                dropped.append(rejection)
                continue
            payload = proposal.model_dump(mode="python")
            if not self.config.cognition.human_evidence_available:
                payload["observation_modalities"] = [
                    modality
                    for modality in proposal.observation_modalities
                    if modality != EvidenceModality.HUMAN_OBSERVATION
                ]
            payload["issue_ids"] = unique_preserving_order(resolved_issues)
            payload["obligation_ids"] = unique_preserving_order(resolved_obligations)
            payload["obligation_keys"] = []
            payload["crux_ids"] = unique_preserving_order(resolved_cruxes)
            payload["crux_keys"] = []
            action_id = new_id("act")
            action = ActionSpec(
                **payload,
                action_id=action_id,
                round_index=round_index,
            )
            actions.append(action)
            if self.config.cognition.mode == "adaptive" and self.config.cognition.action_contracts:
                contracts.append(
                    build_action_contract(
                        proposal,
                        action_id=action_id,
                        obligation_ids=action.obligation_ids,
                        crux_ids=action.crux_ids,
                    )
                )
        return actions, contracts, dropped

    @staticmethod
    def _resolve_reference_ids(
        references: Iterable[str],
        *,
        local_keys: Mapping[str, str],
        existing_ids: Mapping[str, Any],
    ) -> list[str]:
        resolved: list[str] = []
        for reference in references:
            canonical = (
                local_keys.get(reference)
                or local_keys.get(normalize_key(reference))
                or (reference if reference in existing_ids else None)
            )
            if canonical is not None:
                resolved.append(canonical)
        return unique_preserving_order(resolved)

    def _resolve_action_links(
        self,
        proposal: ActionProposal,
        *,
        issue_keymap: Mapping[str, str],
        obligation_keymap: Mapping[str, str],
        crux_keymap: Mapping[str, str],
    ) -> tuple[list[str], list[str], list[str]]:
        issues = self._resolve_reference_ids(
            proposal.issue_ids,
            local_keys=issue_keymap,
            existing_ids=self.state.issues,
        )
        # During migration, issue-local keys may also identify the adaptive
        # obligation or crux derived from that issue.
        obligations = self._resolve_reference_ids(
            [*proposal.issue_ids, *proposal.obligation_ids, *proposal.obligation_keys],
            local_keys=obligation_keymap,
            existing_ids=self.state.obligations,
        )
        cruxes = self._resolve_reference_ids(
            [*proposal.issue_ids, *proposal.crux_ids, *proposal.crux_keys],
            local_keys=crux_keymap,
            existing_ids=self.state.cruxes,
        )
        return issues, obligations, cruxes

    def _action_rejection(
        self,
        proposal: ActionProposal,
        *,
        action_count: int,
        obligation_ids: Sequence[str],
        crux_ids: Sequence[str],
    ) -> str | None:
        if proposal.kind == ActionKind.STOP:
            return f"stop proposal for {proposal.target}"
        if action_count >= self.config.frontier.max_actions_per_batch * 2:
            return proposal.assignment
        adaptive = self.config.cognition.mode == "adaptive"
        if (
            adaptive
            and self.config.cognition.action_contracts
            and not proposal.could_change_decision
        ):
            return f"non-decision-relevant action: {proposal.assignment}"
        attached_lineage = proposal.topology == CognitiveTopology.SUMMIT and bool(
            proposal.lineage_id or proposal.parent_lineage_ids
        )
        frame_break = proposal.kind in {
            ActionKind.REFRAME,
            ActionKind.RECONSTRUCT,
            ActionKind.CEILING_AUDIT,
        }
        if (
            adaptive
            and proposal.substantive
            and not crux_ids
            and not obligation_ids
            and not attached_lineage
            and not frame_break
        ):
            return f"unattached adaptive action: {proposal.assignment}"
        return None

    def _apply_issue_updates(
        self,
        updates: Sequence[IssueUpdate],
        new_drafts: Sequence[IssueDraft],
    ) -> tuple[list[Issue], dict[str, str], list[str]]:
        upserts: list[Issue] = []
        ignored: list[str] = []
        next_seq = self.journal.count() + 1
        projected: dict[str, Issue] = {
            key: value.model_copy(deep=True) for key, value in self.state.issues.items()
        }
        for update in updates:
            issue = projected.get(update.issue_id)
            if issue is None:
                ignored.append(f"unknown issue update: {update.issue_id}")
                continue
            for field in (
                "status",
                "title",
                "description",
                "impact",
                "uncertainty",
                "decision_sensitivity",
                "resolution",
            ):
                value = getattr(update, field)
                if value is not None:
                    setattr(issue, field, value)
            issue.evidence_for = unique_preserving_order(
                [*issue.evidence_for, *update.evidence_for]
            )
            issue.evidence_against = unique_preserving_order(
                [*issue.evidence_against, *update.evidence_against]
            )
            issue.tags = unique_preserving_order([*issue.tags, *update.tags_to_add])
            issue.updated_seq = next_seq
            projected[issue.issue_id] = issue
            upserts.append(issue)

        open_count = sum(1 for issue in projected.values() if issue.status == IssueStatus.OPEN)
        capacity = max(0, self.config.frontier.max_open_issues - open_count)
        created, keymap, dropped = self._instantiate_issue_drafts(
            new_drafts,
            existing=projected.values(),
            active_capacity=capacity,
        )
        upserts.extend(created)
        ignored.extend(f"frontier capacity dropped issue: {title}" for title in dropped)
        return upserts, keymap, ignored

    def _clone_artifact(
        self,
        artifact: ArtifactRef,
        *,
        summary: str,
        source_action_ids: list[str] | None = None,
    ) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=new_id("art"),
            version=artifact.version + 1,
            blob=artifact.blob,
            kind=artifact.kind,
            summary=summary,
            parent_artifact_id=artifact.artifact_id,
            source_action_ids=source_action_ids or [],
            deliverables=list(artifact.deliverables),
            created_at=utc_now(),
        )

    # ------------------------------------------------------------------
    # Provider boundary and trace capture
    # ------------------------------------------------------------------
    def _capture_optional_file(
        self,
        path: Path | None,
        *,
        media_type: str,
        original_name: str,
    ) -> BlobRef | None:
        if path is None or not path.is_file():
            return None
        return self.blobs.put_file(
            path,
            media_type=media_type,
            original_name=original_name,
        )

    def _trace_from_result(
        self,
        *,
        prompt_path: Path,
        schema_path: Path,
        boundary_path: Path,
        result: ProviderCallResult[Any],
    ) -> CallTrace:
        return CallTrace(
            prompt_blob=self._capture_optional_file(
                prompt_path,
                media_type="text/markdown; charset=utf-8",
                original_name="role-prompt.md",
            ),
            schema_blob=self._capture_optional_file(
                schema_path,
                media_type="application/schema+json",
                original_name="boundary.schema.json",
            ),
            boundary_blob=self._capture_optional_file(
                boundary_path,
                media_type="application/json",
                original_name="boundary.json",
            ),
            raw_events_blob=(
                self._capture_optional_file(
                    result.raw_events_path,
                    media_type="application/x-ndjson",
                    original_name="codex-events.jsonl",
                )
                if self.config.runtime.retain_raw_codex_events
                else None
            ),
            stderr_blob=self._capture_optional_file(
                result.stderr_path,
                media_type="text/plain; charset=utf-8",
                original_name="codex-stderr.log",
            ),
            command=result.command,
            thread_id=result.thread_id,
            resumed=result.resumed,
            continuity_mode="resumed" if result.resumed else "new-session",
            provider_trace_summary=result.trace_summary.model_dump(mode="json"),
        )

    def _trace_from_error(
        self,
        *,
        prompt_path: Path,
        schema_path: Path,
        boundary_path: Path,
        error: BaseException,
    ) -> tuple[Usage, CallTrace]:
        if isinstance(error, ProviderCallError):
            usage = error.usage
            raw_events_path = error.raw_events_path
            stderr_path = error.stderr_path
            command = error.command
            provider_trace_summary = error.trace_summary
            thread_id = error.thread_id
        else:
            usage = Usage()
            raw_events_path = None
            stderr_path = None
            command = []
            provider_trace_summary = {}
            thread_id = None
        trace = CallTrace(
            prompt_blob=self._capture_optional_file(
                prompt_path,
                media_type="text/markdown; charset=utf-8",
                original_name="role-prompt.md",
            ),
            schema_blob=self._capture_optional_file(
                schema_path,
                media_type="application/schema+json",
                original_name="boundary.schema.json",
            ),
            boundary_blob=self._capture_optional_file(
                boundary_path,
                media_type="application/json",
                original_name="boundary.json",
            ),
            raw_events_blob=(
                self._capture_optional_file(
                    raw_events_path,
                    media_type="application/x-ndjson",
                    original_name="codex-events.jsonl",
                )
                if self.config.runtime.retain_raw_codex_events
                else None
            ),
            stderr_blob=self._capture_optional_file(
                stderr_path,
                media_type="text/plain; charset=utf-8",
                original_name="codex-stderr.log",
            ),
            command=command,
            thread_id=thread_id,
            provider_trace_summary=provider_trace_summary,
        )
        return usage, trace

    async def _invoke(
        self,
        workspace: CallWorkspace,
        *,
        call_kind: str,
        role: Role,
        prompt: str,
        response_model: type[ResponseT],
        sandbox: Any,
        network_access: bool,
        image_paths: Sequence[Path],
        metadata: dict[str, Any],
        use_lead: bool = False,
        max_provider_calls: int | None = None,
        resume_thread_id_override: str | None = None,
    ) -> tuple[ProviderCallResult[ResponseT], CallTrace]:
        prompt_path = workspace.context_dir / "ROLE_PROMPT.md"
        boundary_path = workspace.output_dir / "boundary.json"
        schema_path = workspace.output_dir / "boundary.schema.json"
        atomic_write_text(prompt_path, prompt)
        provider_call_limit = min(
            max_provider_calls or self.config.provider.schema_attempts,
            self._calls_remaining(),
        )
        if provider_call_limit < 1:
            raise ProviderError("No provider-call budget remains for this harness turn")
        action_id_value = metadata.get("action_id")
        action_id = str(action_id_value) if action_id_value else None
        request = ProviderCallRequest[ResponseT](
            call_id=workspace.call_id,
            call_kind=call_kind,
            role=role,
            prompt=prompt,
            cwd=workspace.cwd,
            response_model=response_model,
            output_path=boundary_path,
            schema_path=schema_path,
            sandbox=sandbox,
            network_access=network_access,
            image_paths=list(image_paths),
            expected_artifact_path=workspace.expected_artifact_path,
            resume_thread_id=(
                resume_thread_id_override
                or (
                    self.state.lead_session.thread_id
                    if use_lead
                    and self.config.cognition.persistent_lead
                    and self.config.provider.resume_lead_sessions
                    and self.state.lead_session.thread_id
                    else None
                )
            ),
            preserve_session=(
                use_lead
                and self.config.cognition.persistent_lead
                and self.config.provider.persist_lead_sessions
            ),
            lead_call=use_lead,
            max_provider_calls=provider_call_limit,
            activity_callback=self.observer.provider_callback(
                call_id=workspace.call_id,
                call_kind=call_kind,
                action_id=action_id,
            ),
            metadata={
                **metadata,
                "provider_session_dir": str(self.run_dir / "provider-sessions"),
                "provider_lead_cwd": str(self.run_dir / "provider-sessions" / "lead-workspace"),
            },
        )
        reconstructed = False
        resume_failure_usage = Usage()
        resume_failure_trace = CallTrace()
        self.observer.begin_call(
            phase=self.state.phase.value,
            call_id=workspace.call_id,
            call_kind=call_kind,
            action_id=action_id,
        )
        try:
            result = await self.provider.run(request)
        except (ProviderCallError, ProviderError) as resume_error:
            if not (
                use_lead
                and request.resume_thread_id
                and self.config.provider.resume_fallback_to_reconstruction
                and self.config.cognition.fallback_to_sparse
            ):
                usage, trace = self._trace_from_error(
                    prompt_path=prompt_path,
                    schema_path=schema_path,
                    boundary_path=boundary_path,
                    error=resume_error,
                )
                resume_error.frontier_usage = usage  # type: ignore[attr-defined]
                resume_error.frontier_trace = trace  # type: ignore[attr-defined]
                self.observer.finish_call(
                    workspace.call_id, phase=self.state.phase.value, failed=True
                )
                raise
            reconstructed = True
            resume_failure_usage, resume_failure_trace = self._trace_from_error(
                prompt_path=prompt_path,
                schema_path=schema_path,
                boundary_path=boundary_path,
                error=resume_error,
            )
            remaining_provider_calls = request.max_provider_calls - getattr(
                resume_error, "boundary_attempts", 1
            )
            if remaining_provider_calls < 1:
                resume_error.frontier_usage = resume_failure_usage  # type: ignore[attr-defined]
                resume_error.frontier_trace = resume_failure_trace  # type: ignore[attr-defined]
                self.observer.finish_call(
                    workspace.call_id, phase=self.state.phase.value, failed=True
                )
                raise
            recovery_prompt = (
                "CONTINUITY RECOVERY: the prior Lead session could not be resumed. "
                "Reconstruct the exact current task model from the explicit capsule, preserve "
                "the artifact and all accepted evidence, and include a strict continuity acknowledgement.\n\n"
                + prompt
            )
            atomic_write_text(prompt_path, recovery_prompt)
            request = request.model_copy(
                update={
                    "prompt": recovery_prompt,
                    "resume_thread_id": None,
                    "preserve_session": True,
                    "max_provider_calls": remaining_provider_calls,
                    "metadata": {**request.metadata, "continuity_recovery": True},
                }
            )
            try:
                result = await self.provider.run(request)
                if resume_failure_usage.calls or resume_failure_usage.wall_seconds:
                    result = result.model_copy(
                        update={
                            "usage": resume_failure_usage.plus(result.usage),
                            "duration_seconds": (
                                resume_failure_usage.wall_seconds + result.duration_seconds
                            ),
                        }
                    )
            except BaseException as recovery_error:
                recovery_usage, recovery_trace = self._trace_from_error(
                    prompt_path=prompt_path,
                    schema_path=schema_path,
                    boundary_path=boundary_path,
                    error=recovery_error,
                )
                usage = resume_failure_usage.plus(recovery_usage)
                recovery_trace.continuity_mode = "reconstruction-failed"
                recovery_error.resume_error = resume_error  # type: ignore[attr-defined]
                recovery_error.frontier_usage = usage  # type: ignore[attr-defined]
                recovery_error.frontier_trace = recovery_trace  # type: ignore[attr-defined]
                self.observer.finish_call(
                    workspace.call_id, phase=self.state.phase.value, failed=True
                )
                raise
        except BaseException as exc:
            usage, trace = self._trace_from_error(
                prompt_path=prompt_path,
                schema_path=schema_path,
                boundary_path=boundary_path,
                error=exc,
            )
            exc.frontier_usage = usage  # type: ignore[attr-defined]
            exc.frontier_trace = trace  # type: ignore[attr-defined]
            self.observer.finish_call(workspace.call_id, phase=self.state.phase.value, failed=True)
            raise
        trace = self._trace_from_result(
            prompt_path=prompt_path,
            schema_path=schema_path,
            boundary_path=boundary_path,
            result=result,
        )
        self.observer.finish_call(workspace.call_id, phase=self.state.phase.value)
        if use_lead and self.config.cognition.persistent_lead:
            ack = getattr(result.response, "continuity_ack", None)
            ack_status, ack_problems = validate_lead_ack(
                ack,
                state=self.state,
                artifact=self.state.current_artifact,
            )
            if reconstructed:
                status = (
                    LeadContinuityStatus.RECONSTRUCTED_VERIFIED
                    if ack_status == LeadContinuityStatus.CONTINUOUS
                    else LeadContinuityStatus.DEGRADED
                )
                trace.continuity_mode = "reconstructed"
            else:
                status = ack_status
                trace.continuity_mode = "resumed" if result.resumed else "new-lead"
            lead = self.state.lead_session.model_copy(deep=True)
            lead.thread_id = result.thread_id or lead.thread_id
            lead.status = status
            lead.turns += 1
            lead.last_call_id = workspace.call_id
            lead.last_ack = ack
            if reconstructed and status != LeadContinuityStatus.RECONSTRUCTED_VERIFIED:
                lead.reconstruction_failures += 1
            lead.degraded_reason = "; ".join(ack_problems) if ack_problems else None
            self._append(
                et.LEAD_RECONSTRUCTION if reconstructed else et.LEAD_SESSION_UPDATED,
                {
                    "lead_session": lead.model_dump(mode="json"),
                    "ack_problems": ack_problems,
                    "call_id": workspace.call_id,
                    "resume_failure_trace": (
                        resume_failure_trace.payload() if reconstructed else None
                    ),
                },
                actor="continuity",
            )
        return result, trace

    @staticmethod
    def _failure_parts(error: BaseException) -> tuple[Usage, CallTrace]:
        usage = getattr(error, "frontier_usage", Usage())
        trace = getattr(error, "frontier_trace", CallTrace())
        return cast(Usage, usage), cast(CallTrace, trace)

    def _capture_recovery_artifact(
        self,
        workspace: CallWorkspace,
        *,
        summary: str,
        parent: ArtifactRef | None,
        source_action_ids: list[str],
    ) -> tuple[ArtifactRef | None, str | None]:
        try:
            return (
                self.adapter.capture_candidate_artifact(
                    workspace,
                    summary=summary,
                    parent=parent,
                    source_action_ids=source_action_ids,
                ),
                None,
            )
        except BaseException as recovery_error:
            return None, f"{type(recovery_error).__name__}: {recovery_error}"

    def _ensure_artifact_file(
        self,
        workspace: CallWorkspace,
        *,
        declared_path: str,
        summary: str,
        current_artifact: ArtifactRef | None,
    ) -> tuple[str, list[str]]:
        notes: list[str] = []
        candidates = [declared_path]
        expected_relative = workspace.expected_artifact_path.relative_to(workspace.cwd).as_posix()
        if expected_relative not in candidates:
            candidates.append(expected_relative)
        for candidate in candidates:
            try:
                path = self.adapter.resolve_declared_path(workspace, candidate)
            except ValueError:
                notes.append(f"rejected escaping artifact path: {candidate}")
                continue
            if path.is_file():
                return path.relative_to(workspace.cwd).as_posix(), notes

        expected = workspace.expected_artifact_path
        expected.parent.mkdir(parents=True, exist_ok=True)
        if self._software:
            atomic_write_text(
                expected,
                "# Artifact summary\n\n"
                + (
                    summary.strip()
                    or "Repository state captured; the model omitted its summary file."
                )
                + "\n",
            )
        elif current_artifact is not None:
            self.blobs.materialize(current_artifact.blob, expected)
        else:
            atomic_write_text(
                expected,
                "# Baseline artifact\n\n"
                + (summary.strip() or "The provider omitted the substantive artifact file.")
                + "\n",
            )
        notes.append(
            "model omitted the declared artifact file; deterministic boundary recovery used the expected path"
        )
        return expected_relative, notes

    def _ensure_worker_result_file(
        self,
        workspace: CallWorkspace,
        envelope: WorkerEnvelope,
    ) -> tuple[str, list[str]]:
        notes: list[str] = []
        expected = workspace.output_dir / "result.md"
        candidates = [envelope.result_or_artifact_reference]
        expected_relative = expected.relative_to(workspace.cwd).as_posix()
        if expected_relative not in candidates:
            candidates.append(expected_relative)
        for candidate in candidates:
            try:
                path = self.adapter.resolve_declared_path(workspace, candidate)
            except ValueError:
                notes.append(f"rejected escaping result path: {candidate}")
                continue
            if path.is_file():
                return path.relative_to(workspace.cwd).as_posix(), notes

        lines = [
            "# Targeted result",
            "",
            f"Target: {envelope.target}",
            "",
            "## Findings",
            "",
            *[f"- {item}" for item in envelope.findings],
            "",
            "## Scope",
            "",
            envelope.scope or "Scope was not explicitly supplied.",
            "",
            "## Unresolved risks",
            "",
            *([f"- {item}" for item in envelope.unresolved_risks] or ["- None stated."]),
        ]
        atomic_write_text(expected, "\n".join(lines) + "\n")
        notes.append(
            "model omitted the result file; deterministic boundary recovery materialized the structured findings"
        )
        return expected_relative, notes

    # ------------------------------------------------------------------
    # Bootstrap and active frontier
    # ------------------------------------------------------------------
    def _bootstrap_recovery_context(self) -> tuple[ArtifactRef | None, str | None]:
        for event in reversed(self.events()):
            if event.event_type == et.BOOTSTRAP_COMPLETED:
                break
            if event.event_type != et.BOOTSTRAP_FAILED:
                continue
            recovery_artifact = None
            if event.payload.get("recovery_artifact"):
                recovery_artifact = ArtifactRef.model_validate(event.payload["recovery_artifact"])
            thread_id = str(event.payload.get("provider_thread_id") or "") or None
            if thread_id is None and event.payload.get("raw_events_blob"):
                try:
                    trace_ref = BlobRef.model_validate(event.payload["raw_events_blob"])
                    for line in self.blobs.read_text(trace_ref).splitlines():
                        record = json.loads(line)
                        safe_event = record.get("event", record)
                        if (
                            isinstance(safe_event, dict)
                            and safe_event.get("type") == "session"
                            and isinstance(safe_event.get("id"), str)
                        ):
                            thread_id = safe_event["id"]
                except (OSError, ValueError, json.JSONDecodeError, LedgerIntegrityError):
                    thread_id = None
            return recovery_artifact, thread_id
        return None, None

    def _bootstrap_obligation_drafts(
        self,
        *,
        output: BootstrapOutput,
        task_source: TaskSource,
        contract: GoalContract,
        charter: TaskCharter,
    ) -> tuple[TaskCharter, list[ObligationDraft]]:
        if self.config.cognition.mode != "adaptive":
            return charter, []

        drafts = list(output.obligations)
        charter, guards = compile_guard_obligations(
            task_source,
            contract,
            charter,
            existing_drafts=drafts,
        )
        guards.extend(
            ObligationDraft(
                local_key=f"release_artifact_{index}",
                title=f"Capture declared release artifact {index}",
                requirement=f"A final artifact matching `{pattern}` must be captured durably.",
                kind="construction",
                acceptance=(
                    "The final ArtifactRef contains the exact generated bytes in its "
                    "durable deliverables manifest."
                ),
                impact=Impact.FATAL,
                release_blocking=True,
                required_artifact_scope="release",
                tags=["runtime-guard", "durable-deliverable"],
            )
            for index, pattern in enumerate(self.config.software.release_artifacts, start=1)
        )
        by_requirement = {normalize_key(item.requirement): item for item in drafts}
        scope_rank = {"targeted": 0, "sequence": 1, "whole_artifact": 2, "release": 3}
        for guard in guards:
            incumbent = by_requirement.get(normalize_key(guard.requirement))
            if incumbent is None:
                drafts.append(guard)
                by_requirement[normalize_key(guard.requirement)] = guard
                continue
            incumbent.release_blocking = incumbent.release_blocking or guard.release_blocking
            if guard.impact == Impact.FATAL:
                incumbent.impact = Impact.FATAL
            incumbent.source_requirement_ids = unique_preserving_order(
                [*incumbent.source_requirement_ids, *guard.source_requirement_ids]
            )
            incumbent.required_evidence_modalities = unique_preserving_order(
                [
                    *incumbent.required_evidence_modalities,
                    *guard.required_evidence_modalities,
                ]
            )
            if (
                scope_rank[guard.required_artifact_scope]
                > scope_rank[incumbent.required_artifact_scope]
            ):
                incumbent.required_artifact_scope = guard.required_artifact_scope
            incumbent.tags = unique_preserving_order([*incumbent.tags, *guard.tags])
        return charter, drafts

    def _bootstrap_crux_drafts(
        self,
        *,
        output: BootstrapOutput,
        issues: Sequence[Issue],
    ) -> list[CruxDraft]:
        if self.config.cognition.mode != "adaptive":
            return []
        drafts = list(output.cruxes)
        if drafts:
            return drafts
        for index, issue in enumerate(issues[: self.config.cognition.max_active_cruxes]):
            local_key = next(
                (
                    tag.removeprefix("local-key:")
                    for tag in issue.tags
                    if tag.startswith("local-key:")
                ),
                f"issue_{index + 1}",
            )
            drafts.append(
                CruxDraft(
                    local_key=local_key,
                    title=issue.title,
                    uncertainty=issue.description,
                    decision_controlled=issue.decision_sensitivity,
                    why_it_matters=issue.decision_sensitivity,
                    obligation_keys=["deliverable"],
                    discriminating_evidence=[],
                    unlock_value=issue.impact,
                )
            )
        return drafts

    def _bootstrap_summit_active(
        self,
        output: BootstrapOutput,
        reasons: list[str],
    ) -> tuple[bool, list[str]]:
        if self.config.cognition.mode != "adaptive":
            return False, reasons
        if self.config.summit.mode == "on":
            return True, unique_preserving_order(["summit.mode=on", *reasons])
        if self.config.summit.mode != "auto" or not reasons:
            return False, reasons
        concrete_enough = (
            not self.config.summit.require_concrete_auto_trigger
            or bool(output.ceiling_scan and output.ceiling_scan.concrete_trigger)
        )
        return concrete_enough, reasons

    def _bootstrap_proposals(
        self,
        *,
        output: BootstrapOutput,
        summit_active: bool,
        lineages: Mapping[str, SummitLineage],
        discovery_records: Mapping[str, Any],
        obligations: Sequence[Obligation],
        cruxes: Sequence[Crux],
        reasons: Sequence[str],
    ) -> list[ActionProposal]:
        proposals = list(output.actions)
        has_summit_action = any(
            item.topology == CognitiveTopology.SUMMIT for item in proposals
        )
        target_cruxes = [
            item.crux_id for item in cruxes if item.status == CruxStatus.ACTIVE
        ][:1]
        target_obligations = [
            item.obligation_id for item in obligations if item.release_blocking
        ][:2]
        summit_reason = reasons[0] if reasons else "summit.mode=on"
        if summit_active and not lineages and not has_summit_action:
            proposals.append(
                ActionProposal(
                    kind=ActionKind.EXPLORE,
                    target="exact-task upper-tail mechanism search",
                    assignment=(
                        "Seed at most two genuinely mechanistically distinct Summit lineages "
                        "for the exact immutable task. Each lineage must name a concrete "
                        "mechanism, assumptions, discriminating prediction, next dependency, "
                        "and a bounded kill condition. Do not generate alternate objectives "
                        "or cosmetic variants."
                    ),
                    obligation_ids=target_obligations,
                    crux_ids=target_cruxes,
                    impact=Impact.HIGH,
                    cost=CostBand.MODERATE,
                    independence_class=IndependenceClass.DIFFERENT_CONDITIONING,
                    topology=CognitiveTopology.SUMMIT,
                    epistemic_mode=EpistemicMode.THINK,
                    expected_decision_effect=(
                        "Either establish a viable distant mechanism for the same task, or "
                        "record why upper-tail expansion is not justified."
                    ),
                    reusable_value=ValueBand.HIGH,
                    distinctive_angle="mechanism-level exact-task support expansion",
                    summit_reason=summit_reason,
                )
            )
        elif (
            summit_active
            and lineages
            and self.config.summit.experimental_frontier
            and not has_summit_action
        ):
            plans = self.experimental_frontier.select(
                dict(lineages),
                dict(discovery_records),
                limit=self.config.summit.max_discovery_actions_per_round,
            )
            proposals.extend(
                self.experimental_frontier.to_action(
                    plan,
                    crux_ids=target_cruxes,
                    obligation_ids=target_obligations,
                    summit_reason=summit_reason,
                )
                for plan in plans
            )
        if not proposals:
            proposals.extend(
                self._fallback_action_proposals(
                    obligations={item.obligation_id: item for item in obligations},
                    cruxes={item.crux_id: item for item in cruxes},
                )
            )
        return proposals

    @staticmethod
    def _bootstrap_stop_signals(
        *,
        output: BootstrapOutput,
        actions: Sequence[ActionSpec],
        issues: Sequence[Issue],
        cruxes: Sequence[Crux],
        semantic_profiles: Sequence[str],
    ) -> tuple[bool, str | None, bool]:
        high_open = any(issue.impact in {Impact.FATAL, Impact.HIGH} for issue in issues)
        active_crux = any(item.status == CruxStatus.ACTIVE for item in cruxes)
        qualitative_architecture = bool(set(semantic_profiles).intersection({"creative", "media"}))
        full_scope = output.artifact_scope in {"whole_artifact", "release"}
        independent_checkpoint_required = bool(
            (full_scope or qualitative_architecture)
            and (high_open or active_crux or full_scope)
        )
        stop_requested = bool(
            output.quality_floor_reached
            and not high_open
            and not active_crux
            and not any(action.topology == CognitiveTopology.SUMMIT for action in actions)
        )
        stop_reason = output.stop_reason if stop_requested else None
        if not actions and not high_open and not active_crux:
            stop_requested = True
            stop_reason = stop_reason or (
                "Baseline reached the quality floor without a decision-relevant frontier."
            )
        return stop_requested, stop_reason, independent_checkpoint_required

    async def _bootstrap(self) -> None:
        if not self._can_call():
            raise ProviderError("No model-call budget is available for the required baseline")
        recovery_artifact, recovery_thread_id = self._bootstrap_recovery_context()
        call_id = new_id("call")
        self._append(
            et.BOOTSTRAP_STARTED,
            {"call_id": call_id, "started_at": utc_now()},
            actor="controller",
        )
        workspace = self.adapter.open_call(
            call_id=call_id,
            call_kind="bootstrap",
            current_artifact=recovery_artifact,
        )
        try:
            capsule = self._capsules.populate(
                workspace,
                task=self.state.source_prompt,
                state=self.state,
                assignment=(
                    "Orient to the immutable request and construct the smallest decisive "
                    "artifact that can falsify the highest-leverage architectural choice. "
                    "Do not build the whole deliverable while a cheaper representative slice "
                    "can still expose a fatal direction error. Compile only the minimum "
                    "obligations/cruxes needed and expose decision-sensitive unresolved work."
                ),
                goal_contract=None,
                task_source=self.state.task_source,
                lens_purpose="bootstrap",
            )
            prompt = bootstrap_prompt(
                workspace,
                profile=self._profile,
                max_issues=self.config.frontier.max_open_issues,
                max_actions=self.config.frontier.max_actions_per_batch * 2,
                software=self._software,
                adaptive=self.config.cognition.mode == "adaptive",
                max_cruxes=self.config.cognition.max_active_cruxes,
                summit_mode=self.config.summit.mode,
            )
            if recovery_thread_id or recovery_artifact:
                prompt += (
                    "\n\nRecovery boundary: a previous bootstrap execution ended before "
                    "returning its structured boundary. Continue the retained provider session "
                    "and preserved workspace state. Treat the explicit workspace named in this "
                    "prompt as authoritative; old workspace paths in session history are stale. "
                    "Recover rather than repeat completed work, finish the smallest coherent "
                    "baseline, verify it, and return the required boundary object."
                )
            result, trace = await self._invoke(
                workspace,
                call_kind="bootstrap",
                role=Role.STRONG,
                prompt=prompt,
                response_model=BootstrapOutput,
                sandbox=self._bootstrap_sandbox(),
                network_access=self.config.provider.default_network_access,
                image_paths=[Path(item) for item in cast(list[str], capsule["image_paths"])],
                metadata={
                    "task": self.state.source_prompt,
                    "task_source_digest": self.state.task_source.digest
                    if self.state.task_source
                    else None,
                },
                use_lead=(
                    self.config.cognition.mode == "adaptive"
                    and self.config.cognition.persistent_lead
                ),
                resume_thread_id_override=(
                    recovery_thread_id if self.config.provider.resume_lead_sessions else None
                ),
            )
            output = result.response
            contract = output.goal_contract.model_copy(deep=True)
            contract.original_request = self.state.source_prompt
            contract.budget = self.config.run.budget.model_copy(deep=True)
            declared_path, normalization = self._ensure_artifact_file(
                workspace,
                declared_path=output.artifact_path,
                summary=output.artifact_summary,
                current_artifact=None,
            )
            artifact = self.adapter.capture_artifact(
                workspace,
                declared_path=declared_path,
                version=1,
                summary=output.artifact_summary,
                parent=None,
                source_action_ids=[],
            )

            task_source = self.state.task_source or capture_task_source(self.state.source_prompt)
            charter = output.task_charter or fallback_charter(task_source, contract)
            charter_notes: list[str] = []
            if charter.source_digest != task_source.digest:
                charter_notes.append(
                    "model task charter digest mismatch; deterministic fallback used"
                )
                charter = fallback_charter(task_source, contract)
            spine = output.artifact_spine or fallback_spine(contract, output.artifact_summary)

            issues, issue_keymap, dropped_issues = self._instantiate_issue_drafts(output.issues)

            charter, obligation_drafts = self._bootstrap_obligation_drafts(
                output=output,
                task_source=task_source,
                contract=contract,
                charter=charter,
            )
            obligations, obligation_keymap, obligation_notes = instantiate_obligations(
                obligation_drafts,
                existing=(),
                capacity=max(32, len(obligation_drafts)),
                created_seq=self.journal.count() + 1,
                charter=charter,
                human_evidence_available=self.config.cognition.human_evidence_available,
            )

            crux_drafts = self._bootstrap_crux_drafts(output=output, issues=issues)
            cruxes, crux_keymap, crux_notes = instantiate_cruxes(
                crux_drafts,
                obligations=obligations,
                existing=(),
                active_limit=self.config.cognition.max_active_cruxes,
                total_limit=max(8, self.config.cognition.max_active_cruxes * 4),
                created_seq=self.journal.count() + 1,
            )

            reasons = ceiling_trigger_reasons(output.ceiling_scan)
            summit_active, reasons = self._bootstrap_summit_active(output, reasons)

            lineages: dict[str, SummitLineage] = {}
            lineage_notes: dict[str, Any] = {}
            if summit_active and output.lineages:
                bootstrap_lineages = [
                    item.model_copy(update={"candidate_artifact": None}) for item in output.lineages
                ]
                lineages, decision = self.summit_archive.admit({}, bootstrap_lineages)
                lineage_notes = {
                    "accepted": decision.accepted,
                    "replaced": decision.replaced,
                    "rejected": decision.rejected,
                    "demoted": decision.demoted,
                }
            discovery_records = self.experimental_frontier.seed_records(lineages)
            overlays, overlay_notes = admit_overlays(
                output.overlays if summit_active else [],
                existing={},
                normal_limit=self.config.cognition.normal_overlay_limit,
                hard_limit=self.config.cognition.hard_overlay_limit,
                require_behavioral_difference=(
                    self.config.cognition.require_behavioral_overlay_difference
                ),
            )

            proposals = self._bootstrap_proposals(
                output=output,
                summit_active=summit_active,
                lineages=lineages,
                discovery_records=discovery_records,
                obligations=obligations,
                cruxes=cruxes,
                reasons=reasons,
            )
            actions, action_contracts, dropped_actions = self._instantiate_actions(
                proposals,
                issue_keymap=issue_keymap,
                obligation_keymap=obligation_keymap,
                crux_keymap=crux_keymap,
                round_index=1,
            )
            frontier_kernel, frontier_notes = reconcile_frontier_kernel(
                None,
                output.frontier_kernel,
                cruxes=cruxes,
                spine=spine,
                next_actions=actions,
                round_index=0,
            )
            profiles = set(self.config.run.semantic_profiles)
            profiles.update(contract.semantic_profiles)
            stop_requested, stop_reason, independent_checkpoint_required = (
                self._bootstrap_stop_signals(
                    output=output,
                    actions=actions,
                    issues=issues,
                    cruxes=cruxes,
                    semantic_profiles=list(profiles),
                )
            )
            payload = {
                "contract": contract.model_dump(mode="json"),
                "task_charter": charter.model_dump(mode="json"),
                "artifact_spine": spine.model_dump(mode="json"),
                "frontier_kernel": frontier_kernel.model_dump(mode="json"),
                "artifact": artifact.model_dump(mode="json"),
                "artifact_scope": output.artifact_scope,
                "independent_checkpoint_required": independent_checkpoint_required,
                "issues": [item.model_dump(mode="json") for item in issues],
                "obligations": [item.model_dump(mode="json") for item in obligations],
                "cruxes": [item.model_dump(mode="json") for item in cruxes],
                "overlays": [item.model_dump(mode="json") for item in overlays.values()],
                "lineages": [item.model_dump(mode="json") for item in lineages.values()],
                "discovery_records": [
                    item.model_dump(mode="json") for item in discovery_records.values()
                ],
                "summit_active": summit_active,
                "summit_reasons": reasons,
                "actions": [item.model_dump(mode="json") for item in actions],
                "action_contracts": [item.model_dump(mode="json") for item in action_contracts],
                "lead_session": self.state.lead_session.model_dump(mode="json"),
                "usage": result.usage.model_dump(mode="json"),
                "stop_requested": stop_requested,
                "stop_reason": stop_reason,
                "frame_break": output.frame_break,
                "normalization_notes": normalization,
                "charter_notes": charter_notes,
                "dropped_issue_drafts": dropped_issues,
                "obligation_admission": asdict(obligation_notes),
                "crux_admission": asdict(crux_notes),
                "overlay_admission": asdict(overlay_notes),
                "lineage_admission": lineage_notes,
                "dropped_action_proposals": dropped_actions,
                "frontier_kernel_notes": asdict(frontier_notes),
                "ceiling_scan": output.ceiling_scan.model_dump(mode="json")
                if output.ceiling_scan
                else None,
                **trace.payload(),
            }
            self._append(et.BOOTSTRAP_COMPLETED, payload, actor="controller")
            self._record_staged_checks(stage="preflight")
        except BaseException as exc:
            usage, trace = self._failure_parts(exc)
            recovery_artifact, recovery_capture_error = self._capture_recovery_artifact(
                workspace,
                summary="Interrupted bootstrap workspace recovered before cleanup.",
                parent=None,
                source_action_ids=[],
            )
            self._append(
                et.BOOTSTRAP_FAILED,
                {
                    "call_id": call_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "usage": usage.model_dump(mode="json"),
                    "recovery_artifact": (
                        recovery_artifact.model_dump(mode="json") if recovery_artifact else None
                    ),
                    "recovery_capture_error": recovery_capture_error,
                    **trace.payload(),
                },
                actor="controller",
            )
            raise
        finally:
            self._close_workspace(workspace)

    def _bootstrap_sandbox(self) -> Any:
        from .models import SandboxPolicy

        return SandboxPolicy.WORKSPACE_WRITE

    def _role_for_action(self, action: ActionSpec) -> Role:
        if action.impact == Impact.FATAL:
            return Role.STRONG
        if action.impact == Impact.HIGH and action.cost == CostBand.EXPENSIVE:
            return Role.STRONG
        if action.impact == Impact.HIGH:
            return Role.WORKER
        return Role.CHEAP

    @staticmethod
    def _changed_models(
        existing: Mapping[str, BaseModel],
        projected: Mapping[str, BaseModel],
    ) -> list[BaseModel]:
        changed: list[BaseModel] = []
        for key, item in projected.items():
            before = existing.get(key)
            if before is None or before.model_dump(mode="json") != item.model_dump(mode="json"):
                changed.append(item)
        return changed

    def _fallback_action_proposals(
        self,
        *,
        obligations: dict[str, Obligation],
        cruxes: dict[str, Crux],
    ) -> list[ActionProposal]:
        """Build one conservative action when a model leaves a live blocker unscheduled.

        This is a non-regression guard, not an autonomous planner. It preserves
        the proven sparse path by ensuring an active crux cannot disappear merely
        because one controller response omitted its next action.
        """

        active = sorted(
            (item for item in cruxes.values() if item.status == CruxStatus.ACTIVE),
            key=lambda item: (
                -_IMPACT_RANK[item.unlock_value],
                item.created_seq,
                item.crux_id,
            ),
        )
        if active:
            crux = active[0]
            evidence_hint = (
                " Test the most discriminating observation already named for this crux: "
                + crux.discriminating_evidence[0]
                if crux.discriminating_evidence
                else " Determine the cheapest credible evidence that would separate the live possibilities."
            )
            return [
                ActionProposal(
                    kind=ActionKind.DISCRIMINATE,
                    target=crux.title,
                    assignment=(
                        "Resolve the active crux without broadening the task." + evidence_hint
                    ),
                    crux_ids=[crux.crux_id],
                    obligation_ids=list(crux.obligation_ids),
                    impact=crux.unlock_value,
                    cost=CostBand.MODERATE,
                    independence_class=IndependenceClass.SAME_MODEL,
                    topology=CognitiveTopology.LEAD,
                    epistemic_mode=EpistemicMode.THINK,
                    hypothesis_family=crux.title,
                    novelty_basis=(
                        "Continue the unresolved controlling crux from the compact frontier "
                        "kernel; attack and eliminate conceptual branches before escalating."
                    ),
                    expected_decision_effect=crux.decision_controlled,
                    reusable_value=ValueBand.MEDIUM,
                    distinctive_angle="Deterministic fallback for an unscheduled live crux.",
                    stop_condition="Stop after the crux is resolved, falsified, or shown not to control a material decision.",
                )
            ]

        blockers = sorted(
            (
                item
                for item in obligations.values()
                if item.release_blocking
                and item.status in {ObligationStatus.OPEN, ObligationStatus.BLOCKED}
            ),
            key=lambda item: (-_IMPACT_RANK[item.impact], item.created_seq, item.obligation_id),
        )
        if blockers:
            obligation = blockers[0]
            return [
                ActionProposal(
                    kind=ActionKind.EXPLOIT,
                    target=obligation.title,
                    assignment=(
                        "Advance the authoritative artifact just enough to satisfy or decisively diagnose this release-blocking obligation: "
                        + obligation.requirement
                    ),
                    obligation_ids=[obligation.obligation_id],
                    impact=obligation.impact,
                    cost=CostBand.MODERATE,
                    independence_class=IndependenceClass.SAME_MODEL,
                    topology=CognitiveTopology.LEAD,
                    epistemic_mode=EpistemicMode.BUILD,
                    execution_trigger=(
                        "The release-blocking obligation is already known; the remaining "
                        "uncertainty is whether a concrete artifact change can satisfy it."
                    ),
                    expected_decision_effect=(
                        "Satisfy the obligation or expose the exact evidence/tool/user input still required."
                    ),
                    reusable_value=ValueBand.MEDIUM,
                    distinctive_angle="Deterministic fallback for an unscheduled release blocker.",
                )
            ]
        return []

    def _ensure_frame_pressure(
        self,
        proposals: list[ActionProposal],
        *,
        obligations: Mapping[str, Obligation],
        cruxes: Mapping[str, Crux],
        frontier_kernel: FrontierKernel | None = None,
    ) -> None:
        """Inject one fresh frame challenge only after observed semantic samsara."""

        frame_kinds = {
            ActionKind.REFRAME,
            ActionKind.RECONSTRUCT,
            ActionKind.CEILING_AUDIT,
            ActionKind.MECHANISM_GRAFT,
        }
        if any(item.kind in frame_kinds for item in proposals):
            return
        kernel = frontier_kernel or self.state.frontier_kernel
        kernel_stalls = kernel.stagnant_rounds if kernel is not None else 0
        target_stalls = self._target_stalls()
        repeated_local_failure = bool(
            kernel_stalls >= 2
            or (
                kernel is None
                and any(
                    count >= self.config.frontier.max_stalled_actions_per_target
                    for count in target_stalls.values()
                )
            )
        )
        blockers = sorted(
            (
                item
                for item in obligations.values()
                if item.release_blocking
                and item.status in {ObligationStatus.OPEN, ObligationStatus.BLOCKED}
            ),
            key=lambda item: (-_IMPACT_RANK[item.impact], item.created_seq),
        )
        active_cruxes = sorted(
            (item for item in cruxes.values() if item.status == CruxStatus.ACTIVE),
            key=lambda item: (-_IMPACT_RANK[item.unlock_value], item.created_seq),
        )
        if not repeated_local_failure or not (blockers or active_cruxes):
            return

        pressure = []
        if kernel_stalls:
            pressure.append(f"Frontier Kernel unchanged for {kernel_stalls} checkpoint(s)")
        pressure.extend(
            f"{target}: {count} non-informative attempt(s)"
            for target, count in sorted(target_stalls.items())
            if count >= self.config.frontier.max_stalled_actions_per_target
        )
        proposals.append(
            ActionProposal(
                kind=ActionKind.CEILING_AUDIT,
                target="shared frame behind the stalled frontier",
                assignment=(
                    "Re-derive the controlling problem from the immutable Task Source, the "
                    "Artifact Spine, raw observations, and the actual artifact. Treat the "
                    "current solution and vocabulary as hypotheses, not authority. Identify "
                    "the shared representation, assumption, objective, or observation channel "
                    "that could explain the repeated local failures. Use any available tools "
                    "that sharpen the reasoning. Generate and kill alternatives internally; "
                    "return one causal frame break with a discriminating prediction and direct "
                    "next move, or strong evidence that the current frame remains the best one. "
                    "Do not write a survey, process report, or generic critique. Trigger: "
                    + "; ".join(pressure)
                ),
                obligation_ids=[item.obligation_id for item in blockers[:2]],
                crux_ids=[item.crux_id for item in active_cruxes[:1]],
                impact=(blockers[0].impact if blockers else active_cruxes[0].unlock_value),
                cost=CostBand.MODERATE,
                independence_class=IndependenceClass.DIFFERENT_CONDITIONING,
                topology=CognitiveTopology.WORKER,
                epistemic_mode=EpistemicMode.THINK,
                hypothesis_family="shared representation or objective behind local failure",
                novelty_basis="Fresh task-anchored context after runtime-observed stagnation.",
                expected_decision_effect=(
                    "Replace the limiting frame and redirect construction, or close frame error "
                    "as the explanation for the observed stalls."
                ),
                reusable_value=ValueBand.HIGH,
                optimization_value=ValueBand.HIGH,
                information_value=ValueBand.HIGH,
                feasibility=ValueBand.HIGH,
                distinctive_angle="Runtime-triggered independent frame pressure.",
                stop_condition=(
                    "Stop after one causal frame and discriminator survives attack, or after "
                    "the current frame is materially vindicated."
                ),
            )
        )

    def _ensure_fresh_global_review(
        self,
        proposals: list[ActionProposal],
        *,
        accepted_action_ids: Sequence[str],
    ) -> None:
        """Require cold observation after experiential whole-artifact construction.

        Local construction context is a correlated evaluator. The next frontier
        therefore gets one fresh, artifact-bound challenge unless the Lead
        already proposed an equivalent one.
        """

        experiential = {
            EvidenceModality.STATIC_VISUAL,
            EvidenceModality.TEMPORAL_VISUAL,
            EvidenceModality.AUDIO,
            EvidenceModality.INTERACTIVE,
        }
        candidates = [
            self.state.actions[action_id]
            for action_id in accepted_action_ids
            if action_id in self.state.actions
            and self.state.actions[action_id].spec.artifact_scope in {"whole_artifact", "release"}
            and self.state.actions[action_id].spec.kind
            not in {ActionKind.DISCRIMINATE, ActionKind.CEILING_AUDIT}
            and experiential.intersection(self.state.actions[action_id].spec.observation_modalities)
        ]
        if not candidates or any(
            proposal.kind in {ActionKind.DISCRIMINATE, ActionKind.CEILING_AUDIT}
            and proposal.artifact_scope in {"whole_artifact", "release"}
            and experiential.intersection(proposal.observation_modalities)
            for proposal in proposals
        ):
            return
        source = candidates[0].spec
        proposals.insert(
            0,
            ActionProposal(
                kind=ActionKind.DISCRIMINATE,
                target=f"fresh whole-artifact challenge of {source.target}",
                assignment=(
                    "Reproduce or open the exact integrated experiential artifact, then inspect "
                    "it in every claimed visual, temporal, audio, or interactive modality. Judge "
                    "it cold against the immutable Task Source, not the creator's rationale. Hunt "
                    "the strongest concrete failure and retain the inspected artifact or extracts."
                ),
                obligation_ids=list(source.obligation_ids),
                crux_ids=list(source.crux_ids),
                impact=source.impact,
                cost=CostBand.MODERATE,
                independence_class=IndependenceClass.DIFFERENT_CONDITIONING,
                topology=CognitiveTopology.WORKER,
                epistemic_mode=EpistemicMode.VERIFY,
                execution_trigger=(
                    "Construction context cannot establish the exact integrated artifact's "
                    "whole-experience quality; cold artifact-bound observation controls release."
                ),
                expected_decision_effect=(
                    "Either admit whole-artifact quality with artifact-bound observation, or "
                    "reopen the causal defect before more local polishing."
                ),
                reusable_value=ValueBand.MEDIUM,
                optimization_value=ValueBand.HIGH,
                information_value=ValueBand.HIGH,
                feasibility=ValueBand.HIGH,
                artifact_scope=source.artifact_scope,
                observation_modalities=[
                    modality
                    for modality in source.observation_modalities
                    if modality in experiential
                ],
                causal_hypothesis=(
                    "The integrated artifact satisfies its global experience and quality claims "
                    "outside the construction context."
                ),
                intervention="Remove creator context and inspect the exact rendered artifact cold.",
                potency_check=(
                    "Verify exact artifact identity and actually decode/watch/listen/use the full "
                    "relevant scope rather than inspecting source or stills alone."
                ),
                decision_rule=(
                    "Any material whole-artifact failure reopens the controlling obligation; "
                    "otherwise record exactly what was observed and the remaining blind spots."
                ),
                stop_condition=(
                    "Stop after full-scope observation establishes the strongest material failure "
                    "or supports the global claim with explicit blind spots."
                ),
            ),
        )

    def _apex_brief(self, stop_reason: str) -> str:
        state = self.state
        lines = [
            "# Apex brief",
            "",
            "## Immutable task",
            "",
            state.task_source.original_text if state.task_source else state.source_prompt,
            "",
            "## Stop reason",
            "",
            stop_reason,
            "",
        ]
        if state.artifact_spine:
            spine = state.artifact_spine
            lines.extend(
                [
                    "## Artifact spine",
                    "",
                    f"Central thesis: {spine.central_thesis}",
                    "",
                    "Key decisions:",
                    *(
                        [f"- {item}" for item in spine.key_decisions]
                        or ["- None explicitly recorded."]
                    ),
                    "",
                    "Must preserve:",
                    *(
                        [f"- {item}" for item in spine.must_preserve]
                        or ["- The exact deliverable and hard constraints."]
                    ),
                    "",
                ]
            )
        lines.extend(["## Release-blocking obligations", ""])
        for item in state.obligations.values():
            if not item.release_blocking:
                continue
            lines.append(
                f"- [{item.status.value}] {item.obligation_id}: {item.requirement} | acceptance: {item.acceptance}"
            )
        lines.extend(["", "## Accepted evidence and substrate", ""])
        for entry in state.substrate.values():
            if entry.global_admission:
                lines.append(f"- [{entry.confidence}] {entry.statement} (scope: {entry.scope})")
        if not any(entry.global_admission for entry in state.substrate.values()):
            lines.append(
                "- Use the evidence ledger and completed action results; do not invent support."
            )
        alternatives = [
            item
            for item in state.overlays.values()
            if item.status in {OverlayStatus.ACTIVE, OverlayStatus.DORMANT}
        ]
        if alternatives:
            lines.extend(["", "## Strongest live alternative", ""])
            best = alternatives[0]
            lines.append(
                f"- {best.name}: {best.mechanism}; difference: {best.behavioral_difference}"
            )
        lines.extend(
            [
                "",
                "## Synthesis law",
                "",
                "Produce one coherent answer to the exact task. Preserve accepted value, expose proportional uncertainty, and reject alternatives for explicit reasons rather than averaging them.",
                "",
            ]
        )
        return "\n".join(lines)

    def _default_completion_case(self, artifact: ArtifactRef) -> CompletionCase:
        claims: list[CompletionClaim] = []
        for obligation in self.state.obligations.values():
            if not obligation.release_blocking:
                continue
            satisfied = obligation.status == ObligationStatus.SATISFIED
            claims.append(
                CompletionClaim(
                    obligation_id=obligation.obligation_id,
                    artifact_location=obligation.artifact_location if satisfied else "",
                    evidence_or_test=list(obligation.evidence_references),
                    assumptions=list(obligation.assumptions),
                    status="satisfied" if satisfied else "unsatisfied",
                    remaining_uncertainty=obligation.residual_uncertainty,
                    reopen_condition=obligation.reopen_condition,
                )
            )
        alternatives = [
            item
            for item in self.state.overlays.values()
            if item.status in {OverlayStatus.ACTIVE, OverlayStatus.DORMANT}
        ]
        strongest = alternatives[0] if alternatives else None
        return CompletionCase(
            task_source_digest=(
                self.state.task_source.digest
                if self.state.task_source
                else sha256_text(self.state.source_prompt.strip())
            ),
            artifact_digest=artifact.blob.digest,
            claims=claims,
            strongest_rejected_alternative=(
                f"{strongest.name}: {strongest.mechanism}" if strongest else ""
            ),
            why_rejected=(
                "Not selected by the current evidence and obligation case." if strongest else ""
            ),
            preserved_insights=(
                list(self.state.artifact_spine.must_preserve) if self.state.artifact_spine else []
            ),
            unresolved_high_impact_risks=[
                item.requirement
                for item in self.state.release_blocking_obligations
                if item.impact in {Impact.FATAL, Impact.HIGH}
            ],
        )

    def _normalize_completion_case(
        self,
        completion: CompletionCase | None,
        artifact: ArtifactRef,
    ) -> CompletionCase:
        case = (completion or self._default_completion_case(artifact)).model_copy(deep=True)
        case.task_source_digest = (
            self.state.task_source.digest
            if self.state.task_source
            else sha256_text(self.state.source_prompt.strip())
        )
        case.artifact_digest = artifact.blob.digest
        known = set(self.state.obligations)
        claims_by_id = {
            item.obligation_id: item for item in case.claims if item.obligation_id in known
        }
        default = {
            item.obligation_id: item for item in self._default_completion_case(artifact).claims
        }
        for obligation_id, claim in default.items():
            claims_by_id.setdefault(obligation_id, claim)
        case.claims = list(claims_by_id.values())
        return case

    async def _execute_action(
        self,
        action: ActionSpec,
        round_state: RunState,
        *,
        max_provider_calls: int = 1,
    ) -> None:
        await self.action_executor.execute(
            self,
            action,
            round_state,
            max_provider_calls=max_provider_calls,
        )

    def _fresh_frontier_keeper(self) -> bool:
        if (
            self.state.runtime.bootstrap.independent_checkpoint_required
            or self.state.runtime.planning.frontier_replan_pending
            or self.state.runtime.release.replan_pending
        ):
            return True
        mode = self.config.cognition.frontier_keeper
        if mode == "fresh":
            return True
        if mode == "continuous":
            return False
        profiles = set(self.config.run.semantic_profiles)
        if self.state.contract is not None:
            profiles.update(self.state.contract.semantic_profiles)
            if self.state.contract.quality_floor in {"very_high", "frontier"}:
                return True
        return bool(
            profiles.intersection({"research", "formal", "decision", "creative", "media"})
            or self.state.summit_active
            or bool(self.state.frontier_kernel and self.state.frontier_kernel.stagnant_rounds > 0)
            or any(
                count >= self.config.frontier.max_stalled_actions_per_target
                for count in self._target_stalls().values()
            )
        )

    async def _checkpoint(self, action_ids: Sequence[str], round_index: int) -> bool:
        return await self.checkpoint_executor.execute(
            self,
            action_ids,
            round_index,
        )

    async def _advance_frontier(self) -> str:
        return await self.frontier_loop.execute(self)

    def _has_unresolved_frontier_debt(self) -> bool:
        return bool(
            self.state.high_impact_open_issues
            or any(
                item.release_blocking
                and item.status in {ObligationStatus.OPEN, ObligationStatus.BLOCKED}
                for item in self.state.obligations.values()
            )
            or any(item.status == CruxStatus.ACTIVE for item in self.state.cruxes.values())
            or self.state.runtime.verification.semantic_ci_gaps
        )

    async def _replan_dead_frontier(
        self,
        *,
        reason: str,
        action_records: Sequence[ActionRecord],
    ) -> bool:
        """Give a dead action slate one fresh causal replan, never false convergence."""

        if not self._has_unresolved_frontier_debt() or not self._can_call():
            return False
        fingerprint = sha256_text(
            canonical_json(
                {
                    "reason": reason,
                    "actions": [
                        {
                            "kind": item.spec.kind.value,
                            "target": item.spec.target,
                            "status": item.status.value,
                            "rejection": item.rejection_reason,
                        }
                        for item in action_records
                    ],
                    "open_obligations": sorted(
                        item.obligation_id
                        for item in self.state.obligations.values()
                        if item.release_blocking
                        and item.status in {ObligationStatus.OPEN, ObligationStatus.BLOCKED}
                    ),
                    "active_cruxes": sorted(
                        item.crux_id
                        for item in self.state.cruxes.values()
                        if item.status == CruxStatus.ACTIVE
                    ),
                }
            )
        )
        if fingerprint in self.state.runtime.planning.frontier_replan_fingerprints:
            return False
        self._append(
            et.FRONTIER_REPLAN_REQUESTED,
            {
                "fingerprint": fingerprint,
                "reason": reason,
                "actions": [item.spec.action_id for item in action_records],
            },
            actor="scheduler",
        )
        return await self._checkpoint([], self.state.round_index)

    # ------------------------------------------------------------------
    # Final synthesis, release, repair, and materialization
    # ------------------------------------------------------------------
    async def _synthesize_final(self, stop_reason: str) -> tuple[FinalOutput, bool]:
        current = self.state.current_artifact
        if current is None:
            raise FrontierError("Cannot finalize a run without a current artifact")
        self._append(
            et.FINALIZATION_STARTED,
            {"started_at": utc_now(), "stop_reason": stop_reason},
            actor="controller",
        )
        prior_text = self.adapter.artifact_text(current)

        def record_fallback(summary: str, uncertainty: str) -> FinalOutput:
            artifact = self._clone_artifact(current, summary=summary)
            completion = self._normalize_completion_case(None, artifact)
            report = run_semantic_ci(
                state=self.state,
                final_text=self.adapter.artifact_text(artifact),
                prior_text=prior_text,
                model_findings=[],
                completion_case=completion,
            )
            output = FinalOutput(
                artifact_path="",
                summary=artifact.summary,
                remaining_uncertainty=[uncertainty],
                artifact_spine=self.state.artifact_spine,
                semantic_regression=report.findings,
                completion_case=completion,
                release_gate_recommended=True,
            )
            self._append(
                et.FINAL_SYNTHESIZED,
                {
                    "artifact": artifact.model_dump(mode="json"),
                    "summary": output.summary,
                    "remaining_uncertainty": output.remaining_uncertainty,
                    "artifact_spine": (
                        output.artifact_spine.model_dump(mode="json")
                        if output.artifact_spine
                        else None
                    ),
                    "semantic_regression": [
                        item.model_dump(mode="json") for item in report.findings
                    ],
                    "completion_case": completion.model_dump(mode="json"),
                    "release_gate_recommended": True,
                    "usage": Usage().model_dump(mode="json"),
                    "fallback": True,
                },
                actor="controller",
            )
            self._append(
                et.SEMANTIC_REGRESSION_COMPLETED,
                {
                    "passed": report.passed,
                    "findings": [item.model_dump(mode="json") for item in report.findings],
                    "completion_gaps": report.completion_gaps,
                    "protected_properties": report.protected_properties,
                },
                actor="semantic-ci",
            )
            self._append(
                et.COMPLETION_CASE_BUILT,
                {"completion_case": completion.model_dump(mode="json")},
                actor="controller",
            )
            return output

        if not self._can_call():
            output = record_fallback(
                "Current integrated artifact promoted because the synthesis budget was exhausted.",
                f"A clean final synthesis call was skipped: {self._budget_limit_reason()}.",
            )
            return output, True

        call_id = new_id("call")
        workspace = self.adapter.open_call(
            call_id=call_id,
            call_kind="final-synthesis",
            current_artifact=current,
        )
        try:
            apex_brief = self._apex_brief(stop_reason)
            capsule = self._capsules.populate(
                workspace,
                task=self.state.source_prompt,
                state=self.state,
                assignment=(
                    "Rebuild the final deliverable coherently from the exact task, artifact spine, accepted decisions, and scoped evidence."
                ),
                goal_contract=self.state.contract,
                evidence_action_ids=[
                    record.spec.action_id
                    for record in self.state.actions.values()
                    if record.status == ActionStatus.COMPLETE
                ],
                extra_notes=f"Frontier stop reason: {stop_reason}",
                task_source=self.state.task_source,
                apex_brief=apex_brief,
                lens_purpose="synthesis",
            )
            prompt = final_prompt(
                workspace,
                profile=self._profile,
                software=self._software,
                adaptive=self.config.cognition.mode == "adaptive",
            )
            use_lead = (
                self.config.cognition.mode == "adaptive" and self.config.cognition.persistent_lead
            )
            result, trace = await self._invoke(
                workspace,
                call_kind="final-synthesis",
                role=Role.STRONG,
                prompt=prompt,
                response_model=FinalOutput,
                sandbox=self._bootstrap_sandbox(),
                network_access=self.config.provider.default_network_access,
                image_paths=[Path(item) for item in cast(list[str], capsule["image_paths"])],
                metadata={
                    "current_artifact_text": prior_text,
                    "task_source_digest": (
                        self.state.task_source.digest if self.state.task_source else None
                    ),
                    "open_obligation_ids": [
                        item.obligation_id for item in self.state.open_obligations
                    ],
                    "active_crux_ids": [item.crux_id for item in self.state.active_cruxes],
                },
                use_lead=use_lead,
            )
            output = result.response
            declared, normalization = self._ensure_artifact_file(
                workspace,
                declared_path=output.artifact_path,
                summary=output.summary,
                current_artifact=current,
            )
            artifact = self.adapter.capture_artifact(
                workspace,
                declared_path=declared,
                version=current.version + 1,
                summary=output.summary,
                parent=current,
                source_action_ids=[
                    record.spec.action_id
                    for record in self.state.actions.values()
                    if record.status == ActionStatus.COMPLETE and record.rejection_reason is None
                ],
            )

            spine, spine_notes = reconcile_artifact_spine(
                self.state.artifact_spine, output.artifact_spine
            )
            if spine is None and self.state.contract is not None:
                spine = fallback_spine(self.state.contract, output.summary)

            completion = self._normalize_completion_case(output.completion_case, artifact)
            report = run_semantic_ci(
                state=self.state,
                final_text=self.adapter.artifact_text(artifact),
                prior_text=prior_text,
                model_findings=(
                    output.semantic_regression if self.config.cognition.semantic_regression else []
                ),
                completion_case=(completion if self.config.cognition.completion_case else None),
            )
            output = output.model_copy(
                update={
                    "artifact_spine": spine,
                    "semantic_regression": report.findings,
                    "completion_case": completion,
                    "release_gate_recommended": (
                        output.release_gate_recommended or not report.passed
                    ),
                }
            )
            self._append(
                et.FINAL_SYNTHESIZED,
                {
                    "call_id": call_id,
                    "artifact": artifact.model_dump(mode="json"),
                    "summary": output.summary,
                    "remaining_uncertainty": output.remaining_uncertainty,
                    "artifact_spine": spine.model_dump(mode="json") if spine else None,
                    "semantic_regression": [
                        item.model_dump(mode="json") for item in report.findings
                    ],
                    "completion_case": completion.model_dump(mode="json"),
                    "preservation_decisions": output.preservation_decisions,
                    "lead_session": (
                        self.state.lead_session.model_dump(mode="json") if use_lead else None
                    ),
                    "release_gate_recommended": output.release_gate_recommended,
                    "usage": result.usage.model_dump(mode="json"),
                    "normalization_notes": normalization,
                    "artifact_spine_notes": spine_notes,
                    "semantic_ci_passed": report.passed,
                    "semantic_ci_gaps": report.completion_gaps,
                    "semantic_ci_deterministic_failures": report.deterministic_failures,
                    **trace.payload(),
                },
                actor="lead" if use_lead else "controller",
            )
            self._append(
                et.SEMANTIC_REGRESSION_COMPLETED,
                {
                    "passed": report.passed,
                    "findings": [item.model_dump(mode="json") for item in report.findings],
                    "completion_gaps": report.completion_gaps,
                    "deterministic_failures": report.deterministic_failures,
                    "protected_properties": report.protected_properties,
                },
                actor="semantic-ci",
            )
            self._append(
                et.COMPLETION_CASE_BUILT,
                {"completion_case": completion.model_dump(mode="json")},
                actor="controller",
            )
            return output, False
        except BaseException as exc:
            usage, trace = self._failure_parts(exc)
            recovery_artifact, recovery_capture_error = self._capture_recovery_artifact(
                workspace,
                summary="Interrupted final synthesis workspace.",
                parent=current,
                source_action_ids=[
                    record.spec.action_id
                    for record in self.state.actions.values()
                    if record.status == ActionStatus.COMPLETE and record.rejection_reason is None
                ],
            )
            self._append(
                et.FINALIZATION_FAILED,
                {
                    "call_id": call_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "usage": usage.model_dump(mode="json"),
                    "recovery_artifact": (
                        recovery_artifact.model_dump(mode="json") if recovery_artifact else None
                    ),
                    "recovery_capture_error": recovery_capture_error,
                    **trace.payload(),
                },
                actor="controller",
            )
            output = record_fallback(
                "Current integrated artifact promoted after final synthesis failure.",
                f"Clean final synthesis failed: {type(exc).__name__}: {exc}",
            )
            return output, True
        finally:
            self._close_workspace(workspace)

    def _reconcile_completion_evidence(
        self,
        *,
        actor: str,
        deterministic_failures: Sequence[str] | None = None,
    ) -> None:
        if self.state.completion_case is None:
            return
        gaps = completion_case_gaps(self.state, self.state.completion_case)
        material_findings = [
            item
            for item in self.state.semantic_regression_findings
            if item.severity in {"fatal", "high"}
            and item.disposition not in {"preserved", "irrelevant"}
        ]
        failures = list(
            deterministic_failures
            if deterministic_failures is not None
            else self.state.runtime.verification.deterministic_failures
        )
        self._append(
            et.SEMANTIC_REGRESSION_COMPLETED,
            {
                "passed": not material_findings and not gaps and not failures,
                "findings": [
                    item.model_dump(mode="json") for item in self.state.semantic_regression_findings
                ],
                "completion_gaps": gaps,
                "deterministic_failures": failures,
                "protected_properties": [],
                "adjudication": {"kind": "artifact-bound evidence reconciliation"},
            },
            actor=actor,
        )

    def _record_deterministic_checks(self) -> list[EvidenceRecord]:
        if self.state.final_artifact is None:
            return []
        evidence = self.adapter.deterministic_checks(self.state.final_artifact)
        if self._software:
            for pattern in self.config.software.release_artifacts:
                matched = [
                    ref
                    for ref in self.state.final_artifact.deliverables
                    if ref.original_name and Path(ref.original_name).match(pattern)
                ]
                evidence.append(
                    EvidenceRecord(
                        evidence_id=new_id("evd"),
                        kind="declared_deliverable_check",
                        summary=(
                            f"Declared release artifact captured: {pattern}"
                            if matched
                            else f"Declared release artifact missing: {pattern}"
                        ),
                        scope="Presence and durable byte capture for the configured artifact pattern.",
                        artifact_scope="release",
                        independence_class=IndependenceClass.DETERMINISTIC_TOOL,
                        references=[ref.digest for ref in matched],
                        negative_result=not matched,
                        modalities=[EvidenceModality.DETERMINISTIC_TEST],
                        establishes=[f"deliverable pattern present: {pattern}"] if matched else [],
                        artifact_digest=self.state.final_artifact.blob.digest,
                    )
                )
        for ref in self.state.final_artifact.deliverables:
            evidence.append(
                EvidenceRecord(
                    evidence_id=new_id("evd"),
                    kind="durable_deliverable_capture",
                    summary=f"Captured declared deliverable: {ref.original_name or ref.digest}",
                    scope="Byte identity and durable availability only; content quality is not established.",
                    artifact_scope="release",
                    independence_class=IndependenceClass.DETERMINISTIC_TOOL,
                    references=[ref.digest],
                    blob=ref,
                    establishes=["durable artifact capture"],
                    cannot_establish=["content quality", "temporal quality", "human usability"],
                    artifact_digest=self.state.final_artifact.blob.digest,
                )
            )
        for item in evidence:
            self._append(
                et.DETERMINISTIC_CHECK_COMPLETED,
                {"evidence": item.model_dump(mode="json")},
                actor="tool",
            )
        self._reconcile_completion_evidence(
            actor="semantic-ci",
            deterministic_failures=[item.summary for item in evidence if item.negative_result],
        )
        return evidence

    def _record_staged_checks(self, *, stage: str) -> list[EvidenceRecord]:
        artifact = self.state.current_artifact
        if artifact is None:
            return []
        prior_stage = self.state.runtime.verification.stages.get(stage)
        prior_digest = prior_stage.artifact_digest if prior_stage else None
        if prior_digest == artifact.blob.digest:
            return []
        evidence = self.adapter.staged_checks(artifact, stage=stage)
        if not evidence:
            return []
        for item in evidence:
            self._append(
                et.DETERMINISTIC_CHECK_COMPLETED,
                {"evidence": item.model_dump(mode="json")},
                actor="tool",
            )
        failures = [item.summary for item in evidence if item.negative_result]
        self._append(
            et.CHECK_STAGE_COMPLETED,
            {
                "stage": stage,
                "artifact_digest": artifact.blob.digest,
                "failed": bool(failures),
                "failures": failures,
                "check_count": len(evidence),
            },
            actor="tool",
        )
        return evidence

    def _should_release(
        self,
        final_output: FinalOutput,
        checks: Sequence[EvidenceRecord],
    ) -> bool:
        return ReleasePolicy.should_challenge(
            policy=self.config.run.release_gate,
            adaptive_mode=self.config.cognition.mode == "adaptive",
            state=self.state,
            final_output=final_output,
            checks=checks,
        )

    @staticmethod
    def _release_needs_repair(release: ReleaseOutput) -> bool:
        return ReleasePolicy.needs_repair(release)

    @staticmethod
    def _release_recovery(release: ReleaseOutput) -> ReleaseRecovery | None:
        return ReleasePolicy.recovery(release)

    def _can_reopen_release(self) -> bool:
        round_limit = self.config.run.budget.max_rounds
        if round_limit is not None and self.state.round_index >= round_limit:
            return False
        return self._active_calls_remaining() >= self._completion_reserve_calls() + 2

    @staticmethod
    def _release_can_adjudicate_model_semantic_findings(
        *,
        semantic_ci_passed: bool,
        completion_gaps: Sequence[str],
        deterministic_failures: Sequence[str] | None,
        checks: Sequence[EvidenceRecord],
        release: ReleaseOutput,
    ) -> bool:
        return ReleasePolicy.can_adjudicate_semantic_findings(
            semantic_ci_passed=semantic_ci_passed,
            completion_gaps=completion_gaps,
            deterministic_failures=deterministic_failures,
            checks=checks,
            release=release,
        )

    def _apply_release_adjudication(
        self,
        release: ReleaseOutput | None,
        checks: Sequence[EvidenceRecord],
    ) -> None:
        if release is None or not ReleasePolicy.can_adjudicate_semantic_findings(
            semantic_ci_passed=self.state.runtime.verification.semantic_ci_passed is True,
            completion_gaps=self.state.runtime.verification.semantic_ci_gaps,
            deterministic_failures=self.state.runtime.verification.deterministic_failures,
            checks=checks,
            release=release,
        ):
            return
        self._append(
            et.SEMANTIC_REGRESSION_COMPLETED,
            {
                "passed": True,
                "findings": [
                    item.model_dump(mode="json") for item in self.state.semantic_regression_findings
                ],
                "completion_gaps": [],
                "deterministic_failures": [],
                "protected_properties": [],
                "adjudication": {
                    "kind": "fresh_release_challenge",
                    "releaseable": release.releaseable,
                    "rationale": release.rationale,
                },
            },
            actor="release",
        )

    def _evaluate_mutation_gate(
        self,
        *,
        checks: Sequence[EvidenceRecord],
        release_required: bool,
        release: ReleaseOutput | None,
        repair_completed: bool,
    ) -> MutationGateDecision:
        return ReleasePolicy.mutation_gate(
            state=self.state,
            checks=checks,
            release_required=release_required,
            release=release,
            repair_completed=repair_completed,
        )

    async def _run_release_tail(
        self,
        final_output: FinalOutput,
        checks: list[EvidenceRecord],
    ) -> tuple[list[EvidenceRecord], ReleaseOutput | None, MutationGateDecision]:
        return await self.release_pipeline.run(self, final_output, checks)

    async def _release_challenge(self) -> ReleaseOutput | None:
        if self.state.final_artifact is None or not self._can_call():
            return None
        call_id = new_id("call")
        workspace = self.adapter.open_call(
            call_id=call_id,
            call_kind="release",
            current_artifact=self.state.final_artifact,
        )
        try:
            capsule = self._capsules.populate(
                workspace,
                task=self.state.source_prompt,
                state=self.state,
                assignment=(
                    "Freshly challenge only fatal errors, major omissions, task drift, unsupported load-bearing claims, invalid completion evidence, semantic regressions, or conditions under which the strongest rejected alternative dominates. Do not cosmetically rewrite."
                ),
                goal_contract=self.state.contract,
                task_source=self.state.task_source,
                semantic_ci={
                    "passed": self.state.runtime.verification.semantic_ci_passed,
                    "completion_gaps": self.state.runtime.verification.semantic_ci_gaps,
                    "findings": [
                        item.model_dump(mode="json")
                        for item in self.state.semantic_regression_findings
                    ],
                },
                completion_case=self.state.completion_case,
                lens_purpose="release",
            )
            prompt = release_prompt(workspace, profile=self._profile)
            role = (
                Role.STRONG
                if self.state.contract and self.state.contract.stakes in {"high", "critical"}
                else Role.WORKER
            )
            result, trace = await self._invoke(
                workspace,
                call_kind="release",
                role=role,
                prompt=prompt,
                response_model=ReleaseOutput,
                sandbox=self._bootstrap_sandbox(),
                network_access=False,
                image_paths=[Path(item) for item in cast(list[str], capsule["image_paths"])],
                metadata={
                    "task_source_digest": (
                        self.state.task_source.digest if self.state.task_source else None
                    ),
                    "completion_case_present": self.state.completion_case is not None,
                    "semantic_ci_passed": self.state.runtime.verification.semantic_ci_passed,
                },
            )
            output = result.response
            if self.state.final_artifact is None:
                raise FrontierError("Release challenge lost its artifact binding")
            observed_modalities = list(output.observed_modalities)
            tool_names = {
                call.name for call in result.trace_summary.tool_calls if call.success is not False
            }
            inspection_tools = {"bash", "browser", "task", "eval", "debug"}
            tool_required = {
                EvidenceModality.TEMPORAL_VISUAL,
                EvidenceModality.AUDIO,
                EvidenceModality.INTERACTIVE,
            }
            unconfirmed = (
                tool_required.intersection(observed_modalities)
                if not tool_names.intersection(inspection_tools)
                else set()
            )
            if unconfirmed:
                observed_modalities = [
                    modality for modality in observed_modalities if modality not in unconfirmed
                ]
            output = output.model_copy(
                update={
                    "artifact_digest": self.state.final_artifact.blob.digest,
                    "observed_modalities": observed_modalities,
                    "cannot_establish": unique_preserving_order(
                        [
                            *output.cannot_establish,
                            *(
                                f"{modality.value}: no provider-observed inspection tool call"
                                for modality in sorted(unconfirmed, key=lambda item: item.value)
                            ),
                        ]
                    ),
                }
            )
            self._append(
                et.RELEASE_COMPLETED,
                {
                    "call_id": call_id,
                    "release": output.model_dump(mode="json"),
                    "rejection_fingerprint": (
                        self._release_rejection_fingerprint(output)
                        if self._release_needs_repair(output)
                        else None
                    ),
                    "usage": result.usage.model_dump(mode="json"),
                    **trace.payload(),
                },
                actor="release",
            )
            release_evidence = EvidenceRecord(
                evidence_id=new_id("evd"),
                kind="artifact_bound_release_challenge",
                summary=output.rationale or "Fresh release challenge completed.",
                scope="The exact final artifact and only the modalities explicitly reported by the challenger.",
                independence_class=IndependenceClass.DIFFERENT_CONDITIONING,
                references=[self.state.final_artifact.blob.digest],
                negative_result=self._release_needs_repair(output),
                modalities=list(output.observed_modalities),
                establishes=list(output.establishes),
                cannot_establish=list(output.cannot_establish),
                artifact_digest=self.state.final_artifact.blob.digest,
            )
            self._append(
                et.EVIDENCE_RECORDED,
                {"evidence": release_evidence.model_dump(mode="json")},
                actor="release",
            )
            self._reconcile_completion_evidence(actor="release")
            return output
        except BaseException as exc:
            usage, trace = self._failure_parts(exc)
            self._append(
                et.RELEASE_FAILED,
                {
                    "call_id": call_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "usage": usage.model_dump(mode="json"),
                    **trace.payload(),
                },
                actor="release",
            )
            return None
        finally:
            self._close_workspace(workspace)

    @staticmethod
    def _format_release_findings(release: ReleaseOutput) -> str:
        lines = [
            "# Material release findings",
            "",
            f"Releaseable: {release.releaseable}",
            f"Requires repair: {release.requires_repair}",
            f"Rationale: {release.rationale or '(none supplied)'}",
            "",
        ]
        for index, finding in enumerate(release.findings, start=1):
            lines.extend(
                [
                    f"## {index}. [{finding.severity.upper()}] {finding.title}",
                    "",
                    finding.explanation,
                    "",
                ]
            )
            if finding.evidence_reference:
                lines.extend([f"Evidence: {finding.evidence_reference}", ""])
            if finding.repair_instruction:
                lines.extend([f"Required repair: {finding.repair_instruction}", ""])
        return "\n".join(lines)

    @staticmethod
    def _release_rejection_fingerprint(release: ReleaseOutput) -> str:
        return ReleasePolicy.rejection_fingerprint(release)

    def _record_repair_loop_stop(
        self,
        reason: str,
        *,
        release: ReleaseOutput | None,
        repairs_used: int,
    ) -> None:
        self._append(
            et.REPAIR_LOOP_STOPPED,
            {
                "reason": reason,
                "repairs_used": repairs_used,
                "max_material_repairs": self.config.cognition.max_material_repairs,
                "artifact_digest": (
                    self.state.final_artifact.blob.digest if self.state.final_artifact else None
                ),
                "rejection_fingerprint": (
                    self._release_rejection_fingerprint(release) if release else None
                ),
            },
            actor="resource-governor",
        )

    async def _repair(self, release: ReleaseOutput) -> bool:
        current = self.state.final_artifact
        if current is None or not self._can_call():
            return False
        call_id = new_id("call")
        workspace = self.adapter.open_call(
            call_id=call_id,
            call_kind="repair",
            current_artifact=current,
        )
        try:
            notes = self._format_release_findings(release)
            prior_text = self.adapter.artifact_text(current)
            capsule = self._capsules.populate(
                workspace,
                task=self.state.source_prompt,
                state=self.state,
                assignment="Perform one bounded repair pass for the material release findings.",
                goal_contract=self.state.contract,
                extra_notes=notes,
                task_source=self.state.task_source,
                semantic_ci={
                    "passed": self.state.runtime.verification.semantic_ci_passed,
                    "completion_gaps": self.state.runtime.verification.semantic_ci_gaps,
                    "findings": [
                        item.model_dump(mode="json")
                        for item in self.state.semantic_regression_findings
                    ],
                },
                completion_case=self.state.completion_case,
                lens_purpose="repair",
            )
            prompt = repair_prompt(
                workspace,
                profile=self._profile,
                software=self._software,
            )
            use_lead = (
                self.config.cognition.mode == "adaptive" and self.config.cognition.persistent_lead
            )
            result, trace = await self._invoke(
                workspace,
                call_kind="repair",
                role=Role.STRONG,
                prompt=prompt,
                response_model=RepairOutput,
                sandbox=self._bootstrap_sandbox(),
                network_access=False,
                image_paths=[Path(item) for item in cast(list[str], capsule["image_paths"])],
                metadata={
                    "current_artifact_text": prior_text,
                    "task_source_digest": (
                        self.state.task_source.digest if self.state.task_source else None
                    ),
                    "open_obligation_ids": [
                        item.obligation_id for item in self.state.open_obligations
                    ],
                    "active_crux_ids": [item.crux_id for item in self.state.active_cruxes],
                },
                use_lead=use_lead,
            )
            output = result.response
            declared, normalization = self._ensure_artifact_file(
                workspace,
                declared_path=output.artifact_path,
                summary="Bounded release repair",
                current_artifact=current,
            )
            artifact = self.adapter.capture_artifact(
                workspace,
                declared_path=declared,
                version=current.version + 1,
                summary="Bounded release repair",
                parent=current,
                source_action_ids=[],
            )
            spine, spine_notes = reconcile_artifact_spine(
                self.state.artifact_spine, output.artifact_spine
            )
            completion = self._normalize_completion_case(
                output.completion_case or self.state.completion_case, artifact
            )
            report = run_semantic_ci(
                state=self.state,
                final_text=self.adapter.artifact_text(artifact),
                prior_text=prior_text,
                model_findings=[],
                completion_case=completion,
            )
            self._append(
                et.REPAIR_COMPLETED,
                {
                    "call_id": call_id,
                    "artifact": artifact.model_dump(mode="json"),
                    "repaired_findings": output.repaired_findings,
                    "remaining_uncertainty": output.remaining_uncertainty,
                    "artifact_spine": spine.model_dump(mode="json") if spine else None,
                    "completion_case": completion.model_dump(mode="json"),
                    "lead_session": (
                        self.state.lead_session.model_dump(mode="json") if use_lead else None
                    ),
                    "semantic_regression": [
                        item.model_dump(mode="json") for item in report.findings
                    ],
                    "semantic_ci_passed": report.passed,
                    "semantic_ci_gaps": report.completion_gaps,
                    "semantic_ci_deterministic_failures": report.deterministic_failures,
                    "usage": result.usage.model_dump(mode="json"),
                    "normalization_notes": normalization,
                    "artifact_spine_notes": spine_notes,
                    **trace.payload(),
                },
                actor="lead" if use_lead else "controller",
            )
            self._append(
                et.SEMANTIC_REGRESSION_COMPLETED,
                {
                    "passed": report.passed,
                    "findings": [item.model_dump(mode="json") for item in report.findings],
                    "completion_gaps": report.completion_gaps,
                    "deterministic_failures": report.deterministic_failures,
                    "protected_properties": report.protected_properties,
                },
                actor="semantic-ci",
            )
            self._append(
                et.COMPLETION_CASE_BUILT,
                {"completion_case": completion.model_dump(mode="json")},
                actor="controller",
            )
            return True
        except BaseException as exc:
            usage, trace = self._failure_parts(exc)
            recovery_artifact, recovery_capture_error = self._capture_recovery_artifact(
                workspace,
                summary="Interrupted release-repair workspace.",
                parent=current,
                source_action_ids=[],
            )
            self._append(
                et.REPAIR_FAILED,
                {
                    "call_id": call_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "usage": usage.model_dump(mode="json"),
                    "recovery_artifact": (
                        recovery_artifact.model_dump(mode="json") if recovery_artifact else None
                    ),
                    "recovery_capture_error": recovery_capture_error,
                    **trace.payload(),
                },
                actor="controller",
            )
            return False
        finally:
            self._close_workspace(workspace)

    def _default_output_path(self) -> Path:
        if self.config.run.final_output is not None:
            return self.config.run.final_output.expanduser().resolve()
        return self.run_dir / f"final{self.adapter.final_suffix}"

    def _materialize_and_complete(
        self,
        *,
        stop_reason: str,
        output_path: Path | None,
        release: ReleaseOutput | None,
        decision: MutationGateDecision,
        actor: str,
    ) -> Path:
        artifact = self.state.final_artifact
        if artifact is None:
            raise FrontierError("Finalization did not produce an artifact")
        destination = (output_path or self._default_output_path()).expanduser().resolve()
        self.adapter.materialize_final(artifact, destination)
        deliverable_paths: list[str] = []
        if artifact.deliverables:
            deliverable_dir = destination.parent / f"{destination.stem}.deliverables"
            deliverable_dir.mkdir(parents=True, exist_ok=True)
            used_names: set[str] = set()
            for index, ref in enumerate(artifact.deliverables, start=1):
                base_name = Path(ref.original_name or f"deliverable-{index}").name
                name = base_name
                if name in used_names:
                    name = f"{index:03d}-{base_name}"
                used_names.add(name)
                materialized = self.blobs.materialize(ref, deliverable_dir / name)
                deliverable_paths.append(str(materialized))

        apply_result: dict[str, Any] | None = None
        if decision.mutation_gate_passed:
            apply_result = self.adapter.apply_final(artifact)
            if apply_result is not None:
                self._append(et.PATCH_APPLIED, apply_result, actor=actor)

        completion_reason = stop_reason
        if decision.block_reason:
            completion_reason += f"; external mutation withheld: {decision.block_reason}"
        payload = {
            "completed_at": utc_now(),
            "stop_reason": completion_reason,
            "output_path": str(destination),
            "deliverable_paths": deliverable_paths,
            "release_gate_run": decision.release_gate_succeeded,
            "release_finding_count": len(release.findings) if release else 0,
            "source_apply_blocked_reason": (decision.block_reason if self._software else None),
            "apply_result": apply_result,
            **decision.payload(),
        }
        self._append(et.RUN_COMPLETED, payload, actor=actor)
        self._write_seal()
        return destination

    async def _finalize(self, *, stop_reason: str, output_path: Path | None) -> Path:
        while True:
            final_output, _ = await self._synthesize_final(stop_reason)
            if await self._control_boundary():
                if not await self._checkpoint([], self.state.round_index):
                    raise FrontierError(
                        "Steering arrived during finalization, but its fresh checkpoint failed"
                    )
                stop_reason = self.state.stop_reason or await self._advance_frontier()
                continue
            checks = self._record_deterministic_checks()
            _, release, decision = await self._run_release_tail(final_output, checks)
            if self.state.runtime.release.replan_pending:
                self.observer.set_runtime(
                    RuntimeStatus.RUNNING,
                    phase=self.state.phase.value,
                    detail="release evidence reopened the causal frontier",
                )
                if not await self._checkpoint([], self.state.round_index):
                    raise FrontierError(
                        "Release evidence reopened an upstream commitment, but its fresh "
                        "causal checkpoint failed"
                    )
                stop_reason = await self._advance_frontier()
                continue
            if await self._control_boundary():
                if not await self._checkpoint([], self.state.round_index):
                    raise FrontierError(
                        "Steering arrived during release, but its fresh checkpoint failed"
                    )
                stop_reason = self.state.stop_reason or await self._advance_frontier()
                continue
            # Close the command/completion race. A command committed before
            # SEALING is visible in the immediate recheck; commands attempted
            # after it are rejected by the control inbox.
            self.observer.set_runtime(
                RuntimeStatus.SEALING,
                phase=self.state.phase.value,
                detail="sealing final result",
            )
            if self.control.commands(pending_only=True):
                self.observer.set_runtime(
                    RuntimeStatus.RUNNING,
                    phase=self.state.phase.value,
                    detail="late command boundary",
                )
                if await self._control_boundary():
                    if not await self._checkpoint([], self.state.round_index):
                        raise FrontierError(
                            "Steering arrived before sealing, but its fresh checkpoint failed"
                        )
                    stop_reason = self.state.stop_reason or await self._advance_frontier()
                    continue
                self.observer.set_runtime(
                    RuntimeStatus.SEALING,
                    phase=self.state.phase.value,
                    detail="sealing final result",
                )
            return self._materialize_and_complete(
                stop_reason=stop_reason,
                output_path=output_path,
                release=release,
                decision=decision,
                actor="runtime",
            )

    # ------------------------------------------------------------------
    # Recovery and public execution
    # ------------------------------------------------------------------
    def _recover_interrupted_actions(self) -> None:
        for record in list(self.state.actions.values()):
            if record.status == ActionStatus.RUNNING:
                latest_attempt = record.attempts[-1] if record.attempts else None
                provider_succeeded = bool(
                    latest_attempt and latest_attempt.status.value == "succeeded"
                )
                recovered_usage = (
                    latest_attempt.usage
                    if latest_attempt is not None and provider_succeeded
                    else None
                )
                self._append(
                    et.ACTION_FAILED,
                    {
                        "action_id": record.spec.action_id,
                        "error": (
                            "provider attempt completed and its raw result was preserved, but "
                            "semantic integration was interrupted"
                            if provider_succeeded
                            else "previous process ended before provider attempt completion"
                        ),
                        "completed_at": utc_now(),
                        "usage": (
                            recovered_usage.model_dump(mode="json")
                            if recovered_usage is not None
                            else Usage().model_dump(mode="json")
                        ),
                    },
                    actor="recovery",
                    action_id=record.spec.action_id,
                )

    def _close_workspace(self, workspace: CallWorkspace) -> None:
        try:
            self.adapter.close_call(workspace)
        finally:
            if (
                not self.config.run.keep_capsules
                and self.adapter.engine_cleans_call_workspace
                and workspace.root.exists()
            ):
                shutil.rmtree(workspace.root, ignore_errors=True)

    async def execute(self, *, output_path: Path | None = None) -> Path:
        """Advance this run through the semantic coordinator."""

        return await RunCoordinator().execute(self, output_path=output_path)
