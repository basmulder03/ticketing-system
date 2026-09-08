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

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from playwright.async_api import Browser, BrowserContext, Page, Request, Route, async_playwright
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
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

    Defaults point at the local Mailpit SMTP sink, resolved via
    ``Settings.seed_smtp_host`` (same convention as ``scripts/seed.py`` and
    ``mailpit_api_base_url`` below) rather than a hardcoded ``"mailpit"``
    hostname — that hostname only resolves inside the docker-compose
    network; running the suite natively (a local venv, or CI's bare-Ubuntu
    runner) needs ``SEED_SMTP_HOST=localhost`` instead, and a hardcoded
    default silently sent every test's mail into the void with no error
    (the SMTP connection just failed) rather than a clear failure. Tests
    that need a *working* SMTP config (e.g. the connection-test action, or
    a real send landing in Mailpit) get one for free without repeating the
    wiring at every call site.
    """

    async def _make(
        *,
        event_id: uuid.UUID,
        smtp_host: str | None = get_settings().seed_smtp_host,
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


# --- Accessibility (axe-core via Playwright) --------------------------------
#
# Per PROJECT_BRIEF.md's Testing section: "Accessibility tests: automated AA
# checks (e.g. axe-core) run against public pages ... as part of the test
# suite, not only as a manual pre-launch audit." These fixtures give
# ``tests/integration/test_public_site_accessibility.py`` a real headless
# Chromium page wired directly to this process's real ASGI app, so axe-core
# audits the actual rendered HTML/CSS/JS a buyer's browser would receive —
# not a static HTML fixture reconstructed by hand.

_AXE_JS_PATH = Path(__file__).parent / "vendor" / "axe.min.js"
_TEST_ORIGIN = "http://a11y-test.local"


@pytest_asyncio.fixture
async def _browser() -> AsyncGenerator[Browser, None]:
    """A headless Chromium instance for one test.

    Deliberately function-scoped, not session-scoped: pytest-asyncio (in
    this project's default configuration — see ``pyproject.toml``'s
    ``asyncio_mode = "auto"``) gives each test function its own event loop
    (see ``_dispose_db_engine_between_tests`` above), and a Playwright
    ``Browser``'s async connection is bound to the event loop it was
    created on — reusing one across tests/loops hangs. Launching Chromium
    per test costs a few hundred ms, which is fine for this suite's size.
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        yield browser
        await browser.close()


async def apply_set_cookie_headers(context: BrowserContext, raw_set_cookie_values: list[str]) -> None:
    """Parse one or more raw ``Set-Cookie`` header values (per
    ``axe_page``'s docstring on why these are applied out-of-band rather
    than via ``route.fulfill``'s headers dict) and add each as a real
    cookie on the Playwright browser context, so subsequent requests from
    the page send them exactly like a real browser would.

    Also used directly by ``test_public_site_accessibility.py`` for the
    order-confirmation page: that test performs checkout via a plain
    ``httpx`` client (not a real form click) specifically to sidestep a
    known Chromium/CDP limitation where ``route.fulfill()`` with a 3xx
    status doesn't route the browser's automatic redirect-follow request
    back through page/context interception (it escapes to real DNS
    resolution instead, which fails for this test's fake ``a11y-test.local``
    origin) — see that test's docstring for the full explanation.
    """
    for raw_value in raw_set_cookie_values:
        parsed = SimpleCookie()
        parsed.load(raw_value)
        for name, morsel in parsed.items():
            path = morsel["path"] or "/"
            same_site_raw = (morsel["samesite"] or "Lax").capitalize()
            same_site: Literal["Strict", "Lax", "None"] = (
                same_site_raw if same_site_raw in ("Strict", "Lax", "None") else "Lax"  # type: ignore[assignment]
            )
            await context.add_cookies(
                [
                    {
                        "name": name,
                        "value": morsel.value,
                        "url": f"{_TEST_ORIGIN}{path}",
                        "httpOnly": bool(morsel["httponly"]),
                        "secure": bool(morsel["secure"]),
                        "sameSite": same_site,
                    }
                ]
            )


@pytest_asyncio.fixture
async def axe_page(_browser: Browser) -> AsyncGenerator[Page, None]:
    """A Playwright ``Page`` wired to ``app.main.app`` via request
    interception — every navigation/asset/form request the page makes is
    routed straight through the same in-process ASGI app the rest of this
    suite exercises (``httpx.ASGITransport``, mirroring ``_make_client``
    above), rather than requiring a real listening HTTP server. axe-core is
    injected as a page-init script, so ``window.axe`` is available on every
    navigation without re-injecting it per test.

    Uses its own random pseudo-IP as the ASGI "client" (see
    ``_make_client``'s docstring) so this page's requests never share a
    rate-limit bucket with another test's.
    """
    host = f"a11y-{uuid.uuid4().hex[:12]}"
    transport = ASGITransport(app=app, client=(host, 12345))

    async def _handle_route(route: Route, request: Request) -> None:
        parsed = urlsplit(request.url)
        path_and_query = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        forwarded_headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in ("host", "content-length")
        }
        body = request.post_data_buffer
        async with AsyncClient(transport=transport, base_url=_TEST_ORIGIN) as client:
            response = await client.request(
                request.method, path_and_query, headers=forwarded_headers, content=body
            )
        # This app can set more than one cookie on a single response (e.g.
        # the checkout redirect sets both the order-confirmation cookie and
        # the locale cookie — see app.web.routes.public_site.submit_checkout).
        # Playwright's ``route.fulfill`` only accepts a flat
        # ``Dict[str, str]`` of headers (one value per name), so a naive
        # dict from ``response.headers.items()`` would silently drop every
        # Set-Cookie but the last. Every real Set-Cookie is applied directly
        # to this test's browser context instead (``context.add_cookies``,
        # via ``apply_set_cookie_headers`` below) and stripped from the
        # fulfilled response headers, so multi-cookie responses behave
        # exactly as a real browser handling them would.
        set_cookie_values = response.headers.get_list("set-cookie")
        response_headers: dict[str, str] = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in ("content-encoding", "content-length", "transfer-encoding", "set-cookie")
        }
        await route.fulfill(status=response.status_code, headers=response_headers, body=response.content)
        if set_cookie_values:
            await apply_set_cookie_headers(context, set_cookie_values)

    context = await _browser.new_context(base_url=_TEST_ORIGIN)
    await context.route("**/*", _handle_route)
    await context.add_init_script(path=str(_AXE_JS_PATH))
    page = await context.new_page()

    yield page

    await context.close()


async def run_axe(
    page: Page, path: str, *, disabled_rules: list[str] | None = None
) -> list[dict[str, Any]]:
    """Navigate ``axe_page`` to ``path`` and run axe-core against the fully
    rendered page, scoped to the WCAG 2.1 A/AA rule tags (matching this
    milestone's "must meet WCAG 2.1 AA" requirement — not the stricter AAA
    tags axe also knows about). Returns the raw ``violations`` array.

    ``disabled_rules``: rule ids to turn off for this run. Used to exclude
    ``color-contrast`` on the themed landing page only — a Theme's fixed
    colors (and anything under ``.event-content`` that inherits them, incl.
    the buy-flow buttons/labels) already get an automated AA contrast report
    from Milestone 1.5 (``app.services.contrast``); re-flagging arbitrary
    per-event theme color choices here would duplicate that check and
    produce false "failures" this suite can't fix (the colors are runtime
    event data, not something this template/CSS controls). Every other page
    (order confirmation, unavailable, 404) has no Theme involved at all, so
    color-contrast stays enabled there — see
    ``test_public_site_accessibility.py``.
    """
    response = await page.goto(path)
    # Deliberately not asserting response.ok here: several of the pages
    # this suite audits (order-confirmation-unavailable, 404) are SUPPOSED
    # to return a non-2xx status — what matters for an a11y audit is that
    # the browser actually received and rendered *some* real response body
    # from the app (not a network-level failure), not its status code.
    assert response is not None, f"navigation to {path} got no response at all"

    rules_option = {rule: {"enabled": False} for rule in (disabled_rules or [])}
    result = await page.evaluate(
        """
        async (rulesOption) => {
          return await axe.run(document, {
            runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] },
            rules: rulesOption,
          });
        }
        """,
        rules_option,
    )
    violations: list[dict[str, Any]] = result["violations"]
    return violations


# --- Mailpit HTTP API (real end-to-end email content assertions) -----------
#
# Milestone 4 ("Ticket generation & delivery") sends real emails via
# aiosmtplib against the local Mailpit SMTP sink (see docker-compose.yml /
# .github/workflows/ci.yml). Most send-path tests in
# tests/integration/test_ticket_delivery.py monkeypatch aiosmtplib.send
# directly (fast, precise control over success/failure/call-count), but
# PROJECT_BRIEF.md's Testing section calls for verifying "email content
# generation checked against actual rendered output... rendered against
# Mailpit or captured output, not just 'did send() get called'" — so at
# least one flow performs a real send and asserts on what actually landed
# in Mailpit, via Mailpit's HTTP API (not just trusting the SMTP send
# succeeded).


def mailpit_api_base_url() -> str:
    """Base URL for Mailpit's HTTP API, derived from the same
    ``Settings.seed_smtp_host`` local/CI SMTP-sink hostname every
    ``EventConfig``-touching test already relies on (see
    ``make_event_config``'s docstring) — Mailpit's web/API port is a fixed
    8025 alongside its SMTP port 1025, both exposed on that same host in
    ``docker-compose.yml`` (local dev) and ``.github/workflows/ci.yml``
    (CI)."""
    settings = get_settings()
    return f"http://{settings.seed_smtp_host}:8025"


async def fetch_latest_mailpit_message_to(to_email: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Poll Mailpit's search API for the most recent message addressed to
    ``to_email`` and return its FULL content (subject, HTML, plain text,
    attachment metadata) via a second ``GET .../message/{id}`` call.

    Searching by a test-unique ``to_email`` (rather than "the single most
    recent message in the whole mailbox") keeps this safe to use even
    though Mailpit's mailbox is a shared, un-isolated sink across this
    entire serial test run (see ``docker-compose.yml``'s single ``mailpit``
    service) — every caller should pass a randomized recipient address so
    two tests' messages can never be confused for one another. Polls
    briefly (Mailpit's HTTP API can lag a few milliseconds behind a
    just-completed SMTP send) rather than assuming the message is visible
    immediately.

    Raises ``AssertionError`` if no matching message shows up within
    ``timeout`` seconds.
    """
    base_url = mailpit_api_base_url()
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    async with AsyncClient(base_url=base_url, timeout=5.0) as api_client:
        while True:
            response = await api_client.get("/api/v1/messages", params={"query": f"to:{to_email}"})
            response.raise_for_status()
            messages = response.json()["messages"]
            if messages:
                message_id = messages[0]["ID"]
                detail_response = await api_client.get(f"/api/v1/message/{message_id}")
                detail_response.raise_for_status()
                result: dict[str, Any] = detail_response.json()
                return result
            if loop.time() >= deadline:
                raise AssertionError(f"No Mailpit message to {to_email!r} appeared within {timeout}s")
            await asyncio.sleep(0.1)


async def fetch_mailpit_attachment(message_id: str, part_id: str) -> bytes:
    """Download one attachment's raw bytes from Mailpit by message/part id
    (see ``fetch_latest_mailpit_message_to``'s ``Attachments`` list, each
    entry's ``PartID``) — used to verify a real PDF attachment landed
    (magic-byte check), not just that ``Attachments`` metadata claims one
    exists."""
    base_url = mailpit_api_base_url()
    async with AsyncClient(base_url=base_url, timeout=5.0) as api_client:
        response = await api_client.get(f"/api/v1/message/{message_id}/part/{part_id}")
        response.raise_for_status()
        return response.content


def format_axe_violations(violations: list[dict[str, Any]]) -> str:
    """Render axe-core violations into a readable failure message: rule id,
    human-readable description, WCAG tags, and every affected node's CSS
    selector + a short HTML snippet — enough to locate/fix the issue without
    re-running the test interactively."""
    lines = []
    for violation in violations:
        lines.append(f"[{violation['id']}] ({violation['impact']}) {violation['help']} — {violation['helpUrl']}")
        for node in violation["nodes"]:
            selector = " ".join(node.get("target", []))
            snippet = (node.get("html") or "")[:200]
            lines.append(f"    at {selector}: {snippet}")
    return "\n".join(lines)
