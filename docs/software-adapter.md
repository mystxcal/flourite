# Software adapter

The software adapter captures the exact starting Git state, creates an internal clone without hard-link optimization, commits an immutable starting snapshot, and gives model calls disposable detached worktrees.

The evolving result is represented as a binary-safe full-index patch. Configured checks run in isolated state. The source repository is never mutated automatically.

`flourite apply` requires a sealed, releaseable run, passing checks, an unchanged source fingerprint, and explicit operator approval. Apply intent and receipt make interruption recovery idempotent.
