# Architecture overview

Flourite has one controller: `KernelEngine` hosts an `IntelligenceKernel` over
an append-only journal. The kernel owns semantic decisions; the host owns files,
processes, commands, and materialization.

```text
operator task + sources + hard envelope
                 │
                 ▼
      objective + current workspace
                 │
                 ▼
        one model/tool move
                 │
                 ▼
  atomic observations + artifact + workspace
                 │
          ┌──────┴──────┐
          │             │
      next move    finish claim
                        │
                        ▼
               fresh challenge
                 │           │
             support    material finding
                 │           │
             satisfied  normal construction
```

The journal is authoritative. `state.json`, the live dashboard, exports, and
materialized artifacts are projections. See [the canonical model](CANONICAL_MODEL.md)
for the full object and [the event model](event-model.md) for legal transitions.
