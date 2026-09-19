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
    HEARTBEAT_BUCKET_SECONDS,
    HEARTBEAT_PATH,
    RESYNC_BUCKET_SECONDS,
    Outcome,
    RunContext,
    plan_run,
    time_bucket,
)

NOW = "2026-09-15T03:14:00Z"

#: Inside the same 300-second heartbeat bucket as NOW, which spans
#: [03:10:00, 03:15:00). A heartbeat committed here is not yet due again.
SAME_BUCKET = "2026-09-15T03:12:00Z"

#: The preceding heartbeat bucket, [03:05:00, 03:10:00) — still the same UTC
#: hour as NOW, which is what makes it useful for proving the hourly re-sync is
#: decoupled from the five-minute heartbeat.
PRIOR_BUCKET = "2026-09-15T03:08:00Z"

#: A previous UTC hour, so both the heartbeat and the re-sync are due.
PRIOR_HOUR = "2026-09-15T02:59:00Z"


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


class TestTimeBucket:
    @pytest.mark.parametrize("width", [HEARTBEAT_BUCKET_SECONDS, RESYNC_BUCKET_SECONDS])
    def test_a_timestamp_buckets_with_itself(self, width):
        assert time_bucket(NOW, width) == time_bucket(NOW, width)

    def test_five_minute_bucket_boundaries(self):
        inside = time_bucket("2026-09-15T03:10:00Z", 300)
        assert time_bucket("2026-09-15T03:14:59Z", 300) == inside
        assert time_bucket("2026-09-15T03:15:00Z", 300) != inside
        assert time_bucket("2026-09-15T03:09:59Z", 300) != inside

    def test_hour_bucket_boundaries(self):
        inside = time_bucket("2026-09-15T03:00:00Z", 3600)
        assert time_bucket("2026-09-15T03:59:59Z", 3600) == inside
        assert time_bucket("2026-09-15T04:00:00Z", 3600) != inside

    def test_widths_are_independent(self):
        # The whole point of decoupling: two instants can share an hour bucket
        # while sitting in different five-minute buckets.
        a, b = "2026-09-15T03:08:00Z", "2026-09-15T03:14:00Z"
        assert time_bucket(a, RESYNC_BUCKET_SECONDS) == time_bucket(b, RESYNC_BUCKET_SECONDS)
        assert time_bucket(a, HEARTBEAT_BUCKET_SECONDS) != time_bucket(b, HEARTBEAT_BUCKET_SECONDS)

    def test_offset_and_z_forms_agree(self):
        assert time_bucket("2026-09-15T03:14:00Z", 300) == time_bucket(
            "2026-09-15T03:14:00+00:00", 300
        )
        assert time_bucket("2026-09-15T05:14:00+02:00", 300) == time_bucket(
            "2026-09-15T03:14:00Z", 300
        )

    def test_bad_input_is_none_so_callers_treat_it_as_due(self):
        # None compares unequal to any real bucket, so a missing or corrupt
        # timestamp errs toward committing a redundant heartbeat rather than
        # silently skipping proof of liveness.
        for bad in (None, "", "2026-09", "not a timestamp"):
            assert time_bucket(bad, 300) is None
        assert time_bucket(NOW, 0) is None
        assert time_bucket(NOW, -300) is None


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
                heartbeat_doc=heartbeat(SAME_BUCKET, testco={}),
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
                heartbeat_doc=heartbeat(SAME_BUCKET, testco={}),
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
                heartbeat_doc=heartbeat(SAME_BUCKET, testco={}),
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
                heartbeat_doc=heartbeat(SAME_BUCKET, testco={}),
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
                heartbeat_doc=heartbeat(PRIOR_HOUR, testco={}),
            ),
        )
        doc = json.loads(
            next(w for w in plan.writes if w.path == HEARTBEAT_PATH).data.decode()
        )
        assert doc["providers"]["testco"]["fetched_at"] == PRIOR_HOUR
        assert doc["providers"]["testco"]["ok"] is False
        assert doc["providers"]["testco"]["consecutive_failures"] == 1

    def test_a_sustained_failure_keeps_committing_heartbeats_but_no_data(self):
        # At a five-minute heartbeat a sustained outage DOES commit every run.
        # That is the point: an uncommitted heartbeat does not survive the
        # ephemeral runner, so continuing to commit is how the archive proves it
        # was alive and trying throughout. What must not happen is any of that
        # touching the provider's last known good data.
        config = cfg()
        snapshot = make_snapshot(provider="testco")
        plan = plan_run(
            [outcome(config, None, error=FetchError("timeout", "x"))],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(snapshot)},
                heartbeat_doc=heartbeat(
                    PRIOR_BUCKET, testco={"consecutive_failures": 3, "ok": False}
                ),
            ),
        )
        assert plan.results[0].consecutive_failures == 4
        assert plan.commit is not None
        assert plan.commit.subject.endswith("heartbeat")
        assert paths(plan) == [FAILURES_PATH, HEARTBEAT_PATH]
        assert "providers/testco.json" not in paths(plan)
        assert "raw/testco.json" not in paths(plan)

    def test_the_failure_counter_keeps_climbing_across_runs(self):
        # The counter is what distinguishes a single blip from a sustained
        # outage, so it survives even though it no longer gates any commit.
        config = cfg()
        for prior, expected in ((0, 1), (3, 4), (11, 12)):
            plan = plan_run(
                [outcome(config, None, error=FetchError("timeout", "x"))],
                ctx=RunContext(
                    now=NOW,
                    heartbeat_doc=heartbeat(
                        PRIOR_BUCKET,
                        testco={"consecutive_failures": prior, "ok": False},
                    ),
                ),
            )
            assert plan.results[0].consecutive_failures == expected

    def test_recovery_resets_the_counter_to_zero(self):
        config = cfg()
        snapshot = make_snapshot(provider="testco")
        plan = plan_run(
            [outcome(config, snapshot)],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(snapshot)},
                heartbeat_doc=heartbeat(
                    PRIOR_BUCKET, testco={"consecutive_failures": 2, "ok": False}
                ),
            ),
        )
        assert plan.results[0].outcome is Outcome.UNCHANGED
        doc = json.loads(
            next(w for w in plan.writes if w.path == HEARTBEAT_PATH).data.decode()
        )
        assert doc["providers"]["testco"]["consecutive_failures"] == 0
        assert doc["providers"]["testco"]["ok"] is True


class TestNotModified:
    def test_a_304_writes_nothing_and_carries_the_hash_forward(self):
        config = cfg()
        snapshot = make_snapshot(provider="testco")
        plan = plan_run(
            [outcome(config, None, not_modified=True, http_status=304)],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(snapshot)},
                heartbeat_doc=heartbeat(SAME_BUCKET, testco={}),
            ),
        )
        result = plan.results[0]
        assert result.outcome is Outcome.NOT_MODIFIED
        assert result.current_hash == result.previous_hash == serialize.content_hash(snapshot)
        assert plan.writes == ()
        assert plan.commit is None


class TestHeartbeatCadence:
    def test_no_change_within_the_same_bucket_does_not_commit(self):
        # Two runs inside one 300-second window, which happens when GitHub
        # delays a scheduled run, produce a single heartbeat rather than two.
        config = cfg()
        snapshot = make_snapshot(provider="testco")
        plan = plan_run(
            [outcome(config, snapshot)],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(snapshot)},
                heartbeat_doc=heartbeat(SAME_BUCKET, testco={}),
            ),
        )
        assert plan.commit is None

    def test_no_change_after_the_bucket_rolls_commits_a_heartbeat(self):
        # PRIOR_BUCKET is only six minutes earlier and inside the same UTC hour,
        # so this would NOT have committed under the old hourly cadence.
        config = cfg()
        snapshot = make_snapshot(provider="testco")
        plan = plan_run(
            [outcome(config, snapshot)],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(snapshot)},
                heartbeat_doc=heartbeat(PRIOR_BUCKET, testco={}),
            ),
        )
        assert plan.commit is not None
        assert plan.commit.subject == "chore(status): 2026-09-15T03:14Z heartbeat"
        assert paths(plan) == [HEARTBEAT_PATH]

    def test_the_heartbeat_reason_is_recorded_for_dry_run_output(self):
        config = cfg()
        snapshot = make_snapshot(provider="testco")
        plan = plan_run(
            [outcome(config, snapshot)],
            ctx=RunContext(
                now=NOW,
                previous_docs={"testco": committed(snapshot)},
                heartbeat_doc=heartbeat(PRIOR_BUCKET, testco={}),
            ),
        )
        assert plan.writes[0].reason == "heartbeat interval elapsed"

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
                heartbeat_doc=heartbeat(PRIOR_HOUR, testco={}),
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
                heartbeat_doc=heartbeat(SAME_BUCKET, alpha={}, bravo={}, charlie={}),
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


class TestStaleApiComponentTags:
    """Guards against the config rot observed live between 2026-09-15 and -09-18.

    OpenAI removed three components tagged as API-relevant and added three
    others, holding the total at 25. Nothing looked wrong, the YAML still
    parsed, and the commit subject line simply stopped being able to describe
    those surfaces.
    """

    def test_a_tag_pointing_at_a_removed_component_warns(self):
        config = cfg(api_components=frozenset({"aaa", "vanished"}))
        plan = plan_run(
            [outcome(config, make_snapshot(provider="testco"))], ctx=RunContext(now=NOW)
        )
        stale = [w for w in plan.warnings if "api_components" in w]
        assert len(stale) == 1
        assert "vanished" in stale[0]
        assert "1 of 2" in stale[0]

    def test_every_missing_id_is_named(self):
        config = cfg(api_components=frozenset({"gone_a", "gone_b", "aaa"}))
        plan = plan_run(
            [outcome(config, make_snapshot(provider="testco"))], ctx=RunContext(now=NOW)
        )
        stale = next(w for w in plan.warnings if "api_components" in w)
        assert "gone_a" in stale and "gone_b" in stale
        assert "2 of 3" in stale

    def test_all_tags_present_produces_no_warning(self):
        config = cfg(api_components=frozenset({"aaa", "bbb"}))
        plan = plan_run(
            [outcome(config, make_snapshot(provider="testco"))], ctx=RunContext(now=NOW)
        )
        assert not [w for w in plan.warnings if "api_components" in w]

    def test_an_untagged_provider_produces_no_warning(self):
        config = cfg(api_components=frozenset())
        plan = plan_run(
            [outcome(config, make_snapshot(provider="testco"))], ctx=RunContext(now=NOW)
        )
        assert not [w for w in plan.warnings if "api_components" in w]

    def test_a_stale_tag_never_blocks_the_commit(self):
        # A provider reshuffling their status page must not fail a poll. The
        # archived data is still correct; only our editorial tagging is stale.
        config = cfg(api_components=frozenset({"vanished"}))
        plan = plan_run(
            [outcome(config, make_snapshot(provider="testco"))], ctx=RunContext(now=NOW)
        )
        assert plan.commit is not None
        assert plan.results[0].outcome is Outcome.BASELINE

    def test_a_failed_provider_is_not_checked_for_stale_tags(self):
        # There is no snapshot to compare against; claiming every tag vanished
        # because we could not reach the provider would be nonsense.
        config = cfg(api_components=frozenset({"vanished"}))
        plan = plan_run(
            [outcome(config, None, error=FetchError("timeout", "x"))],
            ctx=RunContext(now=NOW),
        )
        assert not [w for w in plan.warnings if "api_components" in w]


class TestAllFailed:
    def test_every_provider_failing_is_flagged(self):
        plan = plan_run(
            [
                outcome(cfg("alpha"), None, error=FetchError("timeout", "x")),
                outcome(cfg("bravo"), None, error=FetchError("timeout", "x")),
            ],
            ctx=RunContext(now=NOW, heartbeat_doc=heartbeat(SAME_BUCKET, alpha={}, bravo={})),
        )
        assert plan.all_failed is True
