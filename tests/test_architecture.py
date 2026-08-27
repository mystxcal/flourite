from __future__ import annotations

import ast
from pathlib import Path

import frontier_harness
from frontier_harness.runtime.engine import KernelEngine

SOURCE = Path(__file__).parents[1] / "src" / "frontier_harness"


def _imports(path: Path) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_public_api_has_one_engine() -> None:
    assert frontier_harness.KernelEngine is KernelEngine
    assert not hasattr(frontier_harness, "FrontierEngine")
    engine_classes = []
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        engine_classes.extend(
            (path, node.name)
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name.endswith("Engine")
        )
    assert engine_classes == [(SOURCE / "runtime" / "engine.py", "KernelEngine")]


def test_production_code_cannot_import_retired_controllers() -> None:
    retired = {"execution", "kernel", "orchestration", "scheduler", "state"}
    for path in SOURCE.rglob("*.py"):
        for imported in _imports(path):
            parts = imported.removeprefix("frontier_harness.").split(".")
            assert parts[0] not in retired, f"{path} imports retired module {imported}"


def test_cli_contains_no_hidden_parallel_runtime() -> None:
    source = (SOURCE / "cli.py").read_text(encoding="utf-8")
    assert "legacy-run" not in source
    assert "evolution-check" not in source


def test_kernel_journal_is_the_only_ledger_append_boundary() -> None:
    direct_writers = []
    for path in SOURCE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if ".ledger.append(" in source or "_ledger.append(" in source:
            direct_writers.append(path)
    assert direct_writers == [SOURCE / "core" / "journal.py"]
