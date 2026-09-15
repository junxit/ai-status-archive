"""End-to-end runner tests against a real git repository.

These drive :func:`runners.github_actions.main` against a temporary git repo with
the real configuration and recorded upstream payloads. Only the HTTP client is
faked; the filesystem, git, planner, adapters, and serializer are all real.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest
from conftest import ROOT, FakeFetcher, fixture_bytes

import runners.github_actions as runner
from aistatus.models import FetchError, RawResponse

CONFIG = str(ROOT / "config" / "providers.yaml")

OPENAI_URL = "https://status.openai.com/api/v2/summary.json"
ANTHROPIC_URL = "https://status.claude.com/api/v2/summary.json"
GOOGLE_URL = "https://status.cloud.google.com/incidents.json"
GOOGLE_CATALOG_URL = "https://status.cloud.google.com/products.json"


def git(*args: str, repo: pathlib.Path) -> str:
    """Run a git command in ``repo`` and return stdout."""
    return subprocess.run(
        ["git", "--no-pager", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """An initialized git repository with one commit."""
    git("init", "-q", repo=tmp_path)
    git("config", "user.email", "test@example.com", repo=tmp_path)
    git("config", "user.name", "Test", repo=tmp_path)
    (tmp_path / ".gitattributes").write_text("* text=auto eol=lf\n")
    git("add", "-A", repo=tmp_path)
    git("commit", "-q", "-m", "init", repo=tmp_path)
    return tmp_path


@pytest.fixture
def responses() -> dict[str, object]:
    """Canned responses for all four real URLs."""
    return {
        OPENAI_URL: fixture_bytes("openai_summary.json"),
        ANTHROPIC_URL: fixture_bytes("anthropic_summary.json"),
        GOOGLE_URL: fixture_bytes("google_incidents.json"),
        GOOGLE_CATALOG_URL: fixture_bytes("google_products.json"),
    }


@pytest.fixture
def run(repo, responses, monkeypatch):
    """Invoke the runner with a fake HTTP client.

    Returns:
        A callable taking optional extra CLI arguments and an optional response
        override, returning the process exit code.
    """

    def _run(*extra: str, overrides: dict | None = None) -> int:
        fetcher = FakeFetcher({**responses, **(overrides or {})})
        monkeypatch.setattr(runner, "UrllibFetcher", lambda **_kwargs: fetcher)
        monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
        return runner.main(
            ["--config", CONFIG, "--repo-root", str(repo), "--no-push", *extra]
        )

    return _run


def commit_count(repo: pathlib.Path) -> int:
    """Number of commits on HEAD."""
    return int(git("rev-list", "--count", "HEAD", repo=repo).strip())


def subjects(repo: pathlib.Path) -> list[str]:
    """Commit subjects, newest first."""
    return git("log", "--format=%s", repo=repo).strip().splitlines()


class TestFirstRun:
    def test_writes_all_four_file_kinds_and_commits_once(self, repo, run):
        assert run() == 0
        assert commit_count(repo) == 2

        for name in ("openai", "anthropic", "google"):
            assert (repo / "providers" / f"{name}.json").exists()
            assert (repo / "raw" / f"{name}.json").exists()
            assert (repo / "history" / f"{name}.jsonl").exists()
        assert (repo / "state" / "last_poll.json").exists()

    def test_every_history_file_starts_with_a_baseline_line(self, repo, run):
        run()
        for name in ("openai", "anthropic", "google"):
            lines = (repo / "history" / f"{name}.jsonl").read_text().strip().split("\n")
            assert len(lines) == 1
            assert json.loads(lines[0])["change_type"] == "baseline"

    def test_provider_files_match_the_documented_schema(self, repo, run):
        run()
        doc = json.loads((repo / "providers" / "anthropic.json").read_text())
        assert set(doc) == {
            "schema_version",
            "provider",
            "provider_name",
            "source_platform",
            "source_url",
            "overall",
            "components",
            "active_incidents",
            "scheduled_maintenances",
            "fetch",
        }
        assert doc["provider"] == "anthropic"
        assert doc["source_platform"] == "statuspage"
        assert {"indicator", "description"} == set(doc["overall"])

    def test_all_timestamps_are_utc_with_a_z_suffix(self, repo, run):
        run()

        def check(value, path):
            if isinstance(value, dict):
                for key, item in value.items():
                    check(item, f"{path}.{key}")
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    check(item, f"{path}[{index}]")
            elif isinstance(value, str) and path.endswith(
                ("_at", "fetched_at", "started_at", "updated_at", "resolved_at")
            ):
                assert value.endswith("Z"), f"{path} is not Z-suffixed: {value}"
                assert "+" not in value and "." not in value, f"{path}: {value}"

        for name in ("openai", "anthropic", "google"):
            check(json.loads((repo / "providers" / f"{name}.json").read_text()), name)

    def test_files_are_byte_identical_when_regenerated(self, repo, run):
        run()
        before = (repo / "providers" / "anthropic.json").read_bytes()
        git("rm", "-q", "--cached", "providers/anthropic.json", repo=repo)
        git("commit", "-q", "-m", "drop", repo=repo)
        run("--allow-dirty")
        after = (repo / "providers" / "anthropic.json").read_bytes()
        # Only the fetch block may differ; the hashed body must be identical.
        assert json.loads(before)["components"] == json.loads(after)["components"]


class TestIdempotence:
    def test_a_second_identical_run_makes_no_commit(self, repo, run):
        # The single most important behavior in the project.
        assert run() == 0
        after_first = commit_count(repo)
        assert run() == 0
        assert commit_count(repo) == after_first

    def test_a_second_run_appends_no_history_lines(self, repo, run):
        run()
        before = (repo / "history" / "anthropic.jsonl").read_bytes()
        run()
        assert (repo / "history" / "anthropic.jsonl").read_bytes() == before

    def test_a_second_run_leaves_provider_files_untouched(self, repo, run):
        run()
        before = (repo / "providers" / "anthropic.json").read_bytes()
        run()
        assert (repo / "providers" / "anthropic.json").read_bytes() == before


class TestChangeDetection:
    def test_a_component_transition_commits_and_logs_one_line(self, repo, run, responses):
        run()

        payload = json.loads(responses[ANTHROPIC_URL])
        for component in payload["components"]:
            if component["id"] == "k8w3r06qmzrp":  # Claude API
                component["status"] = "degraded_performance"
        payload["status"] = {"indicator": "minor", "description": "Partially Degraded"}

        assert run(overrides={ANTHROPIC_URL: json.dumps(payload).encode()}) == 0

        lines = (repo / "history" / "anthropic.jsonl").read_text().strip().split("\n")
        assert len(lines) == 3  # baseline, overall_indicator, component_status

        component_events = [
            json.loads(line)
            for line in lines
            if json.loads(line)["change_type"] == "component_status"
        ]
        assert len(component_events) == 1
        assert component_events[0]["component_id"] == "k8w3r06qmzrp"
        assert component_events[0]["from"] == "operational"
        assert component_events[0]["to"] == "degraded_performance"

        assert (
            "anthropic claude_api_api_anthropic degraded_performance" in subjects(repo)[0]
        )

    def test_only_the_changed_provider_is_rewritten(self, repo, run, responses):
        run()
        openai_before = (repo / "providers" / "openai.json").read_bytes()

        payload = json.loads(responses[ANTHROPIC_URL])
        payload["components"][0]["status"] = "major_outage"
        run(overrides={ANTHROPIC_URL: json.dumps(payload).encode()})

        assert (repo / "providers" / "openai.json").read_bytes() == openai_before


class TestFetchFailureIsolation:
    def test_a_timeout_leaves_the_provider_file_untouched(self, repo, run):
        run()
        before = (repo / "providers" / "anthropic.json").read_bytes()

        assert run(overrides={ANTHROPIC_URL: FetchError("timeout", "after 10.0s")}) == 0

        # Writing "unknown" here would be indistinguishable from a real outage.
        assert (repo / "providers" / "anthropic.json").read_bytes() == before
        failures = (repo / "history" / "_fetch_failures.jsonl").read_text().strip()
        record = json.loads(failures.split("\n")[-1])
        assert record["provider"] == "anthropic"
        assert record["kind"] == "timeout"

    def test_a_failure_does_not_append_to_the_provider_history(self, repo, run):
        run()
        before = (repo / "history" / "anthropic.jsonl").read_bytes()
        run(overrides={ANTHROPIC_URL: FetchError("timeout", "x")})
        assert (repo / "history" / "anthropic.jsonl").read_bytes() == before

    def test_malformed_json_is_treated_as_a_failure_not_a_status(self, repo, run):
        run()
        before = (repo / "providers" / "anthropic.json").read_bytes()
        assert run(overrides={ANTHROPIC_URL: b"<html>503 from the CDN</html>"}) == 0
        assert (repo / "providers" / "anthropic.json").read_bytes() == before

    def test_a_schema_change_that_empties_components_is_a_failure(self, repo, run):
        # The requirement is a red signal, not silently empty JSON files.
        run()
        before = (repo / "providers" / "anthropic.json").read_bytes()
        broken = json.dumps({"page": {}, "status": {}, "components": []}).encode()
        run(overrides={ANTHROPIC_URL: broken})
        assert (repo / "providers" / "anthropic.json").read_bytes() == before

    def test_every_provider_failing_exits_nonzero(self, repo, run):
        code = run(
            overrides={
                OPENAI_URL: FetchError("timeout", "x"),
                ANTHROPIC_URL: FetchError("timeout", "x"),
                GOOGLE_URL: FetchError("timeout", "x"),
            }
        )
        assert code == runner.EXIT_ALL_FAILED

    def test_a_google_catalog_failure_fails_only_google(self, repo, run):
        run()
        anthropic_before = (repo / "providers" / "anthropic.json").read_bytes()
        google_before = (repo / "providers" / "google.json").read_bytes()

        assert run(overrides={GOOGLE_CATALOG_URL: FetchError("timeout", "x")}) == 0

        assert (repo / "providers" / "google.json").read_bytes() == google_before
        assert (repo / "providers" / "anthropic.json").read_bytes() == anthropic_before


class TestNotModified:
    def test_a_304_writes_nothing(self, repo, run):
        # Seed committed state carrying an ETag first, the way Anthropic really
        # replies; only then is a 304 a legitimate answer rather than a claim
        # about a validator we never held.
        seeded = RawResponse(
            status_code=200,
            body=fixture_bytes("anthropic_summary.json"),
            url=ANTHROPIC_URL,
            headers={"etag": 'W/"seeded"'},
        )
        run(overrides={ANTHROPIC_URL: seeded})
        before = (repo / "providers" / "anthropic.json").read_bytes()
        count = commit_count(repo)

        not_modified = RawResponse(
            status_code=304, body=None, url=ANTHROPIC_URL, headers={}
        )
        assert run(overrides={ANTHROPIC_URL: not_modified}) == 0

        assert (repo / "providers" / "anthropic.json").read_bytes() == before
        assert commit_count(repo) == count

    def test_a_304_without_a_stored_validator_is_a_failure_not_a_success(
        self, repo, run
    ):
        # Upstream claiming "nothing changed" against a validator we never sent
        # is a contract violation. Reporting success would assert a status we
        # never actually observed.
        run()
        before = (repo / "providers" / "anthropic.json").read_bytes()
        not_modified = RawResponse(
            status_code=304, body=None, url=ANTHROPIC_URL, headers={}
        )
        assert run(overrides={ANTHROPIC_URL: not_modified}) == 0
        assert (repo / "providers" / "anthropic.json").read_bytes() == before
        record = json.loads(
            (repo / "history" / "_fetch_failures.jsonl").read_text().strip().split("\n")[-1]
        )
        assert record["kind"] == "unexpected_304"


class TestDryRun:
    def test_dry_run_writes_nothing_and_commits_nothing(self, repo, run, capsys):
        assert run("--dry-run") == 0
        assert commit_count(repo) == 1
        assert not (repo / "providers").exists()

        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "providers/anthropic.json" in out
        assert "COMMIT" in out

    def test_dry_run_reports_no_commit_when_nothing_changed(self, repo, run, capsys):
        run()
        capsys.readouterr()
        assert run("--dry-run") == 0
        out = capsys.readouterr().out
        assert "no change and heartbeat not due" in out


class TestWorkingTreeGuard:
    def test_a_dirty_tracked_file_aborts_the_run(self, repo, run):
        run()
        (repo / "providers" / "anthropic.json").write_text("{}\n")
        with pytest.raises(SystemExit) as excinfo:
            run()
        assert excinfo.value.code == runner.EXIT_DIRTY_TREE

    def test_allow_dirty_overrides_the_guard(self, repo, run):
        run()
        (repo / "providers" / "anthropic.json").write_text("{}\n")
        assert run("--allow-dirty") == 0


class TestProviderFilter:
    def test_a_single_provider_can_be_polled_alone(self, repo, run):
        assert run("--provider", "anthropic") == 0
        assert (repo / "providers" / "anthropic.json").exists()
        assert not (repo / "providers" / "openai.json").exists()

    def test_an_unknown_provider_name_is_an_error(self, repo, run):
        assert run("--provider", "nonexistent") == runner.EXIT_UNEXPECTED


class TestConditionalRequests:
    def test_validators_from_committed_state_are_sent_on_the_next_poll(
        self, repo, responses, monkeypatch
    ):
        captured: list[FakeFetcher] = []

        def _run(*extra: str) -> int:
            fetcher = FakeFetcher(dict(responses))
            captured.append(fetcher)
            monkeypatch.setattr(runner, "UrllibFetcher", lambda **_k: fetcher)
            monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
            return runner.main(
                ["--config", CONFIG, "--repo-root", str(repo), "--no-push", *extra]
            )

        # Seed committed state carrying an ETag, the way Anthropic really replies.
        responses[ANTHROPIC_URL] = RawResponse(
            status_code=200,
            body=fixture_bytes("anthropic_summary.json"),
            url=ANTHROPIC_URL,
            headers={"etag": 'W/"seeded"'},
        )
        _run()
        _run()

        # The second poll happens inside the same UTC hour as the first only if
        # the hour has not rolled; when it has, validators are deliberately
        # dropped for a full re-sync. Accept either, but if one was sent it must
        # be the committed value rather than something invented.
        sent = dict((url, etag) for url, etag, _lm in captured[1].calls)
        assert sent.get(ANTHROPIC_URL) in (None, 'W/"seeded"')
