"""Thin profile guidance: domain-specific leaves on a stable universal core."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AdapterProfile:
    name: str
    guidance: str


PROFILES: dict[str, AdapterProfile] = {
    "generic": AdapterProfile(
        name="generic",
        guidance=(
            "Preserve the original request and reason holistically. Improve the live artifact "
            "directly, and use evidence only when it can change the result."
        ),
    ),
    "research": AdapterProfile(
        name="research",
        guidance=(
            "Separate sourced facts, model inference, and open uncertainty. Focus probes on "
            "currentness, source grounding, alternative mechanisms, and decision sensitivity."
        ),
    ),
    "formal": AdapterProfile(
        name="formal",
        guidance=(
            "Track assumptions, dependency structure, endpoints, and boundary cases. Treat a "
            "counterexample or hidden premise as higher-value than stylistic proof polishing."
        ),
    ),
    "decision": AdapterProfile(
        name="decision",
        guidance=(
            "Optimize expected utility under the user's actual constraints. Track assumption "
            "sensitivity, downside exposure, reversibility, and option value rather than prestige."
        ),
    ),
    "creative": AdapterProfile(
        name="creative",
        guidance=(
            "Judge coherent intended experience, specificity, originality, and preference fit. "
            "Avoid universal scalar taste scores and preserve holistic review capacity."
        ),
    ),
    "media": AdapterProfile(
        name="media",
        guidance=(
            "Treat the rendered temporal experience as the product, not its source files. "
            "Separate static-frame, motion, timing, audio, and full-sequence evidence; none "
            "proves the others. Establish direction and a representative vertical slice before "
            "expensive full-length production, then inspect the actual rendered sequence."
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
