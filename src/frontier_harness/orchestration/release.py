"""Artifact-bound release policy and repair orchestration.

The engine owns capabilities (provider calls, artifacts, and durable events).
This module owns the release decision: when an independent challenge is
required, what a verdict means, and whether another repair is justified.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from .. import events as et
from ..models import (
    EvidenceRecord,
    FailureScope,
    FinalOutput,
    LeadContinuityStatus,
    RecoveryRoute,
    ReleaseOutput,
    ReleaseRecovery,
    RunState,
)
from ..util import canonical_json, normalize_key, sha256_text, unique_preserving_order

if TYPE_CHECKING:
    from ..engine import FrontierEngine


@dataclass(frozen=True, slots=True)
class MutationGateDecision:
    """Fail-closed decision for mutating an external source artifact."""

    deterministic_checks_run: int
    deterministic_checks_passed: bool
    release_required: bool
    release_gate_succeeded: bool
    release_report_releaseable: bool | None
    repair_completed: bool
    release_gate_passed: bool
    mutation_gate_passed: bool
    block_reason: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "deterministic_checks_run": self.deterministic_checks_run,
            "deterministic_checks_passed": self.deterministic_checks_passed,
            "release_required": self.release_required,
            "release_gate_succeeded": self.release_gate_succeeded,
            "release_report_releaseable": self.release_report_releaseable,
            "release_gate_passed": self.release_gate_passed,
            "releaseable": self.release_gate_passed if self.release_required else None,
            "repair_completed": self.repair_completed,
            "mutation_gate_passed": self.mutation_gate_passed,
            "mutation_gate_block_reason": self.block_reason,
        }


class ReleasePolicy:
    """Pure release semantics, independent of I/O and provider execution."""

    _ROUTE_RANK: ClassVar[dict[RecoveryRoute, int]] = {
        RecoveryRoute.REPAIR: 0,
        RecoveryRoute.EXTERNAL_BLOCKER: 1,
        RecoveryRoute.REOBSERVE: 2,
        RecoveryRoute.RECONSTRUCT: 3,
        RecoveryRoute.REFRAME: 4,
    }
    _SCOPE_RANK: ClassVar[dict[FailureScope, int]] = {
        FailureScope.LOCAL: 0,
        FailureScope.SEQUENCE: 1,
        FailureScope.WHOLE_ARTIFACT: 2,
        FailureScope.OBSERVATION: 3,
        FailureScope.ARCHITECTURE: 4,
        FailureScope.TASK_FRAME: 5,
    }

    @staticmethod
    def should_challenge(
        *,
        policy: str,
        adaptive_mode: bool,
        state: RunState,
        final_output: FinalOutput,
        checks: Sequence[EvidenceRecord],
    ) -> bool:
        if policy == "never":
            return False
        if policy == "always":
            return True
        contract = state.contract
        high_stakes = bool(contract and contract.stakes in {"high", "critical"})
        high_floor = bool(contract and contract.quality_floor in {"very_high", "frontier"})
        continuity_degraded = (
            adaptive_mode and state.lead_session.status == LeadContinuityStatus.DEGRADED
        )
        return any(
            (
                final_output.release_gate_recommended,
                high_stakes,
                high_floor,
                any(item.negative_result for item in checks),
                bool(state.high_impact_open_issues),
                state.runtime.verification.semantic_ci_passed is False,
                bool(state.runtime.verification.semantic_ci_gaps),
                continuity_degraded,
            )
        )

    @staticmethod
    def needs_repair(release: ReleaseOutput) -> bool:
        severe = any(finding.severity in {"fatal", "high"} for finding in release.findings)
        return any(
            (
                release.requires_repair,
                not release.releaseable,
                severe,
                not release.task_fidelity_passed,
                not release.completion_case_valid,
                not release.strongest_alternative_addressed,
            )
        )

    @classmethod
    def recovery(cls, release: ReleaseOutput) -> ReleaseRecovery | None:
        """Route material evidence to the earliest falsified boundary."""

        material = [
            finding for finding in release.findings if finding.severity in {"fatal", "high"}
        ]
        if not material:
            return None

        routed: list[tuple[RecoveryRoute, Any]] = []
        for finding in material:
            route = finding.recovery_route
            if route == RecoveryRoute.REPAIR:
                if finding.scope == FailureScope.TASK_FRAME:
                    route = RecoveryRoute.REFRAME
                elif finding.scope in {FailureScope.WHOLE_ARTIFACT, FailureScope.ARCHITECTURE}:
                    route = RecoveryRoute.RECONSTRUCT
                elif finding.scope == FailureScope.OBSERVATION:
                    route = RecoveryRoute.REOBSERVE
            routed.append((route, finding))

        actionable = [item for item in routed if item[0] != RecoveryRoute.REPAIR]
        if not actionable:
            return None
        route, controlling = max(
            actionable,
            key=lambda item: (cls._ROUTE_RANK[item[0]], cls._SCOPE_RANK[item[1].scope]),
        )
        implicated = [
            finding
            for candidate_route, finding in routed
            if candidate_route == route or cls._SCOPE_RANK[finding.scope] >= 2
        ]
        return ReleaseRecovery(
            route=route,
            scope=max(implicated, key=lambda item: cls._SCOPE_RANK[item.scope]).scope,
            reason=(
                f"Release evidence falsified the {controlling.scope.value} boundary: "
                f"{controlling.title}"
            ),
            finding_titles=unique_preserving_order(item.title for item in implicated),
            causal_layers=unique_preserving_order(
                item.causal_layer for item in implicated if item.causal_layer.strip()
            ),
            falsified_assumptions=unique_preserving_order(
                assumption for item in implicated for assumption in item.falsified_assumptions
            ),
            invalidated_invariants=unique_preserving_order(
                invariant for item in implicated for invariant in item.invalidated_invariants
            ),
            next_discriminators=unique_preserving_order(
                item.next_discriminator for item in implicated if item.next_discriminator.strip()
            ),
            evidence_references=unique_preserving_order(
                reference
                for item in implicated
                for reference in (item.evidence_reference, release.artifact_digest)
                if reference
            ),
        )

    @staticmethod
    def can_adjudicate_semantic_findings(
        *,
        semantic_ci_passed: bool,
        completion_gaps: Sequence[str],
        deterministic_failures: Sequence[str] | None,
        checks: Sequence[EvidenceRecord],
        release: ReleaseOutput,
    ) -> bool:
        return (
            not semantic_ci_passed
            and deterministic_failures == []
            and not completion_gaps
            and not any(item.negative_result for item in checks)
            and not ReleasePolicy.needs_repair(release)
        )

    @staticmethod
    def rejection_fingerprint(release: ReleaseOutput) -> str:
        findings = sorted(
            (
                finding.severity,
                normalize_key(finding.title),
                normalize_key(finding.evidence_reference or ""),
                finding.scope.value,
                finding.recovery_route.value,
                normalize_key(finding.causal_layer),
            )
            for finding in release.findings
        )
        return sha256_text(
            canonical_json(
                {
                    "findings": findings,
                    "requires_repair": release.requires_repair,
                    "releaseable": release.releaseable,
                    "task_fidelity_passed": release.task_fidelity_passed,
                    "completion_case_valid": release.completion_case_valid,
                    "strongest_alternative_addressed": release.strongest_alternative_addressed,
                }
            )
        )

    @staticmethod
    def mutation_gate(
        *,
        state: RunState,
        checks: Sequence[EvidenceRecord],
        release_required: bool,
        release: ReleaseOutput | None,
        repair_completed: bool,
    ) -> MutationGateDecision:
        failed_checks = [item for item in checks if item.negative_result]
        checks_passed = not failed_checks
        semantic_ci_passed = state.runtime.verification.semantic_ci_passed is True
        completion_case_passed = not state.runtime.verification.semantic_ci_gaps
        release_gate_passed = not release_required
        block_reason: str | None = None

        if failed_checks:
            block_reason = (
                f"{len(failed_checks)} of {len(checks)} deterministic release checks failed"
            )
        elif not semantic_ci_passed:
            block_reason = "semantic regression checks did not pass"
        elif not completion_case_passed:
            block_reason = "completion case has unresolved coverage gaps"

        if release_required:
            if release is None:
                release_gate_passed = False
                block_reason = block_reason or "required release challenge did not complete"
            elif state.final_artifact is None or release.artifact_digest != state.final_artifact.blob.digest:
                release_gate_passed = False
                block_reason = block_reason or "release verdict is not bound to the current final artifact"
            elif ReleasePolicy.needs_repair(release):
                release_gate_passed = False
                block_reason = block_reason or (
                    "release report is non-releaseable or contains unresolved high-severity findings"
                )
            else:
                release_gate_passed = True

        mutation_gate_passed = all(
            (checks_passed, semantic_ci_passed, completion_case_passed, release_gate_passed)
        )
        return MutationGateDecision(
            deterministic_checks_run=len(checks),
            deterministic_checks_passed=checks_passed,
            release_required=release_required,
            release_gate_succeeded=release is not None,
            release_report_releaseable=release.releaseable if release else None,
            repair_completed=repair_completed,
            release_gate_passed=release_gate_passed,
            mutation_gate_passed=mutation_gate_passed,
            block_reason=block_reason,
        )


class ReleasePipeline:
    """Durable challenge/repair loop over one exact final artifact."""

    async def run(
        self,
        engine: FrontierEngine,
        final_output: FinalOutput,
        checks: list[EvidenceRecord],
    ) -> tuple[list[EvidenceRecord], ReleaseOutput | None, MutationGateDecision]:
        repairs_used = engine.state.runtime.release.repair_count
        repair_completed = repairs_used > 0 or engine.state.runtime.release.repair_completed
        release_required = (
            ReleasePolicy.should_challenge(
                policy=engine.config.run.release_gate,
                adaptive_mode=engine.config.cognition.mode == "adaptive",
                state=engine.state,
                final_output=final_output,
                checks=checks,
            )
            or engine.state.release is not None
            or bool(engine.state.runtime.release.release_error)
            or repair_completed
        )
        release = self._artifact_bound_verdict(engine) if release_required else None
        if release_required and release is None:
            release = await engine._release_challenge()

        engine._apply_release_adjudication(release, checks)
        recovery = ReleasePolicy.recovery(release) if release is not None else None
        rejection_fingerprints = set(engine.state.runtime.release.rejection_fingerprints)
        repeated = self._record_initial_repetition(
            engine, release, rejection_fingerprints, repairs_used
        )

        while self._repairable(release, recovery, repeated):
            stop_reason = self._repair_stop_reason(engine, repairs_used)
            if stop_reason:
                engine._record_repair_loop_stop(
                    stop_reason, release=release, repairs_used=repairs_used
                )
                break
            assert release is not None
            before_digest = engine.state.final_artifact.blob.digest if engine.state.final_artifact else None
            if not await engine._repair(release):
                engine._record_repair_loop_stop(
                    "repair call failed or produced no capturable candidate",
                    release=release,
                    repairs_used=repairs_used,
                )
                break
            repair_completed = True
            repairs_used = engine.state.runtime.release.repair_count
            after_digest = engine.state.final_artifact.blob.digest if engine.state.final_artifact else None
            if not after_digest or after_digest == before_digest:
                engine._record_repair_loop_stop(
                    "repair did not change the authoritative artifact",
                    release=release,
                    repairs_used=repairs_used,
                )
                break

            checks = engine._record_deterministic_checks()
            release = await engine._release_challenge()
            engine._apply_release_adjudication(release, checks)
            if release is None:
                engine._record_repair_loop_stop(
                    "fresh release challenge did not complete",
                    release=None,
                    repairs_used=repairs_used,
                )
                break
            recovery = ReleasePolicy.recovery(release) if ReleasePolicy.needs_repair(release) else None
            if recovery is not None:
                break
            fingerprint = ReleasePolicy.rejection_fingerprint(release)
            if ReleasePolicy.needs_repair(release) and fingerprint in rejection_fingerprints:
                engine._record_repair_loop_stop(
                    "fresh challenge repeated the same blocking findings",
                    release=release,
                    repairs_used=repairs_used,
                )
                break
            rejection_fingerprints.add(fingerprint)

        decision = ReleasePolicy.mutation_gate(
            state=engine.state,
            checks=checks,
            release_required=release_required,
            release=release,
            repair_completed=repair_completed,
        )
        self._route_recovery(engine, recovery, release, repairs_used)
        return checks, release, decision

    @staticmethod
    def _artifact_bound_verdict(engine: FrontierEngine) -> ReleaseOutput | None:
        release = engine.state.release
        artifact = engine.state.final_artifact
        if release is not None and artifact is not None and release.artifact_digest != artifact.blob.digest:
            return None
        return release

    @staticmethod
    def _record_initial_repetition(
        engine: FrontierEngine,
        release: ReleaseOutput | None,
        fingerprints: set[str],
        repairs_used: int,
    ) -> bool:
        if release is None or not ReleasePolicy.needs_repair(release):
            return False
        fingerprint = ReleasePolicy.rejection_fingerprint(release)
        repeated = engine.state.runtime.release.rejection_fingerprints.count(fingerprint) > 1
        fingerprints.add(fingerprint)
        if repeated:
            engine._record_repair_loop_stop(
                "fresh challenge repeated a prior blocking finding",
                release=release,
                repairs_used=repairs_used,
            )
        return repeated

    @staticmethod
    def _repairable(
        release: ReleaseOutput | None,
        recovery: ReleaseRecovery | None,
        repeated: bool,
    ) -> bool:
        return bool(
            release is not None
            and ReleasePolicy.needs_repair(release)
            and recovery is None
            and not repeated
        )

    @staticmethod
    def _repair_stop_reason(engine: FrontierEngine, repairs_used: int) -> str | None:
        limit = engine.config.cognition.max_material_repairs
        if limit is not None and repairs_used >= limit:
            return "material repair limit reached"
        if engine._calls_remaining() < 2 or engine._budget_limit_reason(calls=False):
            return "insufficient envelope for repair plus fresh challenge"
        return None

    @staticmethod
    def _route_recovery(
        engine: FrontierEngine,
        recovery: ReleaseRecovery | None,
        release: ReleaseOutput | None,
        repairs_used: int,
    ) -> None:
        if recovery is None:
            return
        if recovery.route != RecoveryRoute.EXTERNAL_BLOCKER and engine._can_reopen_release():
            engine._append(
                et.RELEASE_RECOVERY_REQUESTED,
                {
                    "recovery": recovery.model_dump(mode="json"),
                    "artifact_digest": release.artifact_digest if release else None,
                },
                actor="release",
            )
            return
        reason = (
            "release evidence requires genuinely unavailable external authority or evidence"
            if recovery.route == RecoveryRoute.EXTERNAL_BLOCKER
            else "insufficient envelope to reopen the falsified upstream boundary"
        )
        engine._record_repair_loop_stop(reason, release=release, repairs_used=repairs_used)
