"""`flourite` command-line interface."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import typer

from . import __version__
from .arena import ArenaRunner
from .config import EXAMPLE_CONFIG, load_config
from .control import CommandKind, RunControlPlane, RuntimeStatus
from .engine import FrontierEngine
from .errors import FrontierError, OperatorStop
from .events import (
    ACTION_COMPLETED,
    ACTION_FAILED,
    ACTION_SELECTED,
    ACTION_STARTED,
    BOOTSTRAP_COMPLETED,
    BOOTSTRAP_STARTED,
    CHECKPOINT_COMPLETED,
    CHECKPOINT_FAILED,
    CHECKPOINT_STARTED,
    FINAL_SYNTHESIZED,
    FINALIZATION_STARTED,
    RELEASE_COMPLETED,
    RELEASE_FAILED,
    REPAIR_COMPLETED,
    REPAIR_LOOP_STOPPED,
    RESOURCE_DECIDED,
    RESOURCE_INITIALIZED,
    RUN_COMPLETED,
    RUN_EXTENDED,
    RUN_PAUSED,
    RUN_RESUMED,
    RUN_STOPPED,
    TASK_SOURCE_AMENDED,
)
from .evolution import HarnessCandidate, HarnessPromotionGate, HarnessTrial
from .exporter import export_kernel_run, export_run
from .live import open_live_dashboard
from .presentation import (
    data_table,
    key_value_table,
    make_console,
    phase_line,
    print_brand,
)
from .providers import build_provider
from .runtime.engine import KernelEngine
from .util import atomic_write_text

app = typer.Typer(
    name="flourite",
    help="A frontier-scale agent harness for exact, high-ceiling problem solving.",
    no_args_is_help=True,
    rich_markup_mode="markdown",
)
console = make_console()
error_console = make_console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"flourite {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """Flourite — frontier-scale agent harness."""


def _overrides(
    *,
    fake: bool,
    adapter: str | None = None,
    run_root: Path | None = None,
    max_calls: int | None = None,
    max_rounds: int | None = None,
    max_parallel: int | None = None,
    release_gate: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if fake:
        data.setdefault("provider", {})["kind"] = "fake"
    if adapter:
        data.setdefault("run", {})["adapter"] = adapter
    if run_root:
        data.setdefault("run", {})["run_root"] = str(run_root)
    if release_gate:
        data.setdefault("run", {})["release_gate"] = release_gate
    budget: dict[str, Any] = {}
    if max_calls is not None:
        budget["max_calls"] = max_calls
        # Keep the reserve useful but valid for compact demo budgets.
        budget["synthesis_reserve_calls"] = max(1, min(3, max_calls - 1))
    if max_rounds is not None:
        budget["max_rounds"] = max_rounds
    if max_parallel is not None:
        budget["max_parallel"] = max_parallel
    if budget:
        data.setdefault("run", {})["budget"] = budget
    return data


def _event_printer(quiet: bool) -> Callable[[Any, Any], None] | None:
    if quiet:
        return None

    def callback(event: Any, state: Any) -> None:
        payload = event.payload
        if event.event_type == BOOTSTRAP_STARTED:
            console.print(phase_line("orient", "building the first complete solution"))
        elif event.event_type == BOOTSTRAP_COMPLETED:
            console.print(
                phase_line(
                    "baseline",
                    f"{len(state.open_issues)} active issue(s) · "
                    f"{len(state.pending_action_ids)} proposed action(s)",
                    state="done",
                )
            )
        elif event.event_type == ACTION_SELECTED:
            selected = list(payload.get("selected", {}))
            console.print(
                phase_line("focus", f"{len(selected)} decision-relevant action(s) selected")
            )
        elif event.event_type == ACTION_STARTED:
            action = state.actions.get(event.action_id)
            target = action.spec.target if action else event.action_id
            console.print(phase_line("probe", str(target), indent=1))
        elif event.event_type == ACTION_COMPLETED:
            console.print(
                phase_line("evidence", f"{event.action_id} retained", state="done", indent=1)
            )
        elif event.event_type == ACTION_FAILED:
            console.print(
                phase_line(
                    "evidence",
                    f"{event.action_id} failed · residue retained",
                    state="warn",
                    indent=1,
                )
            )
        elif event.event_type == CHECKPOINT_STARTED:
            console.print(phase_line("integrate", "reducing evidence into the current artifact"))
        elif event.event_type == CHECKPOINT_COMPLETED:
            console.print(
                phase_line(
                    "checkpoint",
                    f"round {state.round_index} · {len(state.open_issues)} open issue(s)",
                    state="done",
                )
            )
        elif event.event_type == CHECKPOINT_FAILED:
            console.print(
                phase_line(
                    "checkpoint",
                    "integration failed · current artifact remains recoverable",
                    state="warn",
                )
            )
        elif event.event_type == FINALIZATION_STARTED:
            console.print(phase_line("crystallize", "rebuilding one coherent deliverable"))
        elif event.event_type == FINAL_SYNTHESIZED:
            console.print(phase_line("artifact", "final synthesis captured", state="done"))
        elif event.event_type == RELEASE_COMPLETED:
            release = state.release
            if release and release.requires_repair:
                console.print(phase_line("challenge", "material repair required", state="warn"))
            else:
                console.print(phase_line("challenge", "release case passed", state="done"))
        elif event.event_type == RELEASE_FAILED:
            console.print(
                phase_line("challenge", "failed · complete trace retained in state", state="warn")
            )
        elif event.event_type == REPAIR_COMPLETED:
            console.print(phase_line("repair", "bounded repair integrated", state="done"))
        elif event.event_type == REPAIR_LOOP_STOPPED:
            console.print(
                phase_line(
                    "repair",
                    str(payload.get("reason", "repair loop stopped")),
                    state="warn",
                )
            )
        elif event.event_type == RESOURCE_INITIALIZED:
            resource = payload.get("resource_state", {})
            console.print(
                phase_line(
                    "metabolism",
                    f"{resource.get('active_call_limit', '?')} active · "
                    f"{resource.get('hard_call_limit', '?')} hard ceiling",
                )
            )
        elif event.event_type == RESOURCE_DECIDED:
            decision = payload.get("decision", {})
            kind = str(decision.get("kind", "decision"))
            detail = "; ".join(decision.get("reasons", []))
            console.print(
                phase_line(
                    "metabolism",
                    f"{kind.replace('_', ' ')} · {detail or 'resource horizon updated'}",
                    state="warn" if kind == "extension_required" else "done",
                )
            )
        elif event.event_type == RUN_EXTENDED:
            console.print(
                phase_line("extend", "prior seal archived · fresh Lead checkpoint required")
            )
        elif event.event_type == TASK_SOURCE_AMENDED:
            console.print(phase_line("steer", "admitted · fresh integration required"))
        elif event.event_type == RUN_PAUSED:
            console.print(phase_line("paused", "waiting for operator", state="warn"))
        elif event.event_type == RUN_RESUMED:
            console.print(phase_line("resumed", "controller active", state="done"))
        elif event.event_type == RUN_STOPPED:
            console.print(phase_line("stopped", "durable state retained", state="warn"))
        elif event.event_type == RUN_COMPLETED:
            console.print(phase_line("sealed", str(payload.get("output_path")), state="done"))

    return callback


def _read_task(task: str | None, task_file: Path | None) -> str:
    if task and task_file:
        raise typer.BadParameter("Use either TASK or --task-file, not both")
    if task_file:
        return task_file.expanduser().read_text(encoding="utf-8")
    if task:
        return task
    if not sys.stdin.isatty():
        value = sys.stdin.read()
        if value.strip():
            return value
    return cast(str, typer.prompt("Task"))


def _kernel_overrides(
    *,
    adapter: str,
    run_root: Path | None,
    max_wall_seconds: float | None,
    max_model_turns: int | None,
    max_parallel: int | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"run": {"adapter": adapter}}
    if run_root is not None:
        data["run"]["run_root"] = str(run_root)
    kernel: dict[str, Any] = {}
    if max_wall_seconds is not None:
        kernel["max_wall_seconds"] = max_wall_seconds
    if max_model_turns is not None:
        kernel["max_model_turns"] = max_model_turns
    if max_parallel is not None:
        kernel["max_parallel"] = max_parallel
    if kernel:
        data["kernel"] = kernel
    return data


def _print_run_header(engine: FrontierEngine, *, mode: str, compact: bool = False) -> None:
    print_brand(console, compact=compact)
    table = key_value_table(title=mode)
    table.add_row("run", engine.state.run_id)
    table.add_row("adapter", engine.state.adapter)
    table.add_row("state", str(engine.run_dir))
    console.print(table)


def _print_new_activity(engine: Any, after_seq: int) -> int:
    latest = after_seq
    for item in engine.control.recent_activity(
        limit=engine.control.MAX_ACTIVITY_ROWS,
        after_seq=after_seq,
    ):
        latest = max(latest, item.seq)
        if item.kind == "ledger":
            continue
        state = cast(
            Literal["active", "done", "warn", "error"],
            item.state if item.state in {"active", "done", "warn", "error"} else "active",
        )
        console.print(
            phase_line(
                item.label,
                item.message,
                state=state,
                indent=1,
            )
        )
    return latest


async def _execute_with_activity(
    engine: FrontierEngine,
    output: Path | None,
    *,
    follow_activity: bool,
) -> Path:
    existing = engine.control.recent_activity(limit=engine.control.MAX_ACTIVITY_ROWS)
    latest = existing[-1].seq if existing else 0
    task = asyncio.create_task(engine.execute(output_path=output))
    while not task.done():
        if follow_activity:
            latest = _print_new_activity(engine, latest)
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
    if follow_activity:
        _print_new_activity(engine, latest)
    return await task


async def _execute_kernel_with_activity(
    engine: KernelEngine,
    output: Path | None,
    *,
    follow_activity: bool,
) -> tuple[Any, Path | None]:
    existing = engine.control.recent_activity(limit=engine.control.MAX_ACTIVITY_ROWS)
    latest = existing[-1].seq if existing else 0
    task = asyncio.create_task(engine.execute())
    while not task.done():
        if follow_activity:
            latest = _print_new_activity(engine, latest)
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
    if follow_activity:
        _print_new_activity(engine, latest)
    state = await task
    path = engine.materialize_current(output) if state.current_workspace is not None else None
    return state, path


def _run_kernel_engine(
    engine: KernelEngine,
    output: Path | None,
    *,
    quiet: bool,
) -> None:
    try:
        state, path = asyncio.run(
            _execute_kernel_with_activity(engine, output, follow_activity=not quiet)
        )
        if quiet:
            console.print(path or engine.run_dir)
        else:
            console.print(
                phase_line(
                    state.status.value,
                    str(path or state.terminal_reason or engine.run_dir),
                    state="done" if state.status.value == "satisfied" else "warn",
                )
            )
    except FrontierError as exc:
        error_console.print(phase_line("error", str(exc), state="error"))
        error_console.print(phase_line("retained", str(engine.run_dir), state="muted"))
        raise typer.Exit(1) from exc
    finally:
        engine.close()


def _create_and_run_kernel(
    task_text: str,
    *,
    adapter: str,
    workspace: Path | None,
    source: list[Path],
    output: Path | None,
    config_path: Path | None,
    run_root: Path | None,
    max_wall_seconds: float | None,
    max_model_turns: int | None,
    max_parallel: int | None,
    quiet: bool,
) -> None:
    config = load_config(
        config_path,
        overrides=_kernel_overrides(
            adapter=adapter,
            run_root=run_root,
            max_wall_seconds=max_wall_seconds,
            max_model_turns=max_model_turns,
            max_parallel=max_parallel,
        ),
    )
    if adapter == "software" and workspace is None:
        raise typer.BadParameter("--workspace is required for the software adapter")
    engine = KernelEngine.create(
        task_text,
        config=config,
        adapter_name=adapter,
        workspace=workspace,
        source_paths=source,
    )
    if not quiet:
        print_brand(console, compact=True)
        table = key_value_table(title="new run")
        table.add_row("run", engine.state.run_id)
        table.add_row("adapter", adapter)
        table.add_row("state", str(engine.run_dir))
        console.print(table)
    _run_kernel_engine(engine, output, quiet=quiet)


def _run_engine(
    engine: FrontierEngine,
    output: Path | None,
    *,
    follow_activity: bool,
) -> Path:
    try:
        return asyncio.run(_execute_with_activity(engine, output, follow_activity=follow_activity))
    except OperatorStop as exc:
        console.print(phase_line("stopped", str(exc), state="warn"))
        console.print(phase_line("retained", str(engine.run_dir), state="muted"))
        raise typer.Exit() from exc
    except FrontierError as exc:
        error_console.print(phase_line("error", str(exc), state="error"))
        error_console.print(phase_line("retained", str(engine.run_dir), state="muted"))
        raise typer.Exit(1) from exc
    finally:
        engine.close()


def _open_control(run_ref: str, *, run_root: Path | None = None) -> tuple[Path, RunControlPlane]:
    run_dir = FrontierEngine.resolve_run_dir(run_ref, run_root=run_root)
    manifest = json.loads((run_dir / FrontierEngine.MANIFEST_FILE).read_text(encoding="utf-8"))
    return run_dir, RunControlPlane(
        run_dir / FrontierEngine.CONTROL_FILE,
        str(manifest["run_id"]),
    )


def _is_kernel_run(run_dir: Path) -> bool:
    try:
        manifest = json.loads((run_dir / KernelEngine.MANIFEST_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return str(manifest.get("architecture") or "") == KernelEngine.ARCHITECTURE


def _resume_kernel(
    run_ref: str,
    *,
    run_root: Path | None,
    output: Path | None,
    quiet: bool,
) -> None:
    try:
        engine = KernelEngine.load(run_ref, run_root=run_root)
    except (FrontierError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if engine.state.status.value == "paused":
        engine.control.enqueue(CommandKind.RESUME)
    if not quiet:
        print_brand(console, compact=True)
        table = key_value_table(title="resume")
        table.add_row("run", engine.state.run_id)
        table.add_row("kernel", KernelEngine.ARCHITECTURE)
        table.add_row("state", engine.state.status.value)
        console.print(table)
    _run_kernel_engine(engine, output, quiet=quiet)


def _extend_engine(
    engine: FrontierEngine,
    *,
    additional_calls: int,
    additional_rounds: int | None,
    output: Path | None,
) -> Path:
    try:
        return asyncio.run(
            engine.extend(
                additional_calls=additional_calls,
                additional_rounds=additional_rounds,
                output_path=output,
            )
        )
    except FrontierError as exc:
        error_console.print(phase_line("error", str(exc), state="error"))
        error_console.print(phase_line("retained", str(engine.run_dir), state="muted"))
        raise typer.Exit(1) from exc
    finally:
        engine.close()


@app.command("init")
def init_config(
    path: Annotated[Path, typer.Argument(help="Configuration file to create.")] = Path(
        "flourite.toml"
    ),
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a documented starter configuration."""

    path = path.expanduser().resolve()
    if path.exists() and not force:
        raise typer.BadParameter(f"File already exists: {path}; pass --force to overwrite")
    atomic_write_text(path, EXAMPLE_CONFIG, mode=0o644)
    print_brand(console, compact=True)
    console.print(phase_line("config", str(path), state="done"))


@app.command()
def doctor(
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="TOML configuration file.")
    ] = None,
    fake: Annotated[
        bool, typer.Option("--fake", help="Check only the deterministic offline provider.")
    ] = False,
) -> None:
    """Check the isolated Codex transport and subscription authentication."""

    config = load_config(config_path, overrides=_overrides(fake=fake))
    provider = build_provider(config.provider)
    result = asyncio.run(provider.doctor())
    print_brand(console, compact=True)
    table = key_value_table(title="system")
    table.add_row("provider", result.provider)
    table.add_row("ready", "yes" if result.ok else "no")
    table.add_row("version", result.version or "unknown")
    table.add_row("auth", result.auth_mode or "unknown")
    console.print(table)
    for detail in result.details:
        console.print(phase_line("capability", detail, state="muted"))
    if not result.ok:
        console.print("Install OMP, run `codex login`, and choose **Sign in with ChatGPT**.")
        raise typer.Exit(1)


@app.command()
def run(
    task: Annotated[str | None, typer.Argument(help="Task text; omit to read stdin.")] = None,
    task_file: Annotated[
        Path | None, typer.Option("--task-file", help="Read the task from a UTF-8 file.")
    ] = None,
    adapter: Annotated[
        str,
        typer.Option(
            "--adapter", "-a", help="generic, research, formal, decision, creative, or software"
        ),
    ] = "generic",
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Source Git repository for the software adapter."),
    ] = None,
    source: Annotated[
        list[Path] | None,
        typer.Option("--source", "-s", help="Source file or directory; repeatable."),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Materialize the final artifact here.")
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="TOML configuration file.")
    ] = None,
    run_root: Annotated[
        Path | None, typer.Option("--run-root", help="Directory for durable runs.")
    ] = None,
    max_parallel: Annotated[int | None, typer.Option("--max-parallel", min=1)] = None,
    max_wall_seconds: Annotated[
        float | None,
        typer.Option("--max-wall-seconds", min=30, help="Optional hard wall-time envelope."),
    ] = None,
    max_model_turns: Annotated[
        int | None,
        typer.Option("--max-model-turns", min=1, help="Optional hard model-turn envelope."),
    ] = None,
    fake: Annotated[
        bool, typer.Option("--fake", help="Run the deterministic offline demo provider.")
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
) -> None:
    """Create and execute a phase-free intelligence run."""

    task_text = _read_task(task, task_file)
    if fake:
        config = load_config(
            config_path,
            overrides={
                **_kernel_overrides(
                    adapter=adapter,
                    run_root=run_root,
                    max_wall_seconds=max_wall_seconds,
                    max_model_turns=max_model_turns,
                    max_parallel=max_parallel,
                ),
                "provider": {"kind": "fake"},
            },
        )
        if adapter == "software" and workspace is None:
            raise typer.BadParameter("--workspace is required for the software adapter")
        engine = KernelEngine.create(
            task_text,
            config=config,
            adapter_name=adapter,
            workspace=workspace,
            source_paths=source or [],
        )
        if not quiet:
            print_brand(console, compact=True)
            table = key_value_table(title="offline run")
            table.add_row("run", engine.state.run_id)
            table.add_row("kernel", KernelEngine.ARCHITECTURE)
            table.add_row("state", str(engine.run_dir))
            console.print(table)
        _run_kernel_engine(engine, output, quiet=quiet)
        return
    _create_and_run_kernel(
        task_text,
        adapter=adapter,
        workspace=workspace,
        source=source or [],
        output=output,
        config_path=config_path,
        run_root=run_root,
        max_wall_seconds=max_wall_seconds,
        max_model_turns=max_model_turns,
        max_parallel=max_parallel,
        quiet=quiet,
    )


@app.command("legacy-run", hidden=True)
def legacy_run(
    task: Annotated[str | None, typer.Argument(help="Task text; omit to read stdin.")] = None,
    task_file: Annotated[
        Path | None, typer.Option("--task-file", help="Read the task from a UTF-8 file.")
    ] = None,
    adapter: Annotated[str, typer.Option("--adapter", "-a")] = "generic",
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
    source: Annotated[list[Path] | None, typer.Option("--source", "-s")] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    config_path: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
    fake: Annotated[bool, typer.Option("--fake")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
) -> None:
    """Run the retired controller for controlled compatibility comparisons."""

    task_text = _read_task(task, task_file)
    config = load_config(
        config_path,
        overrides=_overrides(fake=fake, adapter=adapter, run_root=run_root),
    )
    if adapter == "software" and workspace is None:
        raise typer.BadParameter("--workspace is required for the software adapter")
    engine = FrontierEngine.create(
        task_text,
        config=config,
        adapter_name=adapter,
        workspace=workspace,
        sources=source or [],
        on_event=_event_printer(quiet),
    )
    if not quiet:
        _print_run_header(engine, mode="legacy run")
    path = _run_engine(engine, output, follow_activity=not quiet)
    if quiet:
        console.print(path)


@app.command("kernel-run", hidden=True)
def kernel_run(
    task: Annotated[str | None, typer.Argument(help="Task text; omit to read stdin.")] = None,
    task_file: Annotated[
        Path | None, typer.Option("--task-file", help="Read the task from a UTF-8 file.")
    ] = None,
    adapter: Annotated[
        str,
        typer.Option(
            "--adapter", "-a", help="generic, research, formal, decision, creative, or software"
        ),
    ] = "generic",
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Source Git repository for software work."),
    ] = None,
    source: Annotated[
        list[Path] | None,
        typer.Option("--source", "-s", help="Explicit source file or directory; repeatable."),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Materialize the current best here.")
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="TOML configuration file.")
    ] = None,
    run_root: Annotated[
        Path | None, typer.Option("--run-root", help="Directory for durable runs.")
    ] = None,
    max_wall_seconds: Annotated[
        float | None, typer.Option("--max-wall-seconds", min=30)
    ] = None,
    max_model_turns: Annotated[
        int | None, typer.Option("--max-model-turns", min=1)
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
) -> None:
    """Compatibility alias for the phase-free intelligence kernel."""

    task_text = _read_task(task, task_file)
    _create_and_run_kernel(
        task_text,
        adapter=adapter,
        workspace=workspace,
        source=source or [],
        output=output,
        config_path=config_path,
        run_root=run_root,
        max_wall_seconds=max_wall_seconds,
        max_model_turns=max_model_turns,
        max_parallel=None,
        quiet=quiet,
    )


@app.command("kernel-resume", hidden=True)
def kernel_resume(
    run_ref: Annotated[str, typer.Argument(help="Run id or directory.")],
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Materialize the current best here.")
    ] = None,
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Resume a phase-free kernel run."""

    engine = KernelEngine.load(run_ref, run_root=run_root)
    try:
        state = asyncio.run(engine.execute())
        path = engine.materialize_current(output) if state.current_workspace is not None else None
        console.print(
            phase_line(
                state.status.value,
                str(path or state.terminal_reason or engine.run_dir),
                state="done" if state.status.value == "satisfied" else "warn",
            )
        )
    finally:
        engine.close()


@app.command("kernel-status", hidden=True)
def kernel_status(
    run_ref: Annotated[str, typer.Argument(help="Run id or directory.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Inspect a phase-free kernel run."""

    engine = KernelEngine.load(run_ref, run_root=run_root)
    try:
        _print_kernel_status(engine, as_json=False)
    finally:
        engine.close()


def _print_kernel_status(engine: KernelEngine, *, as_json: bool) -> None:
    state = engine.state
    if as_json:
        console.print_json(json.dumps(state.model_dump(mode="json")))
        return
    table = key_value_table(title=state.run_id)
    runtime = engine.control.runtime()
    controller = (
        runtime.status.value.replace("_", " ")
        if runtime.process_alive
        else state.status.value
        if state.status.terminal
        else "resting"
    )
    table.add_row("state", state.status.value)
    table.add_row("controller", f"{controller} · {runtime.detail or '—'}")
    table.add_row("workspace", state.current_workspace_id or "not yet established")
    table.add_row("trajectories", str(len(state.trajectories)))
    table.add_row("artifacts", str(len(state.artifacts)))
    table.add_row(
        "moves",
        f"{len(state.active_move_ids)} running · "
        f"{sum(item.status.value == 'proposed' for item in state.moves.values())} queued · "
        f"{len(state.moves)} total",
    )
    table.add_row("observations", str(len(state.observations)))
    table.add_row("model turns", str(state.usage.model_turns))
    table.add_row(
        "tokens",
        f"{state.usage.input_tokens:,} in · {state.usage.output_tokens:,} out",
    )
    if state.current_workspace is not None:
        table.add_row("current", state.current_workspace.summary)
    if state.finish_claim is not None:
        table.add_row(
            "finish claim",
            "verified" if state.status.value == "satisfied" else "awaiting direct challenge",
        )
    if state.terminal_reason:
        table.add_row("reason", state.terminal_reason)
    console.print(table)


@app.command()
def arena(
    task: Annotated[str | None, typer.Argument(help="Task text; omit to read stdin.")] = None,
    task_file: Annotated[
        Path | None, typer.Option("--task-file", help="Read the task from a UTF-8 file.")
    ] = None,
    adapter: Annotated[
        str,
        typer.Option(
            "--adapter", "-a", help="generic, research, formal, decision, creative, or software"
        ),
    ] = "generic",
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Source Git repository for the software adapter."),
    ] = None,
    source: Annotated[
        list[Path] | None,
        typer.Option("--source", "-s", help="Source file or directory; repeatable."),
    ] = None,
    judges: Annotated[int, typer.Option("--judges", min=1, max=12)] = 4,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="TOML configuration file.")
    ] = None,
    run_root: Annotated[
        Path | None,
        typer.Option("--run-root", help="Base directory for candidate runs and arena evidence."),
    ] = None,
    max_calls: Annotated[int | None, typer.Option("--max-calls", min=4)] = None,
    max_rounds: Annotated[int | None, typer.Option("--max-rounds", min=0)] = None,
    fake: Annotated[
        bool, typer.Option("--fake", help="Use deterministic offline candidates and judges.")
    ] = False,
) -> None:
    """Blindly compare v3.5 adaptive control with the matched-budget sparse control."""

    task_text = _read_task(task, task_file)
    config = load_config(
        config_path,
        overrides=_overrides(
            fake=fake,
            adapter=adapter,
            run_root=run_root,
            max_calls=max_calls,
            max_rounds=max_rounds,
        ),
    )
    if adapter == "software" and workspace is None:
        raise typer.BadParameter("--workspace is required for the software adapter")
    print_brand(console, compact=True)
    console.print(phase_line("arena", f"{judges} blinded judge(s) · matched solver budgets"))
    try:
        result = asyncio.run(
            ArenaRunner(
                task=task_text,
                config=config,
                adapter_name=adapter,
                workspace=workspace,
                sources=source or [],
                judges=judges,
            ).run()
        )
    except FrontierError as exc:
        error_console.print(phase_line("error", str(exc), state="error"))
        raise typer.Exit(1) from exc
    console.print(phase_line("result", str(result.result_path), state="done"))


@app.command("evolution-check")
def evolution_check(
    candidate_path: Annotated[Path, typer.Argument(help="Pre-registered harness candidate JSON.")],
    trials_path: Annotated[
        Path, typer.Argument(help="Matched-budget shadow and held-out trial JSON.")
    ],
    as_json: Annotated[bool, typer.Option("--json", help="Print the decision as JSON.")] = False,
) -> None:
    """Apply the fail-closed held-out promotion gate without running new trials."""

    candidate = HarnessCandidate.model_validate_json(candidate_path.read_text(encoding="utf-8"))
    raw_trials = json.loads(trials_path.read_text(encoding="utf-8"))
    if isinstance(raw_trials, dict):
        raw_trials = raw_trials.get("trials", [])
    if not isinstance(raw_trials, list):
        raise typer.BadParameter("trials JSON must be a list or an object containing 'trials'")
    trials = [HarnessTrial.model_validate(item) for item in raw_trials]
    decision = HarnessPromotionGate().evaluate(candidate, trials)
    if as_json:
        console.print_json(json.dumps(decision.model_dump(mode="json")))
    else:
        print_brand(console, compact=True)
        console.print(
            phase_line(
                "promotion",
                "eligible" if decision.promotable else "withheld",
                state="done" if decision.promotable else "warn",
            )
        )
        console.print(
            phase_line(
                "shadow",
                " · ".join(f"{key} {value}" for key, value in decision.shadow_record.items()),
                state="muted",
            )
        )
        console.print(
            phase_line(
                "held out",
                " · ".join(f"{key} {value}" for key, value in decision.held_out_record.items()),
                state="muted",
            )
        )
        for reason in decision.reasons:
            console.print(phase_line("blocked", reason, state="warn"))
    if not decision.promotable:
        raise typer.Exit(2)


@app.command()
def resume(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[
        Path | None, typer.Option("--run-root", help="Run root when using an ID.")
    ] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
) -> None:
    """Resume an interrupted run from its immutable ledger."""

    try:
        run_dir, control = _open_control(run_ref, run_root=run_root)
    except (FrontierError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    try:
        runtime = control.runtime()
        if runtime.process_alive:
            if runtime.status == RuntimeStatus.PAUSED:
                command = control.enqueue(CommandKind.RESUME)
                print_brand(console, compact=True)
                console.print(phase_line("resume", f"queued · {command.command_id}", state="done"))
                return
            raise typer.BadParameter(
                "The controller is already active; use `flourite live` to observe or steer it"
            )
    finally:
        control.close()
    if _is_kernel_run(run_dir):
        _resume_kernel(
            run_ref,
            run_root=run_root,
            output=output,
            quiet=quiet,
        )
        return
    try:
        engine = FrontierEngine.load(
            run_ref,
            run_root=run_root,
            on_event=_event_printer(quiet),
        )
    except FrontierError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not quiet:
        _print_run_header(engine, mode="resume", compact=True)
    path = _run_engine(engine, output, follow_activity=not quiet)
    if quiet:
        console.print(path)


@app.command()
def steer(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    guidance: Annotated[str, typer.Argument(help="Direction to admit at the next safe boundary.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Steer a live or resumable run without replacing its original task."""

    try:
        run_dir, control = _open_control(run_ref, run_root=run_root)
        try:
            state_data = json.loads(
                (run_dir / FrontierEngine.STATE_FILE).read_text(encoding="utf-8")
            )
            state_label = str(state_data.get("status") or state_data.get("phase") or "created")
            if state_label in {
                "complete",
                "satisfied",
                "exhausted",
                "blocked",
                "stopped",
                "failed",
            }:
                raise FrontierError(
                    f"The run is {state_label}; create a run before steering it"
                )
            command = control.enqueue(CommandKind.STEER, text=guidance)
        finally:
            control.close()
    except (FrontierError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_brand(console, compact=True)
    console.print(
        phase_line(
            "steer",
            f"queued · {command.command_id} · applies at the next safe boundary",
            state="done",
        )
    )


@app.command()
def pause(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Pause a controller at its next safe boundary."""

    try:
        _, control = _open_control(run_ref, run_root=run_root)
        try:
            runtime = control.runtime()
            if not runtime.process_alive:
                raise FrontierError("No live controller is available to pause")
            if runtime.status == RuntimeStatus.PAUSED:
                raise FrontierError("The controller is already paused")
            command = control.enqueue(CommandKind.PAUSE)
        finally:
            control.close()
    except (FrontierError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_brand(console, compact=True)
    console.print(phase_line("pause", f"queued · {command.command_id}", state="done"))


@app.command()
def stop(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Stop at the next safe boundary while keeping the run resumable."""

    try:
        _, control = _open_control(run_ref, run_root=run_root)
        try:
            if not control.runtime().process_alive:
                raise FrontierError("No live controller is available to stop")
            command = control.enqueue(CommandKind.STOP)
        finally:
            control.close()
    except (FrontierError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_brand(console, compact=True)
    console.print(phase_line("stop", f"queued · {command.command_id}", state="warn"))


@app.command("live")
def live_view(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")] = "latest",
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Attach the live run dashboard; detach without stopping the controller."""

    try:
        dashboard = open_live_dashboard(run_ref, run_root=run_root, console=console)
    except (FrontierError, FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    try:
        dashboard.run()
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        dashboard.control.close()


@app.command()
def extend(
    run_ref: Annotated[str, typer.Argument(help="Completed run ID, path, or 'latest'.")],
    additional_calls: Annotated[
        int, typer.Option("--additional-calls", min=1, help="Add to the durable model-call budget.")
    ],
    additional_rounds: Annotated[
        int | None,
        typer.Option("--additional-rounds", min=1, help="Optional explicit round-budget increase."),
    ] = None,
    run_root: Annotated[
        Path | None, typer.Option("--run-root", help="Run root when using an ID.")
    ] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
) -> None:
    """Reopen a sealed run, retain its research, and perform fresh synthesis/release."""

    try:
        engine = FrontierEngine.load(
            run_ref,
            run_root=run_root,
            on_event=_event_printer(quiet),
        )
    except FrontierError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not quiet:
        _print_run_header(engine, mode="extend", compact=True)
    path = _extend_engine(
        engine,
        additional_calls=additional_calls,
        additional_rounds=additional_rounds,
        output=output,
    )
    if quiet:
        console.print(path)


@app.command()
def status(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show compact run state without making a model call."""

    run_dir = FrontierEngine.resolve_run_dir(run_ref, run_root=run_root)
    if _is_kernel_run(run_dir):
        kernel_engine = KernelEngine.load(run_dir)
        try:
            _print_kernel_status(kernel_engine, as_json=as_json)
        finally:
            kernel_engine.close()
        return
    engine = FrontierEngine.load(run_dir)
    try:
        state = engine.state
        if as_json:
            console.print_json(json.dumps(state.model_dump(mode="json")))
            return
        print_brand(console, compact=True)
        table = key_value_table(title=state.run_id)
        runtime = engine.control.runtime()
        controller_status: str
        if state.phase.value in {"complete", "failed"}:
            controller_status = state.phase.value
        elif runtime.process_alive:
            controller_status = runtime.status.value.replace("_", " ")
        elif runtime.status == RuntimeStatus.STOPPED:
            controller_status = "stopped"
        else:
            controller_status = "resting"
        table.add_row("phase", state.phase.value)
        table.add_row("controller", f"{controller_status} · {runtime.detail or '—'}")
        table.add_row("adapter", state.adapter)
        table.add_row("round", str(state.round_index))
        if state.resource_state and state.resource_state.mode == "adaptive":
            calls = (
                f"{state.usage.calls} used · {state.resource_state.active_call_limit} active · "
                f"{state.resource_state.hard_call_limit} hard"
            )
        else:
            calls = f"{state.usage.calls}/{engine.config.run.budget.max_calls}"
        table.add_row("calls", calls)
        if state.resource_state and state.resource_state.last_decision:
            decision = state.resource_state.last_decision
            table.add_row(
                "resource governor",
                f"{decision.kind.value.replace('_', ' ')} · "
                f"{decision.gradient_score} gradient signal(s)",
            )
        table.add_row("model requests", str(state.usage.model_requests))
        table.add_row(
            "tokens in/cache/out",
            f"{state.usage.input_tokens:,} / {state.usage.cached_input_tokens:,} / "
            f"{state.usage.output_tokens:,}",
        )
        table.add_row("open issues", str(len(state.open_issues)))
        table.add_row("open obligations", str(len(state.open_obligations)))
        table.add_row("active cruxes", str(len(state.active_cruxes)))
        table.add_row(
            "active overlays",
            str(
                sum(item.status.value in {"proposed", "active"} for item in state.overlays.values())
            ),
        )
        table.add_row("summit", "active" if state.summit_active else "dormant")
        table.add_row(
            "summit experiments",
            (
                f"{sum(item.attempts for item in state.discovery_records.values())} tried · "
                f"{sum(item.informative_results for item in state.discovery_records.values())} informative · "
                f"{sum(item.productive_results for item in state.discovery_records.values())} productive"
            ),
        )
        table.add_row("lead continuity", state.lead_session.status.value)
        table.add_row(
            "semantic CI",
            (
                "passed · fresh-release adjudicated"
                if state.runtime.verification.semantic_ci_passed
                and state.runtime.verification.adjudication
                else "passed"
                if state.runtime.verification.semantic_ci_passed
                else "pending/not passed"
            ),
        )
        table.add_row(
            "current artifact",
            state.current_artifact.artifact_id if state.current_artifact else "none",
        )
        table.add_row("output", state.runtime.completion.output_path or "not materialized")
        deliverables = state.runtime.completion.deliverable_paths
        if deliverables:
            table.add_row("deliverables", "\n".join(str(item) for item in deliverables))
        console.print(table)
        if state.open_issues:
            issues = data_table(title="active frontier")
            issues.add_column("Impact")
            issues.add_column("Issue")
            issues.add_column("Uncertainty")
            for issue in state.open_issues:
                issues.add_row(issue.impact.value, issue.title, issue.uncertainty.value)
            console.print(issues)
    finally:
        engine.close()


@app.command("inspect")
def inspect_run(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Inspect issues, actions, probes, and evidence without advancing the run."""

    run_dir = FrontierEngine.resolve_run_dir(run_ref, run_root=run_root)
    if _is_kernel_run(run_dir):
        kernel_engine = KernelEngine.load(run_dir)
        try:
            kernel_state = kernel_engine.state
            print_brand(console, compact=True)
            trajectories = data_table(title="trajectories")
            for name in ("ID", "Status", "Purpose", "Artifact head"):
                trajectories.add_column(name)
            for trajectory in kernel_state.trajectories.values():
                trajectories.add_row(
                    trajectory.trajectory_id,
                    trajectory.status.value,
                    trajectory.purpose,
                    trajectory.artifact_head_id or "—",
                )
            console.print(trajectories)

            moves = data_table(title="moves")
            for name in ("ID", "Mode", "Status", "Trajectory", "Intent"):
                moves.add_column(name)
            for move in kernel_state.moves.values():
                moves.add_row(
                    move.move_id,
                    move.mode.value,
                    move.status.value,
                    move.trajectory_id,
                    move.intent,
                )
            console.print(moves)

            observations = data_table(title="observations")
            for name in ("Kind", "Source", "Verdict", "Summary"):
                observations.add_column(name)
            for observation in kernel_state.observations.values():
                observations.add_row(
                    observation.kind.value,
                    observation.source,
                    observation.challenge_verdict.value
                    if observation.challenge_verdict
                    else "—",
                    observation.summary,
                )
            console.print(observations)
            console.print(
                phase_line(
                    "usage",
                    f"{kernel_state.usage.model_turns} turns · "
                    f"{kernel_state.usage.tool_calls} tools · "
                    f"{kernel_state.usage.input_tokens:,}/"
                    f"{kernel_state.usage.output_tokens:,} tokens",
                    state="muted",
                )
            )
        finally:
            kernel_engine.close()
        return
    legacy_engine = FrontierEngine.load(run_dir)
    try:
        state = legacy_engine.state
        print_brand(console, compact=True)
        issues = data_table(title="issue graph")
        for name in ("ID", "Status", "Impact", "Uncertainty", "Title"):
            issues.add_column(name)
        for issue in state.issues.values():
            issues.add_row(
                issue.issue_id,
                issue.status.value,
                issue.impact.value,
                issue.uncertainty.value,
                issue.title,
            )
        console.print(issues)

        actions = data_table(title="actions")
        for name in (
            "ID",
            "Round",
            "Status",
            "Kind",
            "Target",
            "Outcome",
            "Evidence",
            "Observed cost",
            "Integration",
        ):
            actions.add_column(name)
        for record in state.actions.values():
            receipt = record.receipt
            outcome = (
                f"{receipt.outcome_match}"
                + (
                    f"[{receipt.matched_outcome_index}]"
                    if receipt.matched_outcome_index is not None
                    else ""
                )
                if receipt
                else "—"
            )
            evidence = (
                ",".join(item.value for item in receipt.observed_evidence_channels)
                if receipt and receipt.observed_evidence_channels
                else "—"
            )
            cost = (
                f"{receipt.observed_cost.model_turns}m · "
                f"{receipt.observed_cost.tool_calls}t/"
                f"{receipt.observed_cost.tool_errors}e · "
                f"{receipt.observed_cost.wall_seconds:.1f}s"
                if receipt
                else "—"
            )
            actions.add_row(
                record.spec.action_id,
                str(record.spec.round_index),
                record.status.value,
                record.spec.kind.value,
                record.spec.target,
                outcome,
                evidence,
                cost,
                receipt.integration_status if receipt else "—",
            )
        console.print(actions)

        epochs = data_table(title="provider epochs")
        for name in (
            "Seq",
            "Epoch",
            "Actor",
            "Model turns",
            "Tools",
            "Tokens in/cache/out",
            "Wall",
            "Continuity",
        ):
            epochs.add_column(name)
        for event in legacy_engine.events():
            trace = event.payload.get("provider_trace_summary")
            usage = event.payload.get("usage")
            if not isinstance(trace, dict) or not trace or not isinstance(usage, dict):
                continue
            tools = trace.get("tool_calls", [])
            tool_count = len(tools) if isinstance(tools, list) else 0
            epochs.add_row(
                str(event.seq),
                event.event_type,
                event.actor,
                (
                    f"{int(trace.get('parent_model_turns', 0) or 0)}+"
                    f"{int(trace.get('nested_model_turns', 0) or 0)}"
                ),
                f"{tool_count}/{int(trace.get('tool_errors', 0) or 0)} err",
                (
                    f"{int(usage.get('input_tokens', 0) or 0):,}/"
                    f"{int(usage.get('cached_input_tokens', 0) or 0):,}/"
                    f"{int(usage.get('output_tokens', 0) or 0):,}"
                ),
                f"{float(usage.get('wall_seconds', 0.0) or 0.0):.1f}s",
                str(event.payload.get("continuity_mode", "—")),
            )
        if epochs.row_count:
            console.print(epochs)
        console.print(
            phase_line(
                "substrate",
                f"{len(state.evidence)} evidence · {len(state.candidate_deltas)} deltas · "
                f"{len(state.probes)} probes · {len(state.obligations)} obligations · "
                f"{len(state.cruxes)} cruxes · {len(state.overlays)} overlays · "
                f"{len(state.summit_lineages)} summit lineages · "
                f"{sum(item.attempts for item in state.discovery_records.values())} experiments",
                state="muted",
            )
        )
    finally:
        legacy_engine.close()


@app.command()
def verify(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Verify the ledger hash chain, completion seal, and every referenced blob."""

    run_dir = FrontierEngine.resolve_run_dir(run_ref, run_root=run_root)
    if _is_kernel_run(run_dir):
        kernel_engine = KernelEngine.load(run_dir)
        try:
            with kernel_engine.lock:
                count, tip = kernel_engine.verify()
            console.print_json(
                json.dumps(
                    {
                        "architecture": KernelEngine.ARCHITECTURE,
                        "events": count,
                        "ledger_tip": tip,
                        "state": "verified",
                        "blobs": "verified",
                    }
                )
            )
        finally:
            kernel_engine.close()
        return
    engine = FrontierEngine.load(run_dir)
    try:
        with engine.lock:
            engine._refresh_state_from_ledger()
            report = engine.verify_integrity()
        console.print_json(json.dumps(report))
    finally:
        engine.close()


@app.command("events")
def list_events(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Print the immutable event stream as JSON Lines."""

    run_dir = FrontierEngine.resolve_run_dir(run_ref, run_root=run_root)
    if _is_kernel_run(run_dir):
        kernel_engine = KernelEngine.load(run_dir)
        try:
            for event in kernel_engine.journal.events():
                console.print(json.dumps(event.model_dump(mode="json"), ensure_ascii=False))
        finally:
            kernel_engine.close()
        return
    engine = FrontierEngine.load(run_dir)
    try:
        for event in engine.events():
            console.print(json.dumps(event.model_dump(mode="json"), ensure_ascii=False))
    finally:
        engine.close()


@app.command("export")
def export_command(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    destination: Annotated[Path, typer.Option("--output", "-o")],
    mode: Annotated[
        str, typer.Option("--mode", help="diagnostic (redacted) or audit (lossless/sensitive)")
    ] = "diagnostic",
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Export a shareable diagnostic bundle or exact sensitive audit bundle."""

    if mode not in {"diagnostic", "audit"}:
        raise typer.BadParameter("--mode must be diagnostic or audit")
    run_dir = FrontierEngine.resolve_run_dir(run_ref, run_root=run_root)
    if _is_kernel_run(run_dir):
        kernel_engine = KernelEngine.load(run_dir)
        try:
            with kernel_engine.lock:
                path = export_kernel_run(kernel_engine, destination, mode=mode)  # type: ignore[arg-type]
            console.print(path)
        finally:
            kernel_engine.close()
        return
    engine = FrontierEngine.load(run_dir)
    try:
        with engine.lock:
            engine._refresh_state_from_ledger()
            path = export_run(engine, destination, mode=mode)  # type: ignore[arg-type]
        console.print(path)
    finally:
        engine.close()


@app.command("apply")
def apply_patch(
    run_ref: Annotated[str, typer.Argument(help="Completed software run ID or path.")],
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirm mutation of the source repository.")
    ] = False,
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Explicitly apply a final software patch after fingerprint verification."""

    if not yes and not typer.confirm("Apply the final patch to the original source repository?"):
        raise typer.Abort()
    run_dir = FrontierEngine.resolve_run_dir(run_ref, run_root=run_root)
    if _is_kernel_run(run_dir):
        kernel_engine = KernelEngine.load(run_dir)
        try:
            result = kernel_engine.apply_current_explicit()
            console.print_json(json.dumps(result))
        finally:
            kernel_engine.close()
        return
    engine = FrontierEngine.load(run_dir)
    try:
        result = engine.apply_final_patch()
        console.print_json(json.dumps(result))
    finally:
        engine.close()


@app.command()
def demo(
    run_root: Annotated[Path, typer.Option("--run-root")] = Path(".flourite/demo-runs"),
) -> None:
    """Run a deterministic offline end-to-end demonstration."""

    config = load_config(
        overrides={"provider": {"kind": "fake"}, "run": {"run_root": str(run_root)}}
    )
    engine = KernelEngine.create(
        "Demonstrate Flourite's canonical kernel, atomic move commit, and fresh completion challenge.",
        config=config,
        adapter_name="generic",
    )
    print_brand(console, compact=True)
    table = key_value_table(title="offline demo")
    table.add_row("run", engine.state.run_id)
    table.add_row("kernel", KernelEngine.ARCHITECTURE)
    table.add_row("state", str(engine.run_dir))
    console.print(table)
    _run_kernel_engine(engine, None, quiet=False)


if __name__ == "__main__":
    app()
