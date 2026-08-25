"""Blind matched-budget comparison between adaptive v3.5 and the sparse control.

Arena is an optional evaluation utility. It never participates in a production
run and cannot alter either candidate. Solver budgets are identical; judge calls
are recorded separately. Position is alternated to reduce simple A/B bias.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import HarnessConfig
from .engine import FrontierEngine
from .ids import new_id
from .models import ArenaJudgeOutput, Role, SandboxPolicy
from .providers import ModelProvider, ProviderCallRequest, build_provider
from .util import atomic_write_text, canonical_json, utc_now


@dataclass(slots=True)
class ArenaRunResult:
    arena_dir: Path
    result_path: Path
    adaptive_artifact: Path
    legacy_artifact: Path
    payload: dict[str, Any]


def _provider(config: HarnessConfig) -> ModelProvider:
    return build_provider(config.provider)


def _candidate_config(base: HarnessConfig, *, adaptive: bool, run_root: Path) -> HarnessConfig:
    config = base.model_copy(deep=True)
    config.run.run_root = run_root
    if adaptive:
        config.cognition.mode = "adaptive"
    else:
        config.cognition.mode = "legacy"
        config.cognition.persistent_lead = False
        config.summit.mode = "off"
    return config


def _judge_prompt(task: str, artifact_a: Path, artifact_b: Path) -> str:
    return f"""You are performing a blind matched-budget comparison for one exact user task.

TASK
----
{task}

CANDIDATE A
-----------
Read: {artifact_a}

CANDIDATE B
-----------
Read: {artifact_b}

Judge the candidates only against the exact task. Compare correctness, task fidelity,
load-bearing reasoning, evidence quality, completeness, insight, robustness, coherence,
and practical usability. Do not reward length, process complexity, or stylistic polish
that does not improve the result. Identify fatal issues before preferences. Return the
minimal structured boundary requested by the response schema. Choose tie when the
material difference is not defensible.
"""


class ArenaRunner:
    def __init__(
        self,
        *,
        task: str,
        config: HarnessConfig,
        adapter_name: str,
        workspace: Path | None = None,
        sources: Sequence[Path] = (),
        judges: int = 4,
    ) -> None:
        if not task.strip():
            raise ValueError("arena task must not be empty")
        if judges < 1:
            raise ValueError("judges must be at least one")
        self.task = task
        self.config = config
        self.adapter_name = adapter_name
        self.workspace = workspace
        self.sources = list(sources)
        self.judges = judges

    async def run(self) -> ArenaRunResult:
        arena_id = new_id("arena")
        arena_root = self.config.run.run_root.expanduser().resolve().parent / "arenas"
        arena_dir = arena_root / arena_id
        arena_dir.mkdir(parents=True, exist_ok=False)

        adaptive_config = _candidate_config(
            self.config, adaptive=True, run_root=arena_dir / "adaptive-runs"
        )
        legacy_config = _candidate_config(
            self.config, adaptive=False, run_root=arena_dir / "legacy-runs"
        )
        adaptive_engine = FrontierEngine.create(
            self.task,
            config=adaptive_config,
            adapter_name=self.adapter_name,
            workspace=self.workspace,
            sources=self.sources,
            provider=_provider(adaptive_config),
        )
        legacy_engine = FrontierEngine.create(
            self.task,
            config=legacy_config,
            adapter_name=self.adapter_name,
            workspace=self.workspace,
            sources=self.sources,
            provider=_provider(legacy_config),
        )
        adaptive_run_dir = adaptive_engine.run_dir
        legacy_run_dir = legacy_engine.run_dir
        try:
            adaptive_artifact = await adaptive_engine.execute(
                output_path=arena_dir / "candidate-adaptive.bin"
            )
            legacy_artifact = await legacy_engine.execute(
                output_path=arena_dir / "candidate-legacy.bin"
            )
        finally:
            adaptive_engine.close()
            legacy_engine.close()

        judge_provider = _provider(self.config)
        decisions: list[dict[str, Any]] = []
        mapped_counts = {"adaptive": 0, "legacy": 0, "tie": 0}
        for index in range(self.judges):
            judge_dir = arena_dir / "judges" / f"judge-{index + 1:02d}"
            (judge_dir / "input").mkdir(parents=True, exist_ok=True)
            (judge_dir / "output").mkdir(parents=True, exist_ok=True)
            adaptive_is_a = index % 2 == 0
            source_a = adaptive_artifact if adaptive_is_a else legacy_artifact
            source_b = legacy_artifact if adaptive_is_a else adaptive_artifact
            artifact_a = judge_dir / "input" / "candidate-A"
            artifact_b = judge_dir / "input" / "candidate-B"
            artifact_a.write_bytes(source_a.read_bytes())
            artifact_b.write_bytes(source_b.read_bytes())
            request = ProviderCallRequest[ArenaJudgeOutput](
                call_id=new_id("judge"),
                call_kind="arena-judge",
                role=Role.STRONG,
                prompt=_judge_prompt(self.task, artifact_a, artifact_b),
                cwd=judge_dir,
                response_model=ArenaJudgeOutput,
                output_path=judge_dir / "output" / "boundary.json",
                schema_path=judge_dir / "output" / "schema.json",
                sandbox=SandboxPolicy.READ_ONLY,
                network_access=False,
                preserve_session=False,
                metadata={
                    "task": self.task,
                    "candidate_a": "adaptive" if adaptive_is_a else "legacy",
                    "candidate_b": "legacy" if adaptive_is_a else "adaptive",
                    "judge_index": index,
                },
            )
            try:
                result = await judge_provider.run(request)
                response = result.response
                if response.winner == "tie":
                    mapped_winner = "tie"
                elif response.winner == "A":
                    mapped_winner = "adaptive" if adaptive_is_a else "legacy"
                else:
                    mapped_winner = "legacy" if adaptive_is_a else "adaptive"
                mapped_counts[mapped_winner] += 1
                decisions.append(
                    {
                        "judge": index + 1,
                        "position": {
                            "A": "adaptive" if adaptive_is_a else "legacy",
                            "B": "legacy" if adaptive_is_a else "adaptive",
                        },
                        "blind_output": response.model_dump(mode="json"),
                        "mapped_winner": mapped_winner,
                        "usage": result.usage.model_dump(mode="json"),
                        "command": result.command,
                    }
                )
            except BaseException as exc:
                decisions.append(
                    {
                        "judge": index + 1,
                        "position": {
                            "A": "adaptive" if adaptive_is_a else "legacy",
                            "B": "legacy" if adaptive_is_a else "adaptive",
                        },
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        valid = sum(mapped_counts.values())
        if mapped_counts["adaptive"] > mapped_counts["legacy"]:
            aggregate = "adaptive"
        elif mapped_counts["legacy"] > mapped_counts["adaptive"]:
            aggregate = "legacy"
        else:
            aggregate = "tie"
        payload = {
            "arena_id": arena_id,
            "created_at": utc_now(),
            "task": self.task,
            "adapter": self.adapter_name,
            "matched_solver_budget": self.config.run.budget.model_dump(mode="json"),
            "adaptive_run_dir": str(adaptive_run_dir),
            "legacy_run_dir": str(legacy_run_dir),
            "adaptive_artifact": str(adaptive_artifact),
            "legacy_artifact": str(legacy_artifact),
            "judges_requested": self.judges,
            "valid_judges": valid,
            "counts": mapped_counts,
            "aggregate_winner": aggregate,
            "decisions": decisions,
        }
        result_path = arena_dir / "arena-result.json"
        atomic_write_text(result_path, json.dumps(payload, indent=2, ensure_ascii=False))
        atomic_write_text(
            arena_dir / "arena-summary.md",
            "\n".join(
                [
                    "# Blind matched-budget arena",
                    "",
                    f"- Aggregate winner: **{aggregate}**",
                    f"- Adaptive votes: {mapped_counts['adaptive']}",
                    f"- Legacy votes: {mapped_counts['legacy']}",
                    f"- Ties: {mapped_counts['tie']}",
                    f"- Valid judges: {valid}/{self.judges}",
                    "",
                    "The candidates were presented as A/B with alternating positions. Solver budgets were matched; judge calls were additional evaluation cost.",
                    "",
                ]
            ),
        )
        atomic_write_text(arena_dir / "arena-canonical.json", canonical_json(payload))
        return ArenaRunResult(
            arena_dir=arena_dir,
            result_path=result_path,
            adaptive_artifact=adaptive_artifact,
            legacy_artifact=legacy_artifact,
            payload=payload,
        )


def run_arena(**kwargs: Any) -> ArenaRunResult:
    return asyncio.run(ArenaRunner(**kwargs).run())
