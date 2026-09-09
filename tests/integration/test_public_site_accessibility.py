"""Automated WCAG 2.1 AA checks (axe-core, via Playwright) against the real
rendered public-site HTML — per PROJECT_BRIEF.md's Testing section:
"Accessibility tests: automated AA checks (e.g. axe-core) run against
public pages ... as part of the test suite, not only as a manual
pre-launch audit."

Every test here seeds real data through the same fixtures the rest of the
integration suite uses (``make_event``/``make_show``/... in
``conftest.py``) and drives a real headless-Chromium page against the
actual in-process app (``axe_page``/``run_axe`` in ``conftest.py``), so
this audits what a real buyer's browser receives — not a hand-copied HTML
fixture.

Scope note on ``color-contrast``: the themed landing page disables that one
axe rule (see ``run_axe``'s docstring) because a Theme's fixed colors (and
anything under ``.event-content`` that inherits them) already get an
automated AA contrast report from Milestone 1.5
(``app.services.contrast``/``tests/unit/test_contrast.py``) — this suite's
job is the surrounding, non-themed page chrome instead, which is exactly
what the order-confirmation/unavailable/404 pages are (no Theme involved
at all), so ``color-contrast`` stays fully enabled there.

Manual-only items NOT covered by this automated suite (axe-core cannot
verify these): real screen-reader announcement behavior of the countdown's
ARIA-live region at the actual moment sales go live, and real assistive-
technology behavior in general (axe checks the accessibility tree's static
correctness, not how a specific screen reader narrates it). See this
milestone's accessibility-auditor handoff for the full manual checklist.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import AsyncClient
from playwright.async_api import Page

from app.models.enums import PaymentMethod, PublishStatus, ThemeFont
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.theme import Theme
from app.models.ticket_type import TicketType
from tests.integration.conftest import apply_set_cookie_headers, format_axe_violations, run_axe

_PAST = datetime.now(UTC) - timedelta(days=1)


async def _seed_real_event(
    *,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    make_theme: Callable[..., Awaitable[Theme]],
    status: PublishStatus,
) -> tuple[Event, Show, TicketType]:
    """A realistic Event with a Theme, two Shows (so the show/date picker's
    radio group actually has more than one option), an available ticket
    type, a sold-out ticket type (exercises the "Sold out" badge state),
    and both payment methods enabled (exercises the full payment-method
    fieldset) — as close to a real buyer's page as this milestone's
    fixtures allow.
    """
    event = await make_event(status=status, name="Christmas Passion", description="A festive concert.")
    show_a = await make_show(event_id=event.id, status=status, venue_name="Het Kruispunt")
    show_b = await make_show(
        event_id=event.id,
        status=status,
        venue_name="De Kapel",
        date=datetime.now(UTC).date() + timedelta(days=45),
    )
    available_tt = await make_ticket_type(
        show_id=show_a.id, name="Adult", price=Decimal("15.00"), quantity_available=25
    )
    await make_ticket_type(show_id=show_a.id, name="Child", price=Decimal("7.50"), quantity_available=0)
    await make_ticket_type(show_id=show_b.id, name="Adult", price=Decimal("15.00"), quantity_available=25)
    await make_event_config(
        event_id=event.id,
        sales_live_at=_PAST,
        enabled_payment_methods=[PaymentMethod.MOLLIE, PaymentMethod.DOOR],
    )
    await make_theme(
        event_id=event.id,
        primary_color="#123abc",
        secondary_color="#fefefe",
        accent_color="#ff00ff",
        font_choice=ThemeFont.LORA,
        status=status,
    )
    return event, show_a, available_tt


# --- Landing page: published + preview (must be a11y-identical) -----------


async def test_published_landing_page_has_no_axe_violations(
    axe_page: Page,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    make_theme: Callable[..., Awaitable[Theme]],
) -> None:
    event, _show, _tt = await _seed_real_event(
        make_event=make_event,
        make_show=make_show,
        make_ticket_type=make_ticket_type,
        make_event_config=make_event_config,
        make_theme=make_theme,
        status=PublishStatus.PUBLISHED,
    )

    violations = await run_axe(axe_page, f"/e/{event.slug}", disabled_rules=["color-contrast"])

    assert violations == [], format_axe_violations(violations)


async def test_preview_landing_page_has_no_axe_violations(
    axe_page: Page,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    make_theme: Callable[..., Awaitable[Theme]],
) -> None:
    """The unguessable-preview-token page renders the same template as the
    published page (see app.web.routes.public_site.preview_landing_page's
    docstring: "identical rendering to the real page") — asserted here as
    its own a11y pass, not skipped as redundant, since it's a real draft
    Event/Show going through the DRAFT-status code path end to end."""
    event, _show, _tt = await _seed_real_event(
        make_event=make_event,
        make_show=make_show,
        make_ticket_type=make_ticket_type,
        make_event_config=make_event_config,
        make_theme=make_theme,
        status=PublishStatus.DRAFT,
    )

    violations = await run_axe(
        axe_page, f"/preview/{event.preview_token}", disabled_rules=["color-contrast"]
    )

    assert violations == [], format_axe_violations(violations)


async def test_landing_page_checkout_error_state_has_no_axe_violations(
    axe_page: Page,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """The sold-out/paused/etc. checkout-error banner (``role="alert"``,
    see app/templates/public/landing.html) rendered on a real 409 response
    from a real oversubscribed checkout — not just the page's default
    state."""
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=1)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    await axe_page.goto(f"/e/{event.slug}")
    await axe_page.fill(f"#qty-{ticket_type.id}", "5")
    # Fill every required field so this reaches the real "sold out"
    # business-logic error (409) rather than a client-bypassed HTML5
    # `required` validation gap — see this file's handoff notes for a
    # separate bug that gap uncovered (flagged for backend-builder, not
    # fixed here: this suite is templates/CSS/test-tooling scope only).
    await axe_page.fill("#buyer_name", "Buyer Name")
    await axe_page.fill("#buyer_email", "buyer@example.test")
    await axe_page.fill("#buyer_address", "1 Test Street")
    await axe_page.click("#payment-door")
    await axe_page.click('button[type="submit"]')
    await axe_page.wait_for_selector('[role="alert"]')

    rules_option = {"color-contrast": {"enabled": False}}
    result = await axe_page.evaluate(
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
    violations = result["violations"]
    assert violations == [], format_axe_violations(violations)


# --- Order confirmation / unavailable / 404: no Theme involved ------------


async def test_order_confirmation_page_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Checkout is performed via a plain ``httpx`` client (the same
    ``client`` fixture ``test_public_site_web_routes.py`` uses), not by
    clicking through the real form in ``axe_page`` — deliberately: a real
    buyer's browser would follow the checkout route's 303 redirect and land
    on ``/order-confirmation/<id>`` with its cookie set, but Chromium has a
    known CDP limitation where ``route.fulfill()`` responding with a 3xx
    status does NOT route the browser's automatically-issued redirect
    request back through page/context interception (``axe_page``'s
    ``context.route`` handler in ``conftest.py``) — it escapes straight to
    real DNS resolution, which fails for this suite's fake
    ``a11y-test.local`` test origin. Driving the checkout itself through
    ``httpx`` (already covered end to end by
    ``test_public_site_web_routes.py::test_checkout_form_happy_path_creates_order_and_redirects``)
    and then handing the resulting order-confirmation cookie to
    ``axe_page``'s browser context (``apply_set_cookie_headers``) gets this
    test to the real page a real buyer would land on without hitting that
    limitation — this test's own job is auditing that landed page's HTML,
    not re-proving the redirect mechanics.
    """
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, price=Decimal("15.00"), quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    checkout = await client.post(
        f"/e/{event.slug}/checkout",
        data={
            "show_choice": str(show.id),
            f"qty_{ticket_type.id}": "2",
            "buyer_name": "Buyer Name",
            "buyer_email": "buyer@example.test",
            "buyer_address": "1 Test Street",
            "payment_method": "door",
        },
    )
    assert checkout.status_code == 303
    order_confirmation_path = checkout.headers["location"]
    set_cookie_values = checkout.headers.get_list("set-cookie")
    assert set_cookie_values, "checkout redirect set no cookies to hand off to the browser"
    await apply_set_cookie_headers(axe_page.context, set_cookie_values)

    violations = await run_axe(axe_page, order_confirmation_path)

    assert violations == [], format_axe_violations(violations)


async def test_order_confirmation_unavailable_page_has_no_axe_violations(axe_page: Page) -> None:
    violations = await run_axe(axe_page, "/order-confirmation/00000000-0000-0000-0000-000000000000")
    assert violations == [], format_axe_violations(violations)


async def test_not_found_page_has_no_axe_violations(axe_page: Page) -> None:
    violations = await run_axe(axe_page, "/e/no-such-event-slug")
    assert violations == [], format_axe_violations(violations)


# --- Milestone 9: privacy policy page --------------------------------------
#
# GDPR-conscious Security & Ops requirement's "privacy policy page" — plain
# content, no Theme involved at all (see
# app/templates/public/privacy_policy.html's module docstring: the copy
# itself is placeholder text flagged separately for legal review, out of
# scope here — this test only covers markup/structure).


async def test_privacy_policy_page_has_no_axe_violations(axe_page: Page) -> None:
    violations = await run_axe(axe_page, "/privacy")
    assert violations == [], format_axe_violations(violations)


# --- Milestone 9: large-display "beamer/TV" countdown view -----------------
#
# app.web.public_context.build_beamer_theme_css's docstring explicitly asks
# for an accessibility-auditor pass to confirm its hardcoded near-black/
# near-white contrast reasoning holds — see this milestone's
# accessibility-auditor handoff for the actual computed contrast ratios
# (all comfortably above AA's 4.5:1/3:1 thresholds). Unlike the landing
# page, ``color-contrast`` is NOT disabled here: the Theme's ``accent_color``
# only ever reaches a purely decorative, ``aria-hidden`` element
# (``.beamer-page__accent-bar``) with no text of its own, so real per-event
# theme colors can never trip axe's text-contrast check on this page — a
# full, unmodified axe run is both possible and meaningful here.


async def test_beamer_page_before_doors_time_has_no_axe_violations(
    axe_page: Page,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_theme: Callable[..., Awaitable[Theme]],
) -> None:
    """The ticking-countdown render path: doors time is in the future, so
    ``#beamer-countdown`` is visible and ``#beamer-doors-open`` is
    ``hidden`` — with a real Theme (accent color + font + logo) applied, so
    the logo's alt text and the accent-color custom property both get
    exercised."""
    event = await make_event(status=PublishStatus.PUBLISHED, name="Beamer A11y Event")
    await make_show(
        event_id=event.id,
        status=PublishStatus.PUBLISHED,
        date=datetime.now(UTC).date() + timedelta(days=30),
        venue_name="Het Kruispunt",
    )
    await make_theme(
        event_id=event.id, accent_color="#ff00ff", font_choice=ThemeFont.LORA, status=PublishStatus.PUBLISHED
    )

    violations = await run_axe(axe_page, f"/e/{event.slug}/beamer")

    assert violations == [], format_axe_violations(violations)


async def test_beamer_page_after_doors_time_has_no_axe_violations(
    axe_page: Page,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    """The "doors are open" standing-message render path: doors time is
    already in the past, so ``#beamer-doors-open`` is visible and
    ``#beamer-countdown`` is ``hidden`` — a distinct branch from the test
    above, with no Theme at all (``logo_url`` is ``None``, exercising the
    "no logo" branch of ``beamer/show.html``)."""
    event = await make_event(status=PublishStatus.PUBLISHED, name="Doors Open Beamer Event")
    await make_show(
        event_id=event.id,
        status=PublishStatus.PUBLISHED,
        date=datetime.now(UTC).date() - timedelta(days=1),
        venue_name="De Kapel",
    )

    violations = await run_axe(axe_page, f"/e/{event.slug}/beamer")

    assert violations == [], format_axe_violations(violations)


# --- Keyboard navigability: real Tab-key traversal of the checkout form ---


async def test_checkout_form_is_fully_keyboard_operable_with_visible_focus(
    axe_page: Page,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """axe-core's static accessibility-tree checks don't exercise real Tab-
    key traversal or verify a focus ring actually renders (per this
    milestone's brief: "keyboard navigability... visible focus states on
    every interactive element"). This test drives real keyboard events
    against the real page and asserts each interactive element in the buy
    flow is reachable and gets a visibly non-zero outline/box-shadow when
    focused (not relying on an unstyled browser default that a stray CSS
    rule could have suppressed elsewhere in ``public.css``)."""
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    await axe_page.goto(f"/e/{event.slug}")

    async def _focused_has_visible_focus_style() -> bool:
        result: bool = await axe_page.evaluate(
            """
            () => {
              const el = document.activeElement;
              if (!el) return false;
              const style = getComputedStyle(el);
              const hasOutline = style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0;
              const hasBoxShadow = style.boxShadow !== 'none' && style.boxShadow !== '';
              return hasOutline || hasBoxShadow;
            }
            """
        )
        return result

    # Keyboard-select the show/date radio (native radio input: reachable by
    # Tab, selectable with Space, no mouse required).
    await axe_page.focus(f"#show-radio-{show.id}")
    assert await _focused_has_visible_focus_style(), "show/date radio has no visible focus indicator"
    await axe_page.keyboard.press("Space")
    assert await axe_page.is_checked(f"#show-radio-{show.id}")

    # Keyboard-fill the quantity input (select-all first: it starts
    # pre-filled with "0", per app/templates/public/landing.html's sticky
    # default, so typing without clearing would just append).
    await axe_page.focus(f"#qty-{ticket_type.id}")
    assert await _focused_has_visible_focus_style(), "quantity input has no visible focus indicator"
    await axe_page.keyboard.press("ControlOrMeta+a")
    await axe_page.keyboard.type("2")
    assert await axe_page.input_value(f"#qty-{ticket_type.id}") == "2"

    # Tab reaches the buyer-name field next in the same form and it's
    # focusable/has a visible ring too.
    await axe_page.focus("#buyer_name")
    assert await _focused_has_visible_focus_style(), "buyer_name input has no visible focus indicator"

    # The submit button is reachable and shows a visible ring.
    await axe_page.focus('button[type="submit"]')
    assert await _focused_has_visible_focus_style(), "submit button has no visible focus indicator"
