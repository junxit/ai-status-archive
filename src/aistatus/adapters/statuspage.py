"""Adapter for Atlassian Statuspage, covering both generations in use.

OpenAI and Anthropic both publish on Statuspage, but on materially different
generations of it. Anthropic runs the classic product (short alphanumeric page
and component IDs); OpenAI runs a rebuilt one (ULID identifiers) whose payload
omits several fields the classic API guarantees:

======================================  ========  =========
Field                                   OpenAI    Anthropic
======================================  ========  =========
``summary.scheduled_maintenances``      absent    present
``incident.components[]``               absent    present
``incident.started_at``                 absent    present
``incident.resolved_at``                absent    present
``incident.shortlink``                  absent    present
``component.group``                     ``null``  ``false``
timestamp precision                     seconds   millis
======================================  ========  =========

Rather than two adapters, this is one adapter that treats every field as
possibly absent. The differences are all omissions, not contradictions.

The consequence worth knowing when querying the archive: OpenAI incidents carry
**no component linkage at all**, so ``affected_components`` is always empty for
them. That is an upstream omission, not a shortcut here.
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlsplit

from ..models import (
    Component,
    ComponentStatus,
    FetchResult,
    Impact,
    Incident,
    IncidentStatus,
    Indicator,
    NormalizationError,
    ProviderConfig,
    RawResponse,
    Snapshot,
    TERMINAL_INCIDENT_STATUSES,
    normalize_text,
    nz,
    sha12,
    to_utc_z,
)

PLATFORM = "statuspage"

#: Maintenance states that mean "happening right now". Anything merely scheduled
#: for the future is excluded: it would enter and leave the snapshot purely as a
#: function of wall-clock time, producing commits that reflect no real change.
ACTIVE_MAINTENANCE_STATUSES = frozenset(
    {IncidentStatus.IN_PROGRESS, IncidentStatus.VERIFYING}
)


def _component_status(value: Any) -> ComponentStatus:
    """Map an upstream component status onto our vocabulary."""
    try:
        return ComponentStatus(str(value))
    except ValueError:
        return ComponentStatus.UNKNOWN


def _incident_status(value: Any) -> IncidentStatus:
    """Map an upstream incident status onto our vocabulary."""
    try:
        return IncidentStatus(str(value))
    except ValueError:
        return IncidentStatus.UNKNOWN


def _impact(value: Any) -> Impact:
    """Map an upstream impact onto our vocabulary."""
    try:
        return Impact(str(value))
    except ValueError:
        return Impact.UNKNOWN


def _indicator(value: Any) -> Indicator:
    """Map an upstream page indicator onto our vocabulary."""
    try:
        return Indicator(str(value))
    except ValueError:
        return Indicator.UNKNOWN


def _page_base(payload: Mapping[str, Any], source_url: str) -> str:
    """Determine the base URL for building incident permalinks.

    Prefers the page's self-reported URL, falling back to the scheme and host of
    whatever we actually fetched. Anthropic redirects from ``status.anthropic.com``
    to ``status.claude.com``, and permalinks must point at where the content
    actually lives.

    Args:
        payload: Decoded ``summary.json``.
        source_url: Effective URL after redirects.

    Returns:
        A base URL with no trailing slash.
    """
    page = payload.get("page") or {}
    url = nz(page.get("url"))
    if not url:
        parts = urlsplit(source_url)
        url = f"{parts.scheme}://{parts.netloc}"
    return url.rstrip("/")


def _latest_update(raw: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Extract the newest update from an incident.

    Statuspage returns ``incident_updates`` newest-first, but this re-sorts by
    ``created_at`` rather than trusting position. If upstream ever changes that
    ordering, trusting it would silently start reporting the oldest update as the
    latest, which no test would catch.

    Args:
        raw: One incident object from the payload.

    Returns:
        A ``(text, update_id, text_fingerprint)`` triple, any of which may be
        ``None`` when the incident has no updates.
    """
    updates = [u for u in (raw.get("incident_updates") or ()) if isinstance(u, Mapping)]
    if not updates:
        return None, None, None
    newest = max(updates, key=lambda u: (str(u.get("created_at") or ""), str(u.get("id") or "")))
    text = normalize_text(newest.get("body"))
    return text, nz(newest.get("id")), sha12(text)


def _parse_incident(
    raw: Mapping[str, Any], *, page_base: str, is_maintenance: bool
) -> Incident:
    """Normalize one incident or scheduled maintenance.

    Args:
        raw: One incident object from the payload.
        page_base: Base URL for building the permalink.
        is_maintenance: Whether this came from ``scheduled_maintenances``.

    Returns:
        The normalized incident.
    """
    incident_id = nz(raw.get("id")) or ""
    status = _incident_status(raw.get("status"))
    text, update_id, update_hash = _latest_update(raw)

    # OpenAI publishes neither started_at nor resolved_at. created_at is the
    # closest honest substitute for onset, and a terminal status is the only
    # evidence of resolution time available.
    started_at = to_utc_z(raw.get("started_at")) or to_utc_z(raw.get("created_at"))
    updated_at = to_utc_z(raw.get("updated_at"))
    resolved_at = to_utc_z(raw.get("resolved_at"))
    if resolved_at is None and status in TERMINAL_INCIDENT_STATUSES:
        resolved_at = updated_at

    components = tuple(
        sorted(
            component_id
            for component in (raw.get("components") or ())
            if isinstance(component, Mapping)
            and (component_id := nz(component.get("id")))
        )
    )

    return Incident(
        id=incident_id,
        name=nz(raw.get("name")) or "(untitled)",
        status=status,
        impact=_impact(raw.get("impact") or ("maintenance" if is_maintenance else None)),
        started_at=started_at,
        updated_at=updated_at,
        resolved_at=resolved_at,
        affected_components=components,
        url=nz(raw.get("shortlink")) or f"{page_base}/incidents/{incident_id}",
        latest_update=text,
        latest_update_id=update_id,
        latest_update_sha12=update_hash,
    )


def parse(
    payload: Any,
    *,
    cfg: ProviderConfig,
    fetch: FetchResult,
    source_url: str,
    catalog: Any = None,
) -> Snapshot:
    """Normalize a Statuspage ``summary.json`` payload into a snapshot.

    Args:
        payload: Decoded ``summary.json``.
        cfg: Provider configuration.
        fetch: Facts about the HTTP request that produced ``payload``.
        source_url: Effective URL after redirects.
        catalog: Unused; Statuspage needs no secondary catalog request.

    Returns:
        The normalized snapshot.

    Raises:
        NormalizationError: If the payload is not a mapping or carries no
            components. Both indicate a schema change severe enough that writing
            a snapshot would record a fiction; the caller preserves the last
            known good file instead.
    """
    if not isinstance(payload, Mapping):
        raise NormalizationError(
            f"{cfg.name}: expected a JSON object, got {type(payload).__name__}"
        )

    raw_components = [
        c for c in (payload.get("components") or ()) if isinstance(c, Mapping)
    ]
    if not raw_components:
        raise NormalizationError(
            f"{cfg.name}: payload contained no components; schema likely changed"
        )

    page_base = _page_base(payload, source_url)

    # Group headers are themselves entries in the component list, flagged by a
    # truthy `group`. Children point at them by `group_id`.
    group_names = {
        component_id: nz(c.get("name")) or ""
        for c in raw_components
        if c.get("group") and (component_id := nz(c.get("id")))
    }

    components = tuple(
        sorted(
            (
                Component(
                    id=component_id,
                    name=nz(c.get("name")) or "(unnamed)",
                    group=group_names.get(nz(c.get("group_id")) or ""),
                    status=_component_status(c.get("status")),
                    updated_at=to_utc_z(c.get("updated_at")),
                )
                for c in raw_components
                if not c.get("group") and (component_id := nz(c.get("id")))
            ),
            key=lambda c: c.id,
        )
    )

    # Only unresolved incidents are retained. A resolved one disappearing from
    # the feed is how resolution is detected downstream; keeping them around on a
    # time window would make the content hash depend on wall-clock time.
    incidents = []
    for raw in payload.get("incidents") or ():
        if not isinstance(raw, Mapping):
            continue
        incident = _parse_incident(raw, page_base=page_base, is_maintenance=False)
        if incident.status not in TERMINAL_INCIDENT_STATUSES:
            incidents.append(incident)

    # OpenAI omits this key entirely and 404s the dedicated endpoint, so an
    # absent value must mean "none published", never an error.
    maintenances = []
    for raw in payload.get("scheduled_maintenances") or ():
        if not isinstance(raw, Mapping):
            continue
        maintenance = _parse_incident(raw, page_base=page_base, is_maintenance=True)
        if maintenance.status in ACTIVE_MAINTENANCE_STATUSES:
            maintenances.append(maintenance)

    status_block = payload.get("status") or {}

    return Snapshot(
        provider=cfg.name,
        provider_name=cfg.display_name,
        source_platform=PLATFORM,
        source_url=source_url,
        indicator=_indicator(status_block.get("indicator")),
        description=nz(status_block.get("description")),
        components=components,
        active_incidents=tuple(sorted(incidents, key=lambda i: i.id)),
        scheduled_maintenances=tuple(sorted(maintenances, key=lambda i: i.id)),
        fetch=fetch,
    )


__all__ = ["ACTIVE_MAINTENANCE_STATUSES", "PLATFORM", "parse"]
