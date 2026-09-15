"""Adapter registry: source platform to normalizer.

Adding a provider that runs on a platform already listed here is a
``config/providers.yaml`` edit and nothing more. Adding a genuinely new platform
means one new module implementing :data:`ParseFn` and one line in
:data:`REGISTRY`.

Every adapter is a pure function. All of them take the same keyword arguments and
return a :class:`~aistatus.models.Snapshot`, so nothing downstream needs to know
which platform a snapshot came from.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..models import FetchResult, ProviderConfig, Snapshot
from . import google_cloud, statuspage


class ParseFn(Protocol):
    """Normalizes one decoded upstream payload into a snapshot."""

    def __call__(
        self,
        payload: Any,
        *,
        cfg: ProviderConfig,
        fetch: FetchResult,
        source_url: str,
        catalog: Any = None,
    ) -> Snapshot:  # pragma: no cover - protocol definition
        ...


REGISTRY: dict[str, ParseFn] = {
    statuspage.PLATFORM: statuspage.parse,
    google_cloud.PLATFORM: google_cloud.parse,
}


def get_adapter(platform: str) -> ParseFn:
    """Look up the adapter for a source platform.

    Args:
        platform: Platform key from ``config/providers.yaml``.

    Returns:
        The adapter's parse function.

    Raises:
        KeyError: If no adapter is registered, with the known keys in the
            message so a typo in the config is obvious.
    """
    try:
        return REGISTRY[platform]
    except KeyError:
        known = ", ".join(sorted(REGISTRY)) or "(none)"
        raise KeyError(f"unknown platform {platform!r}; registered: {known}") from None


__all__ = ["REGISTRY", "ParseFn", "get_adapter", "google_cloud", "statuspage"]
