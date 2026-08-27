# Software adapter

The software adapter captures the exact starting Git state, creates an internal
clone without hard-link optimization, and commits an immutable starting
snapshot. The user's source repository is never mutated automatically.

Each Lead trajectory owns one isolated worktree for its lifetime. That exact
directory survives ordinary moves, provider restarts, and infrastructure
interruptions, so ignored build caches, generated media, partial work, and the
provider's recorded working directory remain real. A new trajectory is seeded
from the exact artifact visible at its fork point. Challenger and deterministic
check projections are disposable.

Committed state is represented as a binary-safe full-index patch. Configured
release outputs are captured separately in the content-addressed blob store and
are required completion evidence: a missing declared output cannot silently
pass. This makes a run portable without deleting its productive live workspace.

`flourite apply` requires a sealed, releaseable run, passing checks, an unchanged
source fingerprint, and explicit operator approval. Apply intent and receipt
make interruption recovery idempotent.
