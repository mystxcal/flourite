# Architecture overview

Flourite has one semantic controller: `KernelEngine` hosts an
`IntelligenceKernel` over an append-only journal. The kernel owns semantic
decisions. A stable, non-semantic supervisor leases one immutable implementation
generation per activity, so any runtime component can be replaced at a journal
boundary without restarting the run.

```text
operator task + sources + hard envelope
                 │
                 ▼
      objective + current workspace
                 │
                 ▼
      component lease + fresh worker
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

The journal is authoritative. The active component pointer is atomic, workers
are disposable, and in-flight code never changes underneath a model/tool call.
`state.json`, the live dashboard, exports, and materialized artifacts are
projections. See [the canonical model](CANONICAL_MODEL.md) for the full object
and [the event model](event-model.md) for legal transitions.
