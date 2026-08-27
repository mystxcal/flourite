"""Offline provider identity used by ``flourite doctor --fake``.

Offline runs execute through :class:`DeterministicMoveRunner`; they never fake
the model transport itself.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from .base import ModelProvider, ProviderCallRequest, ProviderCallResult, ProviderDoctorResult

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class FakeProvider(ModelProvider):
    async def doctor(self) -> ProviderDoctorResult:
        return ProviderDoctorResult(
            ok=True,
            provider="fake",
            version="deterministic-kernel",
            auth_mode="offline",
            details=["No model call is made."],
        )

    async def run(self, request: ProviderCallRequest[ResponseT]) -> ProviderCallResult[ResponseT]:
        del request
        raise RuntimeError("offline runs use DeterministicMoveRunner, not a fake transport")
