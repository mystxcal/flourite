# Event model

The intelligence kernel has one authoritative event stream. Every event is
append-only and hash chained. `state.json` is a rebuildable projection.

## Canonical events

```text
run.started
steering.received
move.proposed
move.started
move.applied
finish.claimed
run.paused
run.resumed
run.satisfied | run.exhausted | run.blocked | run.stopped | run.failed
```

The reducer still understands fine-grained `observation.recorded`,
`artifact.committed`, `workspace.committed`, and `move.finished` events for
explicit internal construction and compatibility. External model/tool work uses
`move.applied` so all of its semantic meaning commits atomically.

A `move.applied` payload may contain observations, an artifact, a workspace,
new trajectories, a continuation, a finish claim, blocker information, usage,
and failure residue. The journal validates the complete next projection inside
the same SQLite transaction as the append. A validation error leaves no event
behind.

```text
provider execution
      ↓
typed MoveExecutionResult
      ↓
validate every reference and state transition
      ↓
append move.applied + project RunState in one transaction
      ↓
best-effort atomic state.json refresh
```

A later filesystem error while refreshing `state.json` does not rewrite the
already committed truth. Replay repairs the cache. Provider results are cached
by move id so recovery can reapply the same expensive result idempotently.

## Content

Large content never lives in event payloads. Objectives, amendments, workspace
documents, artifacts, deliverables, raw evidence, and staged sources are stored
by digest in the blob store. Events reference typed `ContentRef` objects.

`flourite verify` checks:

1. the ledger hash chain;
2. replay equality with the live projection;
3. every referenced objective, workspace, artifact, deliverable, observation,
   and source blob.

## Control sidecar

`control.sqlite3` is deliberately outside semantic authority. Commands are
durable and receipts are mutable. Activity rows are bounded presentation data.
A steer becomes authoritative only when `steering.received` enters the journal.
Provider and tool activity can disappear without changing the meaning or
recoverability of the run.

Historical pre-kernel ledgers keep their original event models and loaders;
new runs use the canonical events above.
