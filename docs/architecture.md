# Architecture overview

V3.5 solves one exact task through a sparse event-sourced controller. See `V3_5_ARCHITECTURE.md` for the full design.

```text
immutable Task Source
  → strong baseline + revisable Charter + Artifact Spine
  → lazy obligations and 1–3 active cruxes
  → minimum-sufficient action topology
  → scoped evidence / tools / overlays / bounded Summit
  → Lead integration
  → Lead-owned clean synthesis
  → semantic CI + Completion Case
  → one fresh bounded release challenge
  → completion seal
```

The event ledger is authoritative. State snapshots, capsules, indexes, and staged source views are reconstructible.
