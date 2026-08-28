from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from frontier_harness.config import HarnessConfig, ProviderConfig, RunPolicy
from frontier_harness.control import CommandKind
from frontier_harness.core.types import PauseKind, RunPaused
from frontier_harness.errors import FrontierError
from frontier_harness.runtime.components import ComponentRegistry
from frontier_harness.runtime.engine import KernelEngine
from frontier_harness.runtime.supervisor import StepSupervisor


class _Repairer:
    def __init__(self, registry: ComponentRegistry, source: Path | None = None) -> None:
        self.registry = registry
        self.source = source
        self.faults = []

    async def recover(self, _binding, fault) -> bool:
        self.faults.append(fault)
        if self.source is not None:
            self.registry.bind(self.source)
        return True


def _component_copy(tmp_path: Path) -> Path:
    source = ComponentRegistry.current_source()
    destination = tmp_path / "candidate" / "frontier_harness"
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    return destination


async def _run_one_worker(
    supervisor: StepSupervisor,
    registry: ComponentRegistry,
) -> tuple[int, int]:
    binding = registry.active()
    process = await supervisor._spawn(binding)
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode(errors="replace")
    receipt = supervisor._receipt(stdout)
    return binding.generation, receipt.after_seq


def test_component_bind_is_immutable_atomic_and_reversible(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    registry = ComponentRegistry(run_dir)

    first = registry.initialize()
    first_slot = registry.slot_path(first)
    assert first.generation == 1
    assert (first_slot / "frontier_harness" / "runtime" / "step_worker.py").is_file()

    candidate = _component_copy(tmp_path)
    (candidate / "component_marker.py").write_text("GENERATION = 2\n", encoding="utf-8")
    second = registry.bind(candidate)

    assert second.generation == 2
    assert second.digest != first.digest
    assert not (first_slot / "frontier_harness" / "component_marker.py").exists()
    assert (registry.slot_path(second) / "frontier_harness" / "component_marker.py").is_file()
    assert registry.active() == second

    rollback = registry.rollback_if_current(second)
    assert rollback is not None
    assert rollback.generation == 3
    assert rollback.digest == first.digest
    assert registry.active() == rollback


def test_supervisor_executes_fake_run_through_disposable_workers(tmp_path: Path) -> None:
    config = HarnessConfig(
        run=RunPolicy(run_root=tmp_path / "runs"),
        provider=ProviderConfig(kind="fake"),
    )
    engine = KernelEngine.create(
        "Prove replaceable workers preserve one durable objective.",
        config=config,
        adapter_name="generic",
    )
    run_dir = engine.run_dir
    ComponentRegistry(run_dir).initialize()
    engine.close()

    supervisor = StepSupervisor(run_dir)
    try:
        state = asyncio.run(supervisor.execute())
    finally:
        supervisor.close()

    assert state["status"] == "satisfied"
    receipts = [
        json.loads(line)
        for line in (run_dir / ComponentRegistry.RECEIPTS_FILE)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(receipts) >= 2
    assert {item["generation"] for item in receipts} == {1}
    assert all(item["outcome"] == "advanced" for item in receipts)


def test_supervisor_executes_a_queued_resume_for_a_paused_run(tmp_path: Path) -> None:
    config = HarnessConfig(
        run=RunPolicy(run_root=tmp_path / "runs"),
        provider=ProviderConfig(kind="fake"),
    )
    engine = KernelEngine.create(
        "Resume through the replaceable worker boundary.",
        config=config,
        adapter_name="generic",
    )
    run_dir = engine.run_dir
    engine.journal.append("run.paused", RunPaused(reason="synthetic interruption"))
    engine.control.enqueue(CommandKind.RESUME)
    ComponentRegistry(run_dir).initialize()
    engine.close()

    supervisor = StepSupervisor(run_dir)
    try:
        state = asyncio.run(supervisor.execute())
    finally:
        supervisor.close()

    assert state["status"] == "satisfied"


def test_supervisor_rolls_a_failed_replacement_back_without_touching_state(
    tmp_path: Path,
) -> None:
    config = HarnessConfig(
        run=RunPolicy(run_root=tmp_path / "runs"),
        provider=ProviderConfig(kind="fake"),
    )
    engine = KernelEngine.create(
        "Keep the durable state still.",
        config=config,
        adapter_name="generic",
    )
    run_dir = engine.run_dir
    registry = ComponentRegistry(run_dir)
    first = registry.initialize()
    candidate = _component_copy(tmp_path)
    (candidate / "component_marker.py").write_text("GENERATION = 2\n", encoding="utf-8")
    second = registry.bind(candidate)
    state_before = (run_dir / KernelEngine.STATE_FILE).read_bytes()
    engine.close()

    supervisor = StepSupervisor(run_dir)
    try:
        assert (
            asyncio.run(
                supervisor._recover_component(
                    second,
                    stage="worker_start",
                    detail="synthetic boot failure",
                    before_seq=1,
                    after_seq=1,
                )
            )
            is True
        )
    finally:
        supervisor.close()

    active = registry.active()
    assert active.generation == 3
    assert active.digest == first.digest
    assert (run_dir / KernelEngine.STATE_FILE).read_bytes() == state_before


def test_next_activity_uses_a_component_bound_after_the_previous_one(
    tmp_path: Path,
) -> None:
    config = HarnessConfig(
        run=RunPolicy(run_root=tmp_path / "runs"),
        provider=ProviderConfig(kind="fake"),
    )
    engine = KernelEngine.create(
        "Continue across a live implementation change.",
        config=config,
        adapter_name="generic",
    )
    run_dir = engine.run_dir
    registry = ComponentRegistry(run_dir)
    first = registry.initialize()
    engine.close()
    supervisor = StepSupervisor(run_dir)
    try:
        first_generation, first_seq = asyncio.run(_run_one_worker(supervisor, registry))
        assert first_generation == first.generation

        candidate = _component_copy(tmp_path)
        (candidate / "component_marker.py").write_text("GENERATION = 2\n", encoding="utf-8")
        second = registry.bind(candidate)
        final = asyncio.run(supervisor.execute())
    finally:
        supervisor.close()

    assert final["status"] == "satisfied"
    assert int(final["last_event_seq"]) > first_seq
    receipts = [
        json.loads(line)
        for line in (run_dir / ComponentRegistry.RECEIPTS_FILE)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert receipts
    assert {item["generation"] for item in receipts} == {second.generation}


def test_supervisor_repairs_a_crashing_first_generation_and_replays_exact_step(
    tmp_path: Path,
) -> None:
    config = HarnessConfig(
        run=RunPolicy(run_root=tmp_path / "runs"),
        provider=ProviderConfig(kind="fake"),
    )
    engine = KernelEngine.create(
        "Recover the exact activity after implementation failure.",
        config=config,
        adapter_name="generic",
    )
    run_dir = engine.run_dir
    broken = _component_copy(tmp_path)
    worker = broken / "runtime" / "step_worker.py"
    worker.write_text(
        worker.read_text(encoding="utf-8").replace(
            "async def _step(run_dir: Path) -> dict[str, object]:\n",
            "async def _step(run_dir: Path) -> dict[str, object]:\n"
            "    raise RuntimeError('synthetic component crash')\n",
        ),
        encoding="utf-8",
    )
    registry = ComponentRegistry(run_dir)
    failed = registry.initialize(broken)
    state_before = (run_dir / KernelEngine.STATE_FILE).read_bytes()
    engine.close()

    supervisor = StepSupervisor(run_dir)
    repairer = _Repairer(registry, ComponentRegistry.current_source())
    supervisor.repairer = repairer  # type: ignore[assignment]
    try:
        state = asyncio.run(supervisor.execute())
    finally:
        supervisor.close()

    assert state["status"] == "satisfied"
    assert repairer.faults and repairer.faults[0].stage == "worker_exit"
    assert registry.active().digest != failed.digest
    assert state_before != (run_dir / KernelEngine.STATE_FILE).read_bytes()


def test_codex_repair_boundary_admits_a_new_generation_only_then_replays(
    tmp_path: Path,
) -> None:
    repair_command = tmp_path / "fake-codex"
    repair_command.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "root = pathlib.Path(sys.argv[sys.argv.index('--cd') + 1])\n"
        "worker = root / 'frontier_harness/runtime/step_worker.py'\n"
        "worker.write_text(worker.read_text().replace("
        "\"    raise RuntimeError('synthetic component crash')\\n\", \"\"))\n",
        encoding="utf-8",
    )
    repair_command.chmod(0o755)
    config = HarnessConfig(
        run=RunPolicy(run_root=tmp_path / "runs"),
        provider=ProviderConfig(kind="fake"),
    )
    engine = KernelEngine.create(
        "Admit repair only through a new component generation.",
        config=config,
        adapter_name="generic",
    )
    run_dir = engine.run_dir
    snapshot = json.loads((run_dir / KernelEngine.CONFIG_FILE).read_text(encoding="utf-8"))
    snapshot["runtime"]["repair_command"] = str(repair_command)
    (run_dir / KernelEngine.CONFIG_FILE).write_text(json.dumps(snapshot), encoding="utf-8")
    broken = _component_copy(tmp_path)
    worker = broken / "runtime" / "step_worker.py"
    worker.write_text(
        worker.read_text(encoding="utf-8").replace(
            "async def _step(run_dir: Path) -> dict[str, object]:\n",
            "async def _step(run_dir: Path) -> dict[str, object]:\n"
            "    raise RuntimeError('synthetic component crash')\n",
        ),
        encoding="utf-8",
    )
    registry = ComponentRegistry(run_dir)
    failed = registry.initialize(broken)
    engine.close()

    supervisor = StepSupervisor(run_dir)
    try:
        state = asyncio.run(supervisor.execute())
    finally:
        supervisor.close()

    assert state["status"] == "satisfied"
    assert registry.active().digest != failed.digest
    receipts = [
        json.loads(line)
        for line in (run_dir / "repair-receipts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [item["outcome"] for item in receipts] == ["applied"]


def test_unchanged_repair_gets_one_exact_replay_then_stops_without_looping(
    tmp_path: Path,
) -> None:
    repair_command = tmp_path / "no-change-codex"
    repair_command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    repair_command.chmod(0o755)
    config = HarnessConfig(
        run=RunPolicy(run_root=tmp_path / "runs"),
        provider=ProviderConfig(kind="fake"),
    )
    engine = KernelEngine.create(
        "Stop repeated infrastructure failure without a repair gradient.",
        config=config,
        adapter_name="generic",
    )
    run_dir = engine.run_dir
    snapshot = json.loads((run_dir / KernelEngine.CONFIG_FILE).read_text(encoding="utf-8"))
    snapshot["runtime"]["repair_command"] = str(repair_command)
    (run_dir / KernelEngine.CONFIG_FILE).write_text(json.dumps(snapshot), encoding="utf-8")
    broken = _component_copy(tmp_path)
    worker = broken / "runtime" / "step_worker.py"
    worker.write_text(
        worker.read_text(encoding="utf-8").replace(
            "async def _step(run_dir: Path) -> dict[str, object]:\n",
            "async def _step(run_dir: Path) -> dict[str, object]:\n"
            "    raise RuntimeError('synthetic component crash')\n",
        ),
        encoding="utf-8",
    )
    registry = ComponentRegistry(run_dir)
    registry.initialize(broken)
    engine.close()

    supervisor = StepSupervisor(run_dir)
    try:
        with pytest.raises(FrontierError, match="component generation"):
            asyncio.run(supervisor.execute())
    finally:
        supervisor.close()

    receipts = [
        json.loads(line)
        for line in (run_dir / "repair-receipts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [item["outcome"] for item in receipts] == [
        "retry_unchanged",
        "stopped_no_progress",
    ]


def test_supervisor_recovers_an_execution_pause_after_process_restart(tmp_path: Path) -> None:
    config = HarnessConfig(
        run=RunPolicy(run_root=tmp_path / "runs"),
        provider=ProviderConfig(kind="fake"),
    )
    engine = KernelEngine.create(
        "Resume the preserved semantic move after infrastructure recovery.",
        config=config,
        adapter_name="generic",
    )
    run_dir = engine.run_dir
    engine.journal.append(
        "run.paused",
        RunPaused(reason="synthetic execution fault", kind=PauseKind.EXECUTION),
    )
    registry = ComponentRegistry(run_dir)
    registry.initialize()
    engine.close()

    supervisor = StepSupervisor(run_dir)
    repairer = _Repairer(registry)
    supervisor.repairer = repairer  # type: ignore[assignment]
    try:
        state = asyncio.run(supervisor.execute())
    finally:
        supervisor.close()

    assert state["status"] == "satisfied"
    assert repairer.faults and repairer.faults[0].stage == "execution_pause"
