"""Stable run supervisor for disposable, live-switchable activity workers."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import HarnessConfig
from ..control import CommandKind, RunControlPlane, RuntimeStatus
from ..core.journal import KernelJournal
from ..errors import FrontierError
from ..ledger import EventLedger
from ..locking import RunLock
from ..util import utc_now
from .components import STEP_PROTOCOL, ComponentBinding, ComponentRegistry
from .engine import KernelEngine
from .repair import CodexComponentRepairer, ComponentFault


@dataclass(frozen=True)
class StepReceipt:
    before_seq: int
    after_seq: int
    status: str

    @property
    def progressed(self) -> bool:
        return self.after_seq > self.before_seq


class StepSupervisor:
    """Keep the run alive while replacing its implementation between steps."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir.resolve()
        self.registry = ComponentRegistry(self.run_dir)
        manifest = json.loads(
            (self.run_dir / KernelEngine.MANIFEST_FILE).read_text(encoding="utf-8")
        )
        config = HarnessConfig.model_validate_json(
            (self.run_dir / KernelEngine.CONFIG_FILE).read_text(encoding="utf-8")
        )
        self.control = RunControlPlane(
            self.run_dir / KernelEngine.CONTROL_FILE,
            str(manifest["run_id"]),
        )
        self.lock = RunLock(self.run_dir / ".supervisor.lock")
        self.repairer = CodexComponentRepairer(self.run_dir, self.registry, config.runtime)

    @staticmethod
    def _repairable_pause(state: dict[str, Any]) -> bool:
        return (
            state.get("status") == "paused"
            and state.get("pause_kind") == "execution"
            and state.get("failure_domain") in {"component", "provider", "assay"}
        )

    @staticmethod
    def _state(run_dir: Path) -> dict[str, Any]:
        try:
            manifest = json.loads(
                (run_dir / KernelEngine.MANIFEST_FILE).read_text(encoding="utf-8")
            )
            config = HarnessConfig.model_validate_json(
                (run_dir / KernelEngine.CONFIG_FILE).read_text(encoding="utf-8")
            )
        except (FileNotFoundError, json.JSONDecodeError, ValueError, KeyError) as exc:
            raise FrontierError(f"invalid durable run state: {run_dir}") from exc
        ledger = EventLedger(
            run_dir / KernelEngine.LEDGER_FILE,
            str(manifest["run_id"]),
            busy_timeout_ms=config.runtime.sqlite_busy_timeout_ms,
        )
        try:
            state = KernelJournal.recover_projection(
                ledger=ledger,
                snapshot_path=run_dir / KernelEngine.STATE_FILE,
                max_event_payload_bytes=config.kernel.max_event_payload_bytes,
            )
            return state.model_dump(mode="json", exclude_computed_fields=True)
        except (OSError, ValueError) as exc:
            raise FrontierError(f"invalid durable run state: {run_dir}") from exc
        finally:
            ledger.close()

    async def execute(self) -> dict[str, Any]:
        with self.lock:
            self.control.set_runtime(
                status=RuntimeStatus.RUNNING,
                phase="active",
                pid=os.getpid(),
                started_at=utc_now(),
                detail="supervising replaceable activity workers",
            )
            last_generation: int | None = None
            try:
                while True:
                    state = self._state(self.run_dir)
                    status = str(state.get("status", "active"))
                    recoverable_pause = self._repairable_pause(state)
                    pending_resume = status == "paused" and any(
                        command.kind.value == "resume"
                        for command in self.control.commands(pending_only=True)
                    )
                    if (
                        status
                        in {
                            "satisfied",
                            "exhausted",
                            "blocked",
                            "stopped",
                            "failed",
                            "paused",
                        }
                        and not pending_resume
                        and not recoverable_pause
                    ):
                        return state

                    binding = self.registry.active()
                    if binding.generation != last_generation:
                        self.control.record_activity(
                            kind="component.boundary",
                            label="component",
                            message=f"generation {binding.generation} active",
                            state="done",
                            payload={"generation": binding.generation, "digest": binding.digest},
                        )
                        last_generation = binding.generation

                    before_seq = int(state.get("last_event_seq", 0))
                    try:
                        process = await self._spawn(binding)
                    except (OSError, FrontierError) as exc:
                        if await self._recover_component(
                            binding,
                            stage="worker_start",
                            detail=f"worker could not start: {exc}",
                            before_seq=before_seq,
                            after_seq=before_seq,
                        ):
                            continue
                        raise
                    stdout, stderr = await process.communicate()
                    current = self._state(self.run_dir)
                    after_seq = int(current.get("last_event_seq", 0))

                    if process.returncode != 0:
                        self.registry.record_receipt(
                            {
                                "generation": binding.generation,
                                "digest": binding.digest,
                                "before_seq": before_seq,
                                "after_seq": after_seq,
                                "outcome": "worker_error",
                                "returncode": process.returncode,
                            }
                        )
                        detail = stderr.decode(errors="replace").strip()
                        if await self._recover_component(
                            binding,
                            stage="worker_exit",
                            detail=detail or "worker exited without a receipt",
                            before_seq=before_seq,
                            after_seq=after_seq,
                        ):
                            continue
                        raise FrontierError(
                            f"component generation {binding.generation} failed: "
                            f"{detail or 'worker exited without a receipt'}"
                        )

                    try:
                        receipt = self._receipt(stdout)
                    except FrontierError as exc:
                        if await self._recover_component(
                            binding,
                            stage="worker_receipt",
                            detail=str(exc),
                            before_seq=before_seq,
                            after_seq=after_seq,
                        ):
                            continue
                        raise
                    self.registry.record_receipt(
                        {
                            "generation": binding.generation,
                            "digest": binding.digest,
                            "before_seq": receipt.before_seq,
                            "after_seq": receipt.after_seq,
                            "outcome": "advanced" if receipt.progressed else "idle",
                            "status": receipt.status,
                        }
                    )
                    if receipt.before_seq != before_seq or receipt.after_seq != after_seq:
                        detail = "component receipt does not match the durable journal"
                        if await self._recover_component(
                            binding,
                            stage="journal_receipt",
                            detail=detail,
                            before_seq=before_seq,
                            after_seq=after_seq,
                        ):
                            continue
                        raise FrontierError(detail)
                    if receipt.status == "paused" and self._repairable_pause(current):
                        detail = str(current.get("terminal_reason") or "execution paused")
                        if await self._recover_component(
                            binding,
                            stage="execution_pause",
                            detail=detail,
                            before_seq=before_seq,
                            after_seq=after_seq,
                        ):
                            self.control.enqueue(
                                CommandKind.RESUME,
                                text="infrastructure repaired; retry the preserved move",
                            )
                            continue
                        raise FrontierError(f"automatic repair could not recover: {detail}")
                    if (
                        receipt.status
                        in {
                            "satisfied",
                            "exhausted",
                            "blocked",
                            "stopped",
                            "failed",
                            "paused",
                        }
                        or not receipt.progressed
                    ):
                        return current

                    self.control.set_runtime(
                        status=RuntimeStatus.RUNNING,
                        phase=str(current.get("status", "active")),
                        pid=os.getpid(),
                        detail="supervising replaceable activity workers",
                    )
            finally:
                state = self._state(self.run_dir)
                status = str(state.get("status", "active"))
                runtime_status = (
                    RuntimeStatus.PAUSED
                    if status == "paused"
                    else RuntimeStatus.FAILED
                    if status == "failed"
                    else RuntimeStatus.COMPLETE
                    if status in {"satisfied", "exhausted", "blocked", "stopped"}
                    else RuntimeStatus.IDLE
                )
                self.control.set_runtime(
                    status=runtime_status,
                    phase=status,
                    pid=None,
                    detail=str(state.get("terminal_reason") or status),
                )

    async def _spawn(self, binding: ComponentBinding) -> asyncio.subprocess.Process:
        slot = self.registry.slot_path(binding)
        environment = os.environ.copy()
        old_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(slot) if not old_pythonpath else os.pathsep.join((str(slot), old_pythonpath))
        )
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["FLOURITE_COMPONENT_GENERATION"] = str(binding.generation)
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "frontier_harness.runtime.step_worker",
            "--run-dir",
            str(self.run_dir),
            cwd=self.run_dir,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _recover_component(
        self,
        failed: ComponentBinding,
        *,
        stage: str,
        detail: str,
        before_seq: int,
        after_seq: int,
    ) -> bool:
        fallback = self.registry.rollback_if_current(failed)
        if fallback is not None:
            self.control.record_activity(
                kind="component.rollback",
                label="component",
                message=(
                    f"generation {failed.generation} failed · rolled back to {fallback.generation}"
                ),
                state="warn",
                payload={"failed_digest": failed.digest, "detail": detail},
            )
            return True
        if self.registry.active().generation != failed.generation:
            return True
        fault = ComponentFault(
            stage=stage,
            detail=detail,
            generation=failed.generation,
            digest=failed.digest,
            before_seq=before_seq,
            after_seq=after_seq,
            activity_key=self._activity_key(),
        )
        recovered = await self.repairer.recover(failed, fault)
        if recovered:
            active = self.registry.active()
            self.control.record_activity(
                kind="component.repair",
                label="component",
                message=(
                    f"fault {fault.fingerprint[:10]} recovered · generation {active.generation}"
                ),
                state="warn",
                payload={"stage": stage, "failed_digest": failed.digest},
            )
        return recovered

    def _activity_key(self) -> str:
        state = self._state(self.run_dir)
        moves = state.get("moves")
        if not isinstance(moves, dict):
            return "run-boundary"
        candidates = [
            value
            for value in moves.values()
            if isinstance(value, dict) and value.get("status") in {"running", "failed", "proposed"}
        ]
        if not candidates:
            return "run-boundary"
        move = candidates[-1]
        seen: set[str] = set()
        while isinstance(move, dict):
            move_id = str(move.get("move_id") or "run-boundary")
            parent = move.get("retry_of_move_id")
            if not parent or str(parent) in seen or str(parent) not in moves:
                return move_id
            seen.add(move_id)
            move = moves[str(parent)]
        return "run-boundary"

    @staticmethod
    def _receipt(stdout: bytes) -> StepReceipt:
        lines = [line for line in stdout.decode(errors="replace").splitlines() if line.strip()]
        try:
            payload = json.loads(lines[-1])
            if payload.get("protocol") != STEP_PROTOCOL:
                raise ValueError("wrong protocol")
            status = str(payload["status"])
            if status not in {
                "active",
                "paused",
                "satisfied",
                "exhausted",
                "blocked",
                "stopped",
                "failed",
            }:
                raise ValueError("unknown run status")
            return StepReceipt(
                before_seq=int(payload["before_seq"]),
                after_seq=int(payload["after_seq"]),
                status=status,
            )
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FrontierError("component worker returned an invalid receipt") from exc

    def close(self) -> None:
        self.control.close()
