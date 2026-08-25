from __future__ import annotations

import asyncio
from pathlib import Path

from frontier_harness.arena import ArenaRunner


def test_blind_arena_matches_solver_budgets_and_balances_positions(
    tmp_path: Path, fake_config
) -> None:
    config = fake_config(
        run={
            "run_root": str(tmp_path / "runs"),
            "budget": {
                "max_rounds": 2,
                "max_calls": 8,
                "max_parallel": 2,
                "synthesis_reserve_calls": 3,
            },
        }
    )
    result = asyncio.run(
        ArenaRunner(
            task="Produce the strongest exact-task answer.",
            config=config,
            adapter_name="generic",
            judges=2,
        ).run()
    )
    assert result.result_path.is_file()
    assert result.adaptive_artifact.is_file()
    assert result.legacy_artifact.is_file()
    assert result.payload["matched_solver_budget"]["max_calls"] == 8
    assert result.payload["counts"] == {"adaptive": 1, "legacy": 1, "tie": 0}
    assert result.payload["aggregate_winner"] == "tie"
    assert result.payload["decisions"][0]["position"] == {
        "A": "adaptive",
        "B": "legacy",
    }
    assert result.payload["decisions"][1]["position"] == {
        "A": "legacy",
        "B": "adaptive",
    }
