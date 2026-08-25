"""Thin profile guidance: domain-specific leaves on a stable universal core."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AdapterProfile:
    name: str
    artifact_label: str
    guidance: str
    probe_catalog: tuple[str, ...]
    release_anchor: str


PROFILES: dict[str, AdapterProfile] = {
    "generic": AdapterProfile(
        name="generic",
        artifact_label="artifact",
        guidance=(
            "Preserve the original request and reason holistically. Use the issue graph only "
            "as an attention aid. Prefer semantic deltas over regenerating parallel full answers."
        ),
        probe_catalog=(
            "hard invariant check",
            "counterexample attempt",
            "fresh-reader challenge",
            "pairwise facet comparison",
        ),
        release_anchor="Contract consistency and load-bearing claim support.",
    ),
    "research": AdapterProfile(
        name="research",
        artifact_label="research answer or report",
        guidance=(
            "Separate sourced facts, model inference, and open uncertainty. Focus probes on "
            "currentness, source grounding, alternative mechanisms, and decision sensitivity."
        ),
        probe_catalog=(
            "source-grounding check",
            "currentness check",
            "alternative-hypothesis discriminator",
            "assumption-light anchor",
        ),
        release_anchor="Current, attributable support for every load-bearing factual claim.",
    ),
    "formal": AdapterProfile(
        name="formal",
        artifact_label="proof, construction, or formal argument",
        guidance=(
            "Track assumptions, dependency structure, endpoints, and boundary cases. Treat a "
            "counterexample or hidden premise as higher-value than stylistic proof polishing."
        ),
        probe_catalog=(
            "counterexample search",
            "symbolic sanity check",
            "boundary-case test",
            "independent proof route",
        ),
        release_anchor="Logical dependency integrity and an independent challenge to novel load-bearing claims.",
    ),
    "decision": AdapterProfile(
        name="decision",
        artifact_label="ranked decision or strategy",
        guidance=(
            "Optimize expected utility under the user's actual constraints. Track assumption "
            "sensitivity, downside exposure, reversibility, and option value rather than prestige."
        ),
        probe_catalog=(
            "current availability or price check",
            "sensitivity analysis",
            "scenario stress test",
            "pairwise expected-value comparison",
        ),
        release_anchor="Constraint satisfaction and ranking robustness under plausible assumptions.",
    ),
    "creative": AdapterProfile(
        name="creative",
        artifact_label="creative or design artifact",
        guidance=(
            "Judge coherent intended experience, specificity, originality, and preference fit. "
            "Avoid universal scalar taste scores and preserve holistic review capacity."
        ),
        probe_catalog=(
            "facet-specific critique",
            "pairwise preference comparison",
            "holistic fresh review",
            "genericness or incoherence challenge",
        ),
        release_anchor="Coherence with the intended experience and stated preference dimensions.",
    ),
    "media": AdapterProfile(
        name="media",
        artifact_label="time-based media artifact",
        guidance=(
            "Treat the rendered temporal experience as the product, not its source files. "
            "Separate static-frame, motion, timing, audio, and full-sequence evidence; none "
            "proves the others. Establish direction and a representative vertical slice before "
            "expensive full-length production, then inspect the actual rendered sequence."
        ),
        probe_catalog=(
            "representative vertical-slice review",
            "frame collision and safe-area audit",
            "temporal pacing inspection",
            "audio-visual synchronization check",
            "full-sequence continuity review",
        ),
        release_anchor=(
            "Direct inspection of the final rendered sequence for legibility, collision safety, "
            "timing, continuity, and intended experience."
        ),
    ),
}


def get_profile(name: str) -> AdapterProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        available = ", ".join(sorted(PROFILES))
        raise ValueError(
            f"Unknown adapter profile {name!r}; available: {available}, software"
        ) from exc


def combine_profiles(names: list[str]) -> AdapterProfile:
    """Compose orthogonal semantic disciplines without changing artifact storage."""

    normalized: list[str] = []
    for name in names:
        if name and name not in normalized:
            normalized.append(name)
    if not normalized:
        normalized = ["generic"]
    profiles = [get_profile(name) for name in normalized]
    if len(profiles) == 1:
        return profiles[0]
    return AdapterProfile(
        name="+".join(item.name for item in profiles),
        artifact_label=" / ".join(item.artifact_label for item in profiles),
        guidance="\n".join(f"[{item.name}] {item.guidance}" for item in profiles),
        probe_catalog=tuple(
            dict.fromkeys(probe for item in profiles for probe in item.probe_catalog)
        ),
        release_anchor=" ".join(item.release_anchor for item in profiles),
    )
