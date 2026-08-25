"""Minimal Python API example for Flourite 0.6.0."""

from __future__ import annotations

import asyncio
from pathlib import Path

from frontier_harness.config import HarnessConfig
from frontier_harness.engine import FrontierEngine


async def main() -> None:
    config = HarnessConfig.model_validate(
        {
            "provider": {"kind": "fake"},  # omit this override for live OMP/Codex work
            "run": {
                "run_root": ".flourite/example-runs",
                "budget": {
                    "max_rounds": 2,
                    "max_calls": 8,
                    "max_parallel": 2,
                    "synthesis_reserve_calls": 3,
                },
            },
            "cognition": {"mode": "adaptive", "persistent_lead": True},
            "summit": {"mode": "auto"},
        }
    )
    engine = FrontierEngine.create(
        "Produce one exact-task, evidence-backed result.",
        config=config,
        adapter_name="generic",
    )
    try:
        output = await engine.execute(output_path=Path("output/example-result.md"))
        print(output)
        print(engine.verify_integrity())
    finally:
        engine.close()


if __name__ == "__main__":
    asyncio.run(main())
