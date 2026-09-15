"""Stub for a future higher-frequency AWS Lambda poller. Not implemented.

This file exists to keep the architectural seam honest and visible. It is not
wired to anything and should not be deployed.

Why the seam already works
--------------------------

``src/aistatus/collect.py`` is pure: it takes provider configuration plus three
injected ports — an HTTP client, a clock, and a sleep function — and returns
snapshots. It reads no files, shells out to no git, consults no environment
variables, and writes no logs. ``tests/test_purity.py`` enforces that by walking
the module's AST, so the property cannot quietly rot.

That means a Lambda implementation reuses the collection and decision layers
verbatim. The only thing that changes is where bytes land::

    from aistatus.collect import collect_all, load_providers
    from aistatus.fetch import UrllibFetcher, utcnow_z
    from aistatus.plan import RunContext, plan_run

Sketch of the write path
------------------------

The plan layer already returns ``RunPlan.writes`` as a list of ``WriteOp``
records whose ``path`` is a plain repo-relative string rather than a
``pathlib.Path``. That was deliberate: the same plan can be executed against
object storage without touching any pure code.

1. **Read committed state.** Where the GitHub runner uses ``git show HEAD:...``,
   Lambda would read the current snapshot per provider from a DynamoDB table
   keyed by ``provider``, holding the normalized document and its content hash.

2. **Collect.** Identical call to ``collect_all``. Stagger via ``time.sleep``.

3. **Plan.** Identical call to ``plan_run``, with ``RunContext.previous_docs``
   populated from DynamoDB instead of git.

4. **Execute.** Translate each ``WriteOp``:

   * ``providers/{name}.json`` and ``raw/{name}.json`` — write a new immutable
     S3 object under a time-partitioned key such as
     ``raw/{provider}/{yyyy}/{mm}/{dd}/{fetched_at}.json``. S3 versioning plus an
     immutable key is what replaces git history as the append-only substrate.
   * ``history/{name}.jsonl`` — append each event as a DynamoDB item keyed
     ``(provider, at)``, or put to a Kinesis Firehose if fan-out is wanted.
   * ``state/last_poll.json`` — a single DynamoDB item; the hourly heartbeat rule
     carries over unchanged and still bounds write volume.

5. **Failures stay isolated.** The same rule must hold: a failed fetch never
   overwrites the last known good document. Because ``plan_run`` already refuses
   to emit a provider write without a snapshot, this is structural rather than
   something the Lambda runner has to remember.

Considerations before building this
-----------------------------------

* **Politeness.** Higher frequency means more load on somebody else's status
  page. Statuspage's public API is not rate limited, but a sub-minute poll is
  hard to justify against a page that updates on human timescales.
* **Reconciliation.** Two pollers writing the same logical archive will disagree
  at the edges. Decide up front whether Lambda feeds the git archive (via a
  batched commit job) or is a genuinely separate store.
* **Cost.** At one-minute polling, 3 providers is ~130k invocations per month —
  trivial on Lambda, but the S3 PUT and DynamoDB write volume is what to model.
* **Runtime.** Pin Python 3.12 to match ``.python-version``. The only third-party
  dependency is PyYAML, which fits in a small layer, or the config can be inlined
  as JSON to reach zero dependencies.
"""

from __future__ import annotations

from typing import Any


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Not implemented.

    Args:
        event: Lambda event payload.
        context: Lambda runtime context.

    Raises:
        NotImplementedError: Always. See the module docstring for the intended
            design and the reasons it has not been built yet.
    """
    raise NotImplementedError(
        "The Lambda poller is a documented stub. See this module's docstring for "
        "the intended S3 and DynamoDB write path."
    )
