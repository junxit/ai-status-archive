"""Change event derivation.

``history/{provider}.jsonl`` is the file that actually gets grepped when
answering "what changed, and when?". Every event must mean exactly one thing,
and the count must be exact: one transition produces one line, never two.
"""

from __future__ import annotations

from test_serialize import make_snapshot

from aistatus import serialize
from aistatus.diff import diff_snapshots
from aistatus.models import (
    ChangeType,
    Component,
    ComponentStatus,
    Impact,
    Incident,
    IncidentStatus,
    Indicator,
)

AT = "2026-09-15T03:14:00Z"


def incident(**overrides) -> Incident:
    """Build an incident for tests, overriding any field."""
    defaults = dict(
        id="inc1",
        name="Elevated error rates on the Messages API",
        status=IncidentStatus.INVESTIGATING,
        impact=Impact.MAJOR,
        started_at="2026-09-15T02:45:00Z",
        updated_at="2026-09-15T03:00:00Z",
        affected_components=("aaa",),
        latest_update="We are investigating.",
        latest_update_id="u1",
        latest_update_sha12="aaaaaaaaaaaa",
    )
    defaults.update(overrides)
    return Incident(**defaults)


def degraded(component_id: str = "aaa"):
    """A component tuple with one component degraded."""
    return (
        Component(
            id=component_id, name="API", status=ComponentStatus.DEGRADED_PERFORMANCE
        ),
        Component(id="bbb", name="Console", status=ComponentStatus.OPERATIONAL),
    )


class TestBaseline:
    def test_first_observation_emits_one_marker_not_a_burst(self):
        # A 25-line burst for OpenAI would be indistinguishable from a real
        # 25-component incident to anyone grepping later, and would permanently
        # skew any "count the outages" query by the bootstrap.
        events = diff_snapshots(None, make_snapshot(), at=AT)
        assert len(events) == 1
        assert events[0].change_type is ChangeType.BASELINE
        assert events[0].from_ is None
        assert events[0].to == "none"

    def test_baseline_is_emitted_even_for_a_provider_with_many_components(self):
        many = make_snapshot(
            components=tuple(
                Component(id=f"c{i:02d}", name=f"Component {i}", status=ComponentStatus.OPERATIONAL)
                for i in range(25)
            )
        )
        assert len(diff_snapshots(None, many, at=AT)) == 1


class TestNoChange:
    def test_identical_snapshots_produce_no_events(self):
        assert diff_snapshots(make_snapshot(), make_snapshot(), at=AT) == ()

    def test_equal_hashes_imply_no_events(self):
        # The invariant that ties diff.py and serialize.py together. A violation
        # means either the hashed body or the diff vocabulary is wrong.
        before = make_snapshot()
        after = make_snapshot(
            description="Reworded blurb",
            fetch=before.fetch,
            components=tuple(
                Component(id=c.id, name=c.name, status=c.status, updated_at="2026-12-01T00:00:00Z")
                for c in before.components
            ),
        )
        assert serialize.content_hash(before) == serialize.content_hash(after)
        assert diff_snapshots(before, after, at=AT) == ()


class TestComponentEvents:
    def test_single_transition_produces_exactly_one_line(self):
        events = diff_snapshots(
            make_snapshot(), make_snapshot(components=degraded()), at=AT
        )
        assert len(events) == 1
        event = events[0]
        assert event.change_type is ChangeType.COMPONENT_STATUS
        assert event.component_id == "aaa"
        assert event.from_ == "operational"
        assert event.to == "degraded_performance"
        assert event.at == AT
        assert event.incident_id is None

    def test_recovery_produces_the_inverse_line(self):
        events = diff_snapshots(
            make_snapshot(components=degraded()), make_snapshot(), at=AT
        )
        assert len(events) == 1
        assert events[0].from_ == "degraded_performance"
        assert events[0].to == "operational"

    def test_a_new_component_reports_a_null_origin(self):
        before = make_snapshot(components=(Component(id="aaa", name="API"),))
        after = make_snapshot(
            components=(
                Component(id="aaa", name="API"),
                Component(id="ccc", name="Batch", status=ComponentStatus.OPERATIONAL),
            )
        )
        events = [e for e in diff_snapshots(before, after, at=AT) if e.component_id == "ccc"]
        assert len(events) == 1
        assert events[0].from_ is None
        assert events[0].to == "operational"

    def test_a_removed_component_reports_a_null_destination(self):
        before = make_snapshot()
        after = make_snapshot(components=(Component(id="aaa", name="API"),))
        events = [e for e in diff_snapshots(before, after, at=AT) if e.component_id == "bbb"]
        assert len(events) == 1
        assert events[0].to is None

    def test_a_rename_is_its_own_event_type(self):
        # Lets someone later explain why a file changed on a day nothing broke.
        before = make_snapshot()
        after = make_snapshot(
            components=(
                Component(id="aaa", name="Inference API", status=ComponentStatus.OPERATIONAL),
                Component(id="bbb", name="Console", status=ComponentStatus.OPERATIONAL),
            )
        )
        events = diff_snapshots(before, after, at=AT)
        assert len(events) == 1
        assert events[0].change_type is ChangeType.COMPONENT_RENAMED
        assert events[0].from_ == "API"
        assert events[0].to == "Inference API"

    def test_events_are_ordered_deterministically_by_component_id(self):
        before = make_snapshot()
        after = make_snapshot(
            components=(
                Component(id="aaa", name="API", status=ComponentStatus.MAJOR_OUTAGE),
                Component(id="bbb", name="Console", status=ComponentStatus.PARTIAL_OUTAGE),
            )
        )
        events = diff_snapshots(before, after, at=AT)
        assert [e.component_id for e in events] == ["aaa", "bbb"]


class TestOverallIndicator:
    def test_indicator_change_is_recorded(self):
        events = diff_snapshots(
            make_snapshot(), make_snapshot(indicator=Indicator.MAJOR), at=AT
        )
        assert len(events) == 1
        assert events[0].change_type is ChangeType.OVERALL_INDICATOR
        assert events[0].from_ == "none"
        assert events[0].to == "major"

    def test_indicator_event_comes_before_component_events(self):
        events = diff_snapshots(
            make_snapshot(),
            make_snapshot(indicator=Indicator.MINOR, components=degraded()),
            at=AT,
        )
        assert [e.change_type for e in events] == [
            ChangeType.OVERALL_INDICATOR,
            ChangeType.COMPONENT_STATUS,
        ]


class TestIncidentEvents:
    def test_a_new_incident_opens(self):
        events = diff_snapshots(
            make_snapshot(), make_snapshot(active_incidents=(incident(),)), at=AT
        )
        assert len(events) == 1
        assert events[0].change_type is ChangeType.INCIDENT_OPENED
        assert events[0].incident_id == "inc1"
        assert events[0].to == "investigating"
        assert events[0].component_id is None

    def test_a_status_transition_updates(self):
        events = diff_snapshots(
            make_snapshot(active_incidents=(incident(),)),
            make_snapshot(active_incidents=(incident(status=IncidentStatus.IDENTIFIED),)),
            at=AT,
        )
        assert len(events) == 1
        assert events[0].change_type is ChangeType.INCIDENT_UPDATED
        assert (events[0].from_, events[0].to) == ("investigating", "identified")

    def test_a_terminal_status_resolves(self):
        events = diff_snapshots(
            make_snapshot(active_incidents=(incident(),)),
            make_snapshot(active_incidents=(incident(status=IncidentStatus.RESOLVED),)),
            at=AT,
        )
        assert len(events) == 1
        assert events[0].change_type is ChangeType.INCIDENT_RESOLVED

    def test_disappearance_resolves_an_incident_that_was_still_open(self):
        # Statuspage drops resolved incidents out of summary.json rather than
        # restating them, so disappearance is the normal resolution signal.
        events = diff_snapshots(
            make_snapshot(active_incidents=(incident(),)), make_snapshot(), at=AT
        )
        assert len(events) == 1
        assert events[0].change_type is ChangeType.INCIDENT_RESOLVED
        assert events[0].to == "resolved"

    def test_disappearance_of_an_already_resolved_incident_is_silent(self):
        # Google's closed incidents linger in the feed with `end` set and only
        # later age out. Without this guard every aged-out incident would emit a
        # duplicate resolution weeks after the fact.
        already = incident(status=IncidentStatus.RESOLVED, resolved_at="2026-09-15T03:10:00Z")
        events = diff_snapshots(
            make_snapshot(active_incidents=(already,)), make_snapshot(), at=AT
        )
        assert events == ()

    def test_a_new_update_with_no_status_change_marks_itself_by_equal_endpoints(self):
        events = diff_snapshots(
            make_snapshot(active_incidents=(incident(),)),
            make_snapshot(
                active_incidents=(
                    incident(latest_update_id="u2", latest_update_sha12="bbbbbbbbbbbb"),
                )
            ),
            at=AT,
        )
        assert len(events) == 1
        assert events[0].change_type is ChangeType.INCIDENT_UPDATED
        # from == to is the unambiguous marker for "new update, same status".
        assert events[0].from_ == events[0].to == "investigating"

    def test_an_in_place_text_edit_is_detected(self):
        events = diff_snapshots(
            make_snapshot(active_incidents=(incident(),)),
            make_snapshot(active_incidents=(incident(latest_update_sha12="cccccccccccc"),)),
            at=AT,
        )
        assert len(events) == 1
        assert events[0].change_type is ChangeType.INCIDENT_UPDATED

    def test_scheduled_maintenances_participate_in_incident_diffing(self):
        maintenance = incident(id="m1", status=IncidentStatus.IN_PROGRESS, impact=Impact.MAINTENANCE)
        events = diff_snapshots(
            make_snapshot(),
            make_snapshot(scheduled_maintenances=(maintenance,)),
            at=AT,
        )
        assert len(events) == 1
        assert events[0].change_type is ChangeType.INCIDENT_OPENED
        assert events[0].incident_id == "m1"


class TestSerializedForm:
    def test_the_from_field_serializes_as_from(self):
        events = diff_snapshots(
            make_snapshot(), make_snapshot(components=degraded()), at=AT
        )
        line = serialize.event_line(events[0]).decode("utf-8")
        assert '"from":"operational"' in line
        assert "from_" not in line
        assert line.endswith("\n")

    def test_event_lines_are_byte_stable(self):
        events = diff_snapshots(
            make_snapshot(), make_snapshot(components=degraded()), at=AT
        )
        assert serialize.event_line(events[0]) == serialize.event_line(events[0])
