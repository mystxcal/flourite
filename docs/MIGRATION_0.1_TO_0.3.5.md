# Migration from 0.1 to 0.3.5

## Configuration

Existing core sections remain valid. New sections use conservative defaults:

```toml
[cognition]
mode = "adaptive"

[summit]
mode = "auto"
```

To preserve the old sparse control topology:

```toml
[cognition]
mode = "legacy"
persistent_lead = false

[summit]
mode = "off"
```

## Run state

V3.5 adds Task Source, Charter, Artifact Spine, obligations, cruxes, substrate, overlays, instruments, Summit lineages, Lead session state, action receipts, semantic findings, and Completion Case.

Existing sparse events and records remain representable. Old run directories are never rewritten in place. Start a new v3.5 run or export the old result as an admitted source.

## Provider

Generic and research Lead calls may persist and resume Codex sessions. Ordinary workers remain ephemeral.

As of 0.3.6, live calls use OMP's direct `openai-codex` transport. Old `kind = "codex-cli"` settings and their obsolete flags are accepted but migrated in memory. As of 0.4.0, trusted VM/VPS execution is the default; Bubblewrap is required only for opt-in contained mode. As of 0.5.0, the product and primary command are named Flourite. As of 0.6.0, old and new runs gain an operational `control.sqlite3` sidecar when opened; the hash-chained ledger and completion seal remain unchanged. The `frontier` command and `frontier_harness` Python import remain compatible, and run lookup falls back to `.frontier/runs`. Install OMP, retain the ChatGPT login produced by `codex login`, and run `flourite doctor` before the first live task.

## Release

Release now includes semantic CI and Completion Case coverage. Software apply remains explicit and fail closed.
