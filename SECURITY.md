# Operating and trust model

Flourite is designed to run capable Codex agents inside an operator-controlled VM, VPS, or disposable machine. Trusted mode favors task performance and direct feedback over an inner permission maze.

## Trusted mode

The default provider:

- uses the ChatGPT subscription credential created by `codex login`;
- sends no provider system or developer prompt;
- runs OMP with yolo approval;
- inherits the host environment and network;
- enables shell, editing, code intelligence, browser/search, and synchronous subagents;
- disables ambient rules, skills, and extensions so task context remains explicit;
- records safe traces, exact usage, tool outcomes, context hashes, and capability hashes.

The machine is the security boundary. Do not use trusted mode on a host the model is not allowed to control. Credentials available to that host are available to model-run tools.

## Contained mode

`provider.capabilities.mode = "contained"` enables the reduced Bubblewrap path. It narrows filesystem, environment, network, and tools at the cost of capability. It is an operator choice, not the production default.

## Integrity boundaries

The persistent Lead's memory is an optimization, not authority. Durable authority remains in:

- the immutable Task Source and captured inputs;
- explicit configuration and capability manifest;
- the append-only hash-chained ledger;
- content-addressed blobs;
- precommitted action contracts and runtime-observed receipts;
- isolated software snapshots and source fingerprints;
- deterministic checks, semantic CI, Completion Case, and release evidence;
- an explicit patch apply.

## Software work

Model changes occur in disposable Git worktrees derived from an immutable internal snapshot. Configured checks run against the candidate state using the harness interpreter environment. The source repository is not mutated unless the gate passes and apply was explicitly requested. Apply is refused if the source fingerprint changed.

## Exports

Run directories and audit exports may contain prompts, sources, code, local paths, tool traces, and failed attempts. Diagnostic exports use best-effort redaction but still require inspection before sharing. Audit exports are deliberately exact and sensitive.
