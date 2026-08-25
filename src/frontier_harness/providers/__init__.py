from ..config import ProviderConfig
from .base import (
    ModelProvider,
    ProviderCallRequest,
    ProviderCallResult,
    ProviderDoctorResult,
    ProviderTraceSummary,
)
from .fake import FakeProvider
from .omp_codex import OmpCodexProvider


def build_provider(config: ProviderConfig) -> ModelProvider:
    if config.kind == "fake":
        return FakeProvider()
    return OmpCodexProvider(config)


__all__ = [
    "FakeProvider",
    "ModelProvider",
    "OmpCodexProvider",
    "ProviderCallRequest",
    "ProviderCallResult",
    "ProviderDoctorResult",
    "ProviderTraceSummary",
    "build_provider",
]
