"""Flourite's terminal design language.

The runtime deliberately knows nothing about this module.  It translates the
same event and state objects into a compact crystalline visual system while
keeping JSON, JSONL, quiet output, and redirected output machine-safe.
"""

# ruff: noqa: RUF001 - the crystal mark intentionally uses box-drawing diagonals.

from __future__ import annotations

from typing import Literal

from rich import box
from rich.console import Console, Group, RenderableType
from rich.style import Style
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

FLOURITE_THEME = Theme(
    {
        "flourite.ice": Style(color="#dff8ff"),
        "flourite.crystal": Style(color="#69e6ff", bold=True),
        "flourite.blue": Style(color="#5b9dff"),
        "flourite.violet": Style(color="#aa9cff"),
        "flourite.prism": Style(color="#e2bdff"),
        "flourite.line": Style(color="#315d89"),
        "flourite.muted": Style(color="#7890aa"),
        "flourite.dim": Style(color="#4d6278"),
        "flourite.warn": Style(color="#e9c979"),
        "flourite.error": Style(color="#ff7597", bold=True),
    }
)


def make_console(*, stderr: bool = False) -> Console:
    """Create a Flourite console while preserving Rich's NO_COLOR behavior."""

    return Console(stderr=stderr, theme=FLOURITE_THEME, highlight=False)


def _crystal_wordmark() -> Text:
    text = Text()
    colors = (
        "#dff8ff",
        "#aeeeff",
        "#72ddff",
        "#69bfff",
        "#8ea9ff",
        "#b29eff",
        "#d9b9ff",
        "#dff8ff",
    )
    for letter, color in zip("FLOURITE", colors, strict=True):
        text.append(letter, Style(color=color, bold=True))
        text.append(" ")
    text.rstrip()
    text.append("\n")
    text.append("FRONTIER-SCALE AGENT HARNESS", style="flourite.ice")
    text.append("\n")
    text.append("{ inspect  ·  test  ·  synthesize }", style="flourite.muted")
    return text


def brand(*, compact: bool = False) -> RenderableType:
    """Return the terminal mark derived from Flourite's faceted cube banner."""

    if compact:
        return Text.assemble(
            ("◇", "flourite.crystal"),
            ("  FLOURITE", "flourite.ice"),
            ("  /  frontier-scale agent harness", "flourite.muted"),
        )

    crystal = Text()
    crystal.append("     ◇────◇\n", style="flourite.crystal")
    crystal.append("    ╱│╲  ╱│\n", style="flourite.blue")
    crystal.append("   ◇─┼─◇──◇\n", style="flourite.crystal")
    crystal.append("   │ ◇──┼─◇\n", style="flourite.violet")
    crystal.append("   │╱  ╲│╱\n", style="flourite.blue")
    crystal.append("   ◇────◇", style="flourite.crystal")

    lockup = Table.grid(padding=(0, 3))
    lockup.add_row(crystal, _crystal_wordmark())
    route = Text.assemble(
        ("      ○", "flourite.line"),
        ("────────", "flourite.line"),
        ("◇", "flourite.crystal"),
        ("────────────", "flourite.line"),
        ("○", "flourite.blue"),
        ("    [ ]  { }  ⇢", "flourite.dim"),
    )
    return Group(lockup, route)


def phase_line(
    label: str,
    message: str,
    *,
    state: Literal["active", "done", "warn", "error", "muted"] = "active",
    indent: int = 0,
) -> Table:
    """Render one stable event line; no live animation or output rewriting."""

    symbol, style = {
        "active": ("◇", "flourite.crystal"),
        "done": ("◆", "flourite.blue"),
        "warn": ("△", "flourite.warn"),
        "error": ("×", "flourite.error"),
        "muted": ("○", "flourite.dim"),
    }[state]
    line = Table.grid(padding=(0, 1))
    line.add_column(no_wrap=True)
    line.add_column(width=12, no_wrap=True)
    line.add_column()
    line.add_row(
        Text(f"{'  ' * indent}{symbol}", style=style),
        Text(label.upper(), style=style),
        Text(message, style="flourite.muted" if state == "muted" else "flourite.ice"),
    )
    return line


def section_title(label: str) -> Text:
    """Create a compact graph-like section heading."""

    return Text.assemble(
        ("◇", "flourite.crystal"),
        ("──", "flourite.line"),
        (f" {label.upper()} ", "flourite.blue"),
        ("────────", "flourite.line"),
        ("○", "flourite.dim"),
    )


def data_table(*, title: str | None = None, show_header: bool = True) -> Table:
    """High-density table whose hierarchy survives monochrome terminals."""

    return Table(
        title=section_title(title) if title else None,
        title_justify="left",
        box=box.SIMPLE,
        show_edge=False,
        show_header=show_header,
        header_style="flourite.blue",
        border_style="flourite.line",
        row_styles=("", "flourite.ice"),
        padding=(0, 1),
        collapse_padding=True,
    )


def key_value_table(*, title: str | None = None) -> Table:
    table = data_table(title=title, show_header=False)
    table.add_column(style="flourite.muted", no_wrap=True)
    table.add_column(style="flourite.ice")
    return table


def print_brand(console: Console, *, compact: bool = False) -> None:
    console.print(brand(compact=compact or console.width < 64))
