# Release notes — Flourite 0.6.0

## Intent

Flourite can now be watched and directed while it works without turning the
controller into a chat room or compromising its event-sourced integrity. The
new live surface exposes what matters, accepts a small set of high-value
controls, and remains detachable from the actual run.

## Live control surface

`flourite live latest` opens a full-screen terminal view containing:

- current run and controller state;
- rounds, call budget, token use, and elapsed time;
- the unresolved issue/crux frontier;
- sanitized model and tool activity as it happens; and
- queued, applied, or rejected operator commands.

The interface supports steering, pause/resume, resumable stop, controller
restart, and detach. The same operations remain scriptable through `flourite
steer`, `pause`, `resume`, and `stop`.

## Control semantics

Commands are appended to a durable sidecar inbox. The active controller is the
only semantic writer and admits commands only at boundaries where no model call
or integration transaction is in flight.

- Steering becomes a Task Source amendment and forces a fresh checkpoint.
- Pause keeps the controller and run lock alive until resume or stop.
- Stop exits cleanly while leaving the run resumable from its ledger.
- Detaching the view changes nothing about the run.

If a process ends after recording a semantic command event but before updating
its UI receipt, the next controller reconciles the receipt from the ledger
without applying the command twice.

## Observability boundary

OMP JSONL is drained while the model is active. Flourite projects tool names,
hashed argument shape, completion/error state, call kind, and usage into a
bounded activity store. Raw model text, hidden reasoning, and raw tool output
never enter the live database. Observability failures are isolated from the
semantic run.

The ordinary command log remains append-only. `flourite live` is explicitly a
disposable projection and never replaces the ledger, `state.json`, or retained
provider diagnostics.

## Validation

Release gates cover strict mypy, Ruff, the full deterministic suite, command
immutability, in-flight steering, pause/steer/resume ordering, resumable stop,
post-append crash reconciliation, provider-stream callbacks, and negative tests
for raw model-content persistence. A clean wheel install and offline demo remain
part of the packaging gate.
