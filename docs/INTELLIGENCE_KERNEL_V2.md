# Flourite intelligence kernel

Status: implemented architecture and design rationale.

> Historical rebuild record. The authoritative current design is
> [CANONICAL_MODEL.md](CANONICAL_MODEL.md); in particular, the former artifact
> promotion gate is not part of the live control law.

## 1. The outcome

Flourite exists to let a strong model solve one real task better than the same
model would solve it in a single unaided session.

It must improve the result by giving the model:

- durable long-horizon continuity without replacing the original task;
- a faithful, navigable view of the work instead of an ever-growing transcript;
- freedom to think, use tools, build, test, branch, compare, reframe and return;
- fast task-native feedback while changes are still cheap;
- independent challenge before a claim of completion becomes final;
- adaptive compute that follows opportunity rather than a predetermined ritual;
- exact recovery after interruption without pretending partial work succeeded;
- causal memory of what was learned, not a pile of summaries.

The harness is successful only when those properties raise the attainable
quality ceiling. Activity, agent count, test count, schema coverage, and audit
volume are not proxies for success.

## 2. What the existing system taught us

The current implementation contains useful infrastructure, but the cognitive
architecture failed in a repeatable way.

### Observed failures

1. **Local policy overruled the real budget.** A temporary active-call horizon
   stopped useful work while most of the hard envelope remained.
2. **An empty generated queue was mistaken for an exhausted problem.** The
   controller finalized because its own action representation contained no
   accepted moves, not because the objective was satisfied.
3. **Evaluation arrived as a tail gate.** The strongest evidence appeared after
   construction had ended, when the system had already withdrawn permission to
   reconstruct the artifact.
4. **Rejection did not become learning.** Release findings entered a repair
   subsystem with separate limits instead of returning to the same search loop
   that built the artifact.
5. **Repair optimized the visible symptom.** Repeated release passes patched a
   finished candidate while the failed artistic or conceptual boundary needed
   to be reopened upstream.
6. **Semantic schemas competed with the task.** Obligations, cruxes, spines,
   overlays, completion claims, action contracts and several runtime projections
   each held a partial interpretation of the same work. Drift between them
   became a new problem the model had to solve.
7. **Call counts were not compute.** A top-level call could contain hundreds of
   model turns and tool actions, while a cheap short call counted the same.
8. **A fresh model could inherit ceremony rather than understanding.** Large
   synthetic context packages emphasized the harness's ontology and reduced
   direct contact with the artifact, evidence and original objective.
9. **The runtime could report completion for an unreleasable result.** Operational
   termination, objective satisfaction and external publication were not kept
   semantically distinct.
10. **Correctness machinery was concentrated in named phases.** Bootstrap,
    frontier, summit, semantic CI, completion, release and repair created
    hand-off boundaries where information and authority could be lost.

These are not ten unrelated bugs. They have one root cause:

> Flourite made its intermediate representations authoritative. The controller
> began optimizing the machinery it had constructed instead of the user's
> objective.

## 3. Architectural rivals

The redesign was compared against four genuinely different shapes.

### A. Schema-heavy deterministic orchestrator

The current family: compile the task into many typed objects, schedule bounded
workers, reconcile them at checkpoints, then pass through completion and
release gates.

**Strengths:** auditable, deterministic, inspectable, easy to unit test.

**Fatal weakness:** the ontology necessarily approximates open-ended work. Once
the approximations gain scheduling or stopping authority, model intelligence is
limited by controller recall. More schemas make the failure safer-looking, not
less likely.

**Decision:** retain deterministic durability below cognition; reject this as
the cognitive core.

### B. One persistent agent with tools

Give one strong model the task, a shell, files and a long-lived session. Let it
work until it says it is done.

**Strengths:** maximum local agency, minimal translation loss, little ceremony.

**Fatal weakness:** long runs accumulate anchoring, context distortion, repeated
failed ideas, forgotten constraints and self-certifying completion. A process
crash can destroy the only useful state.

**Decision:** the persistent agent is the default executor, but not the entire
system.

### C. Permanent multi-agent organization

Maintain managers, researchers, builders, critics and judges as a standing
society.

**Strengths:** parallelism and viewpoint diversity when the problem naturally
decomposes.

**Fatal weakness:** organization becomes workload. Fixed roles manufacture
messages, correlated opinions and reconciliation cost even when one coherent
mind is the right topology.

**Decision:** branching and independent challenge are capabilities selected at
runtime, never permanent institutions.

### D. Adaptive workspace and search field

One persistent lead works against a durable shared workspace. It may create
temporary trajectories when real alternatives or independent subproblems
exist. Every tool result, experiment, critique and artifact-native evaluation
returns through the same observation path. Periodic fresh-context navigation
checks the global direction. The only hard controller owns durability,
resources and termination semantics.

**Strengths:** preserves agency, supports arbitrary search topology, catches
long-horizon drift, learns continuously, and remains recoverable.

**Risk:** a weak workspace update or navigator can still distort the run.

**Decision:** this is the target. The risk is controlled by keeping raw evidence
and artifacts directly accessible, making interpretations revisable, and never
allowing a navigator or heuristic to declare success.

## 4. The irreducible model

The cognitive kernel has four data concepts and one transition.

### Objective

The immutable destination:

- exact user request and amendments;
- deliverables and hard constraints;
- available resources and explicit boundaries;
- user steering received during the run.

The original text is always available. Derived interpretations may help
attention, but never replace it.

### Workspace

The current compact understanding of the problem and work. It contains:

- current best artifact or answer and any live alternative heads;
- present strategy and why it is plausible;
- load-bearing unknowns, disagreements and risks;
- relevant observations and causal lessons;
- failed approaches and the conditions under which they failed;
- active trajectories and promising next moves;
- explicit uncertainty about what the system may be missing.

The workspace is a model-authored decision map, not a formal world ontology. It
is versioned, inspectable and revisable. Large or exact material is referenced,
not copied into it.

There is always a recoverable current-best artifact or answer. Improvement is
incremental or branch-based; Flourite does not postpone synthesis until a final
phase. If the envelope ends, the latest integrated head remains inspectable even
when the run is honestly marked `exhausted`.

### Move

A bounded attempt to improve understanding or the artifact. A move may:

- think or derive;
- inspect source material;
- retrieve knowledge;
- use tools;
- modify or create an artifact;
- run an experiment;
- create, advance, compare, merge or abandon a trajectory;
- ask for an independent challenge;
- reframe the representation without changing the objective;
- claim completion.

The model chooses the shape of a move. The harness does not force work through
a catalogue of task ontologies.

### Observation

Anything learned by executing or evaluating a move:

- tool output;
- artifact or diff;
- test result;
- measurement;
- environmental response;
- source-backed finding;
- independent critique;
- failed attempt;
- operator steering;
- resource usage;
- completion challenge.

An observation records provenance, artifact/trajectory scope and confidence.
Raw output is immutable; its interpretation is not.

### Transition

There is one cognitive transition:

```text
(Objective, Workspace_t) + Move_t + Observations_t
    -> (Workspace_t+1, ArtifactGraph_t+1, proposed Move_t+1 | FinishClaim)
```

Construction, research, critique, testing, repair and release recovery all use
this transition. No phase owns a second version of the problem.

The transition preserves a simple learning gradient:

```text
intent -> intervention -> observable consequence -> changed understanding
```

For a reversible exploratory move, that record can be one sentence. For an
expensive experiment or consequential artifact change, the model should make
the causal bet and discriminating outcome explicit. The purpose is not a form
to complete; it is to make failed work teach the next move instead of merely
consuming tokens.

## 5. Three planes, one loop

```text
                           OPERATOR
                  steer / pause / resume / stop
                               |
                               v
  +------------------------------------------------------------------+
  | INTELLIGENCE PLANE                                               |
  |                                                                  |
  |  immutable Objective                                             |
  |          |                                                       |
  |          v                                                       |
  |  Workspace <---- fresh Navigator observation                    |
  |      |                                                           |
  |      v                                                           |
  |  Lead chooses Move ---- optional temporary trajectories          |
  |      |                              |                            |
  +------|------------------------------|----------------------------+
         v                              v
  +------------------------------------------------------------------+
  | ENVIRONMENT PLANE                                                |
  | tools / sources / code / experiments / artifact-native checks   |
  | independent challengers / domain adapters / external feedback   |
  +-------------------------------|----------------------------------+
                                  v
                           Observations
                                  |
                                  +-------------> Workspace

  +------------------------------------------------------------------+
  | DURABILITY PLANE                                                 |
  | event journal / artifact graph / blobs / leases / replay / usage |
  +------------------------------------------------------------------+
```

The planes are boundaries of responsibility, not sequential phases.

- The **intelligence plane** decides what thinking or action is useful.
- The **environment plane** makes claims collide with reality.
- The **durability plane** guarantees that the run can be understood and
  resumed exactly.

## 6. Cognitive modes

The system has one lead and two temporary modes. They are context shapes, not
personas or permanent agents.

### Lead

The Lead owns the live solution trajectory. It has broad tools, direct artifact
access and continuity across moves. It may do several tool/model turns inside a
move. The harness asks it for a durable update at meaningful boundaries, not
after every thought.

The Lead receives:

1. the exact objective;
2. the current workspace map;
3. direct paths or handles to current artifacts and raw observations;
4. the last state delta and operator steering;
5. the real remaining hard envelope;
6. available capabilities, tersely described.

Harness instructions should state invariants and capabilities, not prescribe a
performance ritual.

### Navigator

A Navigator is a fresh-context meta-cognitive move. It cannot modify the
artifact or stop the run. It examines the objective, compact trajectory history,
current workspace and the most decision-relevant raw evidence to ask:

- Are we solving the right representation of the original task?
- What has actually improved?
- What are we repeatedly assuming or retrying?
- Is the current search topology appropriate?
- What high-value alternative, simplification or test is missing?
- Is compute following the strongest remaining opportunity?

Its output is an observation consumed by the Lead. It is triggered by evidence,
not a fixed meeting schedule:

- initial orientation;
- repeated low-information moves;
- contradictory evidence;
- a major artifact or strategy transition;
- a large fraction of the hard envelope spent without commensurate progress;
- a completion claim.

### Challenger

A Challenger independently tests a specific claim or candidate against the
objective and artifact itself. It is used when independence has decision value:

- choosing between materially different candidates;
- testing a high-risk assumption;
- diagnosing surprising failure;
- evaluating a completion claim.

A challenge supplies evidence rather than rewriting the objective or choosing
the solution. At an artifact promotion boundary, the controller nevertheless
enforces the direct evidence mechanically: it records the exact disposition and
mints a digest-bound lease only for supported evidence. The Lead remains free to
contest a denial with direct evidence or produce any materially distinct
replacement, but it cannot silently continue from the denied bytes. No critic
can prescribe the repair or force an infinite review phase.

A rejection opens work in the same loop. It never sends the run to a separate
repair world.

## 7. Adaptive search without a permanent swarm

The default search width is one. The Lead can open a trajectory when at least
one of these is true:

- competing hypotheses make different predictions;
- different solution families remain plausible;
- a subproblem is genuinely independent;
- a fresh construction is cheaper than repairing accumulated assumptions;
- a measurable task benefits from population search.

A trajectory is only:

```text
parent workspace version
+ hypothesis or purpose
+ artifact head
+ observations
+ status
```

Trajectories may run sequentially or concurrently. They are compared using
task-native observations, not prose voting. A trajectory that becomes dominated
is archived with its causal lesson. A useful component may be merged even when
the whole branch loses.

The harness never expands because workers are available. It expands because the
current uncertainty has real width.

For problems with cheap objective evaluation, the same mechanism can host beam
search, evolutionary search, simulation, Bayesian optimization, MCTS or RL. The
algorithm is a move policy supplied by the task or model, not baked into the
universal controller.

## 8. Evaluation is continuous construction feedback

Quality does not live in a final gate. Each consequential construction move
should seek the cheapest observation capable of falsifying its assumption.

Evaluation has four layers, all returning ordinary observations:

1. **Immediate feedback:** parsers, type checks, previews, targeted examples,
   quick renders, local simulations.
2. **Decision probes:** experiments or comparisons aimed at a load-bearing
   uncertainty.
3. **Integration feedback:** whole-artifact behavior after meaningful changes.
4. **Completion challenge:** independent, artifact-native evidence for the exact
   objective.

The Lead may create new instruments when the environment lacks one. Instrument
validity is itself an observation: a successful command is not automatically a
valid measurement.

An evaluator must inspect the artifact or environment it judges. Judging a
model's description of an artifact is insufficient whenever direct inspection
is possible.

## 9. Compute is an envelope, not a turn count

The hard envelope is the only resource authority. It may include:

- wall-clock deadline;
- provider token or monetary ceiling;
- model/tool concurrency;
- machine or external-service limits;
- operator stop.

Top-level calls, rounds, repair attempts and generated action counts are not
hard budgets. They are observability metrics.

The Lead receives the actual remaining envelope and chooses the next move. A
small scheduling policy may prefer cheaper moves when they are sufficient, but
it cannot finalize, block reconstruction or hide hard capacity.

Before starting a move, the runtime checks only that its declared ceiling fits
inside the hard remainder while preserving enough wall time for a local journal
commit. The move itself produces a Workspace update and candidate artifact, so
safe stopping requires no reserved model synthesis call. Once a move starts,
unused capacity remains in one pool and can be spent on whichever next action
has the highest value—including reconstruction after a failed completion
challenge.

Soft checkpoints exist to persist state, update the operator and invite a
Navigator when needed. They are not reasons to stop.

Cadence follows reversibility and information value. Cheap speculative thought
may iterate rapidly with minimal ceremony. Expensive, irreversible or
load-bearing work receives deeper context, an explicit causal bet and earlier
feedback. The harness does not apply one heavyweight protocol to every move.

When a proposed move set is empty while the objective is unsatisfied, the
kernel requests a broader move or Navigator reframe. “I generated no actions”
is evidence of search failure, not evidence of completion.

Exact duplicate moves against the same inputs are idempotently reused. A
near-duplicate is not forbidden, but the Lead must state what changed in the
hypothesis, evidence or method. This turns repetition into an explicit bet
rather than an invisible token sink.

## 10. Context and knowledge

### Within a run

The model gets a map, not a dump:

- a compact workspace at the top;
- a delta since its previous checkpoint;
- an index of artifacts, observations and source material;
- direct tools to open exact content at full fidelity.

Nothing important exists only in hidden session memory. Nothing lossless is
destroyed merely to fit a prompt.

Compaction produces navigation and causal summaries while retaining links to
the underlying material. A summary may guide attention but cannot become
stronger evidence than its sources.

### Across runs

Reusable memory is admitted only when it could change a future decision. One
memory item records:

- situation and scope;
- action or hypothesis;
- observed outcome;
- causal lesson;
- provenance;
- invalidation conditions.

Retrieval is based on the current decision, not generic semantic similarity
alone. Contradictions coexist until evidence resolves their scopes.

Cross-run memory is an optional capability. A run remains correct without it.

### Growing capability

Flourite may improve between runs in three ways:

1. **Experience:** scoped causal lessons become retrievable memories.
2. **Instruments:** useful evaluators, probes, transformations and environment
   builders become reusable capabilities with evidence about their validity.
3. **Policy candidates:** repeated outcome patterns may propose changes to
   context assembly, navigation triggers or move policy.

The first two can be admitted when provenance and scope are clear. A policy or
code change is never promoted merely because a model proposed it. It is tested
against held-out tasks and the accumulated failure corpus, then promoted,
canaried or rejected as an ordinary versioned artifact. This creates real
learning without allowing a noisy run to rewrite the harness's governing logic.

## 11. Honest termination

There are exactly five terminal states:

1. **satisfied** — a completion claim is supported by task-appropriate evidence;
2. **exhausted** — the hard compute envelope ended before satisfaction;
3. **blocked** — a specific external dependency prevents meaningful progress;
4. **stopped** — the operator stopped the run;
5. **failed** — the runtime cannot safely continue or reconstruct.

Only `satisfied` means the task is complete.

A completion claim includes:

- exact artifact head or answer;
- claims about how it satisfies the objective;
- direct evidence references;
- known residual uncertainty.

The completion challenge may support, challenge or report uncertainty. The
completion decision reconciles explicit hard checks, the Lead's claim, direct
artifact evidence and independent findings. Material contradictions block
`satisfied` until resolved or explicitly accepted by the operator. Rejection
returns findings to the Workspace and work continues while the hard envelope
allows it. A scheduler, empty queue, round cap or repair limit cannot create a
terminal state.

## 12. Durable execution

The cognitive loop is flexible; side effects are not.

The durability plane enforces:

- append-only, versioned events;
- content-addressed artifacts and raw outputs;
- idempotency keys for every external or model action;
- a durable move intent before external work;
- one atomic semantic result after external work;
- artifact-digest binding for every scoped observation;
- deterministic replay into one derived run state;
- crash recovery that resumes or safely retries unfinished moves;
- explicit compatibility boundaries for event/schema evolution.

The minimal event vocabulary is:

```text
run.started
steering.received
move.proposed
move.started
move.applied                 # evidence + artifacts + workspace + continuation
run.satisfied | run.exhausted | run.blocked | run.stopped | run.failed
```

Provider-specific traces and UI activity may be stored, but they do not alter
semantic state.

Concurrency uses optimistic lineage rather than shared mutation. Every Move is
based on a specific Workspace version and trajectory. Parallel results create
branch observations or artifact heads; only a later Workspace commit integrates
them. A compare-and-swap on the parent version prevents one worker from erasing
another worker's evidence.

The derived run state needs only:

- objective reference and amendments;
- status;
- current workspace version;
- artifact heads and trajectory lineage;
- observation index;
- active move/lease;
- measured resource usage;
- last event sequence.

### Transition legality

```text
run.started
    -> active

active + move.proposed
    -> move.started
    -> external work in an isolated capsule
    -> move.applied          # one atomic semantic commit
    -> active

active + finish claim inside move.applied
    -> challenge move(s)
    -> supported and contradiction-free -> satisfied
    -> challenged or uncertain          -> workspace.committed -> active

active -> paused -> active
active -> exhausted | blocked | stopped | failed
```

Events after a terminal state are invalid. `move.applied` records success or
failure and the exact observations, artifacts, workspace, trajectory forks and
continuation together. This prevents a crash from exposing a half-committed
artifact or workspace. A failed move records its failure as an Observation and
returns to `active` if recovery is possible; runtime failure is reserved for
loss of safe continuation, not task-level disappointment.

## 13. Operator experience

The operator sees the live shape of the run, not internal ceremony:

- current strategy and artifact;
- what changed recently;
- active move and why it matters;
- live trajectories and comparisons;
- strongest evidence and unresolved risk;
- actual hard-envelope consumption;
- completion state.

Controls are pause, resume, stop and steer. Steering becomes an immutable
observation and is visible to the Lead at the next safe boundary. A crash or
reconnect must not discard it.

## 14. Implemented shape

The executable system contains only:

```text
Objective
WorkspaceVersion
Move / FinishClaim
Observation
ArtifactVersion / Trajectory
RunState

Journal + reducer
Context assembler
Lead runner
Move executor
Observer/challenger runner
Kernel loop
```

The source shape is:

```text
core/types.py        canonical data contracts
core/reducer.py      event -> RunState, and nothing else
core/kernel.py       the one transition loop
intelligence/        context · model runner · result compiler
runtime/             lifecycle · component leases · workers · commands · recovery
providers/           OMP transport · safe events · usage accounting
adapters/            task-native artifacts · checks · explicit apply
cli.py + live.py     commands and disposable operator projection
```

Dependencies point inward. The core never imports a provider, adapter, CLI, or
named evaluation strategy. There is one semantic controller and one durable
state model. A non-semantic supervisor may replace the controller's code between
activities, but it cannot make or apply a semantic decision itself.

## 16. Discriminative acceptance tests

The redesign is not accepted because its classes are smaller. It must survive
the failures that killed the old architecture.

### State and recovery

- Crash before and after every event append and external side effect; replay
  yields an equivalent state and at-most-once semantic result.
- A stale observation bound to an old artifact cannot certify the new artifact.
- A provider session can disappear and be reconstructed from durable state.
- Operator steering survives pause, crash and resume exactly once.
- Two parallel moves based on one Workspace cannot overwrite each other; both
  remain available to the integrating transition.

### Intelligence flow

- An empty proposed move set with an unsatisfied objective triggers broadening,
  not completion.
- Repeated low-information moves trigger a fresh Navigator observation.
- Contradictory evidence weakens or changes the workspace interpretation rather
  than being silently reconciled away.
- A useful rejected branch can contribute a component without replacing the
  winning artifact.
- The Lead can choose one trajectory, several trajectories or a task-specific
  search algorithm without controller changes.
- Retrying an equivalent move without new evidence is visible and does not
  silently spend the envelope twice.

### Evaluation and completion

- A completion challenger that finds an upstream design failure reopens normal
  construction with the remaining hard envelope.
- A superficially valid artifact cannot reach `satisfied` without evidence for
  the exact objective.
- Exhaustion and operational failure never render as successful completion.
- The exact sparse-video Luna failure continues after rejection instead of
  stopping with most budget unused.

### Generality

- The same kernel runs a code change, a research synthesis and a creative
  artifact with only adapter/capability changes.
- Objective evaluation can be deterministic, model-based, environmental or a
  composition without changing termination semantics.

### Learning

- A causal lesson is retrieved only within a compatible scope and exposes its
  original evidence and invalidation condition.
- A misleading lesson from one run cannot silently become a universal rule.
- A proposed harness-policy improvement cannot self-promote; it must beat the
  current version on held-out evidence and known failure cases.

## 17. The governing laws

These are the only rules important enough to constrain every run:

1. The objective is immutable except for explicit user amendment.
2. Raw evidence and artifact history are never replaced by interpretation.
3. Every consequential observation is bound to what it actually observed.
4. All findings return through one learning loop.
5. Search topology follows the problem, not the available workers.
6. Soft policy may allocate attention; only the hard envelope limits compute.
7. No subsystem may infer task completion from its own emptiness or limits.
8. Rejection is information, not a terminal phase.
9. Only evidenced objective satisfaction is completion.
10. If a mechanism cannot improve, protect or explain the final result, it does
   not belong in the cognitive core.

That is Flourite's intended shape: a thin durable shell around a strong model,
an adaptive search field, and a fast unbroken path from reality back into the
next act of intelligence.

## Appendix A. Canonical contracts

These are implementation contracts, not a semantic ontology for the task.

```text
Objective
  objective_id
  original_text_blob
  amendments[]              # explicit user changes only
  envelope                  # actual hard resource boundaries
  created_at

WorkspaceVersion
  workspace_id
  parent_workspace_id?
  based_on_event_seq
  document_blob             # compact model-authored decision map
  summary
  artifact_heads[]
  active_trajectory_ids[]
  consumed_observation_ids[]
  created_by_move_id?

ArtifactVersion
  artifact_id
  content_ref               # blob or adapter-owned snapshot
  digest
  parent_artifact_ids[]
  trajectory_id
  created_by_move_id
  deliverables[]
  metadata

Trajectory
  trajectory_id
  parent_trajectory_id?
  purpose
  base_workspace_id
  artifact_head_id?
  status                    # active, merged, archived, failed

Move
  move_id
  based_on_workspace_id
  trajectory_id
  mode                      # lead, navigate, challenge, environment
  intent
  instructions
  input_refs[]
  declared_ceiling
  idempotency_key
  status

Observation
  observation_id
  move_id?
  trajectory_id?
  artifact_digest?
  kind
  summary
  raw_ref?
  source                    # model, tool, environment, operator
  confidence?
  created_at

FinishClaim
  claim_id
  workspace_id
  artifact_head_ids[]
  satisfaction_claims[]
  evidence_refs[]
  residual_uncertainty[]

RunState
  run_id
  objective
  status
  workspace
  artifacts{}
  trajectories{}
  observations{}
  active_moves{}
  finish_claim?
  measured_usage
  last_event_seq
```

The Workspace document is intentionally expressive. The small typed shell
protects lineage, provenance, resource truth and state transitions; it does not
attempt to encode every concept a frontier model may need to invent.
