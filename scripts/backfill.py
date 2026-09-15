#!/usr/bin/env python3
"""Pull whatever incident history the providers already expose.

Run once at setup::

    python scripts/backfill.py

Writes to ``history/_backfill/{provider}.json``, deliberately kept in a separate
namespace from live-collected data and marked with a provenance banner.

The separation is not bookkeeping fussiness. Backfilled incidents are **post-hoc
provider narratives**: written after the fact, with timestamps the provider chose
in hindsight, and routinely edited weeks later. Our own snapshots under
``providers/`` and ``history/`` are **contemporaneous observations** — what the
status page actually said at a moment we recorded. Silently merging the two would
destroy the only property that makes this archive worth keeping, which is that we
can tell the difference.

Note on coverage: Statuspage's ``/api/v2/incidents.json`` returns a deep history
for both OpenAI and Anthropic, but Google's ``incidents.json`` is a rolling
window holding only a handful of significant platform incidents. Google backfill
is therefore thin, and that is upstream's limit, not a bug here.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from aistatus import USER_AGENT  # noqa: E402
from aistatus.collect import load_providers  # noqa: E402
from aistatus.fetch import UrllibFetcher, utcnow_z  # noqa: E402
from aistatus.models import FetchError, ProviderConfig  # noqa: E402
from aistatus.serialize import pretty_bytes  # noqa: E402

OUTPUT_DIR = "history/_backfill"

#: Where each platform's historical incidents live.
HISTORY_ENDPOINTS = {
    "statuspage": "/api/v2/incidents.json",
    "google_cloud": None,  # The primary feed already is the history.
}


def history_url(cfg: ProviderConfig) -> str:
    """Determine the endpoint holding a provider's incident history.

    Args:
        cfg: Provider configuration.

    Returns:
        An absolute URL.
    """
    suffix = HISTORY_ENDPOINTS.get(cfg.platform)
    if suffix is None:
        return cfg.url
    base = cfg.url.split("/api/v2/")[0]
    return base + suffix


def banner(cfg: ProviderConfig, url: str, count: int) -> dict[str, Any]:
    """Build the provenance block stamped onto every backfill file."""
    return {
        "kind": "backfill",
        "warning": (
            "POST-HOC PROVIDER NARRATIVE. These incidents were published after "
            "the fact, carry timestamps the provider chose in hindsight, and may "
            "have been edited since. They are NOT contemporaneous observations "
            "and must never be merged with providers/ or history/ data."
        ),
        "provider": cfg.name,
        "provider_name": cfg.display_name,
        "source_platform": cfg.platform,
        "source_url": url,
        "retrieved_at": utcnow_z(),
        "incident_count": count,
    }


def extract(payload: Any, platform: str) -> list[Any]:
    """Pull the incident list out of a platform-specific payload."""
    if platform == "google_cloud":
        return payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        incidents = payload.get("incidents")
        return incidents if isinstance(incidents, list) else []
    return []


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(
        prog="backfill.py",
        description="Fetch provider-published incident history into a separate namespace.",
    )
    parser.add_argument(
        "--config", default=str(REPO_ROOT / "config" / "providers.yaml")
    )
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument(
        "--provider", action="append", default=None, help="Repeatable filter."
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo_root).resolve()
    configs = load_providers(Path(args.config).read_text(encoding="utf-8"))
    if args.provider:
        wanted = set(args.provider)
        configs = tuple(c for c in configs if c.name in wanted)

    out_dir = repo / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    fetcher = UrllibFetcher()
    failures = 0

    for index, cfg in enumerate(configs):
        if index:
            time.sleep(1.5)
        url = history_url(cfg)
        try:
            response = fetcher(url)
            payload = json.loads((response.body or b"").decode("utf-8"))
        except (FetchError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            sys.stderr.write(f"{cfg.name}: backfill failed: {exc}\n")
            failures += 1
            continue

        incidents = extract(payload, cfg.platform)
        document = {
            "_meta": banner(cfg, response.url or url, len(incidents)),
            "incidents": incidents,
        }
        target = out_dir / f"{cfg.name}.json"
        target.write_bytes(pretty_bytes(document))
        sys.stdout.write(f"{cfg.name}: {len(incidents)} incident(s) -> {OUTPUT_DIR}/{cfg.name}.json\n")

    if failures:
        sys.stderr.write(f"{failures} provider(s) failed\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
