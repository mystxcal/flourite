# Contributing

Changes should preserve the sparse-core philosophy:

- prove a new component interrupts a recurring causal failure;
- compare it under matched call budget against the existing path and a simpler baseline;
- keep semantic interfaces small;
- retain raw evidence losslessly;
- avoid redundant verification ownership;
- keep source mutation explicit and recoverable.

Before submitting:

```bash
ruff check .
mypy src
pytest
```

A change to global scheduling, prompts, memory, or routing should include interaction tests and at least one regression test showing why a task-local adapter or probe was insufficient.
