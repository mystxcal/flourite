# Operator guide

## Recommended starting mode

Use:

```toml
[cognition]
mode = "adaptive"
persistent_lead = true

[resource]
mode = "adaptive"

[summit]
mode = "auto"
profile = "deep"
```

Adaptive mode starts sparse. It does not imply that Summit, overlays, instruments, specialists, or reconstruction will run.

## Budget selection

`run.budget.max_calls` is the operator's hard outer envelope, not Flourite's
planned consumption. The default is deliberately roomy. Adaptive mode derives
a smaller initial horizon from the execution topology and current completion
risk, then grants small additional tranches only when the ledger shows useful
gradient. It can stop far below the envelope. `max_rounds` is optional and is
best reserved for an independent safety ceiling rather than ordinary phase
allocation.

A sparse call is one provider process or boundary attempt, not one internal
model/tool-loop turn. `Model requests`, token totals, and wall time expose the
full parent and nested cost. Configure token or wall envelopes when those are
the real scarcity. Every hard envelope is a cap, never a quota.

Extend the hard envelope when Flourite reports `extension required` and:

- active high-impact cruxes remain;
- a validated instrument can unlock several decisions;
- a mechanism lineage has earned another development step;
- final uncertainty remains decision-sensitive;
- an extension can reuse substantial prior evidence.

Do not extend merely because the run has unused conceptual possibilities. The
resource governor records its latest decision, evidence gradient, active
horizon, completion reserve, and hard ceiling in the ledger and live UI.

## Capability mode

Use trusted mode on a dedicated VM/VPS when result quality is the priority. It gives the model yolo-approved shell, editing, code intelligence, browser/search, and synchronous task delegation. Use contained mode only when you deliberately accept a smaller tool surface in exchange for an inner Bubblewrap boundary.

## Choosing Summit mode

- `off`: prohibit upper-tail lineage expansion.
- `auto`: activate only for a recorded concrete trigger.
- `on`: guarantee that the bounded exact-task upper-tail path is reachable.

`on` does not create a grid search. It seeds or develops a small number of mechanismally distinct lineages within configured archive and active limits.

## Reading status

```bash
flourite status latest
flourite inspect latest
```

Pay attention to:

- open release-blocking obligations;
- active cruxes;
- Lead continuity status;
- active overlays and protected stepping stones;
- Summit activation reasons;
- semantic CI status;
- Completion Case gaps;
- calls used relative to the active horizon and hard envelope;
- the resource governor's latest decision and evidence gradient.

A `degraded` Lead is not necessarily a failed run, but it deserves inspection.

## Live control

Use the attachable control surface for a running task:

```bash
flourite live latest
```

The view combines current semantic state with a bounded stream of sanitized
model/tool activity. It never displays raw hidden reasoning or raw tool output.
The dashboard can:

- steer at the next safe boundary;
- pause and resume without discarding an in-flight call;
- stop while preserving resumable state;
- restart a resting controller; or
- detach without affecting execution.

Operator commands are durable. Steering is recorded as a Task Source amendment
and forces a fresh checkpoint before the prior plan can continue. The semantic
ledger remains single-writer and authoritative; dashboard activity is only a
projection.

For scripts or a second shell, use `flourite steer`, `flourite pause`,
`flourite resume`, and `flourite stop` directly.

## Recovery

Use `flourite resume` only for interrupted active runs. Use `flourite extend` for completed sealed runs that need more research.

Before either:

```bash
flourite verify latest
```

Extension archives the prior seal and produces a new release. It does not overwrite the historical release record.

## Instruments

Inspect generated tools before execution on sensitive data or code. Confirm that the validation plan tests the instrument’s intended inference, not only whether it runs.

## Software

Configure deterministic checks:

```toml
[software]
checks = ["python -m pytest -q", "python -m compileall -q src"]
```

The harness emits a patch but does not mutate the source repository automatically.

```bash
flourite verify latest
flourite apply latest
```

Apply refuses when release is blocked, checks failed, the source fingerprint changed, or the run is not sealed.

## Arena

Use arena to evaluate controller changes, not to solve every production task twice.

```bash
flourite arena --judges 4 --adapter research --source brief.md "Exact task"
```

Review judge rationales and fatal issues, not only the aggregate vote. A tied or noisy arena is evidence that the claimed improvement is not yet established.

## Exports

Use diagnostic exports for operational review after inspecting them. Use audit exports only when exact evidence is required; they are intentionally sensitive.
