# Event model

Every meaningful transition is append-only and hash chained. V3.5 events cover:

- run creation and Task Source capture;
- bootstrap and failure;
- Task Source amendments and operator pause, resume, and resumable stop;
- Task Charter, reframe, Lead continuity, Artifact Spine, obligations, cruxes, substrate, overlays, instruments, and Summit state;
- action contracts, selection, execution, receipts, evidence, and failure;
- checkpoints and round completion;
- adaptive resource initialization, grants, finalization, and extension recommendations;
- final synthesis, semantic CI, Completion Case, release, repair, and repair-loop stops;
- extension intent and archived seals;
- completion and explicit software apply.

`state.json` is a derived convenience snapshot. `flourite verify` replays integrity from the ledger and referenced blobs.

`control.sqlite3` is a separate operational sidecar. Its command rows are
append-only, its receipts are mutable, and its activity rows are bounded. A
steer becomes authoritative only when the controller records the corresponding
Task Source amendment in the hash chain. Transient activity never participates
in replay or completion sealing.
