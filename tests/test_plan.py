"""The commit decision state machine.

This is the crux of the design and the easiest part to get wrong, so the whole
decision table is exercised here — including the mixed cases where one provider
changed, another failed, and a third returned 304 in the same cycle.
"""

from __future__ import annotations

import json

import pytest
from test_diff import degraded
from test_serialize import make_snapshot

from aistatus import serialize
from aistatus.collect import CollectOutcome
from aistatus.models import (
    Component,
    ComponentStatus,
    FetchError,
    NormalizationError,
    ProviderConfig,
)
from aistatus.plan import (
    FAILURES_PATH,
    HEARTBEAT_PATH,
    Outcome,
    RunContext,
    hour_bucket,
    plan_run,
)

NOW = "2026-09-15T03:14:00Z"
SAME_HOUR = "2026-09-15T03:02:00Z"
PREVIOUS_HOUR = "2026-09-15T02:59:00Z"


def cfg(name: str = "testco", **overrides) -> ProviderConfig:
    """Build a provider configuration for tests."""
    defaults = dict(
        name=name,
        display_name=name.title(),
        platform="statuspage",
        url=f"https://status.{name}.com/api/v2/summary.json",
        api_components=frozenset({"aaa"}),
    )
    defaults.update(overrides)
    return ProviderConfig(**defaults)


def outcome(config: ProviderConfig, snapshot=None, **overrides) -> CollectOutcome:
    """Build a collection outcome for tests."""
    defaults = dict(
        cfg=config,
        snapshot=snapshot,
        raw_payload={"raw": True},
        fetched_at=NOW,
        http_status=200,
    )
    defaults.update(overrides)
    return CollectOutcome(**defaults)


def committed(snapshot) -> dict:
    """Render a snapshot the way it would appear at HEAD."""
    return json.loads(serialize.dump_snapshot(snapshot))


def heartbeat(at: str | None, **providers) -> dict:
    """Build a heartbeat document."""
    return {
        "heartbeat_committed_at": at,
        "polled_at": at,
        "providers": {
            name: {"fetched_at": at, "ok": True, "consecutive_failures": 0, **extra}
            for name, extra in providers.items()
        },
    }


def paths(plan) -> list[str]:
    """Paths the plan would write, in order."""
    return [w.path for w in plan.writes]


class TestHourBucket:
    def test_same_hour_compares_equal(self):
        assert hour_bucket("2026-09-15T03:00:00Z") == hour_bucket("2026-09-15T03:59:59Z")

    def test_hour_rollover_compares_unequal(self):
        assert hour_bucket("2026-09-15T03:59:59Z") != hour_bucket("2026-09-15T04:00:00Z")

    def test_missing_or_short_input_is_none(self):
        assert hour_bucket(None) is None
        assert hour_bucket("") is None
        assert hour_bucket("2026-09") is None


class TestBaselineRun:
    def test_first_run_writes_everything_and_commits(self):
        config = cfg()
        plan = plan_run(
            [outcome(config, make_snapshot(provider="testco"))],
            ctx=RunContext(now=NOW),
        )
        assert plan.results[0].outcome is Outcome.BASELINE
        assert paths(plan) == [
            "providers/testco.json",
            "raw/testco.json",
            "history/testco.jsonl",
            HEARTBEAT_PATH,
        ]
        assert plan.commit is not None
        assert "baseline testco operational" in plan.commit.subject

    def test_commit_stages_only_planned_paths(self):
        plan = plan_run(
            [outcome(cfg(), make_snapshot(provider="testco"))], ctx=RunContext(now=NOW)
        )
        assert set(plan.commit.paths) == set(paths(plan))


class TestIdempotence:
    def test_identical_input_twice_writes_nothing_and_does_not_commit(self):
        # The single most important behavior in the project. At 288 runs/day
        # across 3 providers, committing unconditionally would produce roughly
        # 100k junk commits a year.
        config = cfg()
        snapshot = make_snapshot(provider="testco")
        plan = plan_run(
            [outcome(config, snapshot)],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(snapshot)},
                heartbeat_doc=heartbeat(SAME_HOUR, testco={}),
            ),
        )
        assert plan.results[0].outcome is Outcome.UNCHANGED
        assert plan.writes == ()
        assert plan.commit is None

    def test_a_changed_fetch_block_alone_does_not_commit(self):
        config = cfg()
        before = make_snapshot(provider="testco")
        after = make_snapshot(
            provider="testco",
            fetch=serialize.dict_to_snapshot(
                {"fetch": {"ok": True, "fetched_at": "2026-12-25T00:00:00Z"}}
            ).fetch,
        )
        plan = plan_run(
            [outcome(config, after)],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(before)},
                heartbeat_doc=heartbeat(SAME_HOUR, testco={}),
            ),
        )
        assert plan.results[0].outcome is Outcome.UNCHANGED
        assert plan.commit is None


class TestSingleTransition:
    def test_one_component_change_writes_exactly_one_history_line(self):
        config = cfg()
        before = make_snapshot(provider="testco")
        after = make_snapshot(provider="testco", components=degraded())

        plan = plan_run(
            [outcome(config, after)],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(before)},
                heartbeat_doc=heartbeat(SAME_HOUR, testco={}),
            ),
        )
        assert plan.results[0].outcome is Outcome.CHANGED

        history = next(w for w in plan.writes if w.path == "history/testco.jsonl")
        assert history.mode == "append"
        lines = history.data.decode().strip().split("\n")
        assert len(lines) == 1

        event = json.loads(lines[0])
        assert event["change_type"] == "component_status"
        assert event["component_id"] == "aaa"
        assert event["from"] == "operational"
        assert event["to"] == "degraded_performance"

    def test_commit_subject_names_the_api_component(self):
        config = cfg()
        before = make_snapshot(provider="testco")
        after = make_snapshot(provider="testco", components=degraded())
        plan = plan_run(
            [outcome(config, after)],
            ctx=RunContext(now=NOW, previous_docs={"testco": committed(before)}),
        )
        assert plan.commit.subject == (
            "chore(status): 2026-09-15T03:14Z testco api degraded_performance"
        )

    def test_subject_ignores_non_api_components(self):
        # A consumer-surface blip must not dominate a subject line that should
        # be about whether the API is up.
        config = cfg(api_components=frozenset({"aaa"}))
        before = make_snapshot(provider="testco")
        after = make_snapshot(
            provider="testco",
            components=(
                Component(id="aaa", name="API", status=ComponentStatus.OPERATIONAL),
                Component(id="bbb", name="Console", status=ComponentStatus.MAJOR_OUTAGE),
            ),
        )
        plan = plan_run(
            [outcome(config, after)],
            ctx=RunContext(now=NOW, previous_docs={"testco": committed(before)}),
        )
        assert "testco operational" in plan.commit.subject
        assert "major_outage" not in plan.commit.subject


class TestFetchFailureIsolation:
    def test_a_failure_never_touches_the_providers_file(self):
        # Writing "unknown" there would be indistinguishable from a real outage
        # in the history and would poison the dataset.
        config = cfg()
        snapshot = make_snapshot(provider="testco")
        plan = plan_run(
            [outcome(config, None, error=FetchError("timeout", "after 10.0s"))],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(snapshot)},
                heartbeat_doc=heartbeat(SAME_HOUR, testco={}),
            ),
        )
        assert plan.results[0].outcome is Outcome.FAILED
        assert "providers/testco.json" not in paths(plan)
        assert "raw/testco.json" not in paths(plan)
        assert FAILURES_PATH in paths(plan)

    def test_the_failure_line_records_kind_and_detail(self):
        plan = plan_run(
            [outcome(cfg(), None, error=FetchError("timeout", "after 10.0s"))],
            ctx=RunContext(now=NOW),
        )
        line = json.loads(
            next(w for w in plan.writes if w.path == FAILURES_PATH).data.decode()
        )
        assert line["provider"] == "testco"
        assert line["kind"] == "timeout"
        assert "10.0s" in line["detail"]

    def test_a_normalization_error_is_treated_like_a_fetch_failure(self):
        # A provider reorganizing their schema must not overwrite good data.
        config = cfg()
        snapshot = make_snapshot(provider="testco")
        plan = plan_run(
            [outcome(config, None, error=NormalizationError("no components"))],
            ctx=RunContext(now=NOW, previous_docs={"testco": committed(snapshot)}),
        )
        assert plan.results[0].outcome is Outcome.FAILED
        assert "providers/testco.json" not in paths(plan)

    def test_a_failed_provider_keeps_its_previous_fetched_at(self):
        # Advancing it would claim we successfully observed a provider we could
        # not reach, which is exactly the ambiguity the heartbeat exists to kill.
        config = cfg()
        plan = plan_run(
            [outcome(config, None, error=FetchError("timeout", "x"))],
            ctx=RunContext(
                now=NOW,
                heartbeat_doc=heartbeat(PREVIOUS_HOUR, testco={}),
            ),
        )
        doc = json.loads(
            next(w for w in plan.writes if w.path == HEARTBEAT_PATH).data.decode()
        )
        assert doc["providers"]["testco"]["fetched_at"] == PREVIOUS_HOUR
        assert doc["providers"]["testco"]["ok"] is False
        assert doc["providers"]["testco"]["consecutive_failures"] == 1

    def test_a_sustained_failure_stops_committing(self):
        # Otherwise a multi-hour outage would spam a commit every five minutes.
        config = cfg()
        plan = plan_run(
            [outcome(config, None, error=FetchError("timeout", "x"))],
            ctx=RunContext(
                now=NOW,
                heartbeat_doc=heartbeat(
                    SAME_HOUR, testco={"consecutive_failures": 3, "ok": False}
                ),
            ),
        )
        assert plan.results[0].consecutive_failures == 4
        assert plan.commit is None

    def test_entering_and_leaving_a_failure_streak_each_commit_once(self):
        config = cfg()
        # Entering: 0 -> 1 crosses the boundary.
        entering = plan_run(
            [outcome(config, None, error=FetchError("timeout", "x"))],
            ctx=RunContext(
                now=NOW, heartbeat_doc=heartbeat(SAME_HOUR, testco={})
            ),
        )
        assert entering.commit is not None
        assert entering.commit.subject.endswith("heartbeat")

        # Leaving: 2 -> 0 crosses it back, so the recovery is durably recorded
        # even though nothing about the provider's status changed.
        snapshot = make_snapshot(provider="testco")
        leaving = plan_run(
            [outcome(config, snapshot)],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(snapshot)},
                heartbeat_doc=heartbeat(
                    SAME_HOUR, testco={"consecutive_failures": 2, "ok": False}
                ),
            ),
        )
        assert leaving.results[0].outcome is Outcome.UNCHANGED
        assert leaving.commit is not None


class TestNotModified:
    def test_a_304_writes_nothing_and_carries_the_hash_forward(self):
        config = cfg()
        snapshot = make_snapshot(provider="testco")
        plan = plan_run(
            [outcome(config, None, not_modified=True, http_status=304)],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(snapshot)},
                heartbeat_doc=heartbeat(SAME_HOUR, testco={}),
            ),
        )
        result = plan.results[0]
        assert result.outcome is Outcome.NOT_MODIFIED
        assert result.current_hash == result.previous_hash == serialize.content_hash(snapshot)
        assert plan.writes == ()
        assert plan.commit is None


class TestHeartbeatCadence:
    def test_no_change_within_the_same_hour_does_not_commit(self):
        config = cfg()
        snapshot = make_snapshot(provider="testco")
        plan = plan_run(
            [outcome(config, snapshot)],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(snapshot)},
                heartbeat_doc=heartbeat(SAME_HOUR, testco={}),
            ),
        )
        assert plan.commit is None

    def test_no_change_after_the_hour_rolls_commits_a_heartbeat(self):
        config = cfg()
        snapshot = make_snapshot(provider="testco")
        plan = plan_run(
            [outcome(config, snapshot)],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(snapshot)},
                heartbeat_doc=heartbeat(PREVIOUS_HOUR, testco={}),
            ),
        )
        assert plan.commit is not None
        assert plan.commit.subject == "chore(status): 2026-09-15T03:14Z heartbeat"
        assert paths(plan) == [HEARTBEAT_PATH]

    def test_an_absent_heartbeat_counts_as_due(self):
        config = cfg()
        snapshot = make_snapshot(provider="testco")
        plan = plan_run(
            [outcome(config, snapshot)],
            ctx=RunContext(now=NOW, previous_docs={"testco": committed(snapshot)}),
        )
        assert plan.commit is not None

    def test_heartbeat_timestamp_is_stored_inside_the_document(self):
        # Not derived from git log: actions/checkout defaults to fetch-depth 1,
        # where `git log -1 -- <path>` returns empty whenever the last change to
        # that path predates the single fetched commit.
        config = cfg()
        snapshot = make_snapshot(provider="testco")
        plan = plan_run(
            [outcome(config, snapshot)],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(snapshot)},
                heartbeat_doc=heartbeat(PREVIOUS_HOUR, testco={}),
            ),
        )
        doc = json.loads(
            next(w for w in plan.writes if w.path == HEARTBEAT_PATH).data.decode()
        )
        assert doc["heartbeat_committed_at"] == NOW
        assert doc["providers"]["testco"]["ok"] is True


class TestMixedOutcomes:
    @pytest.fixture
    def mixed(self):
        """One provider changed, one failed, one returned 304."""
        changed_cfg, failed_cfg, fresh_cfg = cfg("alpha"), cfg("bravo"), cfg("charlie")
        before_alpha = make_snapshot(provider="alpha")
        after_alpha = make_snapshot(provider="alpha", components=degraded())
        before_charlie = make_snapshot(provider="charlie")

        return plan_run(
            [
                outcome(changed_cfg, after_alpha),
                outcome(failed_cfg, None, error=FetchError("http_5xx", "503")),
                outcome(fresh_cfg, None, not_modified=True, http_status=304),
            ],
            ctx=RunContext(
                now=NOW,
                previous_docs={
                    "alpha": committed(before_alpha),
                    "charlie": committed(before_charlie),
                },
                heartbeat_doc=heartbeat(SAME_HOUR, alpha={}, bravo={}, charlie={}),
            ),
        )

    def test_each_provider_gets_its_correct_outcome(self, mixed):
        assert [r.outcome for r in mixed.results] == [
            Outcome.CHANGED,
            Outcome.FAILED,
            Outcome.NOT_MODIFIED,
        ]

    def test_only_the_changed_provider_has_files_written(self, mixed):
        written = paths(mixed)
        assert "providers/alpha.json" in written
        assert "raw/alpha.json" in written
        assert "history/alpha.jsonl" in written
        # The failure must not contaminate the others' data.
        assert "providers/bravo.json" not in written
        assert "providers/charlie.json" not in written
        assert FAILURES_PATH in written

    def test_a_failure_does_not_suppress_another_providers_commit(self, mixed):
        assert mixed.commit is not None
        assert "alpha api degraded_performance" in mixed.commit.subject
        assert "bravo" not in mixed.commit.subject

    def test_the_failure_line_rides_along_on_that_commit(self, mixed):
        assert FAILURES_PATH in mixed.commit.paths

    def test_not_all_failed_when_only_one_did(self, mixed):
        assert mixed.any_failed is True
        assert mixed.all_failed is False


class TestCommitMessage:
    def test_subject_carries_a_utc_timestamp(self):
        # git log --oneline shows no dates, and scanning this archive by eye is
        # a primary workflow.
        plan = plan_run(
            [outcome(cfg(), make_snapshot(provider="testco"))], ctx=RunContext(now=NOW)
        )
        assert plan.commit.subject.startswith("chore(status): 2026-09-15T03:14Z ")

    def test_body_embeds_events_so_git_log_can_search_them(self):
        config = cfg()
        before = make_snapshot(provider="testco")
        after = make_snapshot(provider="testco", components=degraded())
        plan = plan_run(
            [outcome(config, after)],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(before)},
                run_url="https://github.com/junxit/ai-status-archive/actions/runs/1",
            ),
        )
        assert '"change_type":"component_status"' in plan.commit.body
        assert "Poll-At: 2026-09-15T03:14:00Z" in plan.commit.body
        assert "Run: https://github.com/" in plan.commit.body

    def test_long_subjects_are_truncated_with_a_count(self):
        configs = [cfg(f"provider{i}") for i in range(5)]
        plan = plan_run(
            [
                outcome(c, make_snapshot(provider=c.name, components=degraded()))
                for c in configs
            ],
            ctx=RunContext(now=NOW),
        )
        assert len(plan.commit.subject) <= 90
        assert "more)" in plan.commit.subject


class TestChurnCanary:
    def test_a_hash_change_with_no_events_raises_a_warning(self):
        # Almost always means a field entered the hashed body that no rule
        # considers a change. Surfacing it beats absorbing it silently.
        config = cfg()
        before = make_snapshot(provider="testco")
        after = make_snapshot(provider="testco", source_platform="statuspage_v2")
        plan = plan_run(
            [outcome(config, after)],
            ctx=RunContext(now=NOW, previous_docs={"testco": committed(before)}),
        )
        assert plan.results[0].outcome is Outcome.CHANGED
        assert any("churn" in w for w in plan.warnings)

    def test_a_baseline_does_not_trigger_the_canary(self):
        plan = plan_run(
            [outcome(cfg(), make_snapshot(provider="testco"))], ctx=RunContext(now=NOW)
        )
        assert plan.warnings == ()


class TestAllFailed:
    def test_every_provider_failing_is_flagged(self):
        plan = plan_run(
            [
                outcome(cfg("alpha"), None, error=FetchError("timeout", "x")),
                outcome(cfg("bravo"), None, error=FetchError("timeout", "x")),
            ],
            ctx=RunContext(now=NOW, heartbeat_doc=heartbeat(SAME_HOUR, alpha={}, bravo={})),
        )
        assert plan.all_failed is True
