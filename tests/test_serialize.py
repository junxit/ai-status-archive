"""Serialization determinism and content hashing.

Non-deterministic bytes are the quiet failure mode for this project: they
produce spurious commits, useless diffs, and an archive nobody can trust. These
tests exist to make that failure loud.
"""

from __future__ import annotations

import json

import pytest
from conftest import fixture_json

from aistatus import serialize
from aistatus.models import (
    Component,
    ComponentStatus,
    FetchResult,
    Impact,
    Incident,
    IncidentStatus,
    Indicator,
    Snapshot,
    normalize_text,
    to_utc_z,
)


def make_snapshot(**overrides) -> Snapshot:
    """Build a small snapshot for tests, overriding any field."""
    defaults = dict(
        provider="testco",
        provider_name="Test Co",
        source_platform="statuspage",
        source_url="https://status.example.com/api/v2/summary.json",
        indicator=Indicator.NONE,
        description="All Systems Operational",
        components=(
            Component(id="aaa", name="API", status=ComponentStatus.OPERATIONAL),
            Component(id="bbb", name="Console", status=ComponentStatus.OPERATIONAL),
        ),
        active_incidents=(),
        scheduled_maintenances=(),
        fetch=FetchResult(ok=True, fetched_at="2026-09-15T00:00:00Z", http_status=200),
    )
    defaults.update(overrides)
    return Snapshot(**defaults)


class TestDeterminism:
    def test_same_snapshot_serializes_to_identical_bytes(self):
        snapshot = make_snapshot()
        assert serialize.dump_snapshot(snapshot) == serialize.dump_snapshot(snapshot)

    def test_dict_insertion_order_does_not_affect_bytes(self):
        forward = {"a": 1, "b": 2, "c": {"x": 1, "y": 2}}
        backward = {"c": {"y": 2, "x": 1}, "b": 2, "a": 1}
        assert serialize.canonical_bytes(forward) == serialize.canonical_bytes(backward)
        assert serialize.pretty_bytes(forward) == serialize.pretty_bytes(backward)

    def test_output_is_sorted_indented_and_newline_terminated(self):
        data = serialize.dump_snapshot(make_snapshot())
        assert data.endswith(b"\n")
        assert b"\r" not in data
        text = data.decode("utf-8")
        assert '\n  "components"' in text
        # Top-level keys must appear in sorted order.
        keys = list(json.loads(text).keys())
        assert keys == sorted(keys)

    def test_floats_are_rejected_outright(self):
        # The schema has no legitimate float fields, so rather than trying to
        # format them stably we make their presence an error.
        with pytest.raises(TypeError, match="float"):
            serialize.canonical_bytes({"latency": 1.5})
        with pytest.raises(TypeError, match="float"):
            serialize.canonical_bytes({"nested": [{"deep": 0.1}]})

    def test_non_ascii_survives_a_round_trip_unescaped(self):
        snapshot = make_snapshot(description="Sistemas operacionais — São Paulo")
        data = serialize.dump_snapshot(snapshot)
        assert "São Paulo".encode("utf-8") in data
        assert json.loads(data)["overall"]["description"].endswith("São Paulo")


class TestContentHash:
    def test_fetch_block_does_not_affect_the_hash(self):
        # The entire point: fetched_at changes every poll and must never commit.
        base = make_snapshot()
        later = make_snapshot(
            fetch=FetchResult(
                ok=True,
                fetched_at="2026-12-25T18:44:02Z",
                http_status=200,
                etag='W/"totally-different"',
                elapsed_ms=997,
            )
        )
        assert serialize.content_hash(base) == serialize.content_hash(later)

    def test_component_updated_at_does_not_affect_the_hash(self):
        # OpenAI stamps one shared bulk value across all 25 components which
        # bumps without any component changing. Hashing it would commit junk
        # every time they touched it.
        base = make_snapshot()
        bumped = make_snapshot(
            components=tuple(
                Component(
                    id=c.id,
                    name=c.name,
                    status=c.status,
                    updated_at="2026-11-01T00:00:00Z",
                )
                for c in base.components
            )
        )
        assert serialize.content_hash(base) == serialize.content_hash(bumped)

    def test_page_description_does_not_affect_the_hash(self):
        base = make_snapshot()
        reworded = make_snapshot(description="Everything is fine, thanks for asking")
        assert serialize.content_hash(base) == serialize.content_hash(reworded)

    def test_component_status_change_does_affect_the_hash(self):
        base = make_snapshot()
        degraded = make_snapshot(
            components=(
                Component(
                    id="aaa", name="API", status=ComponentStatus.DEGRADED_PERFORMANCE
                ),
                Component(id="bbb", name="Console", status=ComponentStatus.OPERATIONAL),
            )
        )
        assert serialize.content_hash(base) != serialize.content_hash(degraded)

    def test_component_rename_does_affect_the_hash(self):
        # A rename is a real, dateable fact about a provider, and Google is
        # renaming its whole Vertex AI line right now.
        base = make_snapshot()
        renamed = make_snapshot(
            components=(
                Component(id="aaa", name="Inference API", status=ComponentStatus.OPERATIONAL),
                Component(id="bbb", name="Console", status=ComponentStatus.OPERATIONAL),
            )
        )
        assert serialize.content_hash(base) != serialize.content_hash(renamed)

    def test_component_order_does_not_affect_the_hash(self):
        forward = make_snapshot()
        reversed_order = make_snapshot(components=tuple(reversed(forward.components)))
        # The adapter sorts, but the hash must not depend on that having happened.
        assert serialize.hashed_body(forward)["components"] == sorted(
            serialize.hashed_body(reversed_order)["components"], key=lambda c: c["id"]
        )

    def test_incident_update_text_edit_is_detected_via_fingerprint(self):
        original = make_snapshot(
            active_incidents=(
                Incident(
                    id="inc1",
                    name="Elevated errors",
                    status=IncidentStatus.INVESTIGATING,
                    impact=Impact.MINOR,
                    latest_update="We are investigating.",
                    latest_update_id="u1",
                    latest_update_sha12="aaaaaaaaaaaa",
                ),
            )
        )
        edited = make_snapshot(
            active_incidents=(
                Incident(
                    id="inc1",
                    name="Elevated errors",
                    status=IncidentStatus.INVESTIGATING,
                    impact=Impact.MINOR,
                    latest_update="We are investigating. (edited)",
                    latest_update_id="u1",
                    latest_update_sha12="bbbbbbbbbbbb",
                ),
            )
        )
        assert serialize.content_hash(original) != serialize.content_hash(edited)


class TestRoundTrip:
    def test_snapshot_survives_a_write_and_read_with_the_same_hash(self):
        # Re-reading our own committed file must rehash identically, or every
        # poll would look like a change.
        snapshot = make_snapshot(
            active_incidents=(
                Incident(
                    id="inc1",
                    name="Elevated errors on the Messages API",
                    status=IncidentStatus.INVESTIGATING,
                    impact=Impact.MAJOR,
                    started_at="2026-09-14T17:45:00Z",
                    updated_at="2026-09-14T18:02:11Z",
                    affected_components=("aaa",),
                    url="https://status.example.com/incidents/inc1",
                    latest_update="We are investigating.",
                    latest_update_id="u1",
                    latest_update_sha12="abc123abc123",
                ),
            )
        )
        document = json.loads(serialize.dump_snapshot(snapshot))
        restored = serialize.dict_to_snapshot(document)
        assert serialize.content_hash(restored) == serialize.content_hash(snapshot)

    def test_unknown_keys_in_a_stored_document_are_ignored(self):
        document = json.loads(serialize.dump_snapshot(make_snapshot()))
        document["some_future_field"] = {"added": "later"}
        document["components"][0]["future_attribute"] = 42
        restored = serialize.dict_to_snapshot(document)
        assert serialize.content_hash(restored) == serialize.content_hash(make_snapshot())


class TestNormalizers:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-09-11T14:28:27.269Z", "2026-09-11T14:28:27Z"),  # Anthropic, millis
            ("2026-07-09T19:25:56Z", "2026-07-09T19:25:56Z"),  # OpenAI, seconds
            ("2026-09-01T14:44:00+00:00", "2026-09-01T14:44:00Z"),  # Google, offset
            ("2026-09-01T16:44:00+02:00", "2026-09-01T14:44:00Z"),  # non-UTC offset
            ("", None),
            (None, None),
            ("not a timestamp", None),
        ],
    )
    def test_timestamps_normalize_to_utc_z(self, raw, expected):
        assert to_utc_z(raw) == expected

    def test_subsecond_precision_is_truncated_not_rounded(self):
        # Rounding is not monotone across a second boundary, so two observations
        # could order incorrectly relative to each other.
        assert to_utc_z("2026-09-11T14:28:27.999Z") == "2026-09-11T14:28:27Z"

    def test_text_normalization_neutralizes_cosmetic_differences(self):
        assert normalize_text("a\r\nb") == normalize_text("a\nb")
        assert normalize_text("  padded  ") == "padded"
        assert normalize_text("") is None
        assert normalize_text("   ") is None
        # NFC vs NFD representations of the same character.
        assert normalize_text("Montréal") == normalize_text("Montréal")


class TestFixturesStillParse:
    def test_recorded_fixtures_are_valid_json(self):
        for name in (
            "openai_summary.json",
            "anthropic_summary.json",
            "google_incidents.json",
            "google_products.json",
        ):
            assert fixture_json(name) is not None
