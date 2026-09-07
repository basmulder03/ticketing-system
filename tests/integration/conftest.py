"""Shared fixtures for the integration test suite (``tests/integration/``).

Integration tests hit a real Postgres instance — the same ``db`` service
docker-compose starts for local dev/CI, per PROJECT_BRIEF.md's Testing
section ("integration tests: full request/response flows against a real
(test) database"). There's no separate per-test schema/transaction
sandbox: fixtures create rows through the same async session machinery the
app itself uses (``app.db.session.async_session_factory``) and clean up
the Milestone-0 tables after each test that touches them, so tests stay
independent regardless of execution order.

Unit tests (``tests/unit/``) don't use any of these fixtures.
"""

import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_agent_api_key, hash_password
from app.db.session import async_session_factory, engine
from app.main import app
from app.models.admin_user import AdminUser
from app.models.agent_account import AgentAccount
from app.models.audit_log import AuditLogEntry
from app.models.enums import AdminRole

DEFAULT_TEST_PASSWORD = "correct-horse-battery-staple"


@dataclass(frozen=True)
class SeededAdmin:
    """An ``AdminUser`` row created for a test, plus its known plaintext password.

    The plaintext is never persisted anywhere except this fixture return
    value — tests need it to exercise ``POST /api/v1/auth/login``.
    """

    user: AdminUser
    password: str


@dataclass(frozen=True)
class SeededAgent:
    """An ``AgentAccount`` row created for a test, plus its known raw API key."""

    account: AgentAccount
    raw_key: str


@pytest_asyncio.fixture(autouse=True)
async def _dispose_db_engine_between_tests() -> AsyncGenerator[None, None]:
    """Dispose the shared async engine's connection pool after every test.

    pytest-asyncio gives each test function its own event loop by default,
    but ``app.db.session.engine`` (and its asyncpg connections) is a
    process-wide singleton created once at import time. A connection
    pooled during one test's loop is unusable once the next test's loop
    tries to reuse it ("cannot perform operation: another operation is in
    progress"). Disposing here runs at teardown, while the current test's
    loop is still open, so the pool starts empty for whichever loop the
    next test runs on.
    """
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """A DB session for test setup/assertions, against the app's real Postgres.

    Deletes every row of the Milestone-0 tables after the test, so tests
    stay independent without relying on a rollback boundary the app's own
    request-scoped sessions (created fresh per request via ``get_session``)
    don't participate in anyway.
    """
    async with async_session_factory() as session:
        yield session
    async with async_session_factory() as session:
        await session.execute(delete(AuditLogEntry))
        await session.execute(delete(AgentAccount))
        await session.execute(delete(AdminUser))
        await session.commit()


def _make_client(host: str | None = None) -> AsyncClient:
    """Build an ``AsyncClient`` wired directly to the ASGI app (no real socket).

    Uses ``httpx.AsyncClient`` + ``ASGITransport`` rather than
    ``starlette.testclient.TestClient`` (used by the Milestone-0 health
    smoke test): every route exercised here awaits a real asyncpg session
    on the same event loop pytest-asyncio is already running, and this
    httpx/starlette pairing prints a deprecation warning when TestClient's
    sync-over-async bridge is used instead.

    Each client gets its own random pseudo-IP by default (``request.client
    .host`` doesn't have to be a real routable address, just a stable key)
    so the per-IP rate limiter (``app.core.rate_limit``) never accidentally
    shares a bucket across unrelated tests. Pass an explicit ``host`` when a
    test specifically needs to compare behavior across two IPs or reuse one.
    """
    resolved_host = host or f"test-{uuid.uuid4().hex[:12]}"
    transport = ASGITransport(app=app, client=(resolved_host, 12345))
    return AsyncClient(transport=transport, base_url="http://test")


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """The default per-test HTTP client, with its own isolated pseudo-IP."""
    async with _make_client() as ac:
        yield ac


@pytest.fixture
def client_factory() -> Callable[[str | None], AsyncClient]:
    """Factory for extra clients with an explicit (or freshly random) pseudo-IP.

    Callers are responsible for closing/``async with``-ing clients built
    this way (the fixture doesn't track them, unlike the single-instance
    ``client`` fixture above).
    """
    return _make_client


@pytest_asyncio.fixture
async def make_admin_user(
    db_session: AsyncSession,
) -> Callable[..., Awaitable[SeededAdmin]]:
    """Factory fixture: insert an ``AdminUser`` row, returning it with its known password."""

    async def _make(
        *,
        email: str | None = None,
        password: str = DEFAULT_TEST_PASSWORD,
        role: AdminRole = AdminRole.ADMIN,
        is_active: bool = True,
    ) -> SeededAdmin:
        admin = AdminUser(
            email=(email or f"{uuid.uuid4().hex}@example.test").lower(),
            hashed_password=hash_password(password),
            role=role,
            is_active=is_active,
        )
        db_session.add(admin)
        await db_session.commit()
        await db_session.refresh(admin)
        return SeededAdmin(user=admin, password=password)

    return _make


@pytest_asyncio.fixture
async def make_agent_account(
    db_session: AsyncSession,
) -> Callable[..., Awaitable[SeededAgent]]:
    """Factory fixture: insert an ``AgentAccount`` row directly (bypassing the create
    route), returning it with its known raw API key.

    Inserting directly (rather than going through
    ``POST /api/v1/admin/agent-accounts``) keeps fixtures for deps/rate-limit
    tests independent of the create route's own behavior, which is tested
    separately in ``test_agent_accounts_routes.py``.
    """

    async def _make(*, name: str | None = None, is_active: bool = True) -> SeededAgent:
        raw_key, key_hash, key_prefix = generate_agent_api_key()
        agent = AgentAccount(
            name=name or f"agent-{uuid.uuid4().hex[:8]}",
            key_hash=key_hash,
            key_prefix=key_prefix,
        )
        if not is_active:
            agent.revoked_at = datetime.now(UTC)
        db_session.add(agent)
        await db_session.commit()
        await db_session.refresh(agent)
        return SeededAgent(account=agent, raw_key=raw_key)

    return _make
