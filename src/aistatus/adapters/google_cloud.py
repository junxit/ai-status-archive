"""Adapter for the Google Cloud status feed.

Google's feed is structurally unlike Statuspage, and reconciling that difference
is this module's whole job. Statuspage publishes *current component state*
directly. Google publishes only *incidents*, platform-wide across all of GCP,
with no component states at all — so current state has to be derived from which
incidents are open, and the result must look identical to a Statuspage snapshot
by the time it leaves here.

Two design rules follow from that, and both exist to prevent phantom commits:

1. **The component set is the configured allowlist, not the incident set.** If
   components were discovered from whichever products currently appear in
   incidents, they would blink in and out of existence between polls, producing
   ``null -> operational`` churn instead of clean status transitions.

2. **Nothing here may read the clock.** Derived state is a pure function of the
   open-incident set. A synthetic ``updated_at`` stamped with ``now()`` would
   change the content hash on every single poll and defeat commit-on-change
   entirely. Components that are operational carry ``updated_at = None``; those
   affected by an incident inherit that incident's ``modified`` time.

Filtering is on **stable product IDs**, never display names. Google's own schema
says ``id`` "is stable" while ``title`` "is unstable and could change without
warning" — and they are mid-rebrand from "Vertex AI" to "Agent Platform" right
now, so a name-based filter would silently start matching nothing.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..models import (
    STATUS_SEVERITY,
    Component,
    ComponentStatus,
    FetchResult,
    Impact,
    Incident,
    IncidentStatus,
    Indicator,
    NormalizationError,
    ProviderConfig,
    Snapshot,
    normalize_text,
    nz,
    sha12,
    to_utc_z,
)

PLATFORM = "google_cloud"

#: How Google's ``status_impact`` maps onto our component vocabulary.
IMPACT_TO_COMPONENT_STATUS: dict[str, ComponentStatus] = {
    "SERVICE_OUTAGE": ComponentStatus.MAJOR_OUTAGE,
    "SERVICE_DISRUPTION": ComponentStatus.PARTIAL_OUTAGE,
    "SERVICE_INFORMATION": ComponentStatus.DEGRADED_PERFORMANCE,
}

#: How Google's ``status_impact`` maps onto our incident impact vocabulary.
IMPACT_TO_INCIDENT_IMPACT: dict[str, Impact] = {
    "SERVICE_OUTAGE": Impact.CRITICAL,
    "SERVICE_DISRUPTION": Impact.MAJOR,
    "SERVICE_INFORMATION": Impact.MINOR,
}

#: How the worst component status rolls up into a page-level indicator.
STATUS_TO_INDICATOR: dict[ComponentStatus, Indicator] = {
    ComponentStatus.MAJOR_OUTAGE: Indicator.CRITICAL,
    ComponentStatus.PARTIAL_OUTAGE: Indicator.MAJOR,
    ComponentStatus.DEGRADED_PERFORMANCE: Indicator.MINOR,
    ComponentStatus.UNDER_MAINTENANCE: Indicator.MAINTENANCE,
    ComponentStatus.OPERATIONAL: Indicator.NONE,
}

INCIDENT_BASE_URL = "https://status.cloud.google.com/"


def _is_open(raw: Mapping[str, Any]) -> bool:
    """Determine whether an incident is still ongoing.

    Google marks closure by populating ``end``. All six incidents in the feed as
    observed carry an ``end``, so this is deliberately defensive about the three
    ways "no end" can be represented.

    Args:
        raw: One incident object from the feed.

    Returns:
        ``True`` when the incident has no end time.
    """
    return nz(raw.get("end")) is None


def _product_ids(raw: Mapping[str, Any]) -> frozenset[str]:
    """Collect the stable product IDs an incident affects."""
    return frozenset(
        product_id
        for product in raw.get("affected_products") or ()
        if isinstance(product, Mapping) and (product_id := nz(product.get("id")))
    )


def _parse_catalog(catalog: Any) -> dict[str, str]:
    """Build a stable-ID to display-name map from ``products.json``.

    Args:
        catalog: Decoded ``products.json``.

    Returns:
        Mapping of product ID to current display name.

    Raises:
        NormalizationError: If the catalog is missing or malformed. This is
            deliberately fatal: without it, component names would fall back to
            raw IDs, and since names participate in the content hash, a transient
            catalog failure would rewrite every component name twice and produce
            two junk commits.
    """
    if not isinstance(catalog, Mapping):
        raise NormalizationError(
            "google: product catalog missing or not an object; refusing to "
            "guess component names"
        )
    products = catalog.get("products")
    if not isinstance(products, list) or not products:
        raise NormalizationError("google: product catalog contained no products")

    names: dict[str, str] = {}
    for product in products:
        if not isinstance(product, Mapping):
            continue
        product_id = nz(product.get("id"))
        if not product_id:
            continue
        # `current_title` is the live display name; `title` is the historical one
        # the feed was originally published under. Preferring current_title means
        # a rename is recorded when it happens, which is a fact worth dating.
        names[product_id] = (
            nz(product.get("current_title")) or nz(product.get("title")) or product_id
        )
    return names


def _parse_incident(raw: Mapping[str, Any], *, tracked: frozenset[str]) -> Incident:
    """Normalize one Google incident.

    Args:
        raw: One incident object from the feed.
        tracked: Product IDs we archive, used to narrow ``affected_components``
            to the AI surface rather than listing all of GCP.

    Returns:
        The normalized incident.
    """
    status_impact = str(raw.get("status_impact") or "")
    update = raw.get("most_recent_update")
    update = update if isinstance(update, Mapping) else {}
    text = normalize_text(update.get("text"))

    resolved_at = to_utc_z(raw.get("end"))
    if resolved_at is not None:
        status = IncidentStatus.RESOLVED
    elif str(update.get("status") or "").upper() == "AVAILABLE":
        # Google posts AVAILABLE once service is restored but before the incident
        # is formally closed, which is exactly what "monitoring" means elsewhere.
        status = IncidentStatus.MONITORING
    else:
        status = IncidentStatus.INVESTIGATING

    incident_id = nz(raw.get("id")) or ""
    uri = nz(raw.get("uri")) or f"incidents/{incident_id}"

    return Incident(
        id=incident_id,
        name=normalize_text(raw.get("external_desc")) or "(untitled)",
        status=status,
        impact=IMPACT_TO_INCIDENT_IMPACT.get(status_impact, Impact.UNKNOWN),
        started_at=to_utc_z(raw.get("begin")),
        updated_at=to_utc_z(raw.get("modified")),
        resolved_at=resolved_at,
        affected_components=tuple(sorted(_product_ids(raw) & tracked)),
        url=INCIDENT_BASE_URL + uri.lstrip("/"),
        latest_update=text,
        # Google exposes no update ID, so synthesize a stable one from the
        # update's own creation time plus a fingerprint of its text. Both are
        # properties of the update itself, so this never varies between polls.
        latest_update_id=sha12(f"{nz(update.get('created')) or ''}|{text or ''}"),
        latest_update_sha12=sha12(text),
    )


def parse(
    payload: Any,
    *,
    cfg: ProviderConfig,
    fetch: FetchResult,
    source_url: str,
    catalog: Any = None,
) -> Snapshot:
    """Normalize the Google Cloud incident feed into a snapshot.

    Args:
        payload: Decoded ``incidents.json``, a JSON array.
        cfg: Provider configuration, supplying the tracked product allowlist.
        fetch: Facts about the HTTP request that produced ``payload``.
        source_url: Effective URL after redirects.
        catalog: Decoded ``products.json``, required for component naming.

    Returns:
        The normalized snapshot, with one component per configured product ID.

    Raises:
        NormalizationError: If the feed is not a list, if no product IDs are
            configured, or if the catalog is unusable.
    """
    if not isinstance(payload, list):
        raise NormalizationError(
            f"{cfg.name}: expected a JSON array, got {type(payload).__name__}"
        )
    if not cfg.product_ids:
        raise NormalizationError(
            f"{cfg.name}: no product_ids configured; nothing to track"
        )

    names = _parse_catalog(catalog)
    tracked = frozenset(cfg.product_ids)

    open_incidents: list[Incident] = []
    # Product ID -> (worst status seen, that incident's modified time).
    derived: dict[str, tuple[ComponentStatus, str | None]] = {}

    for raw in payload:
        if not isinstance(raw, Mapping):
            continue
        affected = _product_ids(raw) & tracked
        if not affected:
            continue
        if not _is_open(raw):
            continue

        incident = _parse_incident(raw, tracked=tracked)
        open_incidents.append(incident)

        status = IMPACT_TO_COMPONENT_STATUS.get(
            str(raw.get("status_impact") or ""), ComponentStatus.DEGRADED_PERFORMANCE
        )
        for product_id in affected:
            current = derived.get(product_id)
            if current is None or STATUS_SEVERITY[status] > STATUS_SEVERITY[current[0]]:
                derived[product_id] = (status, incident.updated_at)

    components = []
    for product_id in sorted(tracked):
        # Operational components carry no timestamp at all. Stamping them with
        # the current time would rewrite this file every five minutes forever.
        status, updated_at = derived.get(product_id, (ComponentStatus.OPERATIONAL, None))
        components.append(
            Component(
                id=product_id,
                name=names.get(product_id, product_id),
                group=None,
                status=status,
                updated_at=updated_at,
            )
        )

    worst = max(
        (c.status for c in components),
        key=lambda s: STATUS_SEVERITY[s],
        default=ComponentStatus.OPERATIONAL,
    )
    affected_count = sum(
        1 for c in components if c.status is not ComponentStatus.OPERATIONAL
    )
    description = (
        "All tracked AI products operational"
        if affected_count == 0
        else f"{affected_count} of {len(components)} tracked AI products affected"
    )

    return Snapshot(
        provider=cfg.name,
        provider_name=cfg.display_name,
        source_platform=PLATFORM,
        source_url=source_url,
        indicator=STATUS_TO_INDICATOR.get(worst, Indicator.UNKNOWN),
        description=description,
        components=tuple(components),
        active_incidents=tuple(sorted(open_incidents, key=lambda i: i.id)),
        scheduled_maintenances=(),
        fetch=fetch,
    )


__all__ = [
    "IMPACT_TO_COMPONENT_STATUS",
    "IMPACT_TO_INCIDENT_IMPACT",
    "INCIDENT_BASE_URL",
    "PLATFORM",
    "STATUS_TO_INDICATOR",
    "parse",
]
