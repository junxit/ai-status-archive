"""HTTP with timeouts, bounded retries, and conditional requests.

Standard library only. Keeping the dependency surface at ``urllib`` plus PyYAML
means fast Actions runs and a trivially packageable Lambda later.

This module also defines the three ports that :mod:`aistatus.collect` depends
on. Collect receives them by injection rather than importing concrete
implementations, which is what keeps it free of ambient state and lets the test
suite drive the whole pipeline without mocking internals.

Observed provider behavior this module is built around:

* Anthropic sends a real ``ETag`` and honors ``If-None-Match`` with a genuine
  ``304``.
* OpenAI sends no validator at all, so conditional requests are impossible.
* Google advertises ``Last-Modified`` but ignores ``If-Modified-Since`` and
  returns a full ``200`` regardless.

So the 304 path is real but rare, and nothing may depend on getting one.
"""

from __future__ import annotations

import datetime as _dt
import email.utils
import random
import time
import urllib.error
import urllib.request
from typing import Protocol

from . import USER_AGENT
from .models import FetchError, RawResponse

#: Connect and read timeout, in seconds.
DEFAULT_TIMEOUT = 10.0

#: Maximum retry attempts after the initial request.
DEFAULT_RETRIES = 2

#: Base for exponential backoff, in seconds.
BACKOFF_BASE = 1.5

#: Never wait longer than this between attempts, even if asked to by
#: ``Retry-After``. A status page telling us to back off for an hour must not
#: hold a five-minute poll job open until the workflow times out.
MAX_BACKOFF = 20.0


class Clock(Protocol):
    """Supplies the current time as an RFC 3339 UTC string."""

    def __call__(self) -> str:  # pragma: no cover - protocol definition
        ...


class Sleeper(Protocol):
    """Blocks for a number of seconds."""

    def __call__(self, seconds: float) -> None:  # pragma: no cover
        ...


class Fetcher(Protocol):
    """Performs one HTTP GET and returns a completed response."""

    def __call__(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> RawResponse:  # pragma: no cover - protocol definition
        ...


def utcnow_z() -> str:
    """Return the current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``.

    This is the real clock, injected into pure code rather than called from it.

    Returns:
        The current UTC instant, truncated to whole seconds.
    """
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_retry_after(value: str | None) -> float | None:
    """Interpret a ``Retry-After`` header, which may be seconds or an HTTP date.

    Args:
        value: Raw header value, or ``None``.

    Returns:
        Seconds to wait, clamped to :data:`MAX_BACKOFF`, or ``None`` if the
        header was absent or unparseable.
    """
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return min(float(text), MAX_BACKOFF)
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.UTC)
    delta = (when - _dt.datetime.now(_dt.UTC)).total_seconds()
    return min(max(delta, 0.0), MAX_BACKOFF) if delta > 0 else None


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Compute how long to wait before the next attempt.

    Honors ``Retry-After`` when the server supplied one, otherwise uses
    exponential backoff with full jitter. Jitter matters because three providers
    are polled on the same schedule from the same runner; synchronized retries
    would arrive as a small burst.

    Args:
        attempt: Zero-based index of the attempt that just failed.
        retry_after: Server-requested delay, if any.

    Returns:
        Seconds to sleep.
    """
    if retry_after is not None:
        return retry_after
    ceiling = min(BACKOFF_BASE * (2**attempt), MAX_BACKOFF)
    return random.uniform(0.0, ceiling)


def _headers_lower(raw: object) -> dict[str, str]:
    """Lowercase response header keys so lookups are case-insensitive."""
    items = getattr(raw, "items", None)
    if items is None:
        return {}
    return {str(k).lower(): str(v) for k, v in items()}


class UrllibFetcher:
    """The real :class:`Fetcher`, built on :mod:`urllib.request`.

    Attributes:
        timeout: Connect and read timeout in seconds.
        retries: Maximum retries after the initial attempt.
        sleep: Injected sleep function, so tests need not actually wait.
        user_agent: Sent on every request. Never the default Python agent, which
            the CDN in front of Statuspage will sometimes reject outright.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        sleep: Sleeper = time.sleep,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep
        self.user_agent = user_agent

    def __call__(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> RawResponse:
        """Fetch ``url``, retrying transient failures.

        Args:
            url: Absolute URL to GET. Redirects are followed, and the effective
                final URL is recorded on the response.
            etag: Prior ``ETag`` to send as ``If-None-Match``.
            last_modified: Prior ``Last-Modified`` to send as
                ``If-Modified-Since``.

        Returns:
            A :class:`~aistatus.models.RawResponse` with status ``200`` and a
            body, or status ``304`` and a ``None`` body.

        Raises:
            FetchError: On timeout, connection failure, 4xx, or 5xx after all
                retries are exhausted. Callers must treat this as a fact about
                us, not about the provider.
        """
        last_error: FetchError | None = None

        for attempt in range(self.retries + 1):
            request = urllib.request.Request(url, method="GET")
            request.add_header("User-Agent", self.user_agent)
            request.add_header("Accept", "application/json")
            request.add_header("Accept-Encoding", "identity")
            if etag:
                request.add_header("If-None-Match", etag)
            if last_modified:
                request.add_header("If-Modified-Since", last_modified)

            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    return RawResponse(
                        status_code=response.status,
                        body=body,
                        headers=_headers_lower(response.headers),
                        url=response.geturl(),
                        elapsed_ms=elapsed_ms,
                    )
            except urllib.error.HTTPError as exc:
                elapsed_ms = int((time.monotonic() - started) * 1000)

                # urlopen treats any non-2xx as an error, including 304. A 304 is
                # a successful outcome meaning "nothing changed", so intercept it
                # before the retry logic sees it as a failure.
                if exc.code == 304:
                    return RawResponse(
                        status_code=304,
                        body=None,
                        headers=_headers_lower(exc.headers),
                        url=url,
                        elapsed_ms=elapsed_ms,
                    )

                retry_after = _parse_retry_after(
                    exc.headers.get("Retry-After") if exc.headers else None
                )
                kind = "http_5xx" if exc.code >= 500 else f"http_{exc.code}"
                last_error = FetchError(kind, f"{exc.code} {exc.reason} for {url}")

                # 4xx other than 429 means the request itself is wrong. Retrying
                # an identical request cannot fix that, so fail immediately
                # rather than hammering a provider with a request they rejected.
                if exc.code < 500 and exc.code != 429:
                    raise last_error from exc
            except TimeoutError:
                last_error = FetchError("timeout", f"after {self.timeout}s for {url}")
                retry_after = None
            except urllib.error.URLError as exc:
                # A socket timeout usually arrives wrapped in URLError rather
                # than as a bare TimeoutError, so unwrap it instead of filing
                # every slow provider under "connection".
                if isinstance(exc.reason, TimeoutError):
                    last_error = FetchError(
                        "timeout", f"after {self.timeout}s for {url}"
                    )
                else:
                    last_error = FetchError("connection", f"{exc.reason} for {url}")
                retry_after = None
            except OSError as exc:  # pragma: no cover - defensive
                last_error = FetchError("os_error", f"{exc} for {url}")
                retry_after = None

            if attempt < self.retries:
                self.sleep(_backoff_delay(attempt, retry_after))

        raise last_error or FetchError("unknown", f"no response for {url}")


__all__ = [
    "BACKOFF_BASE",
    "DEFAULT_RETRIES",
    "DEFAULT_TIMEOUT",
    "MAX_BACKOFF",
    "Clock",
    "Fetcher",
    "Sleeper",
    "UrllibFetcher",
    "utcnow_z",
]
