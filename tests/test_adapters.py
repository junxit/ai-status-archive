"""Adapter tests against payloads recorded live on 2026-09-15.

These assert on specific stable IDs on purpose. When a provider reorganizes
their status page — and they will — the requirement is a red test, not silently
empty JSON files that nobody notices for six weeks.
"""

from __future__ import annotations

import copy

import pytest
from conftest import fixture_json

from aistatus.adapters import get_adapter, google_cloud, statuspage
from aistatus.models import (
    ComponentStatus,
    FetchResult,
    Impact,
    Indicator,
    NormalizationError,
)

FETCH = FetchResult(ok=True, fetched_at="2026-09-15T00:00:00Z", http_status=200)

#: The Anthropic component this whole archive exists to answer questions about.
CLAUDE_API_ID = "k8w3r06qmzrp"

#: Google's Gemini API product. Its title has already drifted from
#: "Vertex Gemini API" to "Gemini on Agent Platform".
GEMINI_PRODUCT_ID = "Z0FZJAMvEB4j3NbCJs6B"


class TestRegistry:
    def test_both_platforms_are_registered(self):
        assert get_adapter("statuspage") is statuspage.parse
        assert get_adapter("google_cloud") is google_cloud.parse

    def test_unknown_platform_names_the_known_ones(self):
        with pytest.raises(KeyError, match="google_cloud, statuspage"):
            get_adapter("pagerduty")


class TestAnthropic:
    @pytest.fixture
    def snapshot(self, anthropic_cfg):
        return statuspage.parse(
            fixture_json("anthropic_summary.json"),
            cfg=anthropic_cfg,
            fetch=FETCH,
            source_url="https://status.claude.com/api/v2/summary.json",
        )

    def test_claude_api_component_survives_parsing(self, snapshot):
        component = next(c for c in snapshot.components if c.id == CLAUDE_API_ID)
        assert component.name == "Claude API (api.anthropic.com)"
        assert component.status is ComponentStatus.OPERATIONAL

    def test_all_six_components_are_present(self, snapshot):
        assert {c.id for c in snapshot.components} == {
            "rwppv331jlwc",
            "0qbwn08sd68x",
            CLAUDE_API_ID,
            "yyzkbfz2thpt",
            "bpp5gb3hpjcl",
            "0scnb50nvy53",
        }

    def test_components_are_sorted_by_stable_id(self, snapshot):
        ids = [c.id for c in snapshot.components]
        assert ids == sorted(ids)

    def test_millisecond_timestamps_are_normalized(self, snapshot):
        for component in snapshot.components:
            if component.updated_at:
                assert component.updated_at.endswith("Z")
                assert "." not in component.updated_at
                assert len(component.updated_at) == 20

    def test_group_false_is_normalized_to_none(self, snapshot):
        # Anthropic sends `group: false` where OpenAI sends `group: null`.
        assert all(c.group is None for c in snapshot.components)

    def test_indicator_is_parsed(self, snapshot):
        assert snapshot.indicator is Indicator.NONE


class TestOpenAI:
    @pytest.fixture
    def snapshot(self, openai_cfg):
        return statuspage.parse(
            fixture_json("openai_summary.json"),
            cfg=openai_cfg,
            fetch=FETCH,
            source_url="https://status.openai.com/api/v2/summary.json",
        )

    def test_all_twenty_five_components_are_archived(self, snapshot):
        # All 25 are kept, not just the API-tagged subset: you cannot
        # retroactively recover what you filtered out at collection time.
        assert len(snapshot.components) == 25

    def test_every_tagged_api_component_exists_upstream(self, snapshot, openai_cfg):
        # Guards against a config typo silently tagging nothing, which would
        # make the commit subject line quietly stop mentioning the API.
        present = {c.id for c in snapshot.components}
        assert openai_cfg.api_components <= present
        assert len(openai_cfg.api_components) == 14

    def test_missing_scheduled_maintenances_key_is_not_an_error(self, snapshot):
        # OpenAI's summary.json has no such key and the endpoint 404s.
        assert snapshot.scheduled_maintenances == ()

    def test_incidents_get_a_constructed_permalink(self, openai_cfg):
        payload = copy.deepcopy(fixture_json("openai_summary.json"))
        payload["incidents"] = [
            {
                "id": "01M2GA8XTS6VB3QCDEGZ0HNAQ5",
                "name": "Elevated errors affecting Work Mode",
                "status": "monitoring",
                "impact": "minor",
                "created_at": "2026-09-14T15:58:48Z",
                "updated_at": "2026-09-14T19:42:08Z",
                "incident_updates": [
                    {
                        "id": "u2",
                        "status": "monitoring",
                        "body": "We have applied additional mitigations.",
                        "created_at": "2026-09-14T19:42:08Z",
                    }
                ],
            }
        ]
        snapshot = statuspage.parse(
            payload,
            cfg=openai_cfg,
            fetch=FETCH,
            source_url="https://status.openai.com/api/v2/summary.json",
        )
        incident = snapshot.active_incidents[0]
        assert incident.url == (
            "https://status.openai.com/incidents/01M2GA8XTS6VB3QCDEGZ0HNAQ5"
        )
        # OpenAI publishes no components[] on incidents at all.
        assert incident.affected_components == ()
        # No started_at upstream, so created_at is the honest substitute.
        assert incident.started_at == "2026-09-14T15:58:48Z"
        assert incident.resolved_at is None
        assert incident.latest_update_sha12 is not None


class TestStatuspageRobustness:
    def test_empty_component_list_raises_rather_than_writing_a_fiction(
        self, anthropic_cfg
    ):
        with pytest.raises(NormalizationError, match="no components"):
            statuspage.parse(
                {"components": [], "status": {"indicator": "none"}},
                cfg=anthropic_cfg,
                fetch=FETCH,
                source_url="https://status.claude.com/api/v2/summary.json",
            )

    def test_non_object_payload_raises(self, anthropic_cfg):
        with pytest.raises(NormalizationError, match="expected a JSON object"):
            statuspage.parse(
                ["not", "an", "object"],
                cfg=anthropic_cfg,
                fetch=FETCH,
                source_url="https://status.claude.com/api/v2/summary.json",
            )

    def test_resolved_incidents_are_not_retained(self, anthropic_cfg):
        payload = copy.deepcopy(fixture_json("anthropic_summary.json"))
        payload["incidents"] = [
            {
                "id": "done",
                "name": "Already fixed",
                "status": "resolved",
                "impact": "minor",
                "started_at": "2026-09-10T15:54:10.000Z",
                "resolved_at": "2026-09-14T19:14:45.969Z",
                "incident_updates": [],
            }
        ]
        snapshot = statuspage.parse(
            payload,
            cfg=anthropic_cfg,
            fetch=FETCH,
            source_url="https://status.claude.com/api/v2/summary.json",
        )
        # Retaining resolved incidents on a time window would make the content
        # hash a function of wall-clock time and commit phantom changes.
        assert snapshot.active_incidents == ()

    def test_component_groups_are_resolved_to_parent_names(self, anthropic_cfg):
        payload = {
            "status": {"indicator": "none", "description": "OK"},
            "components": [
                {"id": "grp", "name": "Core Services", "group": True, "status": "operational"},
                {
                    "id": "child",
                    "name": "Inference",
                    "group": False,
                    "group_id": "grp",
                    "status": "operational",
                },
            ],
        }
        snapshot = statuspage.parse(
            payload,
            cfg=anthropic_cfg,
            fetch=FETCH,
            source_url="https://status.claude.com/api/v2/summary.json",
        )
        # The group header itself is not a component.
        assert [c.id for c in snapshot.components] == ["child"]
        assert snapshot.components[0].group == "Core Services"

    def test_newest_update_is_chosen_by_timestamp_not_position(self, anthropic_cfg):
        payload = copy.deepcopy(fixture_json("anthropic_summary.json"))
        payload["incidents"] = [
            {
                "id": "inc",
                "name": "Something",
                "status": "investigating",
                "impact": "minor",
                "incident_updates": [
                    {"id": "old", "body": "First", "created_at": "2026-09-14T10:00:00Z"},
                    {"id": "new", "body": "Second", "created_at": "2026-09-14T12:00:00Z"},
                ],
            }
        ]
        snapshot = statuspage.parse(
            payload,
            cfg=anthropic_cfg,
            fetch=FETCH,
            source_url="https://status.claude.com/api/v2/summary.json",
        )
        assert snapshot.active_incidents[0].latest_update_id == "new"


class TestGoogleCloud:
    @pytest.fixture
    def snapshot(self, google_cfg):
        return google_cloud.parse(
            fixture_json("google_incidents.json"),
            cfg=google_cfg,
            fetch=FETCH,
            source_url="https://status.cloud.google.com/incidents.json",
            catalog=fixture_json("google_products.json"),
        )

    def test_component_set_is_the_config_allowlist(self, snapshot, google_cfg):
        # Not discovered from the incident feed: products must not blink in and
        # out of existence between polls.
        assert len(snapshot.components) == len(google_cfg.product_ids) == 57
        assert {c.id for c in snapshot.components} == set(google_cfg.product_ids)

    def test_gemini_product_resolves_to_its_current_renamed_title(self, snapshot):
        # The live proof that ID filtering was mandatory: this product's display
        # name has already drifted away from what the feed calls it.
        gemini = next(c for c in snapshot.components if c.id == GEMINI_PRODUCT_ID)
        assert gemini.name == "Gemini on Agent Platform"

    def test_all_products_operational_when_no_incidents_are_open(self, snapshot):
        # All six incidents in the recorded feed carry an `end`.
        assert all(c.status is ComponentStatus.OPERATIONAL for c in snapshot.components)
        assert snapshot.indicator is Indicator.NONE
        assert snapshot.active_incidents == ()

    def test_operational_components_carry_no_synthetic_timestamp(self, snapshot):
        # Stamping these with now() would rewrite the file every five minutes
        # forever and defeat commit-on-change entirely.
        assert all(c.updated_at is None for c in snapshot.components)

    def test_id_filter_still_matches_after_a_simulated_rename(
        self, google_cfg, snapshot
    ):
        catalog = copy.deepcopy(fixture_json("google_products.json"))
        for product in catalog["products"]:
            if product["id"] == GEMINI_PRODUCT_ID:
                product["title"] = "Something Else Entirely"
                product["current_title"] = "Renamed Again In 2027"
        renamed = google_cloud.parse(
            fixture_json("google_incidents.json"),
            cfg=google_cfg,
            fetch=FETCH,
            source_url="https://status.cloud.google.com/incidents.json",
            catalog=catalog,
        )
        gemini = next(c for c in renamed.components if c.id == GEMINI_PRODUCT_ID)
        assert gemini.name == "Renamed Again In 2027"
        assert gemini.status is ComponentStatus.OPERATIONAL

    def test_open_incident_derives_component_status(self, google_cfg):
        feed = copy.deepcopy(fixture_json("google_incidents.json"))
        incident = next(i for i in feed if i["id"] == "41E5S3mkTGDfkZuJZH5k")
        del incident["end"]  # Reopen the February Vertex AI Gemini incident.
        incident["status_impact"] = "SERVICE_DISRUPTION"

        snapshot = google_cloud.parse(
            feed,
            cfg=google_cfg,
            fetch=FETCH,
            source_url="https://status.cloud.google.com/incidents.json",
            catalog=fixture_json("google_products.json"),
        )
        gemini = next(c for c in snapshot.components if c.id == GEMINI_PRODUCT_ID)
        assert gemini.status is ComponentStatus.PARTIAL_OUTAGE
        assert gemini.updated_at == "2026-03-09T05:25:43Z"  # incident's modified time
        assert snapshot.indicator is Indicator.MAJOR
        assert len(snapshot.active_incidents) == 1

        active = snapshot.active_incidents[0]
        assert active.impact is Impact.MAJOR
        assert active.started_at == "2026-02-27T12:37:00Z"  # offset normalized to Z
        assert active.resolved_at is None
        # affected_components is narrowed to tracked products, not all of GCP.
        assert GEMINI_PRODUCT_ID in active.affected_components
        assert set(active.affected_components) <= set(google_cfg.product_ids)

    def test_worst_status_wins_when_incidents_overlap(self, google_cfg):
        feed = [
            {
                "id": "minor",
                "external_desc": "Slow",
                "status_impact": "SERVICE_INFORMATION",
                "begin": "2026-09-01T00:00:00+00:00",
                "modified": "2026-09-01T01:00:00+00:00",
                "affected_products": [{"id": GEMINI_PRODUCT_ID, "title": "x"}],
            },
            {
                "id": "severe",
                "external_desc": "Down",
                "status_impact": "SERVICE_OUTAGE",
                "begin": "2026-09-01T00:30:00+00:00",
                "modified": "2026-09-01T01:30:00+00:00",
                "affected_products": [{"id": GEMINI_PRODUCT_ID, "title": "x"}],
            },
        ]
        snapshot = google_cloud.parse(
            feed,
            cfg=google_cfg,
            fetch=FETCH,
            source_url="https://status.cloud.google.com/incidents.json",
            catalog=fixture_json("google_products.json"),
        )
        gemini = next(c for c in snapshot.components if c.id == GEMINI_PRODUCT_ID)
        assert gemini.status is ComponentStatus.MAJOR_OUTAGE
        assert snapshot.indicator is Indicator.CRITICAL

    def test_incidents_outside_the_allowlist_are_ignored(self, google_cfg):
        feed = [
            {
                "id": "unrelated",
                "external_desc": "Cloud SQL degraded",
                "status_impact": "SERVICE_OUTAGE",
                "begin": "2026-09-01T00:00:00+00:00",
                "affected_products": [
                    {"id": "hV87iK5DcEXKgWU2kDri", "title": "Google Cloud SQL"}
                ],
            }
        ]
        snapshot = google_cloud.parse(
            feed,
            cfg=google_cfg,
            fetch=FETCH,
            source_url="https://status.cloud.google.com/incidents.json",
            catalog=fixture_json("google_products.json"),
        )
        assert snapshot.active_incidents == ()
        assert all(c.status is ComponentStatus.OPERATIONAL for c in snapshot.components)

    def test_missing_catalog_raises_rather_than_falling_back_to_bare_ids(
        self, google_cfg
    ):
        # Falling back would rewrite all 57 component names, then rewrite them
        # back on recovery: two junk commits from one transient failure.
        with pytest.raises(NormalizationError, match="catalog"):
            google_cloud.parse(
                fixture_json("google_incidents.json"),
                cfg=google_cfg,
                fetch=FETCH,
                source_url="https://status.cloud.google.com/incidents.json",
                catalog=None,
            )

    def test_non_array_feed_raises(self, google_cfg):
        with pytest.raises(NormalizationError, match="expected a JSON array"):
            google_cloud.parse(
                {"incidents": []},
                cfg=google_cfg,
                fetch=FETCH,
                source_url="https://status.cloud.google.com/incidents.json",
                catalog=fixture_json("google_products.json"),
            )
