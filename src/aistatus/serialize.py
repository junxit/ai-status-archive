"""Deterministic JSON serialization and content hashing.

Non-deterministic output is the failure mode that quietly ruins this archive: it
produces spurious commits, useless diffs, and a history that cannot be trusted.
Every rule that prevents it lives here, and this module is the only thing in the
project permitted to turn an object into JSON bytes.

Two documents are produced from one :class:`~aistatus.models.Snapshot`:

* The **full document** written to ``providers/{name}.json``. It carries
  everything, including the provider's own timestamps and our ``fetch`` block.
* The **hashed body**, an explicit and deliberately small subset used only to
  decide whether anything changed.

The hashed body is a positive allowlist rather than "the full document minus
``fetch``". That distinction matters: OpenAI publishes one shared bulk
``updated_at`` across all 25 components which bumps without any component
actually changing, so a blanket exclusion would let it churn the hash every poll
and defeat the entire commit-on-change design.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .models import (
    SCHEMA_VERSION,
    ChangeEvent,
    ChangeType,
    Component,
    ComponentStatus,
    FetchResult,
    Impact,
    Incident,
    IncidentStatus,
    Indicator,
    Snapshot,
)

#: Maximum characters of incident update text retained in the normalized file.
#: Google attaches full postmortems here, which are rewritten weeks after the
#: fact; the untruncated text lives in ``raw/`` where churn is expected.
LATEST_UPDATE_MAX_CHARS = 500


def _reject_floats(obj: Any, path: str = "$") -> None:
    """Assert that no float appears anywhere in ``obj``.

    Float repr is a classic source of cross-platform byte instability. The schema
    has no legitimate float fields, so rather than trying to format them
    consistently we make their presence a hard error.

    Args:
        obj: Object about to be serialized.
        path: JSON path accumulated during recursion, used in the error message.

    Raises:
        TypeError: If a float is found.
    """
    if isinstance(obj, float):
        raise TypeError(f"float at {path} would make serialization unstable")
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            _reject_floats(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            _reject_floats(value, f"{path}[{index}]")


def canonical_bytes(obj: Any) -> bytes:
    """Serialize to the most compact deterministic form, for hashing.

    Args:
        obj: A JSON-compatible object.

    Returns:
        UTF-8 bytes with sorted keys, no insignificant whitespace, and no
        trailing newline.

    Raises:
        TypeError: If the object contains a float.
    """
    _reject_floats(obj)
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def pretty_bytes(obj: Any) -> bytes:
    """Serialize to the human-readable form written to disk.

    Args:
        obj: A JSON-compatible object.

    Returns:
        UTF-8 bytes with sorted keys, two-space indent, and a trailing newline.

    Raises:
        TypeError: If the object contains a float.
    """
    _reject_floats(obj)
    text = json.dumps(
        obj,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    )
    return (text + "\n").encode("utf-8")


def _truncate(text: str | None, limit: int = LATEST_UPDATE_MAX_CHARS) -> str | None:
    """Truncate text on a character boundary, appending an ellipsis marker."""
    if text is None or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def component_to_dict(component: Component) -> dict[str, Any]:
    """Render a component for the full on-disk document."""
    return {
        "id": component.id,
        "name": component.name,
        "group": component.group,
        "status": str(component.status),
        "updated_at": component.updated_at,
    }


def incident_to_dict(incident: Incident) -> dict[str, Any]:
    """Render an incident for the full on-disk document."""
    return {
        "id": incident.id,
        "name": incident.name,
        "status": str(incident.status),
        "impact": str(incident.impact),
        "started_at": incident.started_at,
        "updated_at": incident.updated_at,
        "resolved_at": incident.resolved_at,
        "affected_components": list(incident.affected_components),
        "url": incident.url,
        "latest_update": _truncate(incident.latest_update),
        "latest_update_id": incident.latest_update_id,
        "latest_update_sha12": incident.latest_update_sha12,
    }


def fetch_to_dict(fetch: FetchResult) -> dict[str, Any]:
    """Render the fetch block for the full on-disk document."""
    return {
        "ok": fetch.ok,
        "fetched_at": fetch.fetched_at,
        "http_status": fetch.http_status,
        "error": fetch.error,
        "etag": fetch.etag,
        "last_modified": fetch.last_modified,
        "content_hash": fetch.content_hash,
        "elapsed_ms": fetch.elapsed_ms,
    }


def snapshot_to_dict(snapshot: Snapshot) -> dict[str, Any]:
    """Render a snapshot as the full document written to ``providers/``.

    Every key is emitted explicitly, including those whose value is ``None``.
    Conditionally omitting a null key would mean a provider switching between
    ``null`` and absent changes our bytes without changing any fact.

    Args:
        snapshot: The snapshot to render.

    Returns:
        A JSON-compatible dict.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": snapshot.provider,
        "provider_name": snapshot.provider_name,
        "source_platform": snapshot.source_platform,
        "source_url": snapshot.source_url,
        "overall": {
            "indicator": str(snapshot.indicator),
            "description": snapshot.description,
        },
        "components": [component_to_dict(c) for c in snapshot.components],
        "active_incidents": [incident_to_dict(i) for i in snapshot.active_incidents],
        "scheduled_maintenances": [
            incident_to_dict(i) for i in snapshot.scheduled_maintenances
        ],
        "fetch": fetch_to_dict(snapshot.fetch),
    }


def hashed_body(snapshot: Snapshot) -> dict[str, Any]:
    """Render the subset of a snapshot that decides whether state changed.

    Deliberately excluded, each for a specific reason:

    * The whole ``fetch`` block, which varies every poll by construction.
    * ``Component.updated_at`` — OpenAI's is a shared bulk value that bumps
      without any component changing, and Google's is synthesized.
    * ``Incident.updated_at`` — churns on cosmetic provider re-saves.
      ``latest_update_id`` and ``latest_update_sha12`` carry the real signal.
    * ``Incident.latest_update`` text and ``url`` — the fingerprint already
      covers meaningful edits without dragging a postmortem into the hash.
    * ``description`` — free-text page blurb that providers reword; ``indicator``
      is the fact that matters.

    Included on purpose: ``name``. A component rename is a genuine, dateable fact
    about a provider, and Google is renaming its entire Vertex AI line right now.

    Args:
        snapshot: The snapshot to reduce.

    Returns:
        A JSON-compatible dict containing only change-significant fields.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": snapshot.provider,
        "source_platform": snapshot.source_platform,
        "indicator": str(snapshot.indicator),
        "components": [
            {
                "id": c.id,
                "name": c.name,
                "group": c.group,
                "status": str(c.status),
            }
            for c in snapshot.components
        ],
        "active_incidents": [
            {
                "id": i.id,
                "name": i.name,
                "status": str(i.status),
                "impact": str(i.impact),
                "started_at": i.started_at,
                "resolved_at": i.resolved_at,
                "affected_components": list(i.affected_components),
                "latest_update_id": i.latest_update_id,
                "latest_update_sha12": i.latest_update_sha12,
            }
            for i in snapshot.active_incidents
        ],
        "scheduled_maintenances": [
            {
                "id": i.id,
                "name": i.name,
                "status": str(i.status),
                "impact": str(i.impact),
                "started_at": i.started_at,
                "affected_components": list(i.affected_components),
                "latest_update_id": i.latest_update_id,
                "latest_update_sha12": i.latest_update_sha12,
            }
            for i in snapshot.scheduled_maintenances
        ],
    }


def content_hash(snapshot: Snapshot) -> str:
    """Compute the change-detection hash for a snapshot.

    Args:
        snapshot: The snapshot to hash.

    Returns:
        ``sha256:<64 hex chars>`` over :func:`hashed_body`.
    """
    digest = hashlib.sha256(canonical_bytes(hashed_body(snapshot))).hexdigest()
    return f"sha256:{digest}"


def _enum(value: Any, enum_cls: Any, default: Any) -> Any:
    """Coerce a stored string back to an enum member, falling back to ``default``."""
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


def dict_to_snapshot(doc: Mapping[str, Any]) -> Snapshot:
    """Parse a previously written ``providers/{name}.json`` back into a snapshot.

    Deliberately lenient: unknown keys are ignored and missing keys take the same
    defaults the writer would have emitted. That means an older file re-hashes to
    the same value it had when written, so a schema addition does not masquerade
    as a status change.

    Args:
        doc: Parsed JSON document.

    Returns:
        The reconstructed snapshot.
    """
    overall = doc.get("overall") or {}
    fetch_doc = doc.get("fetch") or {}

    def _incidents(key: str) -> tuple[Incident, ...]:
        return tuple(
            Incident(
                id=str(raw.get("id", "")),
                name=str(raw.get("name", "")),
                status=_enum(raw.get("status"), IncidentStatus, IncidentStatus.UNKNOWN),
                impact=_enum(raw.get("impact"), Impact, Impact.UNKNOWN),
                started_at=raw.get("started_at"),
                updated_at=raw.get("updated_at"),
                resolved_at=raw.get("resolved_at"),
                affected_components=tuple(raw.get("affected_components") or ()),
                url=raw.get("url"),
                latest_update=raw.get("latest_update"),
                latest_update_id=raw.get("latest_update_id"),
                latest_update_sha12=raw.get("latest_update_sha12"),
            )
            for raw in doc.get(key) or ()
        )

    return Snapshot(
        provider=str(doc.get("provider", "")),
        provider_name=str(doc.get("provider_name", "")),
        source_platform=str(doc.get("source_platform", "")),
        source_url=str(doc.get("source_url", "")),
        indicator=_enum(overall.get("indicator"), Indicator, Indicator.UNKNOWN),
        description=overall.get("description"),
        components=tuple(
            Component(
                id=str(raw.get("id", "")),
                name=str(raw.get("name", "")),
                group=raw.get("group"),
                status=_enum(
                    raw.get("status"), ComponentStatus, ComponentStatus.UNKNOWN
                ),
                updated_at=raw.get("updated_at"),
            )
            for raw in doc.get("components") or ()
        ),
        active_incidents=_incidents("active_incidents"),
        scheduled_maintenances=_incidents("scheduled_maintenances"),
        fetch=FetchResult(
            ok=bool(fetch_doc.get("ok", False)),
            fetched_at=str(fetch_doc.get("fetched_at", "")),
            http_status=fetch_doc.get("http_status"),
            error=fetch_doc.get("error"),
            etag=fetch_doc.get("etag"),
            last_modified=fetch_doc.get("last_modified"),
            content_hash=fetch_doc.get("content_hash"),
            elapsed_ms=fetch_doc.get("elapsed_ms"),
        ),
    )


def event_to_dict(event: ChangeEvent) -> dict[str, Any]:
    """Render a change event, mapping ``from_`` back to the ``from`` key."""
    return {
        "at": event.at,
        "provider": event.provider,
        "change_type": str(event.change_type),
        "component_id": event.component_id,
        "from": event.from_,
        "to": event.to,
        "incident_id": event.incident_id,
    }


def event_line(event: ChangeEvent) -> bytes:
    """Render a change event as one newline-terminated JSONL line."""
    return canonical_bytes(event_to_dict(event)) + b"\n"


def failure_line(
    *, at: str, provider: str, kind: str, detail: str, http_status: int | None
) -> bytes:
    """Render one line for ``history/_fetch_failures.jsonl``.

    Kept in a separate file from real status history on purpose. Our inability to
    reach a status page is a fact about us, not about the provider, and mixing
    the two would make an outage of ours look like an outage of theirs.

    Args:
        at: Observation time.
        provider: Provider key.
        kind: Short failure category.
        detail: Human-readable detail.
        http_status: Status code if one was received.

    Returns:
        One newline-terminated JSONL line.
    """
    return (
        canonical_bytes(
            {
                "at": at,
                "provider": provider,
                "kind": kind,
                "detail": detail,
                "http_status": http_status,
            }
        )
        + b"\n"
    )


def dump_snapshot(snapshot: Snapshot) -> bytes:
    """Render the bytes written to ``providers/{name}.json``."""
    return pretty_bytes(snapshot_to_dict(snapshot))


def dump_raw(payload: Any) -> bytes:
    """Render the bytes written to ``raw/{name}.json``.

    The upstream payload is stored key-sorted and pretty-printed rather than
    verbatim so that a provider reordering their JSON keys does not show up as a
    diff. Providers edit, merge, and backdate incidents after the fact; keeping
    these snapshots in git history is what later lets us prove that an incident's
    ``started_at`` was rewritten.

    Args:
        payload: Decoded upstream JSON.

    Returns:
        UTF-8 bytes, sorted keys, two-space indent, trailing newline.
    """
    return pretty_bytes(payload)


def slug(text: str, limit: int = 24) -> str:
    """Reduce a component name to a short token for a commit subject line.

    Args:
        text: Component display name.
        limit: Maximum length of the result.

    Returns:
        A lowercase token with non-alphanumeric runs collapsed to underscores.
    """
    out: list[str] = []
    previous_underscore = False
    for char in text.lower():
        if char.isalnum():
            out.append(char)
            previous_underscore = False
        elif not previous_underscore:
            out.append("_")
            previous_underscore = True
    return "".join(out).strip("_")[:limit].strip("_") or "component"


__all__ = [
    "LATEST_UPDATE_MAX_CHARS",
    "canonical_bytes",
    "content_hash",
    "dict_to_snapshot",
    "dump_raw",
    "dump_snapshot",
    "event_line",
    "event_to_dict",
    "failure_line",
    "hashed_body",
    "pretty_bytes",
    "slug",
    "snapshot_to_dict",
]
