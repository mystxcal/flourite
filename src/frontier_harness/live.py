"""Attachable live terminal for observing and steering a Flourite run."""

# ruff: noqa: RUF001 - the visual language intentionally uses multiplication marks.

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from bisect import bisect_right
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from .config import HarnessConfig
from .control import CommandKind, RunControlPlane, RuntimeSnapshot, RuntimeStatus
from .core.types import RunState as KernelRunState
from .presentation import brand, section_title
from .runtime.engine import KernelEngine


class KeyReader:
    """Small cross-platform non-blocking key reader with prompt suspension."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdin
        self._original: Any = None
        self._enabled = False

    def enable(self) -> None:
        if self._enabled or os.name == "nt":
            self._enabled = True
            return
        import termios
        import tty

        descriptor = self.stream.fileno()
        self._original = list(termios.tcgetattr(descriptor))
        tty.setcbreak(descriptor)
        self._enabled = True

    def restore(self) -> None:
        if not self._enabled:
            return
        if os.name != "nt" and self._original is not None:
            import termios

            termios.tcsetattr(self.stream.fileno(), termios.TCSADRAIN, self._original)
        self._enabled = False

    def read(self, timeout: float) -> str | None:
        if os.name == "nt":
            import msvcrt

            api: Any = msvcrt
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if api.kbhit():
                    value = str(api.getwch())
                    if value in {"\x00", "\xe0"}:
                        return {
                            "H": "up",
                            "P": "down",
                            "I": "pageup",
                            "Q": "pagedown",
                            "G": "home",
                            "O": "end",
                        }.get(str(api.getwch()))
                    return value.casefold()
                time.sleep(0.02)
            return None
        ready, _, _ = select.select([self.stream], [], [], timeout)
        if not ready:
            return None
        value = self.stream.read(1)
        if value != "\x1b":
            return value.casefold()
        sequence = [value]
        while len(sequence) < 8:
            continuation, _, _ = select.select([self.stream], [], [], 0.01)
            if not continuation:
                break
            sequence.append(self.stream.read(1))
        return {
            "\x1b[A": "up",
            "\x1b[B": "down",
            "\x1b[5~": "pageup",
            "\x1b[6~": "pagedown",
            "\x1b[H": "home",
            "\x1b[1~": "home",
            "\x1b[F": "end",
            "\x1b[4~": "end",
        }.get("".join(sequence))

    def __enter__(self) -> KeyReader:
        self.enable()
        return self

    def __exit__(self, *_: object) -> None:
        self.restore()


def _clock(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[-8:]
    return parsed.astimezone().strftime("%H:%M:%S")


def _elapsed(started_at: str | None) -> str:
    if not started_at:
        return "0s"
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        seconds = max(0, int((datetime.now(UTC) - started.astimezone(UTC)).total_seconds()))
    except ValueError:
        return "—"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _short(value: str, limit: int) -> str:
    clean = " ".join(value.split())
    return clean if len(clean) <= limit else clean[: max(1, limit - 1)] + "…"


class KernelLiveDashboard:
    """Full-screen, detachable view backed by durable run sidecars."""

    def __init__(
        self,
        *,
        run_dir: Path,
        control: RunControlPlane,
        console: Console,
    ) -> None:
        self.run_dir = run_dir
        self.control = control
        self.console = console
        self.config = HarnessConfig.model_validate_json(
            (run_dir / KernelEngine.CONFIG_FILE).read_text(encoding="utf-8")
        )
        self.flash = "attached · controls are durable"
        self.stop_armed_until = 0.0
        self.activity_cursor: int | None = None
        self.activity_page_size = 1

    def _state(self) -> KernelRunState:
        return KernelRunState.model_validate_json(
            (self.run_dir / KernelEngine.STATE_FILE).read_text(encoding="utf-8")
        )

    @staticmethod
    def _terminal_label(state: KernelRunState) -> str | None:
        return state.status.value if state.status.terminal else None

    @staticmethod
    def _effective_status(state: KernelRunState, runtime: RuntimeSnapshot) -> str:
        if state.status.terminal:
            return state.status.value
        if runtime.process_alive:
            return runtime.status.value
        if runtime.status == RuntimeStatus.FAILED:
            return "failed"
        if runtime.status == RuntimeStatus.STOPPED:
            return "stopped"
        if state.status.value == "paused":
            return "paused"
        return "resting"

    @staticmethod
    def _summary_row_count(state: KernelRunState) -> int:
        return 7 + int(state.finish_claim is not None) + int(bool(state.terminal_reason))

    def _summary(self, state: KernelRunState, runtime: RuntimeSnapshot) -> Table:
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(style="flourite.muted", no_wrap=True)
        table.add_column(style="flourite.ice")
        table.add_column(style="flourite.muted", no_wrap=True)
        table.add_column(style="flourite.ice", justify="right")
        proposed = sum(item.status.value == "proposed" for item in state.moves.values())
        envelope = state.objective.envelope
        model_turns = str(state.usage.model_turns)
        if envelope.max_model_turns is not None:
            model_turns += f"/{envelope.max_model_turns}"
        table.add_row(
            "state",
            state.status.value,
            "runtime",
            self._effective_status(state, runtime).replace("_", " "),
        )
        table.add_row("elapsed", _elapsed(runtime.started_at), "model turns", model_turns)
        table.add_row(
            "tokens",
            f"{state.usage.input_tokens:,} in · {state.usage.output_tokens:,} out",
            "tools",
            str(state.usage.tool_calls),
        )
        table.add_row(
            "moves",
            f"{len(state.active_move_ids)} running · {proposed} queued · {len(state.moves)} total",
            "observations",
            str(len(state.observations)),
        )
        table.add_row(
            "trajectories",
            f"{len(state.trajectories)} live/history",
            "artifacts",
            str(len(state.artifacts)),
        )
        table.add_row(
            "current",
            _short(
                state.current_workspace.summary
                if state.current_workspace is not None
                else "establishing the first live result",
                58,
            ),
            "workspace",
            state.current_workspace_id or "—",
        )
        table.add_row(
            "controller",
            _short(runtime.detail or "—", 46),
            "pid",
            str(runtime.pid or "—"),
        )
        if state.finish_claim is not None:
            table.add_row(
                "finish claim",
                _short(" · ".join(state.finish_claim.satisfaction_claims), 58),
                "challenge",
                "verified" if state.status.value == "satisfied" else "pending",
            )
        if state.terminal_reason:
            table.add_row("reason", _short(state.terminal_reason, 58), "", "")
        return table

    @staticmethod
    def _frontier(state: KernelRunState) -> Table:
        table = Table(
            box=box.SIMPLE,
            show_edge=False,
            expand=True,
            padding=(0, 1),
            header_style="flourite.blue",
            border_style="flourite.line",
        )
        table.add_column("state", width=10, no_wrap=True)
        table.add_column("live trajectory / next move")
        table.add_column("head", width=18, no_wrap=True)
        rows = 0
        for trajectory in state.trajectories.values():
            table.add_row(
                trajectory.status.value,
                _short(trajectory.purpose, 62),
                trajectory.artifact_head_id or "—",
            )
            rows += 1
        for move in state.moves.values():
            if move.status.value != "proposed":
                continue
            table.add_row("queued", _short(move.intent, 62), move.mode.value)
            rows += 1
        if not rows:
            table.add_row("—", "reconstructing the next decision", "—")
        return table

    def _activity_limit(self, state: KernelRunState) -> int:
        if self.console.width >= 110:
            return max(1, self.console.height - 6)
        frontier_rows = max(1, min(8, len(state.trajectories) + 2))
        command_rows = max(1, min(5, len(self.control.commands())))
        fixed_rows = self._summary_row_count(state) + frontier_rows + command_rows + 14
        return max(1, self.console.height - fixed_rows)

    def _activity_rows(self, limit: int) -> list[Any]:
        rows = self.control.recent_activity(limit=self.control.MAX_ACTIVITY_ROWS)
        self.activity_page_size = max(1, limit)
        if not rows:
            self.activity_cursor = None
            return []
        if self.activity_cursor is None:
            end = len(rows)
        else:
            end = bisect_right([item.seq for item in rows], self.activity_cursor)
            end = max(min(len(rows), self.activity_page_size), end)
            if end >= len(rows):
                self.activity_cursor = None
                end = len(rows)
        return rows[max(0, end - self.activity_page_size) : end]

    def _scroll_activity(self, steps: int) -> None:
        rows = self.control.recent_activity(limit=self.control.MAX_ACTIVITY_ROWS)
        if len(rows) <= self.activity_page_size:
            self.activity_cursor = None
            self.flash = "all recorded activity is visible"
            return
        sequences = [item.seq for item in rows]
        end = (
            len(rows)
            if self.activity_cursor is None
            else bisect_right(sequences, self.activity_cursor)
        )
        minimum = min(len(rows), self.activity_page_size)
        end = max(minimum, min(len(rows), end + steps))
        if end >= len(rows):
            self.activity_cursor = None
            self.flash = "following live activity"
            return
        self.activity_cursor = rows[end - 1].seq
        self.flash = "viewing activity history · End returns live"

    def _activity(self, limit: int) -> Table:
        table = Table(
            box=None,
            show_header=True,
            expand=True,
            padding=(0, 1),
            header_style="flourite.blue",
        )
        table.add_column("", width=1, no_wrap=True)
        table.add_column("time", width=8, no_wrap=True, style="flourite.dim")
        table.add_column("activity", width=16, no_wrap=True)
        table.add_column("detail")
        symbol = {
            "active": ("◇", "flourite.crystal"),
            "done": ("◆", "flourite.blue"),
            "warn": ("△", "flourite.warn"),
            "error": ("×", "flourite.error"),
        }
        rows = self._activity_rows(limit)
        for item in rows:
            mark, style = symbol.get(item.state, ("○", "flourite.dim"))
            table.add_row(
                Text(mark, style=style),
                _clock(item.timestamp),
                _short(item.label.replace(".", " "), 16),
                _short(item.message, max(30, self.console.width // 2)),
            )
        if not rows:
            table.add_row("○", "—", "waiting", "activity appears as the controller works")
        return table

    def _commands(self) -> Table:
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(width=8, style="flourite.dim", no_wrap=True)
        table.add_column(width=8, style="flourite.blue", no_wrap=True)
        table.add_column(width=10, no_wrap=True)
        table.add_column(style="flourite.ice")
        for item in self.control.commands()[-5:]:
            table.add_row(
                _clock(item.created_at),
                item.kind.value,
                item.status.value,
                _short(item.text or item.status_detail, 72),
            )
        if not self.control.commands():
            table.add_row("—", "—", "—", "no operator commands")
        return table

    def render(self) -> RenderableType:
        state = self._state()
        runtime = self.control.runtime()
        activity_limit = self._activity_limit(state)
        header = Table.grid(expand=True)
        header.add_column()
        header.add_column(justify="right")
        header.add_row(
            brand(compact=True),
            Text.assemble(
                (state.run_id, "flourite.ice"),
                ("  ·  ", "flourite.dim"),
                (self._effective_status(state, runtime).upper(), "flourite.crystal"),
            ),
        )
        status = Panel(
            self._summary(state, runtime),
            title=section_title("run state"),
            title_align="left",
            border_style=(
                "flourite.error" if runtime.status == RuntimeStatus.FAILED else "flourite.line"
            ),
            box=box.ROUNDED,
        )
        frontier = Panel(
            self._frontier(state),
            title=section_title("decision frontier"),
            title_align="left",
            border_style="flourite.line",
            box=box.ROUNDED,
        )
        activity = Panel(
            self._activity(activity_limit),
            title=section_title(
                "live activity"
                if self.activity_cursor is None
                else "activity history · End returns live"
            ),
            title_align="left",
            border_style="flourite.line",
            box=box.ROUNDED,
        )
        commands = Panel(
            self._commands(),
            title=section_title("operator queue"),
            title_align="left",
            border_style="flourite.line",
            box=box.ROUNDED,
        )
        controls = Text.assemble(
            (" s ", "black on #69e6ff"),
            (" steer   ", "flourite.muted"),
            (" p ", "black on #aa9cff"),
            (" pause/resume   ", "flourite.muted"),
            (" x ", "black on #e9c979"),
            (" stop   ", "flourite.muted"),
            (" r ", "black on #5b9dff"),
            (" restart   ", "flourite.muted"),
            (" ↑↓ ", "black on #69e6ff"),
            (" history   ", "flourite.muted"),
            (" q ", "black on #7890aa"),
            (" detach", "flourite.muted"),
        )
        footer = Group(
            controls,
            Text(_short(self.flash, max(20, self.console.width - 2)), style="flourite.ice"),
        )
        if self.console.width < 110:
            return Group(header, status, frontier, activity, commands, footer)
        columns = Table.grid(expand=True, padding=(0, 1))
        columns.add_column(ratio=2)
        columns.add_column(ratio=3)
        columns.add_row(Group(status, frontier, commands), activity)
        return Group(header, columns, footer)

    def _prompt_steer(self, live: Live, keys: KeyReader) -> None:
        state = self._state()
        terminal = self._terminal_label(state)
        if terminal is not None:
            self.flash = f"run is {terminal} · it cannot be steered"
            return
        live.stop()
        keys.restore()
        try:
            guidance = Prompt.ask(
                Text("Steer the run at its next safe boundary", style="flourite.crystal"),
                console=self.console,
            ).strip()
            if guidance:
                try:
                    command = self.control.enqueue(CommandKind.STEER, text=guidance)
                    self.flash = f"steer queued · {command.command_id}"
                except ValueError as exc:
                    self.flash = str(exc)
            else:
                self.flash = "empty steer ignored"
        finally:
            keys.enable()
            live.start(refresh=True)

    def _restart(self, state: KernelRunState, runtime: RuntimeSnapshot) -> None:
        terminal = self._terminal_label(state)
        if terminal is not None:
            self.flash = f"run is {terminal}"
            return
        if runtime.process_alive:
            if runtime.status == RuntimeStatus.PAUSED:
                try:
                    self.control.enqueue(CommandKind.RESUME)
                    self.flash = "resume queued"
                except ValueError as exc:
                    self.flash = str(exc)
            else:
                self.flash = "controller is already alive"
            return
        log_path = self.run_dir / "live-controller.log"
        with log_path.open("ab") as log:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "frontier_harness.cli",
                    "resume",
                    str(self.run_dir),
                    "--quiet",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.flash = "controller restart launched"

    def run(self) -> None:
        if not self.console.is_terminal or not sys.stdin.isatty():
            raise RuntimeError("`flourite live` requires an interactive terminal")
        with (
            KeyReader() as keys,
            Live(
                self.render(),
                console=self.console,
                screen=True,
                auto_refresh=False,
                transient=False,
            ) as live,
        ):
            while True:
                live.update(self.render(), refresh=True)
                key = keys.read(0.25)
                if key is None:
                    continue
                if self._handle_key(key, live=live, keys=keys):
                    return

    def _handle_key(self, key: str, *, live: Live, keys: KeyReader) -> bool:
        if key == "q":
            return True
        if key == "s":
            self._prompt_steer(live, keys)
            return False
        if self._handle_scroll(key):
            return False
        state = self._state()
        runtime = self.control.runtime()
        if key == "p":
            self._toggle_pause(state, runtime)
        elif key == "r":
            self._restart(state, runtime)
        elif key == "x":
            self._stop(state)
        return False

    def _handle_scroll(self, key: str) -> bool:
        distance = {
            "up": -1,
            "k": -1,
            "down": 1,
            "j": 1,
            "pageup": -self.activity_page_size,
            "pagedown": self.activity_page_size,
            "home": -self.control.MAX_ACTIVITY_ROWS,
        }.get(key)
        if distance is not None:
            self._scroll_activity(distance)
            return True
        if key == "end":
            self.activity_cursor = None
            self.flash = "following live activity"
            return True
        return False

    def _toggle_pause(self, state: KernelRunState, runtime: RuntimeSnapshot) -> None:
        terminal = self._terminal_label(state)
        if terminal is not None:
            self.flash = f"run is {terminal}"
        elif not runtime.process_alive:
            self.flash = "controller is resting · press r to restart"
        elif runtime.status == RuntimeStatus.PAUSED:
            self._enqueue(CommandKind.RESUME, "resume queued")
        else:
            self._enqueue(
                CommandKind.PAUSE,
                "pause queued · applies at the next safe boundary",
            )

    def _stop(self, state: KernelRunState) -> None:
        terminal = self._terminal_label(state)
        if terminal is not None:
            self.flash = f"run is {terminal}"
            return
        now = time.monotonic()
        if now > self.stop_armed_until:
            self.stop_armed_until = now + 3.0
            self.flash = "press x again within 3 seconds to stop"
            return
        self.stop_armed_until = 0.0
        self._enqueue(CommandKind.STOP, "stop queued · durable state will remain resumable")

    def _enqueue(self, kind: CommandKind, success: str) -> None:
        try:
            self.control.enqueue(kind)
            self.flash = success
        except ValueError as exc:
            self.flash = str(exc)


def open_live_dashboard(
    run_ref: str,
    *,
    run_root: Path | None,
    console: Console,
) -> KernelLiveDashboard:
    run_dir = KernelEngine.resolve_run_dir(run_ref, run_root=run_root)
    manifest = json.loads((run_dir / KernelEngine.MANIFEST_FILE).read_text(encoding="utf-8"))
    if manifest.get("architecture") != KernelEngine.ARCHITECTURE:
        raise ValueError("flourite live supports canonical kernel runs only")
    control = RunControlPlane(
        run_dir / KernelEngine.CONTROL_FILE,
        str(manifest["run_id"]),
    )
    return KernelLiveDashboard(run_dir=run_dir, control=control, console=console)
