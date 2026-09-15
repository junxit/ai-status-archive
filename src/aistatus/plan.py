"""The commit decision state machine, as a pure function.

This is the part of the system that is easiest to get wrong, so it is isolated
here with no I/O at all. :func:`plan_run` takes what was collected plus what is
currently committed, and returns a :class:`RunPlan`: an explicit list of
byte-level writes and an optional commit. The runner's only job is to execute
that plan.

Two things fall out of that shape:

* ``--dry-run`` is a single branch in the runner. There is no ``if dry_run`` in
  any pure module, and therefore no second code path that can drift.
* A future Lambda runner can execute the same plan against S3 keys, because
  :attr:`WriteOp.path` is a plain repo-relative string rather than a filesystem
  path.

The three commit triggers are a union; any one fires a commit:

a. Some provider's normalized state changed (or was seen for the first time).
b. The heartbeat is due, meaning the UTC hour has rolled over since it was last
   committed. Commit-on-change alone cannot distinguish "stable for nine days"
   from "the poller died nine days ago", and that ambiguity is exactly what
   ruins a forensic dataset.
c. A provider's failure streak crossed the zero boundary in either direction.
   Without this, a multi-hour upstream outage would be recorded only by hourly
   heartbeats, and because the runner is ephemeral, uncommitted failure lines
   would simply evaporate. This costs at most two extra commits per episode.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .collect import CollectOutcome
from .diff import diff_snapshots
from .models import ChangeEvent, ComponentStatus, ProviderConfig, Snapshot
from .serialize import (
    content_hash,
    dict_to_snapshot,
    dump_raw,
    dump_snapshot,
    event_line,
    failure_line,
    pretty_bytes,
    slug,
)

HEARTBEAT_PATH = "state/last_poll.json"
FAILURES_PATH = "history/_fetch_failures.jsonl"

#: Subject lines longer than this are truncated with a "+N more" suffix.
SUBJECT_MAX_CHARS = 72


class Outcome(StrEnum):
    """How one provider's poll turned out."""

    CHANGED = "changed"
    BASELINE = "baseline"
    UNCHANGED = "unchanged"
    NOT_MODIFIED = "not_modified"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class WriteOp:
    """One file write the runner should perform.

    Attributes:
        path: Repo-relative POSIX path. Deliberately a string, not a
            :class:`pathlib.Path`, so the same plan can target object storage.
        mode: ``replace`` overwrites the file; ``append`` adds to the end.
        data: Exact bytes to write or append.
        reason: Human-readable justification, shown by ``--dry-run``.
    """

    path: str
    mode: str
    data: bytes
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitPlan:
    """The commit the runner should make.

    Attributes:
        subject: Commit subject line.
        body: Commit body, carrying the change events verbatim so that
            ``git log --grep`` and ``git log -S`` are useful.
        paths: Exact paths to stage. Never ``git add -A``; staging only what was
            planned means an unexpected file can never ride along.
    """

    subject: str
    body: str
    paths: tuple[str, ...]

    @property
    def message(self) -> str:
        """Full commit message."""
        return f"{self.subject}\n\n{self.body}" if self.body else self.subject


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderResult:
    """Per-provider outcome, for logging and exit codes.

    Attributes:
        name: Provider key.
        outcome: What happened.
        events: Change events derived, if any.
        previous_hash: Content hash of the committed state, if there was one.
        current_hash: Content hash of the newly collected state, if any.
        error_kind: Short failure category when ``outcome`` is ``FAILED``.
        error_detail: Human-readable failure detail.
        consecutive_failures: Failure streak after this poll.
    """

    name: str
    outcome: Outcome
    events: tuple[ChangeEvent, ...] = ()
    previous_hash: str | None = None
    current_hash: str | None = None
    error_kind: str | None = None
    error_detail: str | None = None
    consecutive_failures: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class RunPlan:
    """Everything the runner should do this cycle.

    Attributes:
        writes: Byte-level file operations, in execution order.
        commit: The commit to make, or ``None`` to make none.
        results: Per-provider outcomes.
        warnings: Non-fatal anomalies worth surfacing.
    """

    writes: tuple[WriteOp, ...] = ()
    commit: CommitPlan | None = None
    results: tuple[ProviderResult, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def any_failed(self) -> bool:
        """Whether at least one provider failed."""
        return any(r.outcome is Outcome.FAILED for r in self.results)

    @property
    def all_failed(self) -> bool:
        """Whether every provider failed, which suggests the problem is ours."""
        return bool(self.results) and all(
            r.outcome is Outcome.FAILED for r in self.results
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RunContext:
    """Committed state and ambient values, gathered by the runner.

    Attributes:
        now: Current time, from an injected clock.
        previous_docs: Parsed ``providers/{name}.json`` as committed at ``HEAD``,
            keyed by provider name. Read from ``HEAD`` rather than the working
            tree so that a run which wrote files and then died before committing
            self-heals on the next pass instead of silently losing a transition.
        heartbeat_doc: Parsed ``state/last_poll.json`` as committed at ``HEAD``.
        run_url: Link to the CI run, included as a commit trailer.
    """

    now: str
    previous_docs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    heartbeat_doc: Mapping[str, Any] | None = None
    run_url: str | None = None


def hour_bucket(timestamp: str | None) -> str | None:
    """Reduce a timestamp to its UTC hour.

    Heartbeat cadence is decided by comparing hour buckets rather than by
    measuring elapsed minutes. A ``>= 60 minutes`` test ratchets forward under
    cron drift — 61 minutes, then 65, then 71 — and undershoots the intended
    24 commits per day unpredictably. Flooring to the hour is exact.

    Args:
        timestamp: An RFC 3339 UTC timestamp, or ``None``.

    Returns:
        ``YYYY-MM-DDTHH``, or ``None`` if the input was empty or too short.
    """
    if not timestamp or len(timestamp) < 13:
        return None
    return timestamp[:13]


def _previous_snapshot(ctx: RunContext, name: str) -> Snapshot | None:
    """Reconstruct a provider's committed snapshot, if there is one."""
    doc = ctx.previous_docs.get(name)
    if not doc:
        return None
    try:
        return dict_to_snapshot(doc)
    except (TypeError, ValueError, KeyError):
        return None


def _prior_failures(ctx: RunContext, name: str) -> int:
    """Read a provider's committed failure streak."""
    heartbeat = ctx.heartbeat_doc or {}
    entry = (heartbeat.get("providers") or {}).get(name) or {}
    value = entry.get("consecutive_failures", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def _prior_fetched_at(ctx: RunContext, name: str) -> str | None:
    """Read a provider's committed last-successful-fetch time."""
    heartbeat = ctx.heartbeat_doc or {}
    entry = (heartbeat.get("providers") or {}).get(name) or {}
    value = entry.get("fetched_at")
    return value if isinstance(value, str) and value else None


def _describe(snapshot: Snapshot, cfg: ProviderConfig, *, baseline: bool) -> str:
    """Summarize a provider's state for the commit subject line.

    Speaks about API-tagged components when the provider has any, so that a
    consumer-surface blip does not dominate a subject line that should be about
    whether the API is up.

    Args:
        snapshot: The provider's new state.
        cfg: Provider configuration, supplying the API component tags.
        baseline: Whether this is the provider's first observation.

    Returns:
        A short phrase such as ``anthropic api degraded_performance``.
    """
    scope = cfg.api_components or None
    component, status = snapshot.worst_status(only=scope)
    if component is None or status is ComponentStatus.OPERATIONAL:
        text = f"{snapshot.provider} operational"
    else:
        text = f"{snapshot.provider} {slug(component.name)} {status}"
    return f"baseline {text}" if baseline else text


def _heartbeat_doc(
    ctx: RunContext,
    results: Sequence[ProviderResult],
    outcomes: Sequence[CollectOutcome],
    *,
    committing: bool,
) -> dict[str, Any]:
    """Build the new ``state/last_poll.json`` content.

    ``heartbeat_committed_at`` is stored *inside* this document rather than
    derived from ``git log``. That is not a stylistic choice: ``actions/checkout``
    defaults to ``fetch-depth: 1``, where ``git log -1 -- state/last_poll.json``
    returns nothing whenever the last change to that path predates the single
    fetched commit. Deriving the cadence from commit metadata would therefore
    break silently as soon as the repo had any history.

    Args:
        ctx: Committed state and clock.
        results: Per-provider results for this cycle.
        outcomes: Raw collection outcomes, for the successful fetch times.
        committing: Whether this cycle is producing a commit, which is when the
            heartbeat timestamp advances.

    Returns:
        A JSON-compatible dict.
    """
    by_name = {o.cfg.name: o for o in outcomes}
    previous_committed = (ctx.heartbeat_doc or {}).get("heartbeat_committed_at")

    providers: dict[str, Any] = {}
    for result in results:
        outcome = by_name.get(result.name)
        failed = result.outcome is Outcome.FAILED
        # A failed provider keeps its previous fetched_at. Advancing it would
        # claim we successfully observed a provider we could not reach.
        fetched_at = (
            _prior_fetched_at(ctx, result.name)
            if failed
            else (outcome.fetched_at if outcome else ctx.now)
        )
        providers[result.name] = {
            "fetched_at": fetched_at,
            "ok": not failed,
            "last_outcome": str(result.outcome),
            "consecutive_failures": result.consecutive_failures,
            "content_hash": result.current_hash or result.previous_hash,
        }

    return {
        "heartbeat_committed_at": ctx.now if committing else previous_committed,
        "polled_at": ctx.now,
        "providers": providers,
    }


def plan_run(
    outcomes: Sequence[CollectOutcome], *, ctx: RunContext
) -> RunPlan:
    """Decide what to write and whether to commit.

    Args:
        outcomes: One collection outcome per provider.
        ctx: Committed state, plus the current time.

    Returns:
        A fully materialized :class:`RunPlan`.
    """
    writes: list[WriteOp] = []
    results: list[ProviderResult] = []
    warnings: list[str] = []
    summaries: list[str] = []
    all_events: list[ChangeEvent] = []
    streak_crossed = False

    for outcome in outcomes:
        cfg = outcome.cfg
        name = cfg.name
        prior_failures = _prior_failures(ctx, name)
        previous = _previous_snapshot(ctx, name)
        previous_hash = content_hash(previous) if previous else None

        if outcome.error is not None:
            kind = getattr(outcome.error, "kind", type(outcome.error).__name__)
            detail = str(outcome.error)
            streak = prior_failures + 1
            if prior_failures == 0:
                streak_crossed = True

            # The critical rule: a failed fetch never touches providers/{name}.json.
            # Writing "unknown" there would be indistinguishable from a real
            # outage in the history and would poison the dataset. Our inability
            # to reach a status page is a fact about us, not about them.
            writes.append(
                WriteOp(
                    path=FAILURES_PATH,
                    mode="append",
                    data=failure_line(
                        at=outcome.fetched_at or ctx.now,
                        provider=name,
                        kind=kind,
                        detail=detail,
                        http_status=outcome.http_status,
                    ),
                    reason=f"{name}: fetch failed ({kind})",
                )
            )
            results.append(
                ProviderResult(
                    name=name,
                    outcome=Outcome.FAILED,
                    previous_hash=previous_hash,
                    error_kind=kind,
                    error_detail=detail,
                    consecutive_failures=streak,
                )
            )
            continue

        if prior_failures > 0:
            streak_crossed = True

        if outcome.not_modified or outcome.snapshot is None:
            results.append(
                ProviderResult(
                    name=name,
                    outcome=Outcome.NOT_MODIFIED,
                    previous_hash=previous_hash,
                    current_hash=previous_hash,
                    consecutive_failures=0,
                )
            )
            continue

        snapshot = outcome.snapshot
        current_hash = content_hash(snapshot)
        stamped = _stamp_hash(snapshot, current_hash)

        if previous_hash == current_hash:
            results.append(
                ProviderResult(
                    name=name,
                    outcome=Outcome.UNCHANGED,
                    previous_hash=previous_hash,
                    current_hash=current_hash,
                    consecutive_failures=0,
                )
            )
            continue

        is_baseline = previous is None
        events = diff_snapshots(previous, snapshot, at=snapshot.fetch.fetched_at)

        # Canary: the hash moved but nothing in our event vocabulary explains it.
        # Almost always this means a field entered the hashed body that no rule
        # considers a change — that is, churn. Surfacing it beats absorbing it.
        if not events and not is_baseline:
            warnings.append(
                f"{name}: content hash changed but no change events were derived; "
                "a field in the hashed body may be churning"
            )

        writes.append(
            WriteOp(
                path=f"providers/{name}.json",
                mode="replace",
                data=dump_snapshot(stamped),
                reason=f"{name}: {previous_hash or '(new)'} -> {current_hash}",
            )
        )
        writes.append(
            WriteOp(
                path=f"raw/{name}.json",
                mode="replace",
                data=dump_raw(outcome.raw_payload),
                reason=f"{name}: raw upstream payload",
            )
        )
        if events:
            writes.append(
                WriteOp(
                    path=f"history/{name}.jsonl",
                    mode="append",
                    data=b"".join(event_line(e) for e in events),
                    reason=f"{name}: {len(events)} change event(s)",
                )
            )

        all_events.extend(events)
        summaries.append(_describe(snapshot, cfg, baseline=is_baseline))
        results.append(
            ProviderResult(
                name=name,
                outcome=Outcome.BASELINE if is_baseline else Outcome.CHANGED,
                events=events,
                previous_hash=previous_hash,
                current_hash=current_hash,
                consecutive_failures=0,
            )
        )

    changed = bool(summaries)
    heartbeat_due = hour_bucket(
        (ctx.heartbeat_doc or {}).get("heartbeat_committed_at")
    ) != hour_bucket(ctx.now)
    committing = changed or heartbeat_due or streak_crossed

    if committing:
        writes.append(
            WriteOp(
                path=HEARTBEAT_PATH,
                mode="replace",
                data=pretty_bytes(
                    _heartbeat_doc(ctx, results, outcomes, committing=True)
                ),
                reason=(
                    "status changed"
                    if changed
                    else ("hour rolled over" if heartbeat_due else "failure streak changed")
                ),
            )
        )

    commit = None
    if committing:
        commit = CommitPlan(
            subject=_subject(ctx.now, summaries),
            body=_body(all_events, ctx),
            paths=tuple(dict.fromkeys(w.path for w in writes)),
        )

    return RunPlan(
        writes=tuple(writes),
        commit=commit,
        results=tuple(results),
        warnings=tuple(warnings),
    )


def _stamp_hash(snapshot: Snapshot, digest: str) -> Snapshot:
    """Return a copy of ``snapshot`` carrying its own content hash.

    The hash lives inside the ``fetch`` block, which is excluded from hashing, so
    it is self-excluding with no special case needed.
    """
    return replace(snapshot, fetch=replace(snapshot.fetch, content_hash=digest))


def _subject(now: str, summaries: Sequence[str]) -> str:
    """Build the commit subject line.

    The UTC timestamp is included because ``git log --oneline`` shows no dates,
    and scanning this archive by eye is a primary workflow.

    Args:
        now: Current time.
        summaries: Per-provider phrases, empty for a heartbeat-only commit.

    Returns:
        A subject line, truncated with a ``(+N more)`` suffix if needed.
    """
    stamp = now[:16] + "Z" if len(now) >= 16 else now
    if not summaries:
        return f"chore(status): {stamp} heartbeat"

    ordered = sorted(summaries)
    subject = f"chore(status): {stamp} " + "; ".join(ordered)
    if len(subject) <= SUBJECT_MAX_CHARS:
        return subject

    kept: list[str] = []
    for item in ordered:
        candidate = f"chore(status): {stamp} " + "; ".join([*kept, item])
        if kept and len(candidate) + 12 > SUBJECT_MAX_CHARS:
            break
        kept.append(item)
    remaining = len(ordered) - len(kept)
    base = f"chore(status): {stamp} " + "; ".join(kept)
    return f"{base} (+{remaining} more)" if remaining > 0 else base


def _body(events: Sequence[ChangeEvent], ctx: RunContext) -> str:
    """Build the commit body.

    Change events are embedded verbatim so that ``git log -S`` and
    ``git log --grep`` can search the archive's semantics directly, which is the
    entire forensic use case, for very little cost.
    """
    lines = [event_line(e).decode("utf-8").rstrip("\n") for e in events]
    trailers = [f"Poll-At: {ctx.now}"]
    if ctx.run_url:
        trailers.append(f"Run: {ctx.run_url}")
    return "\n".join([*lines, "", *trailers]).strip()


__all__ = [
    "FAILURES_PATH",
    "HEARTBEAT_PATH",
    "SUBJECT_MAX_CHARS",
    "CommitPlan",
    "Outcome",
    "ProviderResult",
    "RunContext",
    "RunPlan",
    "WriteOp",
    "hour_bucket",
    "plan_run",
]
