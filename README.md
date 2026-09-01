<p align="center">
  <img src="assets/flourite-banner.gif" alt="Flourite" width="100%">
</p>

Flourite is a durable intelligence harness for problems where one strong model
run gets close, but not close enough.

It does not replace the model with a workflow graph or a cast of agents. A
persistent Lead builds the best answer it can with the full tool plane. Flourite
keeps the objective stable, preserves what the run learns, opens a separate
trajectory only when the problem genuinely branches, and asks a fresh
Challenger to inspect the actual deliverable before accepting completion.

The result is one artifact—not a folder of competing drafts—with a replayable
record of how it got there.

[Quick start](#quick-start) · [How it works](#how-it-works) ·
[Live control](#watch-and-steer) · [Commands](#commands) ·
[Canonical model](docs/CANONICAL_MODEL.md) ·
[Rebuild history](docs/INTELLIGENCE_KERNEL_V2.md) ·
[Operations](docs/OPERATOR_GUIDE.md) · [Security](SECURITY.md)

Flourite is an independent open-source project, not an OpenAI product.

> [!CAUTION]
> Trusted mode gives the model the permissions of the current user. Run it in a
> dedicated VM, VPS, or disposable machine without unrelated secrets. Read
> [SECURITY.md](SECURITY.md) before using it elsewhere.

## Why Flourite

| Ordinary harness | Flourite |
| --- | --- |
| Fixed phases and agent roles | One adaptive loop around the live artifact |
| Many independent answers | One current best, with optional real branches |
| Chat history as memory | Compressed frontier, evolving quality lens, and content-addressed evidence |
| Criticism as more prose | Findings that change the next construction move |
| Stop at a call or round limit | Stop when the claim survives direct challenge, or an explicit operator limit is exhausted |
| Crash between model output and integration | Commit the whole semantic move atomically or not at all |
| Restart a run to change its machinery | Replace code atomically; the next activity leases it live |

The model remains the intelligence. Flourite handles the parts long runs are
bad at handling for themselves: exact goal continuity, recovery, global
reframing, causal learning from rejection, artifact-bound completion, and
durable operator control.

There is no default semantic call count, round count, or reserved “synthesis
phase.” Transport and component recovery use bounded retries so a broken
boundary cannot consume the run forever. An operator wall-time envelope can interrupt a live move;
metered token and turn envelopes close the run at the next atomic move boundary.
Otherwise productive work is not stopped by a hidden harness horizon. Flourite
does not pretend to enforce a dollar limit when the provider does not report
authoritative cost.

## Quick start

You need Linux, Python 3.11+, Node.js, the
[Codex CLI](https://developers.openai.com/codex), and
[Oh My Pi](https://github.com/can1357/oh-my-pi).

```sh
npm install -g @openai/codex @oh-my-pi/pi-coding-agent
codex login

git clone https://github.com/mystxcal/flourite.git
cd flourite
python -m venv .venv
source .venv/bin/activate
python -m pip install .

flourite doctor
flourite init flourite.toml
```

`doctor` checks OMP, ChatGPT authentication, and the configured model without
spending a model token. Actual tool availability is proven by the first real
move instead of inferred from CLI error text. The generated configuration works as-is;
the annotated version is in [examples/flourite.toml](examples/flourite.toml).

## Run a task

For research, decisions, formal work, design, or another file artifact:

```sh
flourite run \
  --config flourite.toml \
  --adapter research \
  --source brief.md \
  --output answer.md \
  "Find the strongest defensible answer."
```

For a repository:

```sh
flourite run \
  --config flourite.toml \
  --adapter software \
  --workspace . \
  --output final.patch \
  "Fix the parser bug, preserve compatibility, and prove the change."
```

Software runs stay isolated. Verify the result, then apply it explicitly:

```sh
flourite verify latest
flourite apply latest
```

To test the complete production kernel without a provider call:

```sh
flourite demo
```

The deterministic runner is intentionally not a model simulation. It proves
that creation, atomic commit, challenge, replay, control, and materialization
use the same architecture as a live run.

## Watch and steer

Attach from another terminal:

```sh
flourite live latest
```

The dashboard shows the current workspace, trajectories, move history, usage,
model and tool activity, completion claim, and queued operator commands. It is
a disposable projection over durable state; detaching it does not affect the
run.

Steering is admitted at the next safe move boundary and becomes a durable
objective amendment. Pause and stop use the same boundary, so an in-flight
model call is not silently discarded.

```sh
flourite steer latest "Test the assumption against primary evidence."
flourite pause latest
flourite resume latest
flourite stop latest
```

The run itself is a Ship of Theseus. Its objective, journal, and component
protocol remain stable; the implementation does not have to. To replace code
without rebuilding or restarting the run:

```sh
flourite component bind latest /path/to/flourite
flourite component status latest
```

The active activity finishes with the immutable generation it leased. The next
activity starts in a fresh process from the newly validated generation. A bad
generation cannot partially replace a running worker, and every lease leaves a
receipt beside the journal.

## How it works

Flourite keeps five semantic objects:

- **Objective** — immutable original task plus explicit operator amendments.
- **Frontier** — the shortest causal model from which a fresh strong model can continue.
- **Quality lens** — task-native success and failure signatures, observable discriminators, proxy traps, and blind spots that evolve from evidence.
- **Evidence** — provenance-bearing observations, tests, failures, steering,
  and challenge findings.
- **Artifact and trajectory heads** — the actual current deliverables and the
  few live alternatives that earned independent development.

One loop advances them:

```text
exact objective + frontier + quality lens + direct evidence
                         │
                         ▼
              Lead makes the strongest move
                         │
              ┌──────────┴──────────┐
              │                     │
      artifact/evidence      real uncertainty width
              │                     │
              │             optional trajectories
              └──────────┬──────────┘
                         ▼
              one integrated current best
                         │
             finish claim or next construction
                         │
                 valid direct challenge
                  │                  │
               support          material finding
                  │                  │
              satisfied       ordinary construction
```

The Lead keeps both session and live-workspace continuity within a trajectory;
generated outputs and partial work survive ordinary moves and transport
interruptions. A fresh Navigator appears only
when local continuation stops producing information; it reconstructs the
global frontier without inheriting the Lead's framing. A fresh Challenger can
support, challenge, or remain uncertain about a concrete finish claim. A
material rejection is not a terminal “review phase”—it becomes evidence for
the next normal construction move. A challenge first proves that its assay can
actually perceive the target. Missing material repairs and replays the same
evaluation; it cannot become a semantic verdict. New material distinctions
update the quality lens and reopen affected claims.

Every external result is committed as one `move.applied` transaction:
observations, artifact, workspace, branches, continuation, finish claim, usage,
and failure residue. A crash cannot leave half of that meaning authoritative.
The hash-chained ledger is the source of truth; `state.json` is a rebuildable
projection and large content lives in the blob store.

Infrastructure failure pauses the same semantic move in the same workspace. A
separate Codex repairer works on an isolated component copy; the supervisor
admits it only as a new immutable generation and replays the exact failed
activity. Task reasoning never gets polluted with transport repair, and a
non-terminal or stale artifact cannot cross into evaluation.

Adapters own domain observations. A software adapter can run deterministic
checks against the candidate; a media adapter can retain rendered deliverables;
the universal kernel does not pretend a language-model opinion is equivalent
to a direct measurement.

Read [the canonical model](docs/CANONICAL_MODEL.md) for the authoritative design
and [the rebuild history](docs/INTELLIGENCE_KERNEL_V2.md) for the failure analysis
that led to it.

## Commands

| Command | Purpose |
| --- | --- |
| `flourite run` | Start one exact task |
| `flourite status` | Show the compact current state |
| `flourite inspect` | Inspect trajectories, moves, observations, and usage |
| `flourite live` | Attach the live dashboard |
| `flourite steer` | Amend direction at the next safe boundary |
| `flourite pause` | Pause at the next safe boundary |
| `flourite resume` | Continue from the ledger |
| `flourite stop` | Stop safely while retaining the run |
| `flourite component bind` | Replace implementation code at the next activity boundary |
| `flourite component status` | Show the implementation the next activity will lease |
| `flourite component rollback` | Atomically return to the previous implementation |
| `flourite verify` | Replay the ledger and verify every referenced blob |
| `flourite events` | Print the immutable event stream as JSONL |
| `flourite export` | Create a redacted diagnostic or lossless audit bundle |
| `flourite apply` | Apply a verified software result explicitly |
| `flourite doctor` | Check provider, authentication, model, and tools |
| `flourite demo` | Run the real kernel with a deterministic offline executor |

`frontier` remains an install-time compatibility alias. The Python import stays
`frontier_harness` for compatibility.

## Configuration

The normal path uses GPT-5.6 Sol xhigh through OMP with shell, editing, LSP,
browser, web research, and bounded nested tasks. Ambient rules, skills, and
extensions are disabled so the provider boundary contains only context that
Flourite records explicitly.

The important current sections are:

- `[kernel]` — optional hard envelopes and the in-flight move safety ceiling;
- `[provider]` and `[provider.capabilities]` — transport and tool plane;
- `[provider.strong]` — model and reasoning effort;
- `[software]` — domain checks, deliverables, and explicit apply behavior.

Unknown configuration fields fail immediately; Flourite does not carry a
second set of inert compatibility knobs.

## Compared with

| Project | Flourite is the better fit when | Use the other project when |
| --- | --- | --- |
| [LangGraph](https://github.com/langchain-ai/langgraph) | You want a finished high-ceiling harness, not a graph-building toolkit. | You are building a product and want to own every node and edge. |
| [CrewAI](https://github.com/crewAIInc/crewAI) | The problem should decide whether multiple trajectories exist. | Stable role-play is itself part of your workflow. |
| [OpenHands](https://github.com/All-Hands-AI/OpenHands) / [SWE-agent](https://github.com/SWE-agent/SWE-agent) | Research, design, formal work, and software should share one durable intelligence loop. | Your work is exclusively repository-shaped. |
| Hosted deep research | You need local tools, durable state, live steering, and an auditable artifact. | You want a disposable answer in a chat window. |

## Development

```sh
python -m pip install -e '.[dev]'
pytest -q
ruff check src/frontier_harness tests
mypy src/frontier_harness
```

## Project notes

- [Canonical model](docs/CANONICAL_MODEL.md)
- [Rebuild history](docs/INTELLIGENCE_KERNEL_V2.md)
- [Operator guide](docs/OPERATOR_GUIDE.md)
- [Live validation](docs/LIVE_CODEX_VALIDATION.md)
- [Event model](docs/event-model.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)

Flourite is available under the [Apache License 2.0](LICENSE).
