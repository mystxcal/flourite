# Live Codex validation protocol

Live capability is part of the release gate, not an inference from fake-provider tests.

## Record

```text
operating system
Python version and sys.executable
OMP version
subscription login status
harness version
configuration digest
capability contract digest
```

## Required checks

1. `flourite doctor` accepts the installed OMP model catalog and ChatGPT authentication without spending model tokens.
2. A live worker reads, edits, executes, and verifies a file inside its exact workspace.
3. A synchronous `task` delegation returns a child result before the parent boundary and nested usage is counted.
4. A persistent Lead records a thread ID; a later epoch resumes it in the same trajectory workspace, receives an accurate context delta, and still sees ignored generated state from the prior epoch.
5. Every context manifest has empty system/developer messages, explicit ambient-discovery flags, tool names, runtime overlay, transport version, and capability hash.
6. Invalid JSON consumes a recorded boundary attempt and retries within the assigned sparse-call budget.
7. A forced resume failure reconstructs from explicit state; a double failure preserves both costs and traces.
8. Interrupting a worker pauses the run, retains its exact workspace, and queues the unchanged semantic move with explicit retry lineage; resuming does not invent a model-facing repair task.
9. A sealed pre-upgrade run still passes `flourite verify` after schema migration.
10. A software Lead retains one isolated worktree per trajectory; a branch inherits its exact fork artifact; disposable challenge/check projections produce the correct isolated patch, leave the source untouched, and fail closed on negative or missing declared-output evidence.
11. Parent model turns, nested turns, tool calls/errors, tokens, wall time, and action integration disposition are visible after restart.
12. `flourite verify` succeeds after completion and a fresh process load.

## Outcome evaluation

Transport potency is necessary but not sufficient. Freeze a held-out task suite and compare against strong simple baselines at matched total tokens, model effort, and wall time. Preserve failed and capped attempts. Do not call the harness better from routing success, a single attractive artifact, or fake-provider conformance.

## Failure report

Include the exact command/config, `flourite status --json`, relevant event sequence, context manifest, OMP version, safe trace references, and continuity state. An audit export is intentionally sensitive; inspect it before sharing.
