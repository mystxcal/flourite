> Historical foundation: this is the original sparse design retained for provenance. The executable release is specified by `V3_5_ARCHITECTURE.md`.

# Flourite's reference design

I would materially simplify the previous architecture.

The strongest general-purpose harness is **not** two large populations of solutions and evaluators, a permanent society of agents, or a continuously self-rewriting meta-system.

It is:

> **One current best artifact, a small elastic frontier of unresolved decisions and candidate deltas, a small adaptive portfolio of targeted probes, and an immutable evidence ledger.**

Everything else—agents, critics, branches, tools, evaluators, experiments, retrieval, even planning—is activated temporarily when it is the cheapest credible way to improve the final artifact.

The governing principle is:

> **Spend compute only on uncertainty that can materially change the final result.**

That gives both high final quality and high intelligence density per token.

---

## 1. The real optimization objective

Do **not** optimize a raw “quality per token” ratio. That can rationally prefer a cheap mediocre answer over a substantially better answer.

Use a lexicographic objective:

1. Satisfy hard constraints and avoid unacceptable failure.
2. Reach the appropriate quality/reliability level for the task’s stakes.
3. Within that envelope, minimize total effective cost.
4. Continue beyond the quality floor only while marginal expected improvement remains worth its cost.

At the action level, the useful quantity is approximately:

# [ \operatorname{EVC}(a)

\frac{
\mathbb{E}[\text{reduction in final decision regret}]
\+
\text{reusable knowledge value}
}{
\text{effective cost}
}.
]

“Effective cost” is not merely generated tokens:

# [ C\_{\mathrm{eff}}

C\_{\mathrm{input}}
\+
C\_{\mathrm{output}}
\+
C\_{\mathrm{duplicated\ context}}
\+
C\_{\mathrm{tool}}
\+
C\_{\mathrm{integration}}
\+
C\_{\mathrm{verification}}
\+
C\_{\mathrm{expected\ repair}}
\+
C\_{\mathrm{latency/risk}}.
]

This is why one strong model call can be cheaper than five weak calls whose outputs must be reconciled, repaired, and reverified. A cheapest-sufficient router should optimize total accepted-result cost, not nominal token price. ([GitHub](https://raw.githubusercontent.com/sjarmak/engineering-reliable-coding-agents/main/editing/engineering-reliable-coding-agents.md "https://raw.githubusercontent.com/sjarmak/engineering-reliable-coding-agents/main/editing/engineering-reliable-coding-agents.md"))

The controller should not pretend it can precisely estimate all these terms. Use broad ordinal judgments—fatal/high/medium/low impact; cheap/moderate/expensive cost—and Pareto filtering rather than a fragile weighted scoring equation.

Two questions eliminate most waste:

1. **Could the result of this action change a load-bearing decision?**
2. **Is this the cheapest sufficiently independent way to learn that?**

---

# 2. The six cognitive primitives

The universal core only needs six meaningful objects.

| PrimitiveMeaning     |                                                                                                                 |
| -------------------- | --------------------------------------------------------------------------------------------------------------- |
| **Goal Contract**    | The original request, deliverable, hard constraints, soft objectives, stakes, budget and known user preferences |
| **Working Artifact** | The current best integrated answer, proof, design, codebase, recommendation, report or plan                     |
| **Issue**            | A load-bearing unresolved claim, decision, trade-off, uncertainty or possible failure                           |
| **Candidate Delta**  | A proposed modification to the artifact or to one issue—not necessarily an entirely separate answer             |
| **Probe**            | A test, research action, critique, experiment or observation intended to resolve a specific issue               |
| **Evidence Event**   | An immutable result with provenance, scope, cost, version and links to the issues it updates                    |

The **frontier** is simply the small active set of issues, candidate deltas and probes worth spending attention on now.

This has several important consequences:

- An **agent is not a primitive**. It is an ephemeral executor of an action.
- An **evaluator is not a primitive**. Evaluation is an adaptive portfolio of probes.
- A **population is not a primitive**. It is a temporary beam of candidate deltas when multiple alternatives remain plausible.
- **Memory is not a primitive**. It is a collection of derived views over the evidence ledger.
- A **planner is not a permanent agent**. Planning is a batched controller operation at meaningful checkpoints.
- A **critic is not a permanent persona**. Critique is one probe type.

This removes most multi-agent bureaucracy without giving up any of its useful capabilities.

---

# 3. The architecture

```
                       IMMUTABLE USER GOAL
                               │
                               ▼
                    ┌─────────────────────┐
                    │ GOAL CONTRACT       │
                    │                     │
                    │ exact request       │
                    │ constraints         │
                    │ objectives/stakes   │
                    │ budget              │
                    └──────────┬──────────┘
                               │
                    orient + solve once
                               │
                               ▼
              ┌────────────────────────────────┐
              │ CURRENT WORKING STATE          │
              │                                │
              │ best artifact                  │
              │ soft decision/claim graph      │
              │ active frontier                │
              │ budget and uncertainty state   │
              └──────────────┬─────────────────┘
                             │
                   batched frontier controller
                             │
       ┌─────────────────────┼──────────────────────┐
       ▼                     ▼                      ▼
  SOLUTION ACTION       EVIDENCE ACTION       CONTROL ACTION

  propose delta         retrieve evidence     integrate
  explore alternative   run experiment        reframe problem
  combine branches      test/counterexample   expand hypothesis class
  repair artifact       targeted critique     improve probe portfolio
                        ask user if high-EV    stop
       │                     │                      │
       └─────────────────────┼──────────────────────┘
                             ▼
                 ISOLATED WORKERS AND TOOLS
                             │
                             ▼
              ┌────────────────────────────────┐
              │ IMMUTABLE EVIDENCE LEDGER      │
              │                                │
              │ raw tool outputs               │
              │ source snapshots               │
              │ artifacts and patches          │
              │ probe findings                 │
              │ failed attempts                │
              │ costs and timings              │
              │ decision predictions           │
              └──────────────┬─────────────────┘
                             │
                    deterministic state reducer
                             │
                             └──────────► repeat


             SLOW, CROSS-TASK META-LOOP ONLY

       recurring failure clusters → component experiments
       → shadow evaluation → matched-budget comparison
       → canary → promotion or rollback
```

The runtime should be event-sourced: the ledger is authoritative, while the working state is reconstructible. That matters because later confidence cannot recover evidence that was silently lost upstream. ([GitHub](https://raw.githubusercontent.com/sjarmak/engineering-reliable-coding-agents/main/editing/engineering-reliable-coding-agents.md "https://raw.githubusercontent.com/sjarmak/engineering-reliable-coding-agents/main/editing/engineering-reliable-coding-agents.md"))

---

# 4. Start with one strong baseline, not a swarm

Every run begins with:

1. A concise orientation pass.
2. One credible end-to-end attempt.
3. A diagnosis of what could materially make that attempt wrong or substantially better.

The first attempt is valuable even when incomplete. It reveals:

- whether the task is actually hard;
- which parts are load-bearing;
- what information is missing;
- whether branching is useful;
- what kinds of evaluation are possible;
- where a stronger model or tool would pay off.

The default active beam is therefore **one artifact**.

Expand to two or a few alternatives only when:

- genuinely different solution families remain plausible;
- the alternatives make meaningfully different predictions;
- the task contains independently solvable high-value facets;
- the current approach repeatedly fails without explaining why;
- exploration has a credible chance of producing a discontinuous improvement.

Collapse the beam again when alternatives become redundant or dominated.

This follows a simple economic rule: fan-out must beat the live single-agent path after integration and evaluation costs, not merely produce more text. The reliability literature explicitly recommends retaining a live single-agent control when evaluating debate, delegation or parallel workers. ([GitHub](https://raw.githubusercontent.com/sjarmak/engineering-reliable-coding-agents/main/editing/engineering-reliable-coding-agents.md "https://raw.githubusercontent.com/sjarmak/engineering-reliable-coding-agents/main/editing/engineering-reliable-coding-agents.md"))

### Scale by frontier width, not available agent count

If there are two independent high-value uncertainties, perhaps run two workers.

If there is one tightly coupled conceptual bottleneck, assigning ten agents usually produces ten correlated essays and a costly synthesis problem.

Concurrency should be bounded by the number of **distinct, actionable frontier items**, not by infrastructure capacity.

---

# 5. The decision graph must remain soft

A rigid decomposition can destroy exactly the holistic insights needed for open-ended work.

The harness should therefore maintain a **soft issue graph**, not a formal ontology that every thought must fit into.

An issue might be:

- “Is the proposed theorem actually strong enough for the endpoint?”
- “Could a simpler architecture dominate this design?”
- “Does the recommendation depend on a possibly outdated price?”
- “Are these two system requirements fundamentally in tension?”
- “The overall aesthetic still feels generic despite local polish.”
- “All current approaches assume the same framing; perhaps the framing is wrong.”

The graph should initially contain only a handful of load-bearing issues. It is allowed to be incomplete, merged, split, revised or abandoned.

Most importantly, the original prompt and full working artifact remain available. The graph is an attention aid, not a replacement for understanding.

A worker can return a **frame-break event**:

> The current issue decomposition is missing a more fundamental variable.

That triggers problem-model evolution rather than forcing the discovery into the old structure.

This generalizes MDA’s open-world mechanism: when predictive residuals show that the current hypothesis class is inadequate, MDA expands it instead of endlessly tuning the wrong candidates. It also prunes near-duplicate hypotheses when evidence concentrates. ([arXiv](https://arxiv.org/html/2608.09696 "https://arxiv.org/html/2608.09696"))

The general harness should be open along three dimensions:

- **Solution-open:** current solutions may all be from the wrong family.
- **Evaluation-open:** current probes may be blind to the important difference.
- **Problem-open:** the current interpretation or decomposition may be missing a decisive facet.

The harness itself should not normally become open during the same run; that belongs to the slower meta-loop.

---

# 6. Search over deltas, not entire answers

Large populations of complete answers are token-expensive and discard shared structure.

Most useful alternatives differ at only a few important decisions. Represent them as **candidate deltas**:

```
Target:
    issue 7 / architecture section / lemma 3

Proposed change:
    replace centralized scheduler with work-stealing frontier queues

Expected benefit:
    removes global synchronization bottleneck

Dependencies:
    requires idempotent tasks and lease-based ownership

Risk:
    may weaken deterministic prioritization

Evidence:
    refs E104, E119
```

A delta can be:

- a new claim;
- a changed assumption;
- a proof lemma;
- a design decision;
- a code patch;
- a new recommendation;
- a changed ranking;
- a deleted section;
- a reframing of the problem.

Multiple deltas can share the same artifact base. This provides population-search benefits without repeatedly generating and storing full copies.

For prose and research artifacts, deltas should operate on semantic sections or claim nodes rather than exact text offsets. For code, ordinary version-control patches are appropriate. For proofs, deltas can target lemmas and dependencies.

When accumulated patches make the artifact locally polished but globally incoherent, perform a **clean synthesis checkpoint**: rebuild the artifact from the Goal Contract, accepted decisions and supporting evidence rather than patching it indefinitely.

---

# 7. Evaluation should be an evolving probe portfolio

This is the largest improvement over the earlier “evaluator population” design.

A monolithic evaluator tends to become:

- expensive;
- opaque;
- gameable;
- overgeneralized;
- correlated with the solver;
- tempted to reduce everything to one score.

Instead, evaluation is a portfolio of narrow probes.

A probe records:

| FieldPurpose           |                                                                                                 |
| ---------------------- | ----------------------------------------------------------------------------------------------- |
| **Target**             | The exact issue, claim, candidate difference or failure mode                                    |
| **Method**             | Test, source check, counterexample, simulation, critique, comparison, user query, etc.          |
| **Predicted outcomes** | What each possible result would imply before the probe runs                                     |
| **Scope**              | What the probe can legitimately establish                                                       |
| **Blind spots**        | What it does not test                                                                           |
| **Independence class** | Same-model judgment, other model, executable tool, external evidence, human, real-world outcome |
| **Cost**               | Tokens, tools, latency and integration burden                                                   |
| **Finding**            | Evidence-backed result rather than merely a scalar score                                        |

### Useful probe classes

- **Hard invariant check:** Does the solution violate a literal constraint?
- **Executable test:** Does the system exhibit the intended behavior?
- **Counterexample search:** Can the central claim be broken?
- **Source-grounding check:** Is a load-bearing factual claim supported and current?
- **Discriminating experiment:** Where do competing hypotheses predict different outcomes?
- **Metamorphic test:** Does an expected transformation preserve or change behavior appropriately?
- **Adversarial scenario:** What happens near a boundary or under hostile conditions?
- **Pairwise facet comparison:** Which candidate better satisfies a specific subjective criterion?
- **Fresh-reader challenge:** Is there a fatal omission or incoherence invisible to the construction process?
- **User preference query:** Which unresolved preference would actually change the recommendation?

MDA’s most transferable evaluation idea is precisely this: the observation method is itself chosen according to discriminating information per unit cost, with a cheap approximation used ordinarily and an expensive assumption-light anchor invoked when disagreement exposes a possible blind spot. ([arXiv](https://arxiv.org/html/2608.09696 "https://arxiv.org/html/2608.09696"))

### Evaluator evolution becomes probe evolution

The harness does not continually rewrite a universal judge prompt.

Instead:

1. Current probes fail to separate plausible candidates or miss an observed defect.
2. The controller identifies the blind spot.
3. It proposes a targeted new probe or a better observation method.
4. The probe is calibrated against the case that exposed the failure.
5. It remains scoped to the failure class until broader evidence supports generalization.

This is cheaper, more attributable and less likely to corrupt unrelated evaluation.

---

# 8. No exhaustive candidate × evaluator matrix

Suppose there are four candidate branches and eight probes. Running all 32 combinations is usually wasteful.

Use a sparse evaluation graph:

- Apply a probe first to the current best candidate and the nearest credible challenger.
- Apply it only where its target issue is relevant.
- Stop using it when it does not discriminate and exposes no failure.
- Reuse a probe across more candidates only if the initial result suggests it is decision-relevant.
- Escalate to an expensive anchor only when cheaper probes disagree, a high-stakes claim remains exposed, or a surprising improvement could reflect evaluator gaming.

A probe is valuable because it can change a decision, not because it adds another checkmark.

Two critiques from the same model, using similar prompts and the same evidence, should receive a large **correlation discount**. They are not two independent confirmations.

---

# 9. Avoid verification hell through a risk-triggered ladder

Verification has no fixed quota. It must justify its own cost.

Use the following escalation ladder:

### Tier 0: construction-time sanity

The solving worker checks obvious constraints and contradictions while producing the artifact. No separate agent.

### Tier 1: cheap targeted probe

Run a deterministic check, source consistency check, counterexample attempt or narrow critic against a specific high-impact issue.

### Tier 2: independent evidence

Use a different evidence channel: an executable tool, external source, simulation, alternate method or genuinely differently conditioned model.

### Tier 3: expensive anchor

Invoke formal verification, extensive testing, real-world measurement, a costly evaluator or human review only when the issue’s impact and uncertainty justify it.

### Tier 4: user authority

Ask the user only when the unresolved variable is genuinely preference-dependent or authority-dependent and the answer would materially change the result.

An improving proxy is only evidence that quality may be improving; it cannot be allowed to define and approve success by itself. Hard invariants, outcome measures, diagnostic scores and human-owned thresholds should play distinct roles. ([GitHub](https://raw.githubusercontent.com/sjarmak/engineering-reliable-coding-agents/main/editing/engineering-reliable-coding-agents.md "https://raw.githubusercontent.com/sjarmak/engineering-reliable-coding-agents/main/editing/engineering-reliable-coding-agents.md"))

AHE’s component ablations give a concrete warning against layering redundant “discipline”: several individually useful components all pushed closure-style verification, and their combination spent extra turns rechecking the same things while hurting harder tasks. ([arXiv](https://arxiv.org/html/2604.25850v3 "https://arxiv.org/html/2604.25850v3"))

### One owner per failure mode

Do not put the same invariant into:

- the system prompt;
- worker instructions;
- middleware;
- a critic;
- a final verifier;
- a release hook.

Assign each important invariant one primary owner and, where warranted, one independent audit path. This prevents five layers from repeatedly checking the same condition.

---

# 10. Escape byte-matching hell

Use exact equality only where exact bytes are actually part of the contract:

- protocol fields;
- cryptographic values;
- canonical serialization;
- required filenames;
- API payloads;
- syntax;
- literal user-provided text that must be preserved.

For everything else, evaluate the property the artifact is supposed to possess:

| ArtifactCorrect evaluation level |                                                                                               |
| -------------------------------- | --------------------------------------------------------------------------------------------- |
| Code                             | Observable behavior, invariants, tests, side effects, performance envelope                    |
| Architecture                     | Requirements, failure modes, trade-offs, simulations and operational consequences             |
| Research answer                  | Source grounding, decision sensitivity, factual currency, coverage and reasoning              |
| Mathematical proof               | Logical dependencies, formalizable subclaims, boundary cases and counterexamples              |
| Recommendation                   | Constraint satisfaction, robustness to assumptions, current availability and expected utility |
| Creative/design work             | Coherence, intended experience, specific preference dimensions and pairwise comparison        |
| Strategy                         | Scenario performance, reversibility, downside exposure and option value                       |

Even recovery testing should compare the deterministic state and effects promised by the contract, not assume whole-run byte identity. The engineering reliability framework recommends stating the observable recovery claim before injecting failure. ([GitHub](https://raw.githubusercontent.com/sjarmak/engineering-reliable-coding-agents/main/editing/engineering-reliable-coding-agents.md "https://raw.githubusercontent.com/sjarmak/engineering-reliable-coding-agents/main/editing/engineering-reliable-coding-agents.md"))

### Keep interfaces typed but tiny

Do not force every worker into a giant brittle JSON schema.

Use a minimal envelope:

```
target
result_or_artifact_reference
findings
evidence_references
unresolved_risks
```

The payload inside may be natural language, code, equations, a patch, a table or a file.

Parsing policy:

1. Parse the small envelope.
2. Repair malformed structure once.
3. If repair fails, preserve the raw result and let a cheap normalizer extract the fields.
4. Never spend repeated frontier-model calls fighting cosmetic schema errors.

The principle is:

> **Semantic freedom inside; strictness only at boundaries where strictness has operational meaning.**

---

# 11. The controller runs in batches, not after every thought

A constant LLM manager watching every tool call wastes tokens and creates another failure surface.

The deterministic runtime should handle:

- budgets;
- queues;
- leases;
- retries;
- cancellation;
- artifact versions;
- dependency readiness;
- tool permissions;
- duplicate detection;
- completion events.

Invoke the semantic frontier controller only at meaningful epochs:

- after the baseline;
- after a batch of evidence arrives;
- when a contradiction or surprising residual appears;
- when the budget crosses a milestone;
- before final synthesis.

At each epoch, it produces a small slate of possible actions:

- **Exploit:** improve the current artifact.
- **Explore:** try an orthogonal candidate delta.
- **Discriminate:** run a probe separating plausible alternatives.
- **Acquire:** gather missing external evidence.
- **Repair:** fix the problem model or probe portfolio.
- **Integrate:** merge accepted discoveries.
- **Stop:** further work has insufficient expected value.

The scheduler removes dominated, duplicate and strongly correlated actions, then chooses a small batch.

### Do not optimize one noisy scalar

Use a simple decision order:

1. Fatal constraints and blockers.
2. High-impact uncertainties likely to change the result.
3. Cheap discriminators among credible alternatives.
4. Reusable tools or evidence with multiple downstream uses.
5. Lower-impact refinements.

This is substantially less fragile than tuning coefficients for novelty, coverage, score, confidence, risk and cost.

---

# 12. Context architecture: ledger, views and capsules

There should be three distinct information layers.

## A. Immutable ledger

Contains raw:

- prompts and contract versions;
- tool inputs and outputs;
- source snapshots;
- artifacts;
- patches;
- probe results;
- failures;
- costs;
- model/configuration versions;
- action predictions and outcomes.

## B. Derived views

Rebuildable from the ledger:

- current issue graph;
- candidate lineage;
- accepted evidence;
- failure corpus;
- probe calibration;
- summaries;
- reusable methods;
- user-preference model;
- retrieval indexes.

## C. Per-action context capsules

Each worker receives only:

1. The compact Goal Contract.
2. Its exact assignment.
3. The relevant artifact slice.
4. The targeted issues and evidence.
5. Its budget and stop condition.
6. The minimal output envelope.

Workers should not receive the entire conversation, all prior branches or the whole Experience Bank.

Meta-Harness provides evidence that keeping full source, traces and scores available for selective inspection can outperform aggressively precompressed histories; the key is that the agent can inspect what it needs rather than carrying everything in every prompt. ([arXiv](https://arxiv.org/html/2603.28052v1 "https://arxiv.org/html/2603.28052v1"))

ToFu’s context design supports the complementary operational pattern: externalize large tool outputs behind recoverable references, deterministically compact cold history before paying for semantic summarization, preserve recent and source-sensitive content, and retrieve persistent memory on demand instead of injecting it universally. ([arXiv](https://arxiv.org/html/2607.11423 "https://arxiv.org/html/2607.11423"))

### Signal admission rule

A result enters the **active** working state only when it does at least one of the following:

- changes an issue or decision;
- supports or refutes a load-bearing claim;
- exposes a new failure mode;
- improves probe calibration;
- creates a reusable tool or method;
- narrows a future retrieval or experiment.

Everything else may remain in the raw ledger without consuming active context.

Negative results are retained, but with their scope:

> “Approach X failed under assumptions A and B for reason C.”

Not:

> “Approach X is bad.”

---

# 13. Token-efficient model routing

Use expensive intelligence at bottlenecks, not everywhere.

### Strong model calls

Best spent on:

- initial orientation;
- difficult hypothesis generation;
- problem reframing;
- deep integration across facets;
- the final clean synthesis;
- rare high-stakes critiques where weaker methods are inadequate.

### Cheaper model calls

Best for:

- extraction;
- classification;
- retrieval-query generation;
- deduplication;
- simple comparisons;
- formatting;
- evidence normalization;
- mechanical issue updates.

### Deterministic tools

Best for:

- calculations;
- code execution;
- parsing;
- exact constraints;
- data transformations;
- test execution;
- source/version checks.

The router should learn from **total accepted-result cost**. A cheap worker that causes repeated repair is not cheap. A strong worker that solves a bottleneck in one pass may be the economical choice. ([GitHub](https://raw.githubusercontent.com/sjarmak/engineering-reliable-coding-agents/main/editing/engineering-reliable-coding-agents.md "https://raw.githubusercontent.com/sjarmak/engineering-reliable-coding-agents/main/editing/engineering-reliable-coding-agents.md"))

A practical pattern is:

```
strong orientation
        ↓
mixed cheap tools/workers for evidence and local deltas
        ↓
strong synthesis
        ↓
one bounded release challenge when justified
```

Protect the completion path before opening branches, but derive it from the
actual remaining obligations: clean synthesis, a release challenge when
applicable, and one repair plus fresh challenge while material risk remains.
Do not reserve a fixed fraction of an unrelated hard envelope.

---

# 14. Communication is through evidence, not agent chatter

Workers do not maintain free-form conversations with one another.

They communicate through:

- artifact versions;
- issue updates;
- evidence references;
- candidate deltas;
- concise findings.

This creates a blackboard-like system without accumulating social chatter, duplicated summaries and contradictory private world models.

Persistent researchers are justified only when continuity itself creates value—for example, a long mathematical branch, a complicated codebase investigation or a multi-day scientific hypothesis. Otherwise workers should be fungible and short-lived.

An agent identity should exist for operational ownership, not as a simulated personality.

---

# 15. Rate-separate the different kinds of evolution

The system should evolve at three different speeds.

## Fast loop: solution evolution

Runs every useful batch.

- propose deltas;
- acquire evidence;
- integrate;
- abandon dominated branches.

## Conditional loop: evaluation and problem-model evolution

Runs only when triggered by:

- evaluator disagreement;
- surprising residuals;
- uncovered critical facets;
- suspicious proxy improvement;
- repeated failures shared by all candidates;
- inability of current probes to distinguish alternatives.

This loop adds or repairs targeted probes and may reframe the issue graph.

## Slow loop: harness evolution

Runs across a corpus of tasks, not reflexively during each task.

It may change:

- tools;
- middleware;
- context policy;
- model routing;
- memory representations;
- scheduling;
- domain adapters;
- default prompts;
- recovery behavior.

This separation prevents a bad task-local judgment from rewriting the machinery that handles every future task.

AHE demonstrates that component-level harness changes can improve performance and that tools, memory and middleware may carry more transferable value than system-prompt rewriting. But its own ablations show non-additive interactions and redundant verification costs. ([arXiv](https://arxiv.org/html/2604.25850v3 "https://arxiv.org/html/2604.25850v3"))

More recent matched-budget experiments found that automatic harness evolution did not consistently beat straightforward parallel sampling or sequential refinement and generalized poorly when evolution and evaluation tasks were disjoint. That is strong evidence for making harness evolution a slow, evidence-heavy option rather than the default inner loop. ([arXiv](https://arxiv.org/html/2607.12227 "https://arxiv.org/html/2607.12227"))

---

# 16. How global harness changes are promoted

A persistent harness change must pass a stricter process than a task-local solution delta:

1. A recurring failure cluster is identified across multiple tasks.
2. The smallest component capable of interrupting the causal path is changed.
3. The change declares:
   - expected fixes;
   - likely affected task classes;
   - possible regressions;
   - expected cost effect.
4. It runs in shadow mode on:
   - representative tasks;
   - hard tails;
   - out-of-domain tasks;
   - fault cases.
5. It is compared under matched budget against:
   - the current harness;
   - a simpler test-time scaling baseline;
   - the direct single-agent path.
6. It receives component and interaction ablations.
7. It is canaried and remains instantly reversible.
8. It is promoted globally only when the benefit is genuinely task-general.

Task-specific improvements remain in domain adapters or local memories rather than contaminating the universal core.

This yields a stable core with evolvable leaves:

```
                         UNIVERSAL CORE
          contract · frontier · ledger · scheduler · runtime
                                  │
             ┌────────────────────┼────────────────────┐
             ▼                    ▼                    ▼
         research              software              formal
          adapter               adapter               adapter
             │                    │                    │
      domain probes       repo/test operations   proof/counterexample
      source policies     artifact patching      symbolic tools
```

---

# 17. Thin domain adapters preserve generality

The universal controller does not need separate hard-coded workflows for every problem. A domain adapter supplies only:

- artifact representation and merge operations;
- relevant probe types;
- available tools;
- hard invariants;
- appropriate independent anchors.

| DomainArtifactTypical issuesUseful probes |                           |                                             |                                                                  |
| ----------------------------------------- | ------------------------- | ------------------------------------------- | ---------------------------------------------------------------- |
| Mathematics                               | Proof or construction     | missing lemma, endpoint, hidden assumption  | counterexample search, symbolic check, proof assistant           |
| Engineering                               | Architecture/design       | requirements, failure mode, trade-off       | simulation, fault injection, scenario analysis                   |
| Software                                  | Repository state          | defect, behavior, integration               | tests, static analysis, runtime state                            |
| Research                                  | Explanation/model         | mechanism, evidence, alternative hypothesis | source search, experiment, discriminating prediction             |
| Buying/strategy                           | Ranked decision           | price, reliability, scenario sensitivity    | current-market lookup, sensitivity analysis, pairwise comparison |
| Creative/design                           | Coherent concept/artifact | taste, experience, originality, consistency | facet critique, preference comparison, holistic fresh review     |

The same core loop works because each domain still contains:

> goal → artifact → unresolved issues → candidate changes → informative probes → evidence → integration.

---

# 18. The complete control loop

```
contract = orient(original_prompt)
artifact = produce_one_credible_baseline(contract)

ledger.append(contract, artifact)

frontier = identify_load_bearing_issues(
    contract,
    artifact,
    limit_to_decision_sensitive=True
)

while budget_remains:

    if no_high_impact_open_issue(frontier):
        break

    actions = propose_action_slate(
        exploit=True,
        explore_if_distinct=True,
        discriminate=True,
        acquire=True,
        reframe_on_residual=True,
        integrate=True,
        stop=True
    )

    actions = remove_actions_that:
        - cannot change a decision
        - duplicate existing work
        - are dominated by cheaper probes
        - provide highly correlated evidence
        - consume the synthesis reserve

    batch = choose_small_pareto_batch(actions)

    results = execute_asynchronously(
        batch,
        isolated=True,
        context_capsules=True,
        cancellable=True
    )

    ledger.append(results)

    artifact, frontier = reduce_from_ledger()

    if current_solution_class_explains_residuals_poorly:
        expand_or_reframe_solution_space()

    if probes_disagree_or_share_a_blind_spot:
        propose_targeted_probe_or_anchor()

    collapse_redundant_or_dominated_branches()

    if no_action_has_positive_expected_value:
        break

final = clean_synthesis(
    goal_contract=contract,
    accepted_decisions=frontier.resolved,
    evidence=ledger.accepted_evidence,
    working_artifact=artifact
)

final = bounded_release_gate(
    final,
    check_only_load_bearing_risks=True,
    no_repetitive_closure_loops=True
)

return final
```

---

# 19. The release gate

The final artifact does not need every sentence independently verified.

It needs an adequate evidence case:

1. Hard constraints are satisfied.
2. No known fatal or high-impact issue has been silently ignored.
3. Load-bearing factual and technical claims have appropriate evidence.
4. Important competing alternatives have either been tested or explicitly ruled out for a reason.
5. High-stakes novel claims receive at least one sufficiently independent challenge.
6. Remaining uncertainty is disclosed proportionally.
7. The final artifact is rebuilt coherently rather than exposing branch debris.
8. No further action has enough expected decision value to justify its cost.

The final fresh review should be tightly scoped:

> Find fatal errors, major omissions, unsupported load-bearing claims or contradictions with the Goal Contract. Do not perform cosmetic rewriting.

One complete repair-and-rechallenge cycle is protected if the challenger finds
something material. Further repairs are allowed only for distinct material
findings, and each candidate must face fresh checks and a fresh artifact-bound
challenge. An unchanged artifact or repeated diagnosis stops the loop, so the
system cannot turn review into infinite critic–revision theater.

---

# 20. The final shape

The architecture I would commit to is:

```
ONE immutable goal
ONE current best artifact
ONE soft graph of load-bearing issues
A TINY elastic beam of candidate deltas
A TINY adaptive portfolio of targeted probes
ONE immutable evidence ledger
ONE batched frontier controller
EPHEMERAL workers selected by expected value
A SLOW evidence-grounded harness evolution loop
```

Its central engineering laws are:

- **Search decisions, not prose volume.**
- **Evaluate with targeted falsifiers, not universal judge scores.**
- **Scale with unresolved frontier width, not agent count.**
- **Keep raw evidence losslessly; expose it selectively.**
- **Use the strongest model at bottlenecks and the cheapest sufficient method elsewhere.**
- **Treat verification as an action that must earn its cost.**
- **Use exact matching only where exactness is the actual contract.**
- **Reopen the solution, evaluator or problem frame when evidence demands it.**
- **Never let task-local self-feedback casually rewrite the global harness.**
- **Stop when remaining uncertainty is low-value, not when every imaginable check has run.**

That is the key refinement: **not a maximal harness, but a maximally capable sparse harness**. It can become Hyra-like, CORAL-like, MDA-like or Meta-Harness-like when the problem warrants those behaviors, while its ordinary path remains close to one excellent agent making one excellent attempt, followed only by the few actions most likely to change the answer.
