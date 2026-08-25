# Autoresearch in Flourite

Flourite keeps one authoritative artifact, but it no longer assumes that one
incumbent search path is enough for every difficult task.

When Summit is activated by a concrete ceiling risk, an **experimental
frontier** maintains a small number of causally distinct lineages underneath
the authoritative artifact. In software runs, each lineage can carry a full
isolated candidate state. A worker developing that lineage starts from its
actual candidate state; crossover workers receive the sibling candidate
artifacts rather than reconstructing them from summaries.

This is intentionally smaller than an always-on population or agent society.
The frontier expands only when the exact task earns it.

## Search operators

- **Develop** resolves one load-bearing dependency and must make a state change
  or return a scoped negative result.
- **Falsify** runs the cheapest credible discriminator against the closest
  rival and protects the run from a false champion.
- **Mutate** triggers from actual stagnation or falsification residue. It must
  change a causal assumption, mechanism, prediction, or boundary behavior—not
  merely rename the parent.
- **Crossover** composes supported, complementary mechanisms from different
  niches. It must name the interface and incompatibilities and must beat the
  best parent, not merely one parent.
- **Revive** may extract one valid residual mechanism from a dead high-potential
  lineage. It never retries the falsified thesis.

The deterministic controller performs local Pareto filtering over potential,
information value, diversity, observed productivity, and cost. It rotates
among underused non-dominated operators instead of collapsing these signals
into a tuned universal score.

## Runtime-owned gradient

Workers cannot award themselves novelty, productivity, coverage, or success.
The event reducer derives a `DiscoveryRecord` from:

- action receipts and actual state transitions;
- explicit Lead acceptance at an integration checkpoint;
- independently confirmed evidence channels;
- covered cruxes and obligations;
- informative negative results;
- consecutive non-informative attempts;
- optional domain-objective measurements.

Model-authored lineage changes are informative proposals, not self-awarded
productivity. A result becomes productive only when a configured external
objective improves or the Lead explicitly integrates the completed action.

Parallel results update the latest ledger-derived record, not a stale round
snapshot, so concurrent observations cannot silently overwrite one another.

## Domain objectives

The software adapter can run a user-owned evaluator after a candidate change:

```toml
[software.objective]
command = "python evaluate.py --json"
primary_metric = "score"
direction = "maximize"
timeout_seconds = 900
```

The final non-empty stdout line must be either:

```json
{"score": 12.5}
```

or:

```json
{
  "metrics": {"score": 12.5, "latency": 3.1},
  "valid": true,
  "constraint_violations": [],
  "detail": "optional note"
}
```

Before the first candidate in a lineage is evaluated, Flourite measures its
parent in a separate clean worktree. Candidate progress is therefore
parent-relative. A first score is not automatically counted as an improvement.
For crossover, a candidate must exceed the better recorded parent on the
configured primary objective.

Evaluator output, timing, exit status, constraints, and raw logs are stored as
runtime evidence. Evaluator failure does not become a model failure or a fake
negative result.

## Slow harness evolution

Flourite may dogfood the software adapter to propose changes to its own harness,
but such a patch is only a candidate. Promotion is deliberately outside the
inner task loop.

Every harness candidate must pre-register:

- the exact observed failure mode;
- the causal code or policy change;
- a falsifiable predicted effect and its scope;
- protected behavior that must not regress;
- source traces that motivated the change;
- sealed held-out case digests.

After matched-budget shadow and held-out trials, run:

```sh
flourite evolution-check candidate.json trials.json
```

Promotion is withheld if trials are invalid, budgets differ, raw traces are
missing, protected behavior regresses, shadow and held-out cases overlap, the
pre-registered prediction never appears, the held-out record is losing, or no
held-out win exists. Training-task improvement alone can never promote a
harness change.

## Evidence boundary

These mechanisms make Flourite capable of running a disciplined autoresearch
loop. They do not by themselves establish superiority over GEAR, CORAL, MDA,
Hyra, Meta-Harness, or direct matched-budget model search.

That claim requires a frozen release candidate and a final evaluation with the
same model, task inputs, total solver budget, evaluator calls, and wall-clock
allowance. Until that evaluation exists, the correct claim is architectural
capability—not demonstrated frontier dominance.
