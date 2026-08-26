# Event model

Every meaningful transition is append-only and hash chained. V3.5 events cover:

- run creation and Task Source capture;
- bootstrap and failure;
- Task Source amendments and operator pause, resume, and resumable stop;
- Task Charter, reframe, Lead continuity, Artifact Spine, obligations, cruxes, substrate, overlays, instruments, and Summit state;
- action contracts, selection, durable execution attempts, receipts, evidence,
  semantic integration, and failure;
- checkpoints and round completion;
- adaptive resource initialization, grants, finalization, and extension recommendations;
- final synthesis, semantic CI, Completion Case, release, repair, and repair-loop stops;
- extension intent and archived seals;
- completion and explicit software apply.

Each event records an explicit schema version. Historical version-1 hashes keep
their original material; version 2 and later include the schema version in the
hash. Replay can therefore migrate deliberately without invalidating old runs.

The `RunJournal` is the only authoritative write path. It opens one SQLite
transaction, appends an event, projects and validates the resulting typed
state, and commits only if that projection succeeds. It then atomically
replaces `state.json` as a best-effort derived cache. A projection error leaves
no stray event. A later filesystem failure cannot roll back or disguise an
already-committed semantic event; it is surfaced as snapshot degradation and
the next replay can reconstruct the cache.

Provider execution and semantic integration are separate durable facts:

```text
action_attempt.started
action_attempt.finished   # raw response, trace, usage, outcome
action.completed          # accepted semantic projection
```

This distinction makes interruption recovery honest. A successful model call
is not reported as a failed call merely because the controller died while
integrating it, and its expensive raw output remains available for recovery.
The exact `CONTEXT_LENS.json` used by each call is also retained in the blob
store and referenced by the attempt, so later adjudication can audit scope and
omissions rather than trusting a digest with no recoverable descriptor.

`state.json` is a derived convenience snapshot. `flourite verify` replays
integrity from the ledger and referenced blobs.

`control.sqlite3` is a separate operational sidecar. Its command rows are
append-only, its receipts are mutable, and its activity rows are bounded. A
steer becomes authoritative only when the controller records the corresponding
Task Source amendment in the hash chain. Transient activity never participates
in replay or completion sealing.
