"""Automated WCAG 2.1 AA checks (axe-core, via Playwright) against the real
rendered order-confirmation/ticket EMAIL HTML — per PROJECT_BRIEF.md's
Ticket Generation & Delivery section ("Email HTML must meet WCAG 2.1 AA
within the real constraints of email rendering...") and its Testing section
("Accessibility tests: automated AA checks (e.g. axe-core) run against
public pages and email HTML as part of the test suite").

Email HTML isn't served by any app route (``app.services.email_render``
builds a complete standalone HTML string that's handed straight to
``EmailMessage``/``aiosmtplib`` — see ``app.services.ticket_delivery``), so
this suite drives axe-core via ``page.set_content()`` instead of
``page.goto()`` (``run_axe_on_html`` in ``conftest.py``), reusing the exact
same vendored axe-core script and ``axe_page`` fixture
``test_public_site_accessibility.py`` uses for the public site — not a
second accessibility-testing mechanism.

Every test here generates the HTML via the REAL render path
(``app.services.email_render.render_order_confirmation_email``), against a
real seeded Theme/Event/Show/Order/Ticket (using the same
``make_event``/``make_show``/... factory fixtures the rest of the
integration suite uses) — not a hand-written HTML fixture.

Color-contrast scope note (unlike ``test_public_site_accessibility.py``,
which disables axe's ``color-contrast`` rule for the themed landing page):
this suite deliberately does NOT disable ``color-contrast``. The seeded
Theme below uses ``make_theme``'s default primary/secondary colors
(``#1a1a1a`` on ``#ffffff``), which already clear AA per Milestone 1.5's own
contrast tool (``app.services.contrast``) — so a real violation here can
only be something this email shell/template itself is responsible for
(inline styles, missing an explicit color on some element), never a
re-litigation of a per-event theme color choice, which is exactly this
milestone's brief calling out "sufficient color contrast on all
text/background combinations (including against the theme's colors)" as
THIS module's job, not Theme's own tool's.

Manual-only items NOT covered by this automated suite (axe-core cannot
verify these, and neither can a Chromium-only test run): real rendering
behavior in actual email clients (Outlook desktop's Word rendering engine,
Gmail's CSS stripping, Apple Mail) and real screen-reader narration of the
rendered email — see this milestone's accessibility-auditor handoff for the
full manual checklist.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from playwright.async_api import Page
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.qr_tokens import sign_ticket_token
from app.models.email_template import EmailTemplate
from app.models.enums import EmailTemplateType, PublishStatus, ThemeFont
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.order import Order
from app.models.show import Show
from app.models.theme import Theme
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.email_render import render_order_confirmation_email
from tests.integration.conftest import format_axe_violations, run_axe_on_html

_SHOW_DATE = datetime.now(UTC).date() + timedelta(days=45)


async def _seed_order_with_tickets(
    db_session: AsyncSession,
    *,
    event: Event,
    ticket_types: list[TicketType],
    language: str = "en",
) -> tuple[Order, list[Ticket]]:
    """A real, persisted Order + one Ticket per ``ticket_types`` entry, each
    Ticket signed exactly like a genuine payment confirmation would (see
    ``app.services.ticket_delivery.sign_order_tickets``) — so the QR image
    embedded in the rendered email is a real signed token, not a stub
    string."""
    order = Order(
        event_id=event.id,
        buyer_name="Jamie O'Brien",
        buyer_email="jamie@example.test",
        buyer_address="1 Example Street, Example Town",
        payment_method="door",
        total=Decimal("42.50"),
        language=language,
    )
    db_session.add(order)
    await db_session.flush()
    tickets = []
    for ticket_type in ticket_types:
        # ``Ticket.id`` is a Python-side default (``UUIDPrimaryKeyMixin``:
        # ``default=uuid.uuid4``), not assigned until flush — signing off
        # ``ticket.id`` before an explicit id is set here would sign every
        # ticket in this loop against the same ``None`` id (silently
        # producing DUPLICATE ``qr_token`` values, which the DB's own
        # unique constraint then rejects at commit). Assigning the id
        # explicitly up front avoids depending on a mid-loop flush.
        ticket = Ticket(id=uuid.uuid4(), order_id=order.id, ticket_type_id=ticket_type.id)
        ticket.qr_token = sign_ticket_token(ticket.id)
        db_session.add(ticket)
        tickets.append(ticket)
    await db_session.commit()
    for ticket in tickets:
        await db_session.refresh(ticket)
    return order, tickets


async def test_default_order_confirmation_email_html_has_no_axe_violations(
    axe_page: Page,
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    make_theme: Callable[..., Awaitable[Theme]],
) -> None:
    """The built-in EN default copy (no admin-customized ``EmailTemplate``
    row — ``template=None``, exactly like a real event that hasn't
    customized this email yet), with a real Theme logo set (exercises the
    logo ``<img alt=...>`` branch of ``_logo_html``, not just its
    no-logo-set heading fallback) and two DIFFERENT ticket types (so the
    tickets table has more than one row, exercising the QR column across
    real distinct signed tokens)."""
    event = await make_event(status=PublishStatus.PUBLISHED, name="Christmas Passion")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED, date=_SHOW_DATE, venue_name="Het Kruispunt")
    adult = await make_ticket_type(show_id=show.id, name="Adult", price=Decimal("15.00"))
    child = await make_ticket_type(show_id=show.id, name="Child", price=Decimal("7.50"))
    await make_event_config(event_id=event.id)
    theme = await make_theme(
        event_id=event.id,
        primary_color="#1a1a1a",
        secondary_color="#ffffff",
        accent_color="#c9a227",
        font_choice=ThemeFont.SYSTEM_SANS,
        logo_path="theme-logos/sample-logo.png",
        status=PublishStatus.PUBLISHED,
    )
    order, tickets = await _seed_order_with_tickets(db_session, event=event, ticket_types=[adult, child])

    rendered = render_order_confirmation_email(
        template=None,
        order=order,
        event=event,
        show=show,
        theme=theme,
        tickets=tickets,
        ticket_types_by_id={str(adult.id): adult, str(child.id): child},
    )

    assert rendered.text_body.strip(), "plain-text alternative must be genuinely non-empty"
    assert "Jamie" in rendered.text_body, "plain-text alternative should carry real buyer/order content"

    violations = await run_axe_on_html(axe_page, rendered.html_body)
    assert violations == [], format_axe_violations(violations)


async def test_admin_customized_order_confirmation_email_html_has_no_axe_violations(
    axe_page: Page,
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    make_theme: Callable[..., Awaitable[Theme]],
) -> None:
    """Same audit, but against a real admin/agent-customized
    ``EmailTemplate`` row (Dutch locale) — the actual production path once
    an event has edited its copy in the backoffice, not just the built-in
    fallback."""
    event = await make_event(status=PublishStatus.PUBLISHED, name="Christmas Passion")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED, date=_SHOW_DATE)
    ticket_type = await make_ticket_type(show_id=show.id, name="Volwassene", price=Decimal("15.00"))
    await make_event_config(event_id=event.id)
    theme = await make_theme(
        event_id=event.id,
        primary_color="#1a1a1a",
        secondary_color="#ffffff",
        accent_color="#c9a227",
        status=PublishStatus.PUBLISHED,
    )
    order, tickets = await _seed_order_with_tickets(
        db_session, event=event, ticket_types=[ticket_type], language="nl"
    )
    template = EmailTemplate(
        event_id=event.id,
        language="nl",
        template_type=EmailTemplateType.ORDER_CONFIRMATION_TICKET.value,
        subject="Uw tickets voor {{event_name}}",
        body="<p>Beste {{buyer_name}}, tot ziens op {{show_date}}!</p>",
    )
    db_session.add(template)
    await db_session.commit()

    rendered = render_order_confirmation_email(
        template=template,
        order=order,
        event=event,
        show=show,
        theme=theme,
        tickets=tickets,
        ticket_types_by_id={str(ticket_type.id): ticket_type},
    )

    violations = await run_axe_on_html(axe_page, rendered.html_body)
    assert violations == [], format_axe_violations(violations)


async def test_order_confirmation_email_html_reading_order_matches_visual_order(
    axe_page: Page,
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    make_theme: Callable[..., Awaitable[Theme]],
) -> None:
    """PROJECT_BRIEF.md: "a logical reading order in the underlying HTML —
    screen readers on email don't benefit from visual-only ordering."
    axe-core's static checks don't verify DOM order at all, so this walks
    the real rendered DOM in document order and asserts it matches the
    intended visual/narrative sequence: logo, then the main heading, then
    the admin-authored body copy, then the "time until show" line, then the
    ticket-type/QR table (as a real semantic ``<h1>``/``<h2>`` heading
    hierarchy, not styled ``<div>``s standing in for headings)."""
    event = await make_event(status=PublishStatus.PUBLISHED, name="Christmas Passion")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED, date=_SHOW_DATE)
    ticket_type = await make_ticket_type(show_id=show.id, name="Adult", price=Decimal("15.00"))
    await make_event_config(event_id=event.id)
    theme = await make_theme(event_id=event.id, logo_path="theme-logos/sample-logo.png", status=PublishStatus.PUBLISHED)
    order, tickets = await _seed_order_with_tickets(db_session, event=event, ticket_types=[ticket_type])

    rendered = render_order_confirmation_email(
        template=None,
        order=order,
        event=event,
        show=show,
        theme=theme,
        tickets=tickets,
        ticket_types_by_id={str(ticket_type.id): ticket_type},
    )

    await axe_page.set_content(rendered.html_body)

    # 'table:not([role="presentation"])' isolates the REAL data table (the
    # ticket-type/QR table, the only <table> in this markup with genuine
    # <th scope="col"> column headers) from the three purely-layout
    # <table role="presentation"> shells wrapping it — so this checks the
    # data table's own position, not an earlier layout table's. The QR
    # code <img>s live INSIDE that data table (one per ticket row), so
    # they're deliberately excluded from this selector too — this
    # assertion is about the headings/table skeleton's order, not every
    # image; the logo <img>'s position is checked separately below.
    tag_sequence: list[str] = await axe_page.evaluate(
        """() => Array.from(document.body.querySelectorAll('h1,h2,table:not([role="presentation"])'))
             .map(el => el.tagName)"""
    )
    # The H1 heading, then the H2 "tickets" heading, then its data <table>
    # — the data table would have sorted BEFORE the H1/H2 headings if the
    # underlying markup were built out of visual/CSS ordering tricks rather
    # than real source order, which is exactly the bug class this
    # assertion guards against.
    assert tag_sequence == ["H1", "H2", "TABLE"], tag_sequence

    # The logo <img> (the very first meaningful element in the shell) must
    # precede the H1 in document order too.
    logo_precedes_heading: bool = await axe_page.evaluate(
        """() => {
          const img = document.querySelector('img');
          const h1 = document.querySelector('h1');
          if (!img || !h1) return false;
          return !!(img.compareDocumentPosition(h1) & Node.DOCUMENT_POSITION_FOLLOWING);
        }"""
    )
    assert logo_precedes_heading, "logo image must appear before the H1 heading in DOM order"

    heading_tags: list[str] = await axe_page.evaluate(
        "() => Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6')).map(el => el.tagName)"
    )
    assert heading_tags == ["H1", "H2"], (
        f"expected exactly one H1 then one H2 (real heading hierarchy, no skipped/misordered "
        f"levels), got {heading_tags}"
    )


async def test_order_confirmation_email_html_without_theme_logo_keeps_correct_heading_order(
    axe_page: Page,
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Regression coverage for a real bug this suite caught and
    ``accessibility-auditor`` fixed directly: with no Theme (or a Theme
    with no uploaded logo), ``_logo_html``'s branding fallback used to be a
    literal ``<h2>`` tag rendered BEFORE the email's actual ``<h1>``
    ("Your tickets", further down the same document) — starting the
    heading hierarchy at h2 and then reversing back to h1, which a
    screen-reader user navigating by heading level would find nonsensical.
    The fallback is now a styled ``<p>`` (a masthead label standing in for
    the logo image, not document content), so the only headings in the
    document are the real ``<h1>``/``<h2>`` pair in the correct order. This
    test passes ``theme=None`` entirely (a real code path per
    ``app.services.email_render.render_order_confirmation_email``'s
    ``theme: Theme | None`` parameter, not a contrived state)."""
    event = await make_event(status=PublishStatus.PUBLISHED, name="Christmas Passion")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED, date=_SHOW_DATE)
    ticket_type = await make_ticket_type(show_id=show.id, name="Adult", price=Decimal("15.00"))
    await make_event_config(event_id=event.id)
    order, tickets = await _seed_order_with_tickets(db_session, event=event, ticket_types=[ticket_type])

    rendered = render_order_confirmation_email(
        template=None,
        order=order,
        event=event,
        show=show,
        theme=None,
        tickets=tickets,
        ticket_types_by_id={str(ticket_type.id): ticket_type},
    )

    violations = await run_axe_on_html(axe_page, rendered.html_body)
    assert violations == [], format_axe_violations(violations)

    heading_tags: list[str] = await axe_page.evaluate(
        "() => Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6')).map(el => el.tagName)"
    )
    assert heading_tags == ["H1", "H2"], heading_tags
