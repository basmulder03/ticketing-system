"""Unit tests for ``InMemoryRateLimiter`` itself (not wired through a route).

Route-level 429 behavior (via ``/api/v1/auth/login`` and the agent-key auth
path) is covered as an integration test in
``tests/integration/test_rate_limit_routes.py``, since that's what actually
proves the limiter is wired onto those endpoints. This file covers the
sliding-window logic in isolation.
"""

import time

import pytest
from fastapi import HTTPException

from app.core.rate_limit import InMemoryRateLimiter


def test_allows_requests_up_to_the_limit() -> None:
    limiter = InMemoryRateLimiter(limit=3, window_seconds=60.0)
    for _ in range(3):
        limiter.check("client-a")  # must not raise


def test_raises_429_once_the_limit_is_exceeded() -> None:
    limiter = InMemoryRateLimiter(limit=3, window_seconds=60.0)
    for _ in range(3):
        limiter.check("client-a")
    with pytest.raises(HTTPException) as exc_info:
        limiter.check("client-a")
    assert exc_info.value.status_code == 429


def test_different_keys_have_independent_buckets() -> None:
    limiter = InMemoryRateLimiter(limit=1, window_seconds=60.0)
    limiter.check("client-a")  # exhausts client-a's single allowed request
    limiter.check("client-b")  # must not raise: independent bucket


def test_window_expiry_allows_requests_again_after_it_elapses() -> None:
    limiter = InMemoryRateLimiter(limit=1, window_seconds=0.2)
    limiter.check("client-a")
    with pytest.raises(HTTPException):
        limiter.check("client-a")
    time.sleep(0.25)
    limiter.check("client-a")  # window has slid past the first hit; must not raise
