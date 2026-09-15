"""Derive discrete change events by comparing two snapshots.

These events become the lines in ``history/{provider}.jsonl`` — the file that is
actually grepped when answering "what changed, and when?". Getting the vocabulary
right matters more than getting it rich: every event must mean exactly one thing.

Two invariants hold this module together:

* **Only the hashed body is consulted.** This function must never read
  ``snapshot.fetch``, which is why the observation time arrives as the ``at``
  parameter rather than being read off the snapshot. That buys a property worth
  testing directly: equal content hashes must produce zero events.
* **Everything is keyed on stable IDs.** Display names are never keys; providers
  rename things, and Google is renaming its entire Vertex AI line right now.

Emission order is fully deterministic so that two runs over the same inputs write
byte-identical lines in the same sequence.
"""

from __future__ import annotations

from .models import (
    TERMINAL_INCIDENT_STATUSES,
    ChangeEvent,
    ChangeType,
    Component,
    Incident,
    IncidentStatus,
    Snapshot,
)


def _incidents_by_id(snapshot: Snapshot) -> dict[str, Incident]:
    """Index every incident and in-progress maintenance by stable ID."""
    return {
        incident.id: incident
        for incident in (*snapshot.active_incidents, *snapshot.scheduled_maintenances)
    }


def _components_by_id(snapshot: Snapshot) -> dict[str, Component]:
    """Index components by stable ID."""
    return {component.id: component for component in snapshot.components}


def _baseline(snapshot: Snapshot, *, at: str) -> tuple[ChangeEvent, ...]:
    """Produce the single event recorded the first time a provider is seen.

    Emitting one marker rather than one event per component is deliberate. The
    complete state is already captured in ``providers/{name}.json`` at the same
    commit, so a per-component burst would be pure redundancy — and a 25-line
    burst for OpenAI would be indistinguishable from a real 25-component incident
    to anyone grepping the history later. Worse, it would permanently skew any
    "count the outages" query by the bootstrap.

    Args:
        snapshot: The first snapshot observed for this provider.
        at: Observation time.

    Returns:
        A one-element tuple.
    """
    return (
        ChangeEvent(
            at=at,
            provider=snapshot.provider,
            change_type=ChangeType.BASELINE,
            from_=None,
            to=str(snapshot.indicator),
        ),
    )


def diff_snapshots(
    previous: Snapshot | None, current: Snapshot, *, at: str
) -> tuple[ChangeEvent, ...]:
    """Compare two snapshots and return the changes between them.

    Args:
        previous: Prior state, or ``None`` on the first observation.
        current: Newly collected state.
        at: Our observation time, stamped onto every event. This is deliberately
            our clock and not the provider's: the honest claim this archive makes
            is "we observed X by time T", bounded by the poll interval.

    Returns:
        Events in a stable order: overall indicator, then component changes by
        component ID, then incident changes by incident ID.
    """
    if previous is None:
        return _baseline(current, at=at)

    provider = current.provider
    events: list[ChangeEvent] = []

    if previous.indicator != current.indicator:
        events.append(
            ChangeEvent(
                at=at,
                provider=provider,
                change_type=ChangeType.OVERALL_INDICATOR,
                from_=str(previous.indicator),
                to=str(current.indicator),
            )
        )

    events.extend(_component_events(previous, current, at=at, provider=provider))
    events.extend(_incident_events(previous, current, at=at, provider=provider))
    return tuple(events)


def _component_events(
    previous: Snapshot, current: Snapshot, *, at: str, provider: str
) -> list[ChangeEvent]:
    """Derive component status and rename events."""
    before = _components_by_id(previous)
    after = _components_by_id(current)
    events: list[ChangeEvent] = []

    for component_id in sorted(before.keys() | after.keys()):
        old = before.get(component_id)
        new = after.get(component_id)

        old_status = str(old.status) if old else None
        new_status = str(new.status) if new else None
        if old_status != new_status:
            events.append(
                ChangeEvent(
                    at=at,
                    provider=provider,
                    change_type=ChangeType.COMPONENT_STATUS,
                    component_id=component_id,
                    from_=old_status,
                    to=new_status,
                )
            )

        # A rename is its own kind of fact. Recording it explicitly is what lets
        # someone later explain why a file changed on a day nothing broke.
        if old and new and old.name != new.name:
            events.append(
                ChangeEvent(
                    at=at,
                    provider=provider,
                    change_type=ChangeType.COMPONENT_RENAMED,
                    component_id=component_id,
                    from_=old.name,
                    to=new.name,
                )
            )

    return events


def _incident_events(
    previous: Snapshot, current: Snapshot, *, at: str, provider: str
) -> list[ChangeEvent]:
    """Derive incident lifecycle events."""
    before = _incidents_by_id(previous)
    after = _incidents_by_id(current)
    events: list[ChangeEvent] = []

    for incident_id in sorted(before.keys() | after.keys()):
        old = before.get(incident_id)
        new = after.get(incident_id)

        if old is None and new is not None:
            events.append(
                ChangeEvent(
                    at=at,
                    provider=provider,
                    change_type=ChangeType.INCIDENT_OPENED,
                    incident_id=incident_id,
                    from_=None,
                    to=str(new.status),
                )
            )
            continue

        if old is not None and new is None:
            # Snapshots retain open incidents only, so a disappearance is how
            # resolution normally arrives: Statuspage drops resolved incidents
            # out of summary.json rather than restating them. The guard matters
            # for Google, whose closed incidents linger in the feed with `end`
            # set and only later age out — without it, every aged-out incident
            # would emit a duplicate resolution weeks after the fact.
            if old.resolved_at is None:
                events.append(
                    ChangeEvent(
                        at=at,
                        provider=provider,
                        change_type=ChangeType.INCIDENT_RESOLVED,
                        incident_id=incident_id,
                        from_=str(old.status),
                        to=str(IncidentStatus.RESOLVED),
                    )
                )
            continue

        assert old is not None and new is not None
        if old.status != new.status:
            change_type = (
                ChangeType.INCIDENT_RESOLVED
                if new.status in TERMINAL_INCIDENT_STATUSES
                else ChangeType.INCIDENT_UPDATED
            )
            events.append(
                ChangeEvent(
                    at=at,
                    provider=provider,
                    change_type=change_type,
                    incident_id=incident_id,
                    from_=str(old.status),
                    to=str(new.status),
                )
            )
        elif old.latest_update_id != new.latest_update_id or (
            old.latest_update_sha12 != new.latest_update_sha12
        ):
            # A new update posted with no status transition. `from` equals `to`
            # on purpose: that equality is the unambiguous marker for it. Update
            # identifiers deliberately do not go in these fields — mixing two
            # vocabularies into from/to would make the dataset unqueryable.
            events.append(
                ChangeEvent(
                    at=at,
                    provider=provider,
                    change_type=ChangeType.INCIDENT_UPDATED,
                    incident_id=incident_id,
                    from_=str(old.status),
                    to=str(new.status),
                )
            )

    return events


__all__ = ["diff_snapshots"]
