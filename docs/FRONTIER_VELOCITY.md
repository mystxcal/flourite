# Frontier velocity, memory, and knowledge

Flourite should maximize useful movement per scarce second and token, not the
amount of visible work. The controlling quantity is:

```text
frontier velocity = consequential uncertainty removed / total accepted-result cost
```

Activity, prose, files, tool calls, candidates, votes, and checks are not
progress. They are costs that may purchase progress.

## The fast loop

One persistent solver owns the live line of thought. It can generate and kill
many ideas internally without turning each one into a branch, file, experiment,
or report. At a meaningful integration boundary, a Frontier Keeper compresses
the result into a small explicit kernel:

```text
controlling bottleneck
durable invariants
explicit causal revisions of disproved working invariants
live hypothesis families
eliminated families + failure mechanisms + reopening conditions
best next move
completed actions that caused this revision
```

The Keeper is not an additional phase or permanent managerial persona. The
checkpoint call already required to integrate work performs this function. It
is fresh when correlated self-judgment is dangerous: research, formal,
decision, creative and media work; very-high or frontier quality; Summit;
or observed stagnation. Ordinary work may preserve Lead continuity.

Working invariants cannot disappear by omission. A disproved one is retired
explicitly with its failure mechanism and replacement, while Artifact Spine
hard invariants must be revised at that higher semantic layer first.

The runtime, not either model, owns the kernel revision and stagnation count,
and validates every claimed source action against the completed batch.
Paraphrasing the same frontier does not create progress.

## Epistemic escalation

Each uncertainty should be attacked in the cheapest medium capable of changing
the decision:

```text
recall -> think -> retrieve -> execute -> verify
```

This is a ladder, not a workflow. A run may skip rungs in either direction.

- **Recall** checks explicit active state and relevant prior learning.
- **Think** proposes multiple causal families, attacks them mentally, and keeps
  only survivors or reusable failure mechanisms.
- **Retrieve** opens source material only when missing knowledge distinguishes
  live possibilities.
- **Execute** calculates, edits, simulates, searches, or measures when thought
  cannot settle the residual.
- **Verify** protects a consequential boundary against a named failure mode.

Retrieval, building, execution, and verification must state the residual that
cheaper cognition cannot settle and the decision the observation can change.
This rule is not a safety ritual. It prevents ten minutes of machinery from
replacing ten seconds of reasoning.

Deterministic or external evidence is still execution even when the assignment
sounds conceptual. Conversely, access to tools is not a reason to use them.

## Gradient ownership

Models propose meaning. The runtime measures what it can.

A new work horizon may be earned by:

- a runtime objective improving against a valid baseline;
- a confirmed observation changing a decision or obligation;
- a Keeper-adjudicated semantic advance in the Frontier Kernel;
- credible scoped evidence reaching a previously unsupported requirement;
- a changed artifact whose result was actually accepted.

It is not earned by model-authored importance, claimed novelty, a negative flag,
rewritten bytes, issue bookkeeping, or closing an item by assertion.

Rejection is still information. The artifact may reject a result while the
kernel retains the causal reason a family failed. The system backpropagates
failure mechanisms, not just winners.

## Samsara control

An eliminated direction is recorded as:

```text
family: the semantic approach, independent of vocabulary or implementation
failure mechanism: why it failed
reopen if: what fact or condition would make another attempt rational
```

The scheduler blocks the same family when no reopening evidence is supplied.
After repeated non-informative attempts, a new label is insufficient: the next
action must name a different causal family and what makes it genuinely new.

This deliberately uses two defenses:

1. deterministic matching catches obvious repeats cheaply;
2. the Frontier Keeper catches semantic repeats whose wording changed.

Neither defense pretends that lexical similarity is a semantic oracle.

## Four information layers

Memory and knowledge should not be another prompt appendix. They are different
projections over one lossless foundation.

### 1. Immutable atoms

The event ledger and blob store preserve exact prompts, sources, artifacts,
observations, failures, decisions, costs, versions, and provenance. Nothing
else is authoritative. Indexes and summaries may be deleted and rebuilt.

### 2. Active Frontier Kernel

The kernel is working memory for one run. It is tiny enough to remain visible
on every consequential turn and explicit enough to survive interruption. It is
not a transcript summary and does not try to preserve every useful detail.

### 3. Experience memory

Experience answers: *Have we encountered this shape of problem or failure
before?* Its natural geometry is temporal and causal.

The useful atomic record is dense:

```text
situation and scope
action or hypothesis family
observation
decision consequence
failure mechanism or reusable method
reopening condition
exact provenance pointers
```

Victor Taelin's [OptMem](https://github.com/VictorTaelin/OptMem) supplies a
strong navigation primitive: immutable chronological notes beneath a
rebuildable binary summary tree, with recent detail retained and older history
shown at progressively coarser resolution. A constant-size cover gives the
solver a map; zoom opens the relevant branch; exact atoms remain recoverable.

Flourite should adopt that principle, not blindly install the package as its
entire memory system. Temporal multiresolution is excellent for experience. It
does not represent source structure, claim conflict, or evidential scope well
enough to be the knowledge base.

### 4. Knowledge base

Knowledge answers: *What is currently supportable about this subject?* Its
natural geometry is source structure plus claims and their relations.

Raw sources remain content-addressed. Rebuildable projections expose:

- source trees: corpus -> document -> section -> exact span;
- claim nodes with scope and validity conditions;
- supports, refutes, qualifies, depends-on, and supersedes edges;
- contradiction sets instead of averaged consensus;
- lexical, semantic, causal, and provenance indexes as alternate views.

A vector index may help locate candidates. It must never be the knowledge
model: textual similarity is not decision relevance, truth, scope, or
independence.

## Map, zoom, open

Retrieval should navigate rather than dump.

1. The active crux and kernel form a precise query.
2. Experience memory returns a constant-budget temporal/causal map, including
   nearby eliminated families and reopening conditions.
3. The knowledge base returns a compact source/claim map with contradictions
   visible.
4. The solver chooses which branch to zoom.
5. Exact source spans or raw episodes are opened only when load-bearing.

The context composer always includes the immutable task, artifact spine,
Frontier Kernel, and exact action contract. The question-specific context lens
allocates the remaining attention to relevant memory and knowledge; the
epistemic mode may bias that projection but never gates tools or access to
lossless detail.

This preserves two kinds of compression:

- **semantic density**: each line changes a future decision or prevents a
  known mistake;
- **resolution control**: detail fades from the map without disappearing from
  storage.

## Admission and compaction

Workers may emit candidate facts and lessons, but they do not write durable
memory directly. The existing integration checkpoint admits an item only when
it can do at least one of these in a future run:

- prevent rediscovery of a known failure mechanism;
- change a selection, routing, escalation, or stopping policy;
- preserve a supported invariant or causal method;
- expose a contradiction or validity boundary;
- recover a user decision or stable preference in its proper namespace.

Everything else stays in the raw ledger. There is no novelty quota and no
periodic summarization ceremony.

Compaction is triggered by retrieval/context pressure, not the clock. A merge
must preserve scope, causal why, contradiction, reopening condition, and
provenance pointers. If those cannot survive, the items remain separate.

## Failure boundaries

The architecture must resist these specific failures:

| Failure | Structural answer |
| --- | --- |
| Compression deletes the useful exception | Raw atoms are immutable; maps are disposable and zoomable. |
| Old memory poisons a changed task | Every lesson carries scope and a reopening/invalidation condition. |
| Similar text floods context | Retrieval begins from the active decision, then map/zoom, not global nearest neighbours. |
| The model rewards its own activity | Runtime measurements and Keeper-adjudicated kernel changes own the gradient. |
| A worker rewrites shared history | Only integration admits durable memory; worker output remains a proposal. |
| A manager becomes ceremony | Keeper work is fused into an existing checkpoint and triggered freshness is selective. |
| Narrow retrieval suppresses discovery | Summit or sustained stagnation may reserve one deliberately distant causal branch. |
| A killed idea returns under new jargon | Semantic family plus failure mechanism is remembered; reopening needs new evidence. |
| Summaries become stale authority | Exact evidence and the immutable task always outrank derived views. |

## Build order

The active loop comes first because bad memory only makes a bad search loop
more confidently repetitive.

1. Make the Frontier Kernel, epistemic ladder, runtime gradient, and samsara
   barrier reliable within one run.
2. Add read-only experience maps over existing ledgers and evaluate whether
   retrieval changes decisions per token.
3. Add explicit keeper-owned memory admission and causal negative records.
4. Add the source/claim knowledge projection with contradiction-preserving
   navigation.
5. Only then learn retrieval and compression policy from accepted-result cost.

No global memory writer, background society, or autonomous self-editing policy
is required. The smallest complete system is one fast solver, one integrated
Keeper judgment, one immutable record, and navigation that spends detail only
where the live decision demands it.
