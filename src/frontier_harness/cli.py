"""The canonical Flourite command-line interface."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import typer

from . import __version__
from .config import EXAMPLE_CONFIG, load_config
from .control import CommandKind, RunControlPlane, RuntimeStatus
from .errors import FrontierError
from .exporter import export_kernel_run
from .presentation import data_table, key_value_table, make_console, phase_line, print_brand
from .providers import build_provider
from .runtime.components import ComponentRegistry
from .runtime.engine import KernelEngine
from .runtime.supervisor import StepSupervisor
from .util import atomic_write_text

app = typer.Typer(
    name="flourite",
    help="A frontier-scale agent harness for exact, high-ceiling problem solving.",
    no_args_is_help=True,
    rich_markup_mode="markdown",
)
component_app = typer.Typer(
    name="component",
    help="Inspect or replace a run's implementation at the next activity boundary.",
    no_args_is_help=True,
)
app.add_typer(component_app, name="component")
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
    run_root: Path | None,
    max_wall_seconds: float | None,
    max_model_turns: int | None,
    max_parallel: int | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if run_root is not None:
        data["run"] = {"run_root": str(run_root)}
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


def _print_new_activity(engine: KernelEngine, after_seq: int) -> int:
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
        console.print(phase_line(item.label, item.message, state=state, indent=1))
    return latest


async def _execute_with_activity(
    engine: KernelEngine,
    output: Path | None,
    *,
    follow: bool,
) -> tuple[Any, Path | None]:
    ComponentRegistry(engine.run_dir).initialize()
    supervisor = StepSupervisor(engine.run_dir)
    existing = engine.control.recent_activity(limit=engine.control.MAX_ACTIVITY_ROWS)
    latest = existing[-1].seq if existing else 0
    try:
        task = asyncio.create_task(supervisor.execute())
        while not task.done():
            if follow:
                latest = _print_new_activity(engine, latest)
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
        if follow:
            _print_new_activity(engine, latest)
        await task
        state = engine.journal.refresh()
        path = engine.materialize_current(output) if state.current_workspace is not None else None
        return state, path
    finally:
        supervisor.close()


def _run_engine(engine: KernelEngine, output: Path | None, *, quiet: bool) -> None:
    try:
        state, path = asyncio.run(_execute_with_activity(engine, output, follow=not quiet))
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
        if state.status.value == "failed":
            raise typer.Exit(1)
        if state.status.value == "paused" and (state.terminal_reason or "").startswith(
            "execution paused"
        ):
            raise typer.Exit(75)
    except FrontierError as exc:
        error_console.print(phase_line("error", str(exc), state="error"))
        error_console.print(phase_line("retained", str(engine.run_dir), state="muted"))
        raise typer.Exit(1) from exc
    finally:
        engine.close()


def _create_engine(
    task: str,
    *,
    adapter: str,
    workspace: Path | None,
    source: list[Path],
    config_path: Path | None,
    run_root: Path | None,
    max_wall_seconds: float | None,
    max_model_turns: int | None,
    max_parallel: int | None,
    fake: bool,
) -> KernelEngine:
    overrides = _kernel_overrides(
        run_root=run_root,
        max_wall_seconds=max_wall_seconds,
        max_model_turns=max_model_turns,
        max_parallel=max_parallel,
    )
    if fake:
        overrides["provider"] = {"kind": "fake"}
    config = load_config(config_path, overrides=overrides)
    if adapter == "software" and workspace is None:
        raise typer.BadParameter("--workspace is required for the software adapter")
    return KernelEngine.create(
        task,
        config=config,
        adapter_name=adapter,
        workspace=workspace,
        source_paths=source,
    )


def _open_control(
    run_ref: str,
    *,
    run_root: Path | None = None,
) -> tuple[Path, RunControlPlane]:
    run_dir = KernelEngine.resolve_run_dir(run_ref, run_root=run_root)
    try:
        manifest = json.loads((run_dir / KernelEngine.MANIFEST_FILE).read_text(encoding="utf-8"))
        run_id = str(manifest["run_id"])
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        raise FrontierError(f"incomplete Flourite run: {run_dir}") from exc
    return run_dir, RunControlPlane(run_dir / KernelEngine.CONTROL_FILE, run_id)


def _terminal_state(run_dir: Path) -> str | None:
    try:
        state = json.loads((run_dir / KernelEngine.STATE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise FrontierError(f"invalid Flourite state: {run_dir}") from exc
    label = str(state.get("status") or state.get("phase") or "created")
    return (
        label
        if label
        in {
            "complete",
            "satisfied",
            "exhausted",
            "blocked",
            "stopped",
            "failed",
        }
        else None
    )


def _load_engine(run_ref: str | Path, *, run_root: Path | None = None) -> KernelEngine:
    try:
        return KernelEngine.load(run_ref, run_root=run_root)
    except (FrontierError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc


def _component_registry(run_ref: str, run_root: Path | None) -> ComponentRegistry:
    try:
        run_dir = KernelEngine.resolve_run_dir(run_ref, run_root=run_root)
    except FrontierError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return ComponentRegistry(run_dir)


@component_app.command("status")
def component_status(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the implementation generation that the next activity will lease."""

    registry = _component_registry(run_ref, run_root)
    if not registry.path.exists():
        raise typer.BadParameter("This run predates live components; bind one to enable them")
    active = registry.active()
    payload = {
        "protocol": "flourite-step/v1",
        "generation": active.generation,
        "digest": active.digest,
        "activated_at": active.activated_at,
        "generations": len(registry.history()),
    }
    if as_json:
        console.print_json(json.dumps(payload))
        return
    print_brand(console, compact=True)
    table = key_value_table(title="live component")
    table.add_row("generation", str(active.generation))
    table.add_row("digest", active.digest[:16])
    table.add_row("protocol", str(payload["protocol"]))
    table.add_row("history", str(payload["generations"]))
    console.print(table)


@component_app.command("bind")
def component_bind(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    source: Annotated[
        Path,
        typer.Argument(help="Package, source root, or repository containing frontier_harness."),
    ] = Path("."),
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Atomically select new code for the next activity; the run stays alive."""

    registry = _component_registry(run_ref, run_root)
    try:
        binding = registry.bind(source)
    except FrontierError as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_brand(console, compact=True)
    console.print(
        phase_line(
            "component",
            f"generation {binding.generation} · {binding.digest[:16]} · next boundary",
            state="done",
        )
    )


@component_app.command("rollback")
def component_rollback(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Point the next activity back to the preceding distinct implementation."""

    registry = _component_registry(run_ref, run_root)
    try:
        binding = registry.rollback_if_current(registry.active())
    except FrontierError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if binding is None:
        raise typer.BadParameter("No earlier distinct component is available")
    print_brand(console, compact=True)
    console.print(
        phase_line(
            "component",
            f"rolled back as generation {binding.generation} · {binding.digest[:16]}",
            state="warn",
        )
    )


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

    overrides = {"provider": {"kind": "fake"}} if fake else None
    provider = build_provider(load_config(config_path, overrides=overrides).provider)
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

    engine = _create_engine(
        _read_task(task, task_file),
        adapter=adapter,
        workspace=workspace,
        source=source or [],
        config_path=config_path,
        run_root=run_root,
        max_wall_seconds=max_wall_seconds,
        max_model_turns=max_model_turns,
        max_parallel=max_parallel,
        fake=fake,
    )
    if not quiet:
        print_brand(console, compact=True)
        table = key_value_table(title="offline run" if fake else "new run")
        table.add_row("run", engine.state.run_id)
        table.add_row("adapter", adapter)
        table.add_row("state", str(engine.run_dir))
        console.print(table)
    _run_engine(engine, output, quiet=quiet)


@app.command()
def resume(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[
        Path | None, typer.Option("--run-root", help="Run root when using an ID.")
    ] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
) -> None:
    """Resume an interrupted canonical run from its immutable ledger."""

    run_dir, control = _open_control(run_ref, run_root=run_root)
    try:
        runtime = control.runtime()
        if runtime.process_alive:
            if runtime.status == RuntimeStatus.PAUSED:
                command = control.enqueue(CommandKind.RESUME)
                console.print(phase_line("resume", f"queued · {command.command_id}", state="done"))
                return
            raise typer.BadParameter(
                "The controller is already active; use `flourite live` to observe or steer it"
            )
    finally:
        control.close()
    engine = _load_engine(run_dir)
    if engine.state.status.value == "paused":
        engine.control.enqueue(CommandKind.RESUME)
    if not quiet:
        print_brand(console, compact=True)
        table = key_value_table(title="resume")
        table.add_row("run", engine.state.run_id)
        table.add_row("state", engine.state.status.value)
        console.print(table)
    _run_engine(engine, output, quiet=quiet)


def _queue_control(
    run_ref: str,
    kind: CommandKind,
    *,
    run_root: Path | None,
    text: str = "",
    require_live: bool,
) -> str:
    run_dir, control = _open_control(run_ref, run_root=run_root)
    try:
        terminal = _terminal_state(run_dir)
        if terminal is not None:
            raise FrontierError(f"The run is {terminal}; it cannot accept {kind.value}")
        runtime = control.runtime()
        if require_live and not runtime.process_alive:
            raise FrontierError(f"No live controller is available to {kind.value}")
        if kind == CommandKind.PAUSE and runtime.status == RuntimeStatus.PAUSED:
            raise FrontierError("The controller is already paused")
        return control.enqueue(kind, text=text).command_id
    finally:
        control.close()


@app.command()
def steer(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    guidance: Annotated[str, typer.Argument(help="Direction to admit at the next safe boundary.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Steer a live or resumable run without replacing its original task."""

    try:
        command_id = _queue_control(
            run_ref,
            CommandKind.STEER,
            run_root=run_root,
            text=guidance,
            require_live=False,
        )
    except (FrontierError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_brand(console, compact=True)
    console.print(
        phase_line(
            "steer",
            f"queued · {command_id} · applies at the next safe boundary",
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
        command_id = _queue_control(
            run_ref,
            CommandKind.PAUSE,
            run_root=run_root,
            require_live=True,
        )
    except (FrontierError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_brand(console, compact=True)
    console.print(phase_line("pause", f"queued · {command_id}", state="done"))


@app.command()
def stop(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Stop at the next safe boundary while retaining durable state."""

    try:
        command_id = _queue_control(
            run_ref,
            CommandKind.STOP,
            run_root=run_root,
            require_live=True,
        )
    except (FrontierError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    print_brand(console, compact=True)
    console.print(phase_line("stop", f"queued · {command_id}", state="warn"))


@app.command("live")
def live_view(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")] = "latest",
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Attach the live dashboard; detach without stopping the controller."""

    from .live import open_live_dashboard

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
def status(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show compact canonical run state without making a model call."""

    engine = _load_engine(run_ref, run_root=run_root)
    try:
        state = engine.state
        if as_json:
            console.print_json(json.dumps(state.model_dump(mode="json")))
            return
        print_brand(console, compact=True)
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
    finally:
        engine.close()


@app.command("inspect")
def inspect_run(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Inspect trajectories, moves, observations, and usage."""

    engine = _load_engine(run_ref, run_root=run_root)
    try:
        state = engine.state
        print_brand(console, compact=True)
        trajectories = data_table(title="trajectories")
        for name in ("ID", "Status", "Purpose", "Artifact head"):
            trajectories.add_column(name)
        for trajectory in state.trajectories.values():
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
        for move in state.moves.values():
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
        for observation in state.observations.values():
            observations.add_row(
                observation.kind.value,
                observation.source,
                observation.challenge_verdict.value if observation.challenge_verdict else "—",
                observation.summary,
            )
        console.print(observations)
        console.print(
            phase_line(
                "usage",
                f"{state.usage.model_turns} turns · {state.usage.tool_calls} tools · "
                f"{state.usage.input_tokens:,}/{state.usage.output_tokens:,} tokens",
                state="muted",
            )
        )
    finally:
        engine.close()


@app.command()
def verify(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Verify the ledger hash chain and every referenced blob."""

    engine = _load_engine(run_ref, run_root=run_root)
    try:
        with engine.lock:
            count, tip = engine.verify()
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
        engine.close()


@app.command("events")
def list_events(
    run_ref: Annotated[str, typer.Argument(help="Run ID, path, or 'latest'.")],
    run_root: Annotated[Path | None, typer.Option("--run-root")] = None,
) -> None:
    """Print the immutable event stream as JSON Lines."""

    engine = _load_engine(run_ref, run_root=run_root)
    try:
        for event in engine.journal.events():
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
    """Export a shareable diagnostic or exact sensitive audit bundle."""

    if mode not in {"diagnostic", "audit"}:
        raise typer.BadParameter("--mode must be diagnostic or audit")
    engine = _load_engine(run_ref, run_root=run_root)
    try:
        with engine.lock:
            path = export_kernel_run(engine, destination, mode=mode)  # type: ignore[arg-type]
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
    """Explicitly apply a final software result after fingerprint verification."""

    if not yes and not typer.confirm("Apply the final patch to the original source repository?"):
        raise typer.Abort()
    engine = _load_engine(run_ref, run_root=run_root)
    try:
        console.print_json(json.dumps(engine.apply_current_explicit()))
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
    _run_engine(engine, None, quiet=False)


if __name__ == "__main__":
    app()
