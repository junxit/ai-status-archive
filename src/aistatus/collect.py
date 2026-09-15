"""Pure collection: configuration in, snapshots out.

This module is an architectural seam, and it is enforced by a test rather than
by good intentions. Nothing here may import :mod:`os`, :mod:`pathlib`,
:mod:`subprocess`, :mod:`logging`, or anything from ``runners``. It performs
network I/O — it must, in order to fetch — but it reads no files, runs no git,
consults no environment variables, and emits no log output.

Everything ambient arrives by injection: the HTTP client, the clock, and the
sleep function are all parameters. That is what makes the planned AWS Lambda
runner a real possibility rather than an aspiration: it will import
:func:`collect_all` unchanged and supply its own I/O.

``collect_all`` never raises for a single provider. One unreachable status page
must not blank the other two, so failures are captured per provider and returned
as data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

import yaml

from .adapters import get_adapter
from .fetch import Clock, Fetcher, Sleeper
from .models import (
    FetchError,
    FetchResult,
    NormalizationError,
    ProviderConfig,
    RawResponse,
    Snapshot,
)

#: Seconds to wait between providers. Three requests fired simultaneously from
#: one runner is a small burst; spacing them out is cheap politeness toward
#: services we depend on and want to keep depending on.
STAGGER_SECONDS = 1.5


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectOutcome:
    """The result of polling one provider, successful or not.

    Exactly one of the three outcome fields is meaningful:

    * ``snapshot`` set — a usable response was parsed.
    * ``not_modified`` true — upstream returned ``304``; prior state stands.
    * ``error`` set — we could not obtain or parse a response.

    Attributes:
        cfg: The provider that was polled.
        snapshot: Normalized state, or ``None``.
        not_modified: Whether upstream reported no change.
        error: Why this poll failed, or ``None``.
        raw_payload: Decoded upstream JSON, retained so the runner can archive
            the raw form. ``None`` on ``304`` or failure.
        fetched_at: When the request completed.
        http_status: Status code, or ``None`` if the transport failed.
    """

    cfg: ProviderConfig
    snapshot: Snapshot | None = None
    not_modified: bool = False
    error: Exception | None = None
    raw_payload: Any = None
    fetched_at: str = ""
    http_status: int | None = None

    @property
    def ok(self) -> bool:
        """Whether this poll produced usable information (a snapshot or a 304)."""
        return self.error is None


def load_providers(config_text: str) -> tuple[ProviderConfig, ...]:
    """Parse ``config/providers.yaml`` content into provider configurations.

    Takes the file's *text* rather than its path, so that config parsing stays
    on the pure side of the seam and the runner owns the file read.

    Args:
        config_text: Raw YAML content.

    Returns:
        Enabled providers, in the order they appear in the file.

    Raises:
        ValueError: If the document is malformed or a provider entry is missing
            a required field.
    """
    document = yaml.safe_load(config_text)
    if not isinstance(document, dict):
        raise ValueError("providers.yaml must be a mapping at the top level")

    entries = document.get("providers")
    if not isinstance(entries, list) or not entries:
        raise ValueError("providers.yaml must define a non-empty 'providers' list")

    configs: list[ProviderConfig] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"providers[{index}] must be a mapping")
        for required in ("name", "display_name", "platform", "url"):
            if not entry.get(required):
                raise ValueError(f"providers[{index}] is missing {required!r}")

        products = entry.get("product_ids") or []
        configs.append(
            ProviderConfig(
                name=str(entry["name"]),
                display_name=str(entry["display_name"]),
                platform=str(entry["platform"]),
                url=str(entry["url"]),
                catalog_url=(str(entry["catalog_url"]) if entry.get("catalog_url") else None),
                api_components=frozenset(str(c) for c in entry.get("api_components") or ()),
                product_ids=tuple(sorted(str(p) for p in products)),
                enabled=bool(entry.get("enabled", True)),
            )
        )

    names = [c.name for c in configs]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ValueError(f"duplicate provider names: {', '.join(sorted(duplicates))}")

    return tuple(c for c in configs if c.enabled)


def _decode(body: bytes | None, *, provider: str) -> Any:
    """Decode a JSON response body.

    Args:
        body: Raw response bytes.
        provider: Provider name, for the error message.

    Returns:
        The decoded object.

    Raises:
        NormalizationError: If the body is empty or not valid JSON. Treated the
            same as a fetch failure, so the last known good file survives.
    """
    if not body:
        raise NormalizationError(f"{provider}: empty response body")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NormalizationError(f"{provider}: response was not valid JSON: {exc}") from exc


def collect_one(
    cfg: ProviderConfig,
    *,
    fetcher: Fetcher,
    now: Clock,
    etag: str | None = None,
    last_modified: str | None = None,
) -> CollectOutcome:
    """Poll one provider and normalize the result.

    Args:
        cfg: Provider to poll.
        fetcher: Injected HTTP client.
        now: Injected clock, used to stamp the real completion time.
        etag: Prior ``ETag`` to revalidate against, if any.
        last_modified: Prior ``Last-Modified`` to revalidate against, if any.

    Returns:
        A :class:`CollectOutcome`. Never raises for an expected failure; network
        and schema problems are returned as data so that one bad provider cannot
        abort a poll of the others.
    """
    try:
        response: RawResponse = fetcher(cfg.url, etag=etag, last_modified=last_modified)
    except (FetchError, NormalizationError) as exc:
        return CollectOutcome(cfg=cfg, error=exc, fetched_at=now())

    fetched_at = now()

    if response.status_code == 304:
        # A 304 with no prior state is a contract violation: upstream is telling
        # us nothing changed relative to a validator we should not have had.
        # Reporting success here would assert a status we never observed.
        if etag is None and last_modified is None:
            return CollectOutcome(
                cfg=cfg,
                error=FetchError("unexpected_304", f"304 without a prior validator for {cfg.url}"),
                fetched_at=fetched_at,
                http_status=304,
            )
        return CollectOutcome(
            cfg=cfg, not_modified=True, fetched_at=fetched_at, http_status=304
        )

    # Google needs a second request to resolve stable product IDs to display
    # names. It is fetched unconditionally (no validator) because it changes
    # rarely and a stale name would be hashed as a rename. A failure here fails
    # the whole provider rather than falling back to bare IDs, which would
    # rewrite every component name and produce two junk commits.
    catalog_body: bytes | None = None
    if cfg.catalog_url:
        try:
            catalog_response = fetcher(cfg.catalog_url)
        except (FetchError, NormalizationError) as exc:
            return CollectOutcome(
                cfg=cfg, error=exc, fetched_at=fetched_at, http_status=response.status_code
            )
        catalog_body = catalog_response.body

    try:
        payload = _decode(response.body, provider=cfg.name)
        catalog = (
            _decode(catalog_body, provider=f"{cfg.name} catalog")
            if catalog_body is not None
            else None
        )
        snapshot = get_adapter(cfg.platform)(
            payload,
            cfg=cfg,
            source_url=response.url or cfg.url,
            catalog=catalog,
            fetch=FetchResult(
                ok=True,
                fetched_at=fetched_at,
                http_status=response.status_code,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                elapsed_ms=response.elapsed_ms,
            ),
        )
    except (NormalizationError, KeyError, ValueError, TypeError) as exc:
        return CollectOutcome(
            cfg=cfg,
            error=exc if isinstance(exc, NormalizationError) else NormalizationError(str(exc)),
            fetched_at=fetched_at,
            http_status=response.status_code,
        )

    return CollectOutcome(
        cfg=cfg,
        snapshot=snapshot,
        raw_payload=payload,
        fetched_at=fetched_at,
        http_status=response.status_code,
    )


def collect_all(
    configs: Sequence[ProviderConfig],
    *,
    fetcher: Fetcher,
    now: Clock,
    sleep: Sleeper,
    validators: dict[str, tuple[str | None, str | None]] | None = None,
    stagger_seconds: float = STAGGER_SECONDS,
) -> tuple[CollectOutcome, ...]:
    """Poll every provider in turn, staggered.

    Args:
        configs: Providers to poll.
        fetcher: Injected HTTP client.
        now: Injected clock.
        sleep: Injected sleep, so tests need not wait.
        validators: Per-provider ``(etag, last_modified)`` from prior state.
            Omit a provider, or pass ``(None, None)``, to force an unconditional
            request.
        stagger_seconds: Delay between providers.

    Returns:
        One outcome per provider, in configuration order.
    """
    validators = validators or {}
    outcomes: list[CollectOutcome] = []

    for index, cfg in enumerate(configs):
        if index:
            sleep(stagger_seconds)
        etag, last_modified = validators.get(cfg.name, (None, None))
        outcomes.append(
            collect_one(
                cfg, fetcher=fetcher, now=now, etag=etag, last_modified=last_modified
            )
        )

    return tuple(outcomes)


__all__ = [
    "STAGGER_SECONDS",
    "CollectOutcome",
    "collect_all",
    "collect_one",
    "load_providers",
]
