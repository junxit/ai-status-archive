"""Shared test fixtures and fakes.

The fakes here replace exactly three things: the HTTP client, the clock, and
sleep. Everything else under test is the real code path, which is the point of
injecting those three in the first place.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

from aistatus.collect import load_providers  # noqa: E402
from aistatus.models import FetchError, ProviderConfig, RawResponse  # noqa: E402


def fixture_bytes(name: str) -> bytes:
    """Read a recorded upstream payload as raw bytes."""
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> Any:
    """Read a recorded upstream payload as decoded JSON."""
    return json.loads(fixture_bytes(name).decode("utf-8"))


class FakeClock:
    """A clock that advances one second per call.

    Deterministic so that snapshots taken in a test have distinct, predictable
    timestamps without any real waiting.
    """

    def __init__(self, start: str = "2026-09-15T00:00:00Z") -> None:
        self.start = start
        self.calls = 0

    def __call__(self) -> str:
        import datetime as dt

        base = dt.datetime.strptime(self.start, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.UTC
        )
        value = base + dt.timedelta(seconds=self.calls)
        self.calls += 1
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeFetcher:
    """An HTTP client that serves canned responses keyed by URL.

    Attributes:
        responses: URL to response mapping. A value may be a
            :class:`RawResponse`, raw ``bytes``, or an exception to raise.
        calls: Every ``(url, etag, last_modified)`` triple requested, in order.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str | None, str | None]] = []

    def __call__(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> RawResponse:
        self.calls.append((url, etag, last_modified))
        try:
            value = self.responses[url]
        except KeyError:
            raise FetchError("connection", f"no canned response for {url}") from None
        if isinstance(value, Exception):
            raise value
        if isinstance(value, RawResponse):
            return value
        return RawResponse(status_code=200, body=value, url=url, headers={})


def noop_sleep(_seconds: float) -> None:
    """A sleep that does not sleep."""


@pytest.fixture
def configs() -> tuple[ProviderConfig, ...]:
    """The real provider configuration from config/providers.yaml."""
    return load_providers((ROOT / "config" / "providers.yaml").read_text())


@pytest.fixture
def openai_cfg(configs: tuple[ProviderConfig, ...]) -> ProviderConfig:
    """The OpenAI provider configuration."""
    return next(c for c in configs if c.name == "openai")


@pytest.fixture
def anthropic_cfg(configs: tuple[ProviderConfig, ...]) -> ProviderConfig:
    """The Anthropic provider configuration."""
    return next(c for c in configs if c.name == "anthropic")


@pytest.fixture
def google_cfg(configs: tuple[ProviderConfig, ...]) -> ProviderConfig:
    """The Google Cloud provider configuration."""
    return next(c for c in configs if c.name == "google")


@pytest.fixture
def live_responses() -> dict[str, Any]:
    """Canned responses mirroring what the three real endpoints return."""
    return {
        "https://status.openai.com/api/v2/summary.json": fixture_bytes(
            "openai_summary.json"
        ),
        "https://status.claude.com/api/v2/summary.json": fixture_bytes(
            "anthropic_summary.json"
        ),
        "https://status.cloud.google.com/incidents.json": fixture_bytes(
            "google_incidents.json"
        ),
        "https://status.cloud.google.com/products.json": fixture_bytes(
            "google_products.json"
        ),
    }
