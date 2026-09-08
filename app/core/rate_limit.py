"""A minimal in-memory rate limiter, appropriate for a single small VPS.

Deliberately not Redis-backed: PROJECT_BRIEF.md says to avoid adding
services unless a requirement actually needs one, and rate-limiting a
handful of auth/checkout/scan endpoints on a single-instance deployment
doesn't. This scaffold is wired onto the login and agent-API-key auth
paths now; checkout/scan endpoints reuse the same mechanism in later
milestones.

Known limitation: counters are per-process. If the app is ever run with
multiple uvicorn workers or replicas, each process enforces the limit
independently, so the effective global limit becomes ``limit * workers``.
Fine for the current single-small-VPS, single-process target; flagged as a
follow-up for ``devops-agent`` if/when multi-worker deployment happens.
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import HTTPException, Request, status

from app.core.config import get_settings


class InMemoryRateLimiter:
    """A per-key sliding-window request counter.

    Not thread-safe across real OS threads, but safe under asyncio's
    single-threaded event loop since :meth:`check` never awaits mid-update.
    """

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        """Record a hit for ``key``; raise HTTP 429 if it exceeds the limit within the window."""
        now = time.monotonic()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > self._window_seconds:
            bucket.popleft()
        if len(bucket) >= self._limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests, please try again later.",
            )
        bucket.append(now)


def rate_limit_dependency(limiter: InMemoryRateLimiter) -> Callable[[Request], None]:
    """Build a FastAPI dependency that enforces ``limiter`` keyed by client IP."""

    def _dependency(request: Request) -> None:
        client_host = request.client.host if request.client else "unknown"
        limiter.check(client_host)

    return _dependency


_settings = get_settings()

login_rate_limiter = InMemoryRateLimiter(limit=_settings.login_rate_limit_per_minute, window_seconds=60.0)
"""Applied to ``POST /api/v1/auth/login`` — limits password-guessing attempts per IP."""

agent_auth_rate_limiter = InMemoryRateLimiter(
    limit=_settings.agent_auth_rate_limit_per_minute, window_seconds=60.0
)
"""Applied to every agent-API-key-authenticated request — limits key-guessing attempts per IP."""

checkout_rate_limiter = InMemoryRateLimiter(limit=_settings.checkout_rate_limit_per_minute, window_seconds=60.0)
"""Applied to ``POST /api/v1/public/checkout`` (Milestone 2) — per
PROJECT_BRIEF.md's Security & Ops section ("rate limiting on checkout and
scan endpoints"), limits how many checkout attempts a single client IP can
make per rolling minute, independent of the row-locked stock check (which
guards correctness, not abuse/load)."""

scan_rate_limiter = InMemoryRateLimiter(limit=_settings.scan_rate_limit_per_minute, window_seconds=60.0)
"""Applied to ``POST /api/v1/shows/{show_id}/scan`` (Milestone 7) — per
PROJECT_BRIEF.md's Security & Ops section ("rate limiting on checkout and
scan endpoints"). Keyed by client IP like every other limiter here, which
means a whole venue's scanning devices sharing one NATed IP share one
budget — see ``Settings.scan_rate_limit_per_minute`` for why the limit is
set generously relative to ``checkout_rate_limiter``."""

mollie_webhook_rate_limiter = InMemoryRateLimiter(
    limit=_settings.mollie_webhook_rate_limit_per_minute, window_seconds=60.0
)
"""Applied to ``POST /api/v1/public/mollie-webhook`` (Milestone 3) — kept
deliberately generous (see ``Settings.mollie_webhook_rate_limit_per_minute``
for the reasoning): the endpoint's real caller is Mollie's own
infrastructure, not a buyer, so an aggressive per-IP limit here risks
dropping Mollie's legitimate retries rather than stopping abuse."""
