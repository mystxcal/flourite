# Changelog

## Unreleased

- Made a run's implementation live-replaceable: each activity leases an immutable, content-addressed component generation while the journal and step protocol remain stable.
- Added atomic component binding, explicit rollback, per-activity receipts, disposable worker processes, and automatic fallback when a replacement worker fails.
- Split typed move-result compilation from all-or-nothing state transition validation, so provider output cannot partially mutate canonical state.
- Reduced the canonical runtime's maximum cyclomatic complexity from 85 to 15 and removed every canonical routine above 20 without changing the model-facing contract.
- Rebuilt OMP execution around explicit call, attempt, trace, safe-event, and diagnostic components while preserving retries, usage accounting, and session continuity.
- Made the public CLI and live dashboard canonical-only; the retired controller is now lazy-loaded solely by the hidden `legacy-run` comparison command.
- Added adversarial aggregate-transition coverage for identity, ownership, lineage, digest, workspace, continuation, finish-claim, blocker, rollback, and exact replay invariants.

## 0.6.0 — 2026-08-24

- Added `flourite live`, an attachable full-screen view of run phase, budget, frontier, sanitized model/tool activity, and operator-command receipts.
- Added durable safe-boundary steering, pause, resume, and resumable stop controls in both the dashboard and CLI.
- Made steering an immutable Task Source amendment followed by a fresh integration checkpoint, rather than an ephemeral prompt injected into an in-flight model call.
- Added an append-only control inbox with separately mutable receipts and a bounded transient activity stream while preserving the hash-chained semantic ledger as the sole authority.
- Streamed sanitized OMP JSONL activity during calls without persisting raw model text, hidden reasoning, or raw tool output in the live database.
- Added crash reconciliation for the ledger-append/receipt-update boundary, safe stop recovery, and regression coverage for command immutability, pause/resume, steering, stop, sanitization, and provider streaming.

## 0.5.0 — 2026-08-24

- Renamed the user-facing product, package, and primary command to Flourite while retaining `frontier` as a compatibility command and the stable `frontier_harness` Python API.
- Added a crystalline terminal design system derived from the Flourite banner: faceted cube mark, cyan/blue/violet hierarchy, graph-route section rules, and semantic phase glyphs.
- Reworked run, resume, extend, doctor, status, inspect, and demo output around a compact orient/focus/integrate/crystallize/prove flow without changing any runtime event or decision logic.
- Preserved clean JSON, JSONL, quiet, redirected, and `NO_COLOR` output paths.
- Moved new default state and examples to `.flourite` while retaining automatic lookup of legacy `.frontier` runs.

## 0.4.0 — 2026-08-24

- Added trusted full-host OMP execution with yolo approval, inherited VM/VPS environment, shell, editing, LSP, browser, web search, and synchronous subagents.
- Added a zero-token installed-tool compatibility probe and exact capability/context manifests.
- Split sparse provider-attempt accounting from parent and nested model requests while retaining aggregate token cost.
- Added observed action receipts with tool-derived evidence channels, costs, outcome matching, failure retention, and checkpoint acceptance/rejection feedback.
- Added hashed context deltas for persistent Lead epochs and removed the redundant model-facing todo plane.
- Preserved exact replay and completion-seal verification across the worker observation/runtime receipt schema split.
- Preserved both failed resume and failed reconstruction usage.
- Made software release checks resolve the harness interpreter environment reliably.
- Let a clean fresh release challenge adjudicate model-only semantic false positives without overriding deterministic failures.

## 0.3.7 — 2026-08-24

- Made GPT-5.6 Sol the default for every substantive model call.
- Routed bounded ordinary probes to Sol medium, consequential workers to Sol high, and persistent-Lead or fatal-impact work to Sol xhigh.
- Kept lower-capability models available only through an explicit configuration override rather than silently placing them in the default reasoning path.

## 0.3.6 — 2026-08-24

- Replaced the ambient Codex CLI execution path with an explicit OMP transport over the existing ChatGPT subscription login.
- Removed provider-side system and developer prompts, ambient rules, skills, extensions, LSP discovery, shell access, and inherited secret-bearing environment variables.
- Added per-call context manifests, bounded schema retries with exact usage accounting, safe event retention, stable Lead continuation, and verified reconstruction after resume failure.
- Added Bubblewrap filesystem isolation, explicit read/write/web-search tool admission, and startup checks for the configured models and sandbox runtime.
- Enforced provider-call allocations across parallel work and recovery so schema retries cannot silently exceed the run budget.
- Hardened semantic CI, Completion Case validation, software mutation gates, interrupted-run recovery, and idempotent patch application.
- Removed the obsolete direct Codex provider and transparently migrate its old configuration keys.
- Validated the repaired runtime with offline tests, live Lead continuation, an isolated software repair, and a matched-budget blind comparison.

## 0.3.5 — 2026-08-24

- Hardened exact-task fidelity with immutable Task Source, Charter provenance, and reframe witnesses.
- Added persistent Lead resume/reconstruction with continuity verification and durable failed-attempt accounting.
- Added lazy obligations/cruxes, dependency reopening, shared substrate, bounded overlays, action contracts, instruments, Artifact Spine, semantic CI, and Completion Case.
- Added bounded Summit archive and explicit full-Summit reachability without default population search.
- Added sealed-run extension and blind matched-budget arena comparison.
- Preserved legacy sparse control and all source-integrity/software-apply safeguards.
- Expanded behavioral and adversarial validation.

## 0.1.0 — 2026-08-19

- Initial sparse event-sourced runtime.
- Codex CLI subscription provider.
- Generic and software adapters.
- Immutable ledger, blob store, deterministic reducer, recovery, release gate, and explicit patch apply.
