# Codex subscription transport

The live provider uses OMP's direct `openai-codex` transport with the OAuth credential created by `codex login`. It does not invoke `codex exec` or inherit the Codex coding-agent prompt.

Lead calls preserve one Codex session. Later Lead epochs resume it and receive a hash-derived list of changed, added, removed, and unchanged context files. Ordinary harness workers remain separate sessions, but they may use OMP's synchronous `task` tool for bounded specialist delegation.

## Trusted mode

Trusted mode is the production default for a dedicated VM, VPS, or disposable host:

- `NULL_PROMPT=true` with no provider system or developer message;
- yolo approval and no inner permission prompts;
- inherited host environment and network;
- explicit `read`, shell, edit/write, grep/glob, LSP, AST editing, debug/eval, browser, web search, and task tools;
- ambient rules, skills, and extensions disabled so the harness owns the context;
- synchronous task delegation with bounded concurrency, recursion, runtime, and request accounting;
- compaction, retry, and continuation configured explicitly;
- safe event traces, exact boundary attempts, parent/nested usage, capability hashes, and context manifests.

The host is the security boundary. Use a machine the model is allowed to control.

## Contained mode

`provider.capabilities.mode = "contained"` uses a reduced tool and filesystem surface and requires Bubblewrap. It is useful when the operator values isolation over the full execution ceiling.

## Accounting

One provider process or boundary retry consumes one sparse `call`. Every model turn inside the parent tool loop and every nested task request increments `model_requests`. All parent and nested tokens are aggregated. The run budget remains sparse without hiding the real work performed inside a capable call.

`flourite doctor` checks the installed OMP version, ChatGPT login, model catalog, and exact configured tool names without making a model request.
