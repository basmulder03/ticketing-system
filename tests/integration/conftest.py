"""Shared fixtures for the integration test suite (``tests/integration/``).

Integration tests hit a real Postgres instance — the same ``db`` service
docker-compose starts for local dev/CI, per PROJECT_BRIEF.md's Testing
section ("integration tests: full request/response flows against a real
(test) database"). There's no separate per-test schema/transaction
sandbox: fixtures create rows through the same async session machinery the
app itself uses (``app.db.session.async_session_factory``) and clean up
the Milestone-0 and Milestone-1 tables after each test that touches them,
so tests stay independent regardless of execution order.

Unit tests (``tests/unit/``) don't use any of these fixtures.
"""

import datetime as dt
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

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
from app.models.enums import AdminRole, PaymentMethod, PublishStatus, SmtpEncryptionMode, ThemeFont
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.theme import Theme
from app.models.ticket_type import TicketType

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

    Deletes every row of the Milestone-0 and Milestone-1 tables after the
    test, so tests stay independent without relying on a rollback boundary
    the app's own request-scoped sessions (created fresh per request via
    ``get_session``) don't participate in anyway. Deleting ``Event`` rows is
    enough to also clear ``EventConfig``/``Show``/``TicketType``: all three
    have a DB-level ``ON DELETE CASCADE`` foreign key back to ``events``
    (see ``alembic/versions/0003_event_config_show_tickettype.py``), so a
    plain ``DELETE FROM events`` cascades without needing separate deletes
    per table.
    """
    async with async_session_factory() as session:
        yield session
    async with async_session_factory() as session:
        await session.execute(delete(AuditLogEntry))
        await session.execute(delete(Event))
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


@pytest_asyncio.fixture
async def make_event(db_session: AsyncSession) -> Callable[..., Awaitable[Event]]:
    """Factory fixture: insert an ``Event`` row directly (bypassing the create
    route), returning it. Mirrors ``make_admin_user``/``make_agent_account``'s
    "insert directly" convention so fixtures for other models' tests stay
    independent of the Event create route's own behavior (tested separately
    in ``test_events_routes.py``).
    """

    async def _make(
        *,
        name: str = "Test Event",
        slug: str | None = None,
        description: str | None = None,
        status: PublishStatus = PublishStatus.DRAFT,
        sales_paused: bool = False,
    ) -> Event:
        event = Event(
            name=name,
            slug=slug or f"test-event-{uuid.uuid4().hex[:12]}",
            description=description,
            status=status,
            sales_paused=sales_paused,
        )
        db_session.add(event)
        await db_session.commit()
        await db_session.refresh(event)
        return event

    return _make


@pytest_asyncio.fixture
async def make_show(db_session: AsyncSession) -> Callable[..., Awaitable[Show]]:
    """Factory fixture: insert a ``Show`` row directly under a given Event id."""

    async def _make(
        *,
        event_id: uuid.UUID,
        date: dt.date | None = None,
        doors_time: dt.time = dt.time(19, 30),
        start_time: dt.time = dt.time(20, 0),
        venue_name: str = "Test Venue",
        venue_address: str = "1 Test Street, Test City",
        capacity: int = 100,
        status: PublishStatus = PublishStatus.DRAFT,
    ) -> Show:
        show = Show(
            event_id=event_id,
            date=date or (dt.datetime.now(tz=UTC).date() + dt.timedelta(days=30)),
            doors_time=doors_time,
            start_time=start_time,
            venue_name=venue_name,
            venue_address=venue_address,
            capacity=capacity,
            status=status,
        )
        db_session.add(show)
        await db_session.commit()
        await db_session.refresh(show)
        return show

    return _make


@pytest_asyncio.fixture
async def make_ticket_type(db_session: AsyncSession) -> Callable[..., Awaitable[TicketType]]:
    """Factory fixture: insert a ``TicketType`` row directly under a given Show id."""

    async def _make(
        *,
        show_id: uuid.UUID,
        name: str = "Adult",
        price: Decimal = Decimal("15.00"),
        service_fee_included: bool = True,
        quantity_available: int = 100,
    ) -> TicketType:
        ticket_type = TicketType(
            show_id=show_id,
            name=name,
            price=price,
            service_fee_included=service_fee_included,
            quantity_available=quantity_available,
        )
        db_session.add(ticket_type)
        await db_session.commit()
        await db_session.refresh(ticket_type)
        return ticket_type

    return _make


@pytest_asyncio.fixture
async def make_event_config(db_session: AsyncSession) -> Callable[..., Awaitable[EventConfig]]:
    """Factory fixture: insert an ``EventConfig`` row directly for a given Event id.

    Defaults point at the local Mailpit SMTP sink (same convention as
    ``scripts/seed.py``), so tests that need a *working* SMTP config (e.g.
    the connection-test action against Mailpit) get one for free without
    repeating the wiring at every call site.
    """

    async def _make(
        *,
        event_id: uuid.UUID,
        smtp_host: str | None = "mailpit",
        smtp_port: int | None = 1025,
        smtp_encryption: SmtpEncryptionMode = SmtpEncryptionMode.NONE,
        smtp_username: str | None = None,
        smtp_password: str | None = None,
        sender_name: str | None = "Test Event",
        sender_email: str | None = "sender@example.test",
        mollie_test_api_key: str | None = None,
        mollie_live_api_key: str | None = None,
        invoice_company_name: str | None = "Test Company",
        invoice_company_address: str | None = "1 Test Street",
        invoice_company_vat_number: str | None = "NL000000000B01",
        invoice_number_prefix: str | None = "TEST-",
        sales_live_at: datetime | None = None,
        enabled_payment_methods: list[PaymentMethod] | None = None,
    ) -> EventConfig:
        config = EventConfig(
            event_id=event_id,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_encryption=smtp_encryption,
            smtp_username=smtp_username,
            smtp_password=smtp_password,
            sender_name=sender_name,
            sender_email=sender_email,
            mollie_test_api_key=mollie_test_api_key,
            mollie_live_api_key=mollie_live_api_key,
            invoice_company_name=invoice_company_name,
            invoice_company_address=invoice_company_address,
            invoice_company_vat_number=invoice_company_vat_number,
            invoice_number_prefix=invoice_number_prefix,
            sales_live_at=sales_live_at,
            enabled_payment_methods=enabled_payment_methods if enabled_payment_methods is not None else [],
        )
        db_session.add(config)
        await db_session.commit()
        await db_session.refresh(config)
        return config

    return _make


@pytest_asyncio.fixture
async def make_theme(db_session: AsyncSession) -> Callable[..., Awaitable[Theme]]:
    """Factory fixture: insert a ``Theme`` row directly for a given Event id
    (bypassing ``PUT /api/v1/events/{event_id}/theme``), mirroring
    ``make_event_config``'s "insert directly" convention. ``custom_css`` is
    stored as given (NOT re-sanitized here) — tests that need "the real
    save path sanitizes on write" should go through the route instead.
    """

    async def _make(
        *,
        event_id: uuid.UUID,
        primary_color: str = "#1a1a1a",
        secondary_color: str = "#ffffff",
        accent_color: str = "#c9a227",
        font_choice: ThemeFont = ThemeFont.SYSTEM_SANS,
        custom_css: str | None = None,
        logo_path: str | None = None,
        background_image_path: str | None = None,
        status: PublishStatus = PublishStatus.DRAFT,
    ) -> Theme:
        theme = Theme(
            event_id=event_id,
            primary_color=primary_color,
            secondary_color=secondary_color,
            accent_color=accent_color,
            font_choice=font_choice,
            custom_css=custom_css,
            logo_path=logo_path,
            background_image_path=background_image_path,
            status=status,
        )
        db_session.add(theme)
        await db_session.commit()
        await db_session.refresh(theme)
        return theme

    return _make
