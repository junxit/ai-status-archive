#!/usr/bin/env python3
"""Answer "what was this provider's status at time T?" from git history.

This is the point of the archive. Usage::

    python scripts/at.py --provider anthropic --at 2026-09-14T03:14:00Z
    python scripts/at.py --provider openai --at 2026-09-14T03:14:00Z --window 2h
    python scripts/at.py --provider google --at 2026-09-14T03:14:00Z --json

The subtle part is coverage. Because the poller commits only when state changes,
the commit in effect at a given instant may legitimately be days old — that means
"nothing changed", not "nothing was recorded". So the commit date cannot be used
to judge whether the archive actually covers the moment being asked about.

Instead, coverage is judged from ``state/last_poll.json``, which records the last
successful fetch per provider and is committed at least hourly. This tool
resolves the heartbeat on both sides of the requested instant and reports the gap
explicitly. Without that, a long quiet stretch looks like a gap and a real gap
looks quiet — which is exactly the ambiguity a forensic dataset must not have.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A poll older than this relative to the requested instant is reported as a
#: coverage gap. Polling runs every five minutes, so ten minutes means roughly
#: two consecutive misses.
COVERAGE_GAP_SECONDS = 600

DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

STATUS_MARKERS = {
    "operational": "ok",
    "degraded_performance": "DEGRADED",
    "partial_outage": "PARTIAL OUTAGE",
    "major_outage": "MAJOR OUTAGE",
    "under_maintenance": "maintenance",
    "unknown": "unknown",
}


class QueryError(Exception):
    """A query could not be answered."""


def git(*args: str, repo: Path) -> str:
    """Run a git command and return stdout, or empty string on failure."""
    result = subprocess.run(
        ["git", "--no-pager", *args], cwd=repo, capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else ""


def parse_instant(value: str) -> dt.datetime:
    """Parse a user-supplied timestamp into an aware UTC datetime.

    Args:
        value: RFC 3339 timestamp, with or without a ``Z`` suffix.

    Returns:
        A timezone-aware datetime in UTC.

    Raises:
        QueryError: If the value cannot be parsed.
    """
    text = value.strip()
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise QueryError(
            f"could not parse {value!r}; expected something like 2026-09-14T03:14:00Z"
        ) from exc
    return (
        parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed.astimezone(dt.UTC)
    )


def parse_duration(value: str) -> dt.timedelta:
    """Parse a duration such as ``2h``, ``30m``, or ``1d``.

    Args:
        value: Duration string.

    Returns:
        The corresponding timedelta.

    Raises:
        QueryError: If the value cannot be parsed.
    """
    match = DURATION_RE.match(value.strip())
    if not match:
        raise QueryError(f"could not parse duration {value!r}; try 30m, 2h, or 1d")
    return dt.timedelta(seconds=int(match.group(1)) * DURATION_UNITS[match.group(2).lower()])


def iso(moment: dt.datetime) -> str:
    """Format a datetime as RFC 3339 UTC with a ``Z`` suffix."""
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def commit_before(repo: Path, path: str, instant: dt.datetime) -> str | None:
    """Find the last commit touching ``path`` at or before ``instant``."""
    out = git(
        "rev-list", "-1", f"--before={iso(instant)}", "HEAD", "--", path, repo=repo
    ).strip()
    return out or None


def commit_after(repo: Path, path: str, instant: dt.datetime) -> str | None:
    """Find the first commit touching ``path`` strictly after ``instant``."""
    lines = git(
        "rev-list", f"--since={iso(instant)}", "HEAD", "--", path, repo=repo
    ).split()
    # rev-list is newest-first, so the oldest of the "after" set is last.
    return lines[-1] if lines else None


def show_json(repo: Path, sha: str, path: str) -> dict[str, Any] | None:
    """Read and parse a file as it existed at a given commit."""
    blob = git("show", f"{sha}:{path}", repo=repo)
    if not blob.strip():
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def commit_meta(repo: Path, sha: str) -> dict[str, str]:
    """Return a commit's UTC date and subject.

    ``%cI`` renders in the committer's recorded timezone, which on a local run is
    whatever the machine is set to. This archive states times in UTC only, so the
    value is reparsed and reformatted rather than passed through.
    """
    out = git("show", "-s", "--format=%cI%x00%s", sha, repo=repo).strip()
    if not out:
        return {"sha": sha, "committed_at": "", "subject": ""}
    raw, _, subject = out.partition("\0")
    try:
        committed_at = iso(parse_instant(raw))
    except QueryError:
        committed_at = raw
    return {"sha": sha[:12], "committed_at": committed_at, "subject": subject}


def heartbeat_at(
    repo: Path, provider: str, instant: dt.datetime
) -> tuple[str | None, str | None]:
    """Resolve the provider's last successful fetch bracketing an instant.

    Args:
        repo: Repository root.
        provider: Provider key.
        instant: The moment in question.

    Returns:
        A ``(before, after)`` pair of fetch timestamps. Either may be ``None``
        when the archive does not extend that far in that direction.
    """

    def read(sha: str | None) -> str | None:
        if not sha:
            return None
        doc = show_json(repo, sha, "state/last_poll.json") or {}
        entry = (doc.get("providers") or {}).get(provider) or {}
        value = entry.get("fetched_at")
        return value if isinstance(value, str) else None

    return (
        read(commit_before(repo, "state/last_poll.json", instant)),
        read(commit_after(repo, "state/last_poll.json", instant)),
    )


def assess_coverage(
    instant: dt.datetime, before: str | None, after: str | None
) -> dict[str, Any]:
    """Judge whether the archive genuinely covers a moment.

    Args:
        instant: The moment in question.
        before: Last successful fetch at or before it.
        after: First successful fetch after it.

    Returns:
        A dict with ``status``, ``gap_seconds``, and the bracketing timestamps.
        ``status`` is one of ``covered``, ``gap``, ``before_archive``, or
        ``after_archive``.
    """
    result: dict[str, Any] = {
        "status": "covered",
        "gap_seconds": None,
        "poll_before": before,
        "poll_after": after,
        "threshold_seconds": COVERAGE_GAP_SECONDS,
    }
    if before is None:
        result["status"] = "before_archive"
        return result

    gap = int((instant - parse_instant(before)).total_seconds())
    result["gap_seconds"] = gap
    if gap > COVERAGE_GAP_SECONDS:
        result["status"] = "after_archive" if after is None else "gap"
    return result


def read_history(repo: Path, provider: str) -> list[dict[str, Any]]:
    """Read a provider's change log, preferring the working tree.

    The history files are append-only, so the working tree holds a superset of
    what is committed.
    """
    path = repo / "history" / f"{provider}.jsonl"
    text = path.read_text(encoding="utf-8") if path.exists() else git(
        "show", f"HEAD:history/{provider}.jsonl", repo=repo
    )
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def events_in_window(
    repo: Path, provider: str, instant: dt.datetime, window: dt.timedelta
) -> list[dict[str, Any]]:
    """Return change events within ``window`` either side of ``instant``."""
    low, high = instant - window, instant + window
    selected = []
    for event in read_history(repo, provider):
        raw = event.get("at")
        if not isinstance(raw, str):
            continue
        try:
            when = parse_instant(raw)
        except QueryError:
            continue
        if low <= when <= high:
            selected.append(event)
    return sorted(selected, key=lambda e: e.get("at", ""))


def summarize(doc: dict[str, Any], api_only: frozenset[str] | None) -> dict[str, Any]:
    """Extract a headline verdict from a stored snapshot."""
    components = doc.get("components") or []
    scoped = [
        c for c in components if api_only is None or c.get("id") in api_only
    ] or components
    severity = {
        "operational": 0,
        "unknown": 1,
        "under_maintenance": 2,
        "degraded_performance": 3,
        "partial_outage": 4,
        "major_outage": 5,
    }
    worst = max(scoped, key=lambda c: severity.get(c.get("status"), 1), default=None)
    return {
        "indicator": (doc.get("overall") or {}).get("indicator"),
        "description": (doc.get("overall") or {}).get("description"),
        "worst_component": worst,
        "component_count": len(components),
        "active_incidents": doc.get("active_incidents") or [],
    }


def load_api_components(repo: Path, provider: str) -> frozenset[str] | None:
    """Read the API component tags for a provider, if the config is available."""
    config = repo / "config" / "providers.yaml"
    if not config.exists():
        return None
    try:
        import yaml
    except ImportError:
        return None
    try:
        document = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    for entry in document.get("providers") or []:
        if entry.get("name") == provider:
            tagged = entry.get("api_components") or []
            return frozenset(str(c) for c in tagged) or None
    return None


def render_text(report: dict[str, Any]) -> str:
    """Render a human-readable report."""
    lines: list[str] = []
    provider = report["provider"]
    lines.append(f"{provider} @ {report['requested_at']}")
    lines.append("")

    coverage = report["coverage"]
    if report["snapshot"] is None:
        lines.append("  NO DATA — the archive has no commit for this provider at or")
        lines.append("            before that instant.")
        if coverage["poll_after"]:
            lines.append(f"            Earliest recorded poll: {coverage['poll_after']}")
        return "\n".join(lines) + "\n"

    summary = report["summary"]
    worst = summary["worst_component"]
    status = (worst or {}).get("status", "unknown")
    marker = STATUS_MARKERS.get(status, status)

    lines.append(f"  status     {marker}")
    lines.append(f"  indicator  {summary['indicator']}  ({summary['description']})")
    if worst and status != "operational":
        lines.append(f"  worst      {worst.get('name')}  [{worst.get('id')}]")
    lines.append(f"  scope      {summary['component_count']} component(s) archived")

    incidents = summary["active_incidents"]
    if incidents:
        lines.append("")
        lines.append(f"  {len(incidents)} active incident(s):")
        for incident in incidents:
            lines.append(f"    - {incident.get('name')}")
            lines.append(
                f"      {incident.get('status')} / {incident.get('impact')}"
                f"  started {incident.get('started_at')}"
            )
            if incident.get("url"):
                lines.append(f"      {incident['url']}")

    source = report["source_commit"]
    lines.append("")
    lines.append(f"  from commit {source['sha']} ({source['committed_at']})")
    lines.append(f"    {source['subject']}")

    lines.append("")
    gap = coverage["gap_seconds"]
    if coverage["status"] == "covered":
        lines.append(f"  coverage   OK — last poll {gap}s before the requested instant")
    elif coverage["status"] == "gap":
        lines.append(
            f"  coverage   ** GAP ** — the nearest poll was {gap}s "
            f"({gap // 60}m) before the requested instant,"
        )
        lines.append(
            f"             beyond the {coverage['threshold_seconds']}s threshold. "
            "Treat this answer as unverified."
        )
        lines.append(f"             bracketed by {coverage['poll_before']} .. {coverage['poll_after']}")
    elif coverage["status"] == "before_archive":
        lines.append("  coverage   ** NO COVERAGE ** — instant predates the archive.")
    else:
        lines.append(
            f"  coverage   ** UNBOUNDED ** — last poll was {gap}s before the instant "
            "and no later poll exists."
        )
        lines.append("             The poller may have stopped. Treat as unverified.")

    timeline = report.get("timeline")
    if timeline is not None:
        lines.append("")
        lines.append(f"  timeline (±{report['window']}):")
        if not timeline:
            lines.append("    (no recorded changes in this window)")
        for event in timeline:
            target = event.get("component_id") or event.get("incident_id") or "-"
            lines.append(
                f"    {event.get('at')}  {event.get('change_type'):<19} {target}"
                f"  {event.get('from')} -> {event.get('to')}"
            )

    return "\n".join(lines) + "\n"


def build_report(
    repo: Path, provider: str, instant: dt.datetime, window: dt.timedelta | None
) -> dict[str, Any]:
    """Assemble the full answer for one provider at one instant."""
    path = f"providers/{provider}.json"
    sha = commit_before(repo, path, instant)
    doc = show_json(repo, sha, path) if sha else None
    before, after = heartbeat_at(repo, provider, instant)

    report: dict[str, Any] = {
        "provider": provider,
        "requested_at": iso(instant),
        "snapshot": doc,
        "source_commit": commit_meta(repo, sha) if sha else None,
        "coverage": assess_coverage(instant, before, after),
    }
    if doc is not None:
        report["summary"] = summarize(doc, load_api_components(repo, provider))
    if window is not None:
        report["window"] = f"{int(window.total_seconds() // 60)}m"
        report["timeline"] = events_in_window(repo, provider, instant, window)
    return report


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(
        prog="at.py",
        description="Resolve an AI provider's recorded status at a past instant.",
    )
    parser.add_argument("--provider", required=True, help="Provider key, e.g. anthropic.")
    parser.add_argument(
        "--at", required=True, help="Instant to query, e.g. 2026-09-14T03:14:00Z."
    )
    parser.add_argument(
        "--window",
        default=None,
        help="Also print the change timeline within this window, e.g. 2h.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON for piping.")
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="Repository root.")
    args = parser.parse_args(argv)

    repo = Path(args.repo_root).resolve()
    try:
        instant = parse_instant(args.at)
        window = parse_duration(args.window) if args.window else None
    except QueryError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    if not (repo / ".git").exists():
        sys.stderr.write(f"error: {repo} is not a git repository\n")
        return 2

    report = build_report(repo, args.provider, instant, window)

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(report))

    # A coverage problem is worth a non-zero exit so this composes in scripts.
    return 0 if report["coverage"]["status"] == "covered" else 1


if __name__ == "__main__":
    raise SystemExit(main())
