"""In-memory, per-IP sliding-window rate limiting.

Counters are per-process: with multiple workers or replicas the effective
limit becomes ``limit * workers``. Fine for the single-process deployment
target; revisit (e.g. Redis) before scaling out.
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import HTTPException, Request, status

from app.core.config import get_settings


class InMemoryRateLimiter:
    """Per-key sliding-window counter. Safe under asyncio because
    :meth:`check` never awaits mid-update (not safe across OS threads)."""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        """Record a hit for ``key``; raise 429 once it exceeds the limit."""
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
    """FastAPI dependency enforcing ``limiter`` keyed by client IP."""

    def _dependency(request: Request) -> None:
        client_host = request.client.host if request.client else "unknown"
        limiter.check(client_host)

    return _dependency


_settings = get_settings()

login_rate_limiter = InMemoryRateLimiter(limit=_settings.login_rate_limit_per_minute, window_seconds=60.0)
"""Admin login — limits password guessing."""

agent_auth_rate_limiter = InMemoryRateLimiter(
    limit=_settings.agent_auth_rate_limit_per_minute, window_seconds=60.0
)
"""Every agent-API-key request — limits key guessing."""

checkout_rate_limiter = InMemoryRateLimiter(limit=_settings.checkout_rate_limit_per_minute, window_seconds=60.0)
"""Public checkout and demo-payment actions — abuse/load protection. Stock
correctness is guarded separately by row locks."""

scan_rate_limiter = InMemoryRateLimiter(limit=_settings.scan_rate_limit_per_minute, window_seconds=60.0)
"""Ticket scanning. Set generously: a venue's scanners often share one NATed IP."""

mollie_webhook_rate_limiter = InMemoryRateLimiter(
    limit=_settings.mollie_webhook_rate_limit_per_minute, window_seconds=60.0
)
"""Mollie webhook. Set generously so Mollie's own retries are never dropped."""
