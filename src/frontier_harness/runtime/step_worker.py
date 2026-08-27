"""Execute one durable activity with one immutable component lease."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .components import STEP_PROTOCOL
from .engine import KernelEngine


async def _step(run_dir: Path) -> dict[str, object]:
    engine = KernelEngine.load(run_dir)
    before = engine.state.last_event_seq
    try:
        state = await engine.execute(max_steps=1)
        return {
            "protocol": STEP_PROTOCOL,
            "before_seq": before,
            "after_seq": state.last_event_seq,
            "status": state.status.value,
        }
    finally:
        engine.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(_step(arguments.run_dir.resolve())), sort_keys=True))


if __name__ == "__main__":
    main()
