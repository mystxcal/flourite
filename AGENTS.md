# Repository instructions

This project implements a sparse, event-sourced orchestration harness for one exact user task.

When modifying it:

- Treat `ledger.sqlite3` events as authoritative and `state.json` as derived.
- Preserve the immutable Task Source; a reframe may change representation, not destination.
- Keep one authoritative artifact and a compact Artifact Spine.
- Compile obligations and cruxes lazily; do not force full upfront decomposition.
- Preserve Lead continuity where useful, but never depend on hidden session memory for integrity.
- Externalize large data into `BlobStore`; event payloads must remain compact.
- Do not add permanent agent personas or free-form inter-agent chat.
- Keep workers isolated and output envelopes small.
- Admit overlays only for consequential behavioral disagreement.
- Keep Summit bounded and subordinate to the exact task.
- Preserve the synthesis reserve before selecting workers.
- Add verification only for a named failure mode with one primary owner.
- Distinguish successful instrument execution from instrument validity.
- Never mutate a user's source repository except through fingerprint-checked explicit apply.
- Add interruption, idempotency, and semantic-regression tests around new side effects.
- Prefer thin domain adapters over contaminating the universal controller.
