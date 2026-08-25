# Flourite terminal language

Flourite's interface is the terminal translation of its banner, not a separate theme.

## Visual grammar

- **Crystal:** the faceted cube is the product mark. A hollow diamond means work in motion; a filled diamond means evidence or a phase has resolved.
- **Refraction:** cyan is the primary edge, cold blue carries structure, and violet appears only as a secondary facet. Errors and uncertainty use distinct warm colors so meaning never depends on the blue palette alone.
- **Routes:** thin lines, nodes, braces, brackets, and arrows suggest evidence moving through a graph. They belong in headers and separators, not inside substantive prose.
- **Dark field:** the banner supplies the visual atmosphere. The CLI assumes the terminal background and avoids painted panels, gradients, or large blocks that fight the user's theme.
- **Instrument typography:** labels are short, uppercase, and aligned. Explanations remain sentence case and high contrast.

## Runtime mapping

```text
◇  ORIENT        establish one complete baseline
◇  FOCUS         select decision-relevant work
◇  PROBE         execute one bounded action
◇  INTEGRATE     reduce evidence into the artifact
◇  CRYSTALLIZE   rebuild one coherent result
◇  CHALLENGE     test the release case
◆  SEALED        complete and replay-verifiable
```

The labels reveal existing runtime phases. They never create, merge, suppress, or reorder events.

## Output invariants

- Interactive output may use the full crystal lockup once per command.
- Ordinary long-running output is append-only: no spinner churn, transient rewriting, or hidden history.
- `flourite live` is an explicit attachable full-screen projection. It may redraw freely, but it never replaces the durable run log or owns semantic state.
- Status and inspection views favor density and alignment over panels or ornamental borders.
- `--quiet` emits only the requested material result.
- JSON and JSONL never receive branding or explanatory text.
- `NO_COLOR`, redirected output, narrow terminals, and monochrome terminals retain the complete semantic hierarchy.
- Color reinforces symbols and labels; it is never the sole carrier of state.

The implementation is isolated in `frontier_harness.presentation`. Solver behavior belongs elsewhere.
