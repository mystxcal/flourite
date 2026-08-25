# V3.5 architecture

## 1. Objective

Flourite solves one exact task. It may change representation, decomposition, mechanism, evidence strategy, or local execution topology, but it may not optimize a replacement objective.

The lexicographic objective is:

1. preserve the immutable task and hard constraints;
2. avoid unacceptable failure;
3. reach the task-appropriate quality and reliability level;
4. within that envelope, minimize accepted-result cost;
5. continue beyond the quality floor only while a concrete action can plausibly improve the result.

## 2. Stable foundation

V3.5 keeps the original sparse foundation:

- one authoritative artifact;
- a small live frontier;
- targeted evidence and candidate deltas;
- immutable event and artifact provenance;
- deterministic scheduling and recovery;
- strong synthesis and bounded release.

The continuity layer is additive. It can remain dormant.

## 3. Immutable Task Source and revisable Charter

`TaskSource` contains the exact original request and append-only amendments. Its digest binds the run.

`TaskCharter` is the current interpretation. It contains deliverable, real-world purpose, audience, hard and soft constraints, unacceptable failures, evidence requirements, unresolved authority questions, provenance-labelled assertions, and exact `RequirementTrace` links back to source wording. The model may enrich this structure but cannot replace the deterministic release surface compiled from the Task Source and contract.

A material change requires `ReframeWitness`:

```text
original success condition
new representation
mapping back to the original deliverable
preserved constraints
new leverage
drift risks
invalidation evidence
```

The runtime rejects a changed destination without a valid witness.

## 4. Artifact Spine

The artifact remains the user-facing truth. The `ArtifactSpine` is a compact coherence layer:

```text
central thesis or mechanism
artifact architecture
key decisions
hard invariants
must-preserve strengths
trade-offs
residual uncertainty
revision
```

A local delta may preserve the spine. A mechanism change revises it. A materially changed spine signals that clean synthesis is preferable to continued patching.

## 5. Lazy obligations and cruxes

An obligation is something that must become true for release. It includes an acceptance condition, dependencies, assumptions, evidence, required observation modalities, artifact location, status, residual uncertainty, and reopen condition. Explicit requirements, prohibitions, evidence demands, and declared generated deliverables always receive runtime guard obligations even when bootstrap supplied its own graph.

A crux is an uncertainty controlling one or more important obligations. The runtime normally exposes one to three active cruxes.

The graph is lazy:

- compile only what current execution makes useful;
- invalidate descendants when a premise fails;
- reopen dependent obligations;
- reactivate controlling cruxes;
- cancel stale plans;
- replan only the next useful horizon.

The graph aids attention; it never replaces the original task or full artifact.

## 5.1 Frontier Kernel and Keeper

The `FrontierKernel` is the compact working navigation between solver turns:

```text
controlling bottleneck
durable invariants
explicit causal revisions of disproved working invariants
live hypothesis families
eliminated families with failure mechanisms and reopen conditions
best next move
validated source action IDs for the latest revision
runtime-owned revision and stagnation
```

It is neither a transcript summary nor long-term memory. Paraphrase does not
advance its runtime-owned revision. A repeated eliminated family needs genuine
reopening evidence before it can consume another action.

At checkpoints, the Frontier Keeper integrates results, compresses the current
search frontier, and detects semantic repetition. It uses a fresh model context
where correlated self-judgment is expensive: frontier-quality and
research/formal/decision/creative/media work, Summit, and observed stagnation.
This is not an extra phase or permanent manager; the existing integration call
owns the role.

The solver has one composable capability palette:

```text
recall · think · retrieve · inspect · execute · build · verify
```

`epistemic_mode` records the dominant intent and biases attention; it never
removes provider-native tools. The model may combine the palette freely inside
one turn. A costly experiment can name the residual uncertainty and decision
branch that justify it, but ordinary tool use carries no permission ritual.
See [Frontier velocity, memory, and knowledge](FRONTIER_VELOCITY.md).

## 5.2 Loss-aware context and observation geometry

Each call receives a `CONTEXT_LENS.json` that identifies the exact task and
artifact digests, question scope, targeted obligations and cruxes, selected
causal evidence, included material, known omissions, and paths to lossless
detail. It is a navigational projection, not a wall: the model may open the
full artifact, staged sources, raw result blobs, or ledger-backed state whenever
the question needs them.

`OBSERVATION_CONTRACT.json` states what release properties are in scope and
which artifact scope and modalities can establish them. Planned modalities do
not become evidence. The runtime retains a requested modality only when the
provider trace shows a matching successful observation tool; runtime-owned
objective measurements remain independently captured. This lets models use
tools aggressively without letting intent self-certify as proof.

## 6. Persistent Lead

The Lead owns solver continuity across orientation, tightly coupled thought,
and final synthesis when enabled. Checkpoint integration may stay in that
session for ordinary work or use a fresh Frontier Keeper when independence is
worth more than hidden-context continuity.

Codex calls use an explicit OMP transport, a persistent session for the Lead, and separate sessions for ordinary harness workers. Lead calls are serialized. In trusted mode each call has the full configured host tool plane and may synchronously delegate to OMP task agents. The provider boundary has no ambient system/developer prompt or project discovery; repository instructions required for software work are explicit capsule inputs. Context and capability hashes make the client-visible boundary auditable.

Every Lead response includes a continuity acknowledgement:

```text
task-source digest
current-artifact digest
active obligation IDs
active crux IDs
artifact-spine revision
```

The acknowledgement is checked against explicit state.

### Resume failure

If session resume fails:

1. retain failed-call usage, stderr, raw events, and command trace;
2. invoke a fresh Lead with the exact reconstructed state capsule;
3. validate its continuity acknowledgement;
4. mark continuity `reconstructed_verified` or `degraded`;
5. continue without treating hidden session memory as authoritative.

## 7. Shared substrate and overlays

The shared substrate contains scoped, provenance-backed knowledge that multiple paths may reuse:

- facts;
- calculations;
- tests;
- tools;
- counterexamples;
- accepted claims;
- resolved subproblems.

Global admission requires evidence references. Branch-local assertions remain branch local.

An overlay contains only the consequential difference from the trunk. It must identify a different mechanism, prediction, action, boundary behavior, assumption, or artifact change.

Ordinary overlays are bounded. Protected stepping stones require an unlock contract and expire unless they produce evidence, a useful instrument, a viable mechanism, or reusable residue.

## 8. Action contracts and receipts

The deterministic controller rejects expensive activity that cannot affect state.

Before execution, a substantive action declares:

```text
target crux
question
possible outcomes
decision effect under each outcome
obligations unlocked
evidence channel
optimization value and information value
feasibility and artifact scope
observation modality
cost
stop condition
failure handling
```

After execution, its receipt records actual state changes, evidence scope, reusable assets, forecast quality, and recommended next action.

Most control bookkeeping is produced in calls already required for solving.
V3.5 does not install a permanent model manager. Models cannot mint progress
from claimed materiality, novelty, negative results, or issue-state churn;
runtime measurements and Keeper-adjudicated kernel revisions own that gradient.

## 9. Local topology compiler

For each active crux, the controller selects the minimum sufficient topology:

| Revealed structure | Execution topology |
|---|---|
| One tightly coupled conceptual bottleneck | Resumed Lead or one specialist thread |
| Independent evidence questions | Small parallel worker batch |
| Exact calculation or transformation | Deterministic tool |
| Two incompatible mechanisms | Two overlays plus discriminator |
| Weak feedback channel | Build and validate an instrument |
| User-dependent authority | Ask the user |
| Concrete representation failure | One task-equivalent reframe |
| Credible upper-tail mechanism risk | Bounded Summit capability |
| Routine local improvement | Direct artifact delta |

A persistent specialist is promoted only after repeated state reuse and a meaningful remaining horizon.

### Evidence-driven resource governor

The call budget is an operator-owned hard envelope, not a phase allocation.
New runs begin with a smaller derived horizon: orientation, one feasible worker
wave, its checkpoint, and the current completion path. At a horizon boundary,
a deterministic governor reads the ledger and preserves gradient as separate
quality, epistemic, feasibility, exploration, and reliability components plus
observed cost. Confirmed discriminative evidence, runtime objective
improvement, an accepted artifact effect, scoped observation coverage,
productive mechanism discovery, or a source-backed Frontier Kernel revision
can earn another worker-plus-checkpoint tranche. Bookkeeping debt reduction and
artifact mutation alone cannot unlock compute. A model-requested delayed-payoff
line can continue only through a bounded contract whose prior step was
integrated and produced a confirmed observation or Frontier Kernel advance.
Material unresolved debt receives only a bounded grace period.

The completion reserve is recalculated from actual remaining work rather than
as a fixed budget fraction. It protects clean synthesis, a release challenge
when applicable, and one repair plus fresh challenge while material risk
remains. Calls, inner model requests, tokens, wall time, grant decisions, and
extension recommendations remain explicit in the event ledger and live UI.
The governor never raises the hard envelope itself.

## 10. Instruments

An instrument is a constructed observation channel. Its lifecycle is:

```text
propose
justify expected decision value
build
validate
execute
capture raw output
interpret within scope
update obligations and cruxes
retain as reusable substrate
```

The runtime distinguishes:

- execution succeeded;
- the instrument is valid for the intended inference.

## 11. Summit capability library

Summit is not a mandatory phase sequence. It is a bounded upper-tail capability library.

Activation reasons include:

- a concrete representation failure;
- a shared hidden assumption;
- an unresolved mechanism fork;
- a weak observation channel;
- a credible developmental stepping stone;
- a ceiling scan with a specific trigger;
- explicit operator choice via `summit.mode = "on"`.

The archive preserves:

- lineages with mechanism, assumptions, dependencies, evidence, predictions, history, and residue;
- temporary stepping-stone protection;
- niche and global capacity;
- near-duplicate replacement;
- sparse development batches;
- falsification residue.

The archive is not the active workspace. Only a few lineages become overlays or live actions.

## 12. Synthesis and semantic CI

The Lead constructs an Apex Brief from exact state, then writes one coherent final artifact.

Semantic CI evaluates artifact-specific properties:

- task and hard-constraint preservation;
- Artifact Spine invariants and key decisions;
- protected insights;
- verified substrate;
- satisfied release-blocking obligations;
- strongest rejected alternative;
- domain-specific deterministic checks.

A cheap lexical guard can detect likely losses, but it is not treated as a semantic oracle. The fresh release challenge owns adjudication of uncertain cases.

## 13. Completion Case

Every release-blocking obligation receives a claim containing:

```text
artifact location
evidence or test
assumptions
status
remaining uncertainty
reopen condition
```

A Completion Case passes only when every release-blocking obligation and its claim are both satisfied. Partial, blocked, deferred, or open work remains a release gap. Required visual, temporal, audio, interactive, source, data, and test modalities must be backed by scoped evidence; one modality cannot impersonate another.

## 14. Release

The release challenge is fresh and narrow. It looks for:

- fatal errors;
- major omissions;
- task drift;
- unsupported load-bearing claims;
- invalid Completion Case evidence;
- a strongest alternative that materially dominates under credible conditions.

Each verdict is bound to the exact artifact digest it inspected. One complete
repair-and-rechallenge cycle is protected in the completion reserve. Distinct
material findings may earn further bounded repairs, but every repair must
change the authoritative artifact, rerun deterministic checks, and face a
fresh release challenge. Repeated identical rejection stops the loop. A stale
verdict or missing fresh challenge fails closed. Cosmetic rewriting is outside
the release contract, but domain fidelity and intended experience are material
when the task makes them material.

For software runs, `software.release_artifacts` declares generated outputs such as videos or PDFs. Flourite captures their exact bytes in the content-addressed artifact, restores them in later calls, exposes them to release review, and materializes them beside the final patch. Presence proves durability only; content or temporal quality still requires the appropriate observation modality.

## 15. Extension

A completed run can be reopened with more calls. Extension:

- verifies the current ledger, blobs, state, artifact, and seal;
- archives the prior seal;
- records durable extension intent;
- preserves research state;
- expands the budget;
- forces fresh replanning;
- performs fresh synthesis and release;
- writes a new seal.

## 16. Legacy control and arena

`cognition.mode = "legacy"` keeps sparse issue/delta/probe semantics without obligations, cruxes, overlays, Lead continuity, or Summit.

`flourite arena` runs adaptive and legacy controllers independently with matched solver budgets. Candidate labels are blinded and A/B position alternates. Judge calls are additional evaluation cost and are recorded separately.
