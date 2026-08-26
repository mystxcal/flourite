"""Semantic run coordinator: resume, advance, release, and seal."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from .. import events as et
from ..control import RuntimeStatus
from ..errors import FrontierError, OperatorStop
from ..models import FinalOutput, RunPhase
from ..util import utc_now

if TYPE_CHECKING:
    from ..engine import FrontierEngine


class RunCoordinator:
    """Drive semantic boundaries; delegate domain work to focused capabilities."""

    async def execute(
        self,
        engine: FrontierEngine,
        *,
        output_path: Path | None = None,
    ) -> Path:
        """Resume safely and advance one run until its result is sealed."""

        with engine.lock:
            engine._refresh_state_from_ledger()
            engine.verify_integrity()
            terminal = self._terminal_result(engine, output_path)
            if terminal is not None:
                return terminal

            self._set_runtime(engine, RuntimeStatus.RUNNING, "controller starting")
            engine._recover_interrupted_actions()
            engine._ensure_resource_state()
            try:
                return await self._drive(engine, output_path)
            except OperatorStop:
                self._set_runtime(engine, RuntimeStatus.STOPPED, "stopped by operator")
                raise
            except (asyncio.CancelledError, KeyboardInterrupt):
                self._set_runtime(engine, RuntimeStatus.STOPPED, "controller interrupted")
                raise
            except FrontierError:
                self._set_runtime(engine, RuntimeStatus.FAILED, "controller error")
                raise
            except BaseException as exc:
                self._fail_unexpected(engine, exc)

    @staticmethod
    def _terminal_result(
        engine: FrontierEngine,
        output_path: Path | None,
    ) -> Path | None:
        intent_path = engine.run_dir / engine.EXTENSION_INTENT_FILE
        if engine.state.phase == RunPhase.COMPLETE and intent_path.exists():
            raise FrontierError(
                "A durable extension intent is pending; run `flourite extend` to complete it"
            )
        if engine.state.phase == RunPhase.FAILED:
            raise FrontierError(
                f"Run is terminally failed: {engine.state.stop_reason or 'unknown error'}"
            )
        if engine.state.phase != RunPhase.COMPLETE:
            return None
        artifact = engine.state.final_artifact
        if artifact is None:
            raise FrontierError("Completed run has no final artifact")
        if output_path is not None:
            return engine.adapter.materialize_final(artifact, output_path.expanduser().resolve())
        existing = engine.state.runtime.completion.output_path
        if existing and Path(existing).exists():
            return Path(existing)
        return engine.adapter.materialize_final(
            artifact,
            engine._default_output_path().expanduser().resolve(),
        )

    async def _drive(
        self,
        engine: FrontierEngine,
        output_path: Path | None,
    ) -> Path:
        steered_before_bootstrap = await self._prepare_run(engine)
        await self._satisfy_entry_checkpoints(engine, steered_before_bootstrap)

        if engine.state.phase == RunPhase.RELEASE and engine.state.final_artifact is not None:
            path = await self._resume_release(engine, output_path)
        else:
            path = await self._advance_and_finalize(engine, output_path)
        self._set_runtime(engine, RuntimeStatus.COMPLETE, "result sealed")
        return path

    @staticmethod
    async def _prepare_run(engine: FrontierEngine) -> bool:
        if engine.state.runtime.control.status in {"paused", "stopped"}:
            engine._append(
                et.RUN_RESUMED,
                {"detail": "new controller process resumed durable state"},
                actor="recovery",
            )
        steered = await engine._control_boundary()
        if engine.state.contract is None or engine.state.current_artifact is None:
            await engine._bootstrap()
            return False
        return steered

    @staticmethod
    async def _satisfy_entry_checkpoints(
        engine: FrontierEngine,
        steered_before_bootstrap: bool,
    ) -> None:
        if (
            engine.state.runtime.bootstrap.independent_checkpoint_required
            and not await engine._checkpoint([], engine.state.round_index)
        ):
            raise FrontierError(
                "A full-scope bootstrap required an independent architecture checkpoint, "
                "but that checkpoint failed"
            )

        if (
            steered_before_bootstrap
            or engine.state.runtime.control.steering_replan_pending
        ) and not await engine._checkpoint([], engine.state.round_index):
            raise FrontierError(
                "Operator steering was admitted, but its required checkpoint failed"
            )

        if engine.state.runtime.extension.replan_pending:
            (engine.run_dir / engine.EXTENSION_INTENT_FILE).unlink(missing_ok=True)
            if not await engine._checkpoint([], engine.state.round_index):
                raise FrontierError(
                    "Extension was admitted but its required fresh Lead checkpoint failed; "
                    "the run remains recoverable"
                )

    async def _resume_release(
        self,
        engine: FrontierEngine,
        output_path: Path | None,
    ) -> Path:
        if await engine._control_boundary():
            if not await engine._checkpoint([], engine.state.round_index):
                raise FrontierError(
                    "Operator steering was admitted, but its required checkpoint failed"
                )
            return await self._advance_and_finalize(engine, output_path)

        checks = engine._record_deterministic_checks()
        artifact = engine.state.final_artifact
        if artifact is None:
            raise FrontierError("Release recovery lost the final artifact")
        dummy = FinalOutput(
            artifact_path="",
            summary=artifact.summary,
            remaining_uncertainty=list(engine.state.runtime.release.remaining_uncertainty),
            release_gate_recommended=bool(engine.state.runtime.release.gate_recommended),
        )
        _, release, decision = await engine._run_release_tail(dummy, checks)
        if engine.state.runtime.release.replan_pending:
            if not await engine._checkpoint([], engine.state.round_index):
                raise FrontierError(
                    "Resumed release evidence reopened an upstream commitment, "
                    "but its causal checkpoint failed"
                )
            return await self._advance_and_finalize(engine, output_path)
        return engine._materialize_and_complete(
            stop_reason=engine.state.stop_reason or "resumed finalization tail",
            output_path=output_path,
            release=release,
            decision=decision,
            actor="recovery",
        )

    @staticmethod
    async def _advance_and_finalize(
        engine: FrontierEngine,
        output_path: Path | None,
    ) -> Path:
        stop_reason = engine.state.stop_reason or await engine._advance_frontier()
        if await engine._control_boundary():
            if not await engine._checkpoint([], engine.state.round_index):
                raise FrontierError(
                    "Operator steering was admitted, but its required checkpoint failed"
                )
            stop_reason = engine.state.stop_reason or await engine._advance_frontier()
        return await engine._finalize(stop_reason=stop_reason, output_path=output_path)

    @staticmethod
    def _set_runtime(
        engine: FrontierEngine,
        status: RuntimeStatus,
        detail: str,
    ) -> None:
        engine.observer.set_runtime(status, phase=engine.state.phase.value, detail=detail)

    @staticmethod
    def _fail_unexpected(engine: FrontierEngine, exc: BaseException) -> NoReturn:
        engine._append(
            et.RUN_FAILED,
            {"failed_at": utc_now(), "error": f"{type(exc).__name__}: {exc}"},
            actor="runtime",
        )
        RunCoordinator._set_runtime(
            engine,
            RuntimeStatus.FAILED,
            f"{type(exc).__name__}: {exc}",
        )
        raise exc
