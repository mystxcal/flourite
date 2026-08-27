"""Minimal Python API example for Flourite."""

from __future__ import annotations

import asyncio
from pathlib import Path

from frontier_harness.config import HarnessConfig
from frontier_harness.runtime.engine import KernelEngine


async def main() -> None:
    config = HarnessConfig.model_validate(
        {
            "provider": {"kind": "fake"},
            "run": {"run_root": ".flourite/example-runs"},
        }
    )
    engine = KernelEngine.create(
        "Produce one exact, evidence-backed result.",
        config=config,
        adapter_name="generic",
    )
    try:
        await engine.execute()
        output = engine.materialize_current(Path("output/example-result.md"))
        print(output)
        print(engine.verify())
    finally:
        engine.close()


if __name__ == "__main__":
    asyncio.run(main())
