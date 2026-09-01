# Repository instructions

This project implements a phase-free, event-sourced intelligence kernel for one exact user task.

When modifying it:

- Treat `ledger.sqlite3` events as authoritative and `state.json` as derived.
- Preserve the immutable Objective; steering appends an explicit amendment rather than rewriting history.
- Keep the compressed Frontier and task-native Quality Lens expressive. Do not encode a task's semantic world into controller schemas.
- Treat one Move as the semantic unit of work. Its observations, artifacts, workspace, continuation, and usage commit atomically or not at all.
- Preserve Lead continuity by trajectory, but never depend on hidden session memory for integrity.
- Externalize large data into `BlobStore`; event payloads must remain compact.
- Do not add fixed phases, permanent agent casts, universal issue taxonomies, arbitrary call grants, or a separate synthesis reserve.
- Let the Lead do ordinary construction. Use a fresh Navigator only to escape a local frame and a fresh Challenger only to test a concrete finish claim.
- Widen into trajectories only when there is real uncertainty width; integration must still return to one current Workspace.
- Preserve rejected and negative observations as learning. A failed completion claim returns to ordinary construction.
- Add deterministic verification only where an adapter can observe a named property directly.
- Distinguish successful instrument execution from instrument validity.
- Only the operator's explicit compute envelope may terminate productive work for resource reasons.
- Never mutate a user's source repository except through fingerprint-checked explicit apply.
- Add interruption, idempotency, and semantic-regression tests around new side effects.
- Prefer thin domain adapters over contaminating the universal controller.
