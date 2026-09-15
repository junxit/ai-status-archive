"""The GitHub Actions runner: all filesystem and git side effects live here.

Everything above this file is pure. This module reads configuration, performs
HTTP, reads committed state out of git, executes the write plan, and commits.
It contains no decision logic of its own — :func:`aistatus.plan.plan_run` decides
what should happen and this module carries it out.

That split is what makes ``--dry-run`` trustworthy: it is a single branch here,
skipping execution while everything that produced the plan ran normally.

Committed state is read with ``git show HEAD:<path>`` rather than from the
working tree. If a run writes its files and then dies before committing — a
rejected push, an expired token, a cancelled job — a working-tree read would
compare the next run's data against that uncommitted data, conclude "unchanged",
and lose the transition permanently. Reading ``HEAD`` makes the system self-heal.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aistatus.collect import CollectOutcome, collect_all, load_providers  # noqa: E402
from aistatus.fetch import UrllibFetcher, utcnow_z  # noqa: E402
from aistatus.models import ProviderConfig  # noqa: E402
from aistatus.plan import (  # noqa: E402
    HEARTBEAT_PATH,
    Outcome,
    RunContext,
    RunPlan,
    WriteOp,
    hour_bucket,
    plan_run,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_ALL_FAILED = 2
EXIT_DIRTY_TREE = 3

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def git(
    *args: str, repo: Path, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    """Run a git command with the pager disabled.

    Args:
        *args: Arguments after ``git``.
        repo: Repository root.
        check: Whether a non-zero exit should raise.

    Returns:
        The completed process, with bytes captured.
    """
    return subprocess.run(
        ["git", "--no-pager", *args],
        cwd=repo,
        check=check,
        capture_output=True,
    )


def has_commits(repo: Path) -> bool:
    """Whether the repository has at least one commit."""
    return git("rev-parse", "--verify", "HEAD", repo=repo, check=False).returncode == 0


def read_head_bytes(repo: Path, path: str) -> bytes | None:
    """Read a file's contents as committed at ``HEAD``.

    Args:
        repo: Repository root.
        path: Repo-relative path.

    Returns:
        The committed bytes, or ``None`` if the path is absent from ``HEAD`` or
        the repository has no commits yet.
    """
    if not has_commits(repo):
        return None
    result = git("show", f"HEAD:{path}", repo=repo, check=False)
    return result.stdout if result.returncode == 0 else None


def read_head_json(repo: Path, path: str) -> dict[str, Any] | None:
    """Read and parse a JSON file as committed at ``HEAD``.

    A committed file that will not parse is treated as absent rather than fatal.
    That degrades to a baseline observation, which is recoverable, instead of
    wedging the poller on corrupt state.
    """
    data = read_head_bytes(repo, path)
    if not data:
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def assert_clean_tree(repo: Path, *, allow_dirty: bool) -> None:
    """Refuse to run against a dirty working tree.

    In Actions the checkout is always fresh, so this costs nothing. Locally it
    matters: ``history/*.jsonl`` files are appended to rather than rewritten, and
    running against a tree that has diverged from ``HEAD`` would append the same
    events twice.

    Args:
        repo: Repository root.
        allow_dirty: Skip the check when the caller knows what they are doing.

    Raises:
        SystemExit: With :data:`EXIT_DIRTY_TREE` if the tree is dirty.
    """
    if allow_dirty or not has_commits(repo):
        return
    tracked = [
        line
        for line in git("status", "--porcelain", repo=repo).stdout.decode().splitlines()
        if line and not line.startswith("??")
    ]
    if tracked:
        sys.stderr.write(
            "refusing to run against a dirty working tree; commit, stash, or pass "
            "--allow-dirty:\n" + "\n".join(tracked) + "\n"
        )
        raise SystemExit(EXIT_DIRTY_TREE)


def apply_writes(repo: Path, writes: Sequence[WriteOp]) -> None:
    """Execute a plan's write operations.

    Args:
        repo: Repository root.
        writes: Operations in execution order.
    """
    for op in writes:
        target = repo / op.path
        target.parent.mkdir(parents=True, exist_ok=True)
        if op.mode == "append":
            with target.open("ab") as handle:
                handle.write(op.data)
        else:
            target.write_bytes(op.data)


def commit_and_push(
    repo: Path, plan: RunPlan, *, no_push: bool, remote: str = "origin"
) -> bool:
    """Stage exactly the planned paths, commit, and push with one retry.

    Only the planned paths are staged. ``git add -A`` would let an unrelated file
    ride along in a commit whose message claims to describe a status change.

    Args:
        repo: Repository root.
        plan: The plan being executed.
        no_push: Commit locally without pushing.
        remote: Remote name.

    Returns:
        Whether a commit was created.
    """
    if plan.commit is None:
        return False

    git("add", "--", *plan.commit.paths, repo=repo)

    if git("diff", "--cached", "--quiet", repo=repo, check=False).returncode == 0:
        sys.stderr.write(
            "planner produced a commit but nothing was staged; this is a bug in "
            "the planner, not a no-op run\n"
        )
        return False

    git(
        "-c",
        f"user.name={BOT_NAME}",
        "-c",
        f"user.email={BOT_EMAIL}",
        "commit",
        "--no-verify",
        "-m",
        plan.commit.message,
        repo=repo,
    )

    if no_push:
        return True

    for attempt in range(2):
        if git("push", remote, "HEAD", repo=repo, check=False).returncode == 0:
            return True
        # A delayed scheduled run can collide with the next one. Rebasing onto
        # whatever landed first is safe here: providers/ and raw/ are whole-file
        # replacements, and history/*.jsonl uses union merge (see .gitattributes).
        if attempt == 0:
            git("pull", "--rebase", "--autostash", remote, repo=repo, check=False)
            time.sleep(2)

    sys.stderr.write("push failed after retry; the commit is local only\n")
    return True


def render_dry_run(plan: RunPlan) -> str:
    """Render what a run would do, without doing it.

    Args:
        plan: The plan that would have been executed.

    Returns:
        A human-readable report.
    """
    lines = ["DRY RUN — nothing was written and nothing was committed", ""]

    for result in plan.results:
        detail = ""
        if result.outcome is Outcome.FAILED:
            detail = f"{result.error_kind}: {result.error_detail}"
        elif result.outcome in (Outcome.CHANGED, Outcome.BASELINE):
            detail = f"{result.previous_hash or '(new)'} -> {result.current_hash}"
            if result.events:
                detail += f"  ({len(result.events)} event(s))"
        else:
            detail = result.current_hash or ""
        lines.append(f"  {result.name:<10} {str(result.outcome):<13} {detail}")

    lines.append("")
    if plan.writes:
        lines.append(f"WRITES ({len(plan.writes)})")
        for op in plan.writes:
            size = f"{len(op.data)}B"
            lines.append(f"  {op.mode:<7} {op.path:<34} {size:>8}  [{op.reason}]")
    else:
        lines.append("WRITES (0) — nothing to write")

    lines.append("")
    if plan.commit:
        lines.append(f"COMMIT  {plan.commit.subject}")
        for line in plan.commit.body.splitlines():
            lines.append(f"        {line}")
    else:
        lines.append("COMMIT  none — no change and heartbeat not due")

    if plan.warnings:
        lines.append("")
        lines.append("WARNINGS")
        lines.extend(f"  ! {w}" for w in plan.warnings)

    return "\n".join(lines) + "\n"


def render_summary(plan: RunPlan, *, committed: bool) -> str:
    """Render a one-screen summary of a real run."""
    parts = [
        f"{r.name}={r.outcome}" + (f"({len(r.events)})" if r.events else "")
        for r in plan.results
    ]
    head = "  ".join(parts)
    tail = (
        f"committed: {plan.commit.subject}"
        if committed and plan.commit
        else "no commit (no change, heartbeat not due)"
    )
    warnings = "".join(f"\n  ! {w}" for w in plan.warnings)
    return f"{head}\n{tail}{warnings}\n"


def exit_code(plan: RunPlan) -> int:
    """Choose a process exit code.

    A partial failure exits zero on purpose. A red X every time one status page
    hiccups trains whoever inherits this to ignore the alert, and the failure is
    already recorded in ``history/_fetch_failures.jsonl``. Every provider failing
    is different: that points at our own network or configuration, which is
    genuinely actionable.
    """
    if plan.all_failed:
        return EXIT_ALL_FAILED
    return EXIT_OK


def build_validators(
    configs: Sequence[ProviderConfig],
    previous_docs: Mapping[str, Mapping[str, Any]],
    *,
    force_unconditional: bool,
) -> dict[str, tuple[str | None, str | None]]:
    """Collect per-provider cache validators from committed state.

    Args:
        configs: Providers being polled.
        previous_docs: Committed ``providers/{name}.json`` documents.
        force_unconditional: Drop all validators, forcing full responses.

    Returns:
        Mapping of provider name to ``(etag, last_modified)``.
    """
    if force_unconditional:
        return {}
    validators: dict[str, tuple[str | None, str | None]] = {}
    for cfg in configs:
        fetch = (previous_docs.get(cfg.name) or {}).get("fetch") or {}
        validators[cfg.name] = (fetch.get("etag"), fetch.get("last_modified"))
    return validators


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="github_actions.py",
        description="Poll AI provider status pages and commit only on change.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written and committed without touching git.",
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "config" / "providers.yaml"),
        help="Path to providers.yaml.",
    )
    parser.add_argument(
        "--repo-root", default=str(REPO_ROOT), help="Repository root to write into."
    )
    parser.add_argument(
        "--provider",
        action="append",
        default=None,
        help="Poll only this provider. Repeatable.",
    )
    parser.add_argument(
        "--no-push", action="store_true", help="Commit locally without pushing."
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Run even if the working tree has uncommitted changes.",
    )
    parser.add_argument(
        "--run-url", default=None, help="CI run URL, recorded as a commit trailer."
    )
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="Per-request timeout in seconds."
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point.

    Returns:
        A process exit code.
    """
    args = parse_args(argv)
    repo = Path(args.repo_root).resolve()

    if not args.dry_run:
        assert_clean_tree(repo, allow_dirty=args.allow_dirty)

    configs = load_providers(Path(args.config).read_text(encoding="utf-8"))
    if args.provider:
        wanted = set(args.provider)
        configs = tuple(c for c in configs if c.name in wanted)
        if not configs:
            sys.stderr.write(f"no providers matched {sorted(wanted)}\n")
            return EXIT_UNEXPECTED

    now = utcnow_z()
    heartbeat = read_head_json(repo, HEARTBEAT_PATH)
    previous_docs = {
        cfg.name: doc
        for cfg in configs
        if (doc := read_head_json(repo, f"providers/{cfg.name}.json")) is not None
    }

    # Once an hour, deliberately discard cache validators and take a full
    # response. A 304 asserts "unchanged" on the origin's authority rather than
    # on a hash we computed ourselves; this bounds how long a buggy or overeager
    # ETag could hide a real change. It reuses the heartbeat's hour bucket, so it
    # needs no extra state and no extra clock.
    heartbeat_due = hour_bucket(
        (heartbeat or {}).get("heartbeat_committed_at")
    ) != hour_bucket(now)

    outcomes: tuple[CollectOutcome, ...] = collect_all(
        configs,
        fetcher=UrllibFetcher(timeout=args.timeout),
        now=utcnow_z,
        sleep=time.sleep,
        validators=build_validators(
            configs, previous_docs, force_unconditional=heartbeat_due
        ),
    )

    plan = plan_run(
        outcomes,
        ctx=RunContext(
            now=now,
            previous_docs=previous_docs,
            heartbeat_doc=heartbeat,
            run_url=args.run_url,
        ),
    )

    if args.dry_run:
        sys.stdout.write(render_dry_run(plan))
        return exit_code(plan)

    apply_writes(repo, plan.writes)
    committed = commit_and_push(repo, plan, no_push=args.no_push)
    sys.stdout.write(render_summary(plan, committed=committed))

    for warning in plan.warnings:
        sys.stderr.write(f"::warning::{warning}\n")
    for result in plan.results:
        if result.outcome is Outcome.FAILED:
            sys.stderr.write(
                f"::warning::{result.name} fetch failed "
                f"({result.error_kind}): {result.error_detail}\n"
            )

    return exit_code(plan)


if __name__ == "__main__":
    raise SystemExit(main())
