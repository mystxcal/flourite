from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from frontier_harness.config import HarnessConfig, ProviderConfig, RunPolicy
from frontier_harness.runtime.components import ComponentRegistry
from frontier_harness.runtime.engine import KernelEngine
from frontier_harness.runtime.supervisor import StepSupervisor


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
        assert supervisor._recover_component(second, "synthetic boot failure") is True
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
