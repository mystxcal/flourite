"""Projection of semantic memory, commitments, and discovery state."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import events as et
from ..ledger import LedgerEvent
from ..models import (
    ActionContract,
    ActionReceipt,
    ArtifactSpine,
    Crux,
    DiscoveryRecord,
    InstrumentSpec,
    LeadSessionState,
    Obligation,
    RunState,
    SpeculativeOverlay,
    SubstrateEntry,
    SummitLineage,
    TaskCharter,
)

Handler = Callable[[RunState, dict[str, Any]], None]


class SemanticProjector:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {
            et.TASK_CHARTER_UPDATED: self._charter,
            et.ARTIFACT_SPINE_UPDATED: self._spine,
            et.OBLIGATIONS_UPDATED: self._obligations,
            et.CRUXES_UPDATED: self._cruxes,
            et.SUBSTRATE_UPDATED: self._substrate,
            et.OVERLAYS_UPDATED: self._overlays,
            et.INSTRUMENT_UPDATED: self._instrument,
            et.SUMMIT_ACTIVATED: self._summit_activated,
            et.SUMMIT_ARCHIVE_UPDATED: self._summit_archive,
            et.LEAD_SESSION_UPDATED: self._lead,
            et.LEAD_RECONSTRUCTION: self._lead,
            et.ACTION_CONTRACTED: self._contract,
            et.ACTION_RECEIPTED: self._receipt,
        }
        self.event_types = frozenset(self._handlers)

    def apply(self, state: RunState, event: LedgerEvent) -> None:
        try:
            handler = self._handlers[event.event_type]
        except KeyError as exc:  # pragma: no cover - registry enforces this
            raise ValueError(f"Semantic projector cannot handle {event.event_type}") from exc
        handler(state, event.payload)

    @staticmethod
    def _charter(state: RunState, payload: dict[str, Any]) -> None:
        charter = TaskCharter.model_validate(payload["task_charter"])
        state.task_charter = charter
        state.charter_history.append(charter)

    @staticmethod
    def _spine(state: RunState, payload: dict[str, Any]) -> None:
        state.artifact_spine = ArtifactSpine.model_validate(payload["artifact_spine"])

    @staticmethod
    def _obligations(state: RunState, payload: dict[str, Any]) -> None:
        for raw in payload.get("obligations", []):
            item = Obligation.model_validate(raw)
            state.obligations[item.obligation_id] = item

    @staticmethod
    def _cruxes(state: RunState, payload: dict[str, Any]) -> None:
        for raw in payload.get("cruxes", []):
            item = Crux.model_validate(raw)
            state.cruxes[item.crux_id] = item

    @staticmethod
    def _substrate(state: RunState, payload: dict[str, Any]) -> None:
        for raw in payload.get("entries", []):
            item = SubstrateEntry.model_validate(raw)
            state.substrate[item.entry_id] = item

    @staticmethod
    def _overlays(state: RunState, payload: dict[str, Any]) -> None:
        for raw in payload.get("overlays", []):
            item = SpeculativeOverlay.model_validate(raw)
            state.overlays[item.overlay_id] = item

    @staticmethod
    def _instrument(state: RunState, payload: dict[str, Any]) -> None:
        item = InstrumentSpec.model_validate(payload["instrument"])
        state.instruments[item.instrument_id] = item

    @staticmethod
    def _summit_activated(state: RunState, payload: dict[str, Any]) -> None:
        state.summit_active = True
        state.summit_reasons = [str(item) for item in payload.get("reasons", [])]

    @staticmethod
    def _summit_archive(state: RunState, payload: dict[str, Any]) -> None:
        for raw in payload.get("lineages", []):
            lineage = SummitLineage.model_validate(raw)
            state.summit_lineages[lineage.lineage_id] = lineage
        for raw in payload.get("discovery_records", []):
            discovery = DiscoveryRecord.model_validate(raw)
            state.discovery_records[discovery.lineage_id] = discovery

    @staticmethod
    def _lead(state: RunState, payload: dict[str, Any]) -> None:
        state.lead_session = LeadSessionState.model_validate(payload["lead_session"])

    @staticmethod
    def _contract(state: RunState, payload: dict[str, Any]) -> None:
        item = ActionContract.model_validate(payload["contract"])
        if item.action_id and item.action_id in state.actions:
            state.actions[item.action_id].contract = item

    @staticmethod
    def _receipt(state: RunState, payload: dict[str, Any]) -> None:
        item = ActionReceipt.model_validate(payload["receipt"])
        state.action_receipts[item.action_id] = item
        if item.action_id in state.actions:
            state.actions[item.action_id].receipt = item
