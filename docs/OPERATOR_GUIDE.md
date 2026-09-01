# Operator guide

## Start with the default

```sh
flourite doctor
flourite run --source brief.md --output answer.md "Exact task"
```

The normal run uses one persistent Sol xhigh Lead with the configured tool
plane. Flourite introduces a fresh Navigator only when the local frame stalls,
a fresh Challenger only for a concrete finish claim, and additional
trajectories only when the problem contains real uncertainty width.

Do not set an artificial call or round budget. If the work needs a hard
boundary, choose the resource that is actually scarce:

```sh
flourite run \
  --max-wall-seconds 7200 \
  --max-model-turns 1000 \
  "Exact task"
```

Token and model-turn envelopes are also available under `[kernel]`; because the
provider reports them after a call, they stop the run at that atomic boundary.
Dollar limits are rejected until the active provider reports authoritative
monetary cost; Flourite never silently treats an unmetered run as free.

## Capability mode

Trusted mode is the intended high-ceiling configuration on a dedicated VM or
VPS. It gives the model shell, editing, code intelligence, browser/search, and
bounded nested-task access with automatic approval. Contained mode deliberately
trades capability for an inner sandbox.

## Read a run

```sh
flourite status latest
flourite inspect latest
flourite live latest
```

`status` answers where the run is. `inspect` shows trajectories, moves,
observations, challenge verdicts, and usage. `live` combines the durable state
with sanitized provider and tool activity.

Useful questions are:

- Is the Lead still changing the artifact or producing direct evidence?
- Did a trajectory open for a real alternative or for superficial variation?
- Is a completion finding material to the claim?
- Does support bind to the current artifact digest?
- Is the run active, paused, honestly exhausted, externally blocked, stopped,
  failed, or satisfied?

Tool activity is not semantic progress. The ledger changes only at move
boundaries.

## Steer and control

```sh
flourite steer latest "Inspect the rendered artifact, not only its source."
flourite pause latest
flourite resume latest
flourite stop latest
```

Commands are durable. Steering becomes an objective amendment at the next safe
boundary. Pause and stop do not throw away an in-flight provider call or claim
that partial output was integrated.

An interrupted active run resumes from the ledger:

```sh
flourite verify latest
flourite resume latest
```

## Software

Give the adapter cheap direct checks where they exist:

```toml
[software]
candidate_checks = ["ruff check ."]
checks = ["python -m pytest -q"]
```

The source repository is not mutated during the run. Applying a result is a
separate explicit action:

```sh
flourite verify latest
flourite apply latest
```

Apply requires a satisfied run and a matching source fingerprint.

## Exports

```sh
flourite export latest --output diagnostic.zip
flourite export latest --mode audit --output exact-audit.zip
```

Diagnostic export applies best-effort secret-pattern redaction and must still be
reviewed before sharing. Audit export is lossless by design and may contain
private sources, prompts, traces, and credentials.
