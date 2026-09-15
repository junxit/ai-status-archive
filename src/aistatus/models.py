"""Normalized schema types shared by every provider adapter.

Every provider, regardless of the platform it publishes on, is normalized into a
:class:`Snapshot`. Adapters own all knowledge of upstream quirks; nothing
downstream of an adapter should be able to tell which platform a snapshot came
from.

Design constraints that shape this module:

* Every type is a frozen dataclass holding only ``str``, ``int``, ``bool``,
  ``None``, or ``tuple`` of the same. No floats anywhere, which removes float
  repr from the list of things that could make serialization non-deterministic.
* Every timestamp is a string already normalized to RFC 3339 UTC with a ``Z``
  suffix and second precision. Normalization happens once, at the adapter
  boundary, via :func:`to_utc_z`.
* Sequence fields are tuples, built pre-sorted by stable ID, so serialization
  order never depends on upstream ordering.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

SCHEMA_VERSION = 1


class ComponentStatus(StrEnum):
    """Normalized per-component health."""

    OPERATIONAL = "operational"
    DEGRADED_PERFORMANCE = "degraded_performance"
    PARTIAL_OUTAGE = "partial_outage"
    MAJOR_OUTAGE = "major_outage"
    UNDER_MAINTENANCE = "under_maintenance"
    UNKNOWN = "unknown"


class Indicator(StrEnum):
    """Normalized page-level severity indicator."""

    NONE = "none"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"
    MAINTENANCE = "maintenance"
    UNKNOWN = "unknown"


class IncidentStatus(StrEnum):
    """Normalized incident lifecycle state."""

    INVESTIGATING = "investigating"
    IDENTIFIED = "identified"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    POSTMORTEM = "postmortem"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class Impact(StrEnum):
    """Normalized incident impact."""

    NONE = "none"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"
    MAINTENANCE = "maintenance"
    UNKNOWN = "unknown"


class ChangeType(StrEnum):
    """Kinds of discrete change recorded in ``history/{provider}.jsonl``.

    ``BASELINE`` and ``COMPONENT_RENAMED`` are additions to the originally
    specified set; see the README section "History event types" for why.
    """

    BASELINE = "baseline"
    OVERALL_INDICATOR = "overall_indicator"
    COMPONENT_STATUS = "component_status"
    COMPONENT_RENAMED = "component_renamed"
    INCIDENT_OPENED = "incident_opened"
    INCIDENT_UPDATED = "incident_updated"
    INCIDENT_RESOLVED = "incident_resolved"


#: Severity ordering used to pick the "worst" component status. Higher is worse.
#: ``UNKNOWN`` deliberately ranks below any real degradation so that a single
#: unparseable component cannot masquerade as an outage.
STATUS_SEVERITY: dict[ComponentStatus, int] = {
    ComponentStatus.OPERATIONAL: 0,
    ComponentStatus.UNKNOWN: 1,
    ComponentStatus.UNDER_MAINTENANCE: 2,
    ComponentStatus.DEGRADED_PERFORMANCE: 3,
    ComponentStatus.PARTIAL_OUTAGE: 4,
    ComponentStatus.MAJOR_OUTAGE: 5,
}

#: Incident states that mean "this incident is over".
TERMINAL_INCIDENT_STATUSES = frozenset(
    {IncidentStatus.RESOLVED, IncidentStatus.POSTMORTEM, IncidentStatus.COMPLETED}
)

_WS_RE = re.compile(r"[ \t]+")


def nz(value: str | None) -> str | None:
    """Collapse empty and whitespace-only strings to ``None``.

    Upstream feeds are inconsistent about whether an absent optional string is
    ``null``, missing, or ``""``. Left alone, that inconsistency would change the
    content hash when a provider switched representations without changing
    meaning.

    Args:
        value: Raw string from an upstream payload, or ``None``.

    Returns:
        The stripped string, or ``None`` if it held no content.
    """
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def normalize_text(value: str | None) -> str | None:
    """Normalize free text so cosmetic encoding differences cannot churn the hash.

    Applies Unicode NFC normalization, converts CRLF and CR to LF, collapses runs
    of spaces and tabs, and strips leading and trailing whitespace. Providers
    routinely re-save incident bodies through editors that flip line endings or
    emit decomposed Unicode; none of that is a status change.

    Args:
        value: Raw free text from an upstream payload, or ``None``.

    Returns:
        Normalized text, or ``None`` if it held no content.
    """
    if value is None:
        return None
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_WS_RE.sub(" ", line).rstrip() for line in text.split("\n"))
    return text.strip() or None


def to_utc_z(value: str | None) -> str | None:
    """Normalize any RFC 3339 timestamp to UTC with a ``Z`` suffix.

    Handles the three representations actually observed across our providers:
    Anthropic's millisecond precision (``2026-09-11T14:28:27.269Z``), OpenAI's
    second precision (``2026-07-09T19:25:56Z``), and Google's explicit offset
    (``2026-09-01T14:44:00+00:00``).

    Sub-second precision is **truncated, not rounded**. Rounding is not monotone
    across a second boundary, so two observations could order incorrectly.

    Args:
        value: An RFC 3339 timestamp string, or ``None``.

    Returns:
        ``YYYY-MM-DDTHH:MM:SSZ``, or ``None`` if the input was empty or could not
        be parsed. Unparseable input yields ``None`` rather than raising, because
        one malformed timestamp should not fail an entire poll.
    """
    cleaned = nz(value)
    if cleaned is None:
        return None
    candidate = cleaned[:-1] + "+00:00" if cleaned.endswith(("Z", "z")) else cleaned
    try:
        parsed = _dt.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    parsed = parsed.astimezone(_dt.UTC).replace(microsecond=0)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def sha12(value: str | None) -> str | None:
    """Return a 12-hex-character SHA-256 prefix of ``value``.

    Used to fingerprint incident update text so that an in-place edit registers
    as a change without dragging a multi-kilobyte postmortem into the hashed
    body.

    Args:
        value: Text to fingerprint, or ``None``.

    Returns:
        Twelve lowercase hex characters, or ``None`` if ``value`` was ``None``.
    """
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True, kw_only=True)
class Component:
    """A single service component and its current health.

    Attributes:
        id: Stable upstream identifier. Never a display name; providers rename
            components and Google is actively doing so.
        name: Current display name. Recorded because it is useful, hashed because
            a rename is a real fact worth timestamping, but never used as a key.
        group: Parent group name, or ``None`` when the component is top-level.
        status: Normalized health.
        updated_at: The provider's own last-updated timestamp, preserved as a
            distinct fact from our observation time. **Excluded from the content
            hash** because OpenAI reports one shared bulk value across all
            components that bumps without any component actually changing.
    """

    id: str
    name: str
    group: str | None = None
    status: ComponentStatus = ComponentStatus.UNKNOWN
    updated_at: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Incident:
    """An incident or scheduled maintenance affecting one or more components.

    Attributes:
        id: Stable upstream identifier.
        name: Human-readable title.
        status: Normalized lifecycle state.
        impact: Normalized severity.
        started_at: When the provider says the incident began. Note this means
            subtly different things per provider: for Google it is the real onset
            (``begin``), while for OpenAI it falls back to ``created_at``, which
            is when the provider first posted about it.
        updated_at: Provider's last-modified time. Preserved but **excluded from
            the content hash**; ``latest_update_id`` and ``latest_update_sha12``
            carry the change signal instead, so cosmetic re-saves stay quiet.
        resolved_at: When the incident closed, or ``None`` while open. Always
            ``None`` for OpenAI, which does not publish the field.
        affected_components: Stable component IDs, sorted. Always empty for
            OpenAI, whose incidents carry no component linkage at all.
        url: Permalink to the incident.
        latest_update: Most recent update text, truncated for legibility.
        latest_update_id: Stable ID of the most recent update, or a synthesized
            fingerprint where the provider exposes none.
        latest_update_sha12: Fingerprint of the full normalized update text, so
            an in-place edit is detected without hashing the whole body.
    """

    id: str
    name: str
    status: IncidentStatus = IncidentStatus.UNKNOWN
    impact: Impact = Impact.UNKNOWN
    started_at: str | None = None
    updated_at: str | None = None
    resolved_at: str | None = None
    affected_components: tuple[str, ...] = ()
    url: str | None = None
    latest_update: str | None = None
    latest_update_id: str | None = None
    latest_update_sha12: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchResult:
    """Facts about our HTTP request, as opposed to facts about the provider.

    This whole block is excluded from the content hash. The rule that keeps the
    archive quiet is: **anything that varies per poll belongs here, no
    exceptions.**

    Attributes:
        ok: Whether a usable response was obtained.
        fetched_at: When the HTTP request actually completed. Never the
            workflow's scheduled time; GitHub's cron drifts and recording the
            intended time would silently corrupt the timeline.
        http_status: Status code, or ``None`` when the transport failed outright.
        error: Short error description when ``ok`` is false.
        etag: Validator echoed back on the next poll.
        last_modified: Validator echoed back on the next poll.
        content_hash: Hash of this snapshot's hashed body. Stored inside the
            excluded block so it is self-excluding with no special case.
        elapsed_ms: Round-trip duration, for spotting a degrading status page.
    """

    ok: bool
    fetched_at: str
    http_status: int | None = None
    error: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None
    elapsed_ms: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Snapshot:
    """One provider's normalized state at one observed instant.

    Attributes:
        provider: Short key, e.g. ``anthropic``. Matches the filename stem.
        provider_name: Display name.
        source_platform: Adapter key, e.g. ``statuspage``.
        source_url: The *effective* URL fetched, after redirects.
        indicator: Page-level severity.
        description: Page-level human summary.
        components: Sorted by ``id``.
        active_incidents: Open incidents only, sorted by ``id``. Resolved
            incidents are never retained; a time-based retention window would
            make the content hash a function of wall-clock time and produce
            phantom commits as items aged out.
        scheduled_maintenances: In-progress maintenances only, same reasoning.
        fetch: Facts about our request, excluded from the hash.
    """

    provider: str
    provider_name: str
    source_platform: str
    source_url: str
    indicator: Indicator = Indicator.UNKNOWN
    description: str | None = None
    components: tuple[Component, ...] = ()
    active_incidents: tuple[Incident, ...] = ()
    scheduled_maintenances: tuple[Incident, ...] = ()
    fetch: FetchResult

    def worst_status(
        self, only: frozenset[str] | None = None
    ) -> tuple[Component | None, ComponentStatus]:
        """Return the worst-off component and its status.

        Args:
            only: If given, restrict consideration to these component IDs. Used
                to let the commit subject line speak about API components rather
                than whichever consumer surface happens to be flapping.

        Returns:
            A ``(component, status)`` pair. ``component`` is ``None`` when no
            component matched, in which case the status is ``OPERATIONAL``.
        """
        pool = [c for c in self.components if only is None or c.id in only]
        if not pool:
            return None, ComponentStatus.OPERATIONAL
        worst = max(pool, key=lambda c: (STATUS_SEVERITY.get(c.status, 1), c.id))
        return worst, worst.status


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangeEvent:
    """One discrete change, appended as a line to ``history/{provider}.jsonl``.

    Attributes:
        at: **Our observation time**, not the provider's timestamp. The honest
            claim this archive can make is "we observed X by time T", bounded by
            the poll interval.
        provider: Provider key.
        change_type: What kind of change this is.
        component_id: Component this concerns, or ``None``.
        from_: Prior value. Serialized as ``from``, which is a Python keyword.
        to: New value.
        incident_id: Incident this concerns, or ``None``.
    """

    at: str
    provider: str
    change_type: ChangeType
    component_id: str | None = None
    from_: str | None = None
    to: str | None = None
    incident_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderConfig:
    """One provider entry from ``config/providers.yaml``.

    Attributes:
        name: Short key and filename stem.
        display_name: Human-readable provider name.
        platform: Adapter registry key.
        url: Primary endpoint to poll.
        catalog_url: Secondary endpoint for ID-to-name resolution. Only Google
            needs one.
        api_components: Component IDs considered API-relevant. Drives the commit
            subject line and the query helper's headline verdict; does not affect
            what gets archived.
        product_ids: Stable upstream product IDs to track. Google only, where
            components are synthesized from this allowlist rather than discovered
            from the incident feed.
        enabled: Whether to poll this provider.
    """

    name: str
    display_name: str
    platform: str
    url: str
    catalog_url: str | None = None
    api_components: frozenset[str] = frozenset()
    product_ids: tuple[str, ...] = ()
    enabled: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class RawResponse:
    """A completed HTTP response, handed to a pure adapter.

    Attributes:
        status_code: ``200`` or ``304``.
        body: Response bytes, or ``None`` when the status was ``304``.
        headers: Response headers with lowercased keys.
        url: Effective URL after redirects.
        fetched_at: When the request completed, from an injected clock.
        elapsed_ms: Round-trip duration.
        catalog_body: Secondary catalog payload, when the provider needs one.
    """

    status_code: int
    body: bytes | None
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""
    fetched_at: str = ""
    elapsed_ms: int | None = None
    catalog_body: bytes | None = None


class FetchError(Exception):
    """A fetch that failed in a way that must not be recorded as provider state.

    Our inability to reach a status page is a fact about us, not about the
    provider. Callers translate this into a ``history/_fetch_failures.jsonl``
    entry and leave the last known good snapshot untouched.

    Attributes:
        kind: Short machine-readable category, e.g. ``timeout`` or ``http_5xx``.
        detail: Human-readable detail.
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}" if detail else kind)


class NormalizationError(Exception):
    """An upstream payload could not be normalized.

    Raised when a provider's schema has changed enough that the adapter cannot
    produce a trustworthy snapshot. Treated exactly like a fetch failure: the
    last known good file is preserved rather than overwritten with a guess.
    """
