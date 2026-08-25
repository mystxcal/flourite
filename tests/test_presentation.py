from __future__ import annotations

from rich.console import Console

from frontier_harness.presentation import FLOURITE_THEME, brand, phase_line


def test_brand_has_crystal_lockup_and_plain_terminal_fallback() -> None:
    console = Console(
        theme=FLOURITE_THEME,
        record=True,
        force_terminal=False,
        color_system=None,
        width=80,
    )
    console.print(brand())
    rendered = console.export_text()

    assert "F L O U R I T E" in rendered
    assert "◇────◇" in rendered
    assert "FRONTIER-SCALE AGENT HARNESS" in rendered
    assert "\x1b[" not in rendered


def test_phase_line_keeps_label_and_message_semantically_visible() -> None:
    console = Console(record=True, force_terminal=False, color_system=None, width=44)
    console.print(phase_line("crystallize", "rebuilding one coherent deliverable"))
    rendered = console.export_text()

    assert "◇ CRYSTALLIZE  rebuilding one coherent" in rendered
    assert "               deliverable" in rendered
