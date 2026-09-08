"""Renders the actual order-confirmation/ticket email content (Milestone 4):
resolves the Event's ``order_confirmation_ticket`` ``EmailTemplate`` (or a
built-in EN/NL default), substitutes placeholders through
``app.services.email_placeholders.render_placeholders``, and wraps the
result in a table-based, inline-CSS HTML shell plus an independently
meaningful plain-text alternative.

Scope note for whoever picks this up next (`frontend-theming`): this module
builds a functional, WCAG-reasonable default email shell — table layout,
inlined styles, a labelled ticket table, alt text on the logo and every QR
image, a logical (source-order) reading structure — using only the Theme's
FIXED color fields (never ``Theme.custom_css``, mirroring
``app.services.ticket_pdf``'s same rule for PDFs) so it's real and
testable end-to-end today. The visual polish/design pass is explicitly
`frontend-theming`'s job, not redone here.

The EN/NL copy behind ``DEFAULT_SUBJECT``/``DEFAULT_BODY``/``_SHELL_STRINGS``
below is a functional fallback only (so a genuine payment confirmation
never hard-fails over a missing template — PROJECT_BRIEF.md's Ticket
Generation & Delivery section), used only when the Event has no customized
``order_confirmation_ticket`` ``EmailTemplate`` for the buyer's language.

**Content-i18n decision (Milestone 4 review):** this copy now lives in
``app/i18n/en.json``/``nl.json`` under the ``email.order_confirmation.*``
key namespace, resolved through the normal :func:`app.i18n.translate`
lookup — NOT as separate Python dict literals — because it was previously
duplicated a second time (as a literal copy, "kept in sync by comment") in
``app.web.routes.email_templates`` for the backoffice editor's pre-fill,
which is exactly the kind of drift risk the real i18n system exists to
prevent. Folding it in was a clean fit, not a compromise: every value here
is still just a flat string (the ``{{placeholder}}``/``{ticket_type}``
tokens inside them are literal text as far as ``translate()`` is
concerned — nothing about JSON string storage conflicts with either
substitution mechanism used downstream). ``translate()`` only resolves the
raw string; ``DEFAULT_SUBJECT``/``DEFAULT_BODY``/``_SHELL_STRINGS`` below
remain as module-level names (now built from ``translate()`` at import
time) so the rest of this module and its docstrings/call sites are
unchanged — only where the copy is SOURCED changed, not the substitution
logic itself (that's still exclusively ``email_placeholders.render_placeholders``,
never Jinja/``translate()`` itself, for the security reasons documented in
that module).
"""

import html
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal

from app.i18n import SUPPORTED_LOCALES, translate
from app.i18n.formatting import format_currency, format_date, format_time
from app.models.email_template import EmailTemplate
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.theme import Theme
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.email_placeholders import render_placeholders
from app.services.theme_images import public_url_for
from app.services.theme_preview import FONT_STACKS
from app.services.ticket_pdf import qr_data_uri

DEFAULT_SUBJECT: dict[str, str] = {
    locale: translate("email.order_confirmation.subject", locale) for locale in SUPPORTED_LOCALES
}
"""Built-in fallback ``EmailTemplate.subject`` per locale, used only when the
Event has no customized ``order_confirmation_ticket`` template for the
buyer's language. Sourced from ``app/i18n/{locale}.json`` — see this
module's docstring for why it lives there rather than as a literal here."""

DEFAULT_BODY: dict[str, str] = {
    locale: translate("email.order_confirmation.body", locale) for locale in SUPPORTED_LOCALES
}
"""Built-in fallback ``EmailTemplate.body`` per locale. Same sourcing note as
:data:`DEFAULT_SUBJECT`."""

_SHELL_KEYS: tuple[str, ...] = (
    "heading",
    "days_prefix",
    "days_suffix_plural",
    "days_suffix_singular",
    "days_today",
    "tickets_heading",
    "ticket_type_column",
    "qr_column",
    "qr_alt",
    "attachment_note",
    "logo_alt",
    "footer",
)

_SHELL_STRINGS: dict[str, dict[str, str]] = {
    locale: {key: translate(f"email.order_confirmation.{key}", locale) for key in _SHELL_KEYS}
    for locale in SUPPORTED_LOCALES
}
"""Built-in fallback shell chrome strings (labels around the admin-authored
body content — never buyer/agent-controlled), one dict per locale, each key
resolved from ``app/i18n/{locale}.json``'s ``email.order_confirmation.<key>``
entry. Same sourcing note as :data:`DEFAULT_SUBJECT`."""

_DEFAULT_FONT_STACK = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
_DEFAULT_PRIMARY = "#1a1a1a"
_DEFAULT_SECONDARY = "#ffffff"


def _shell(locale: str) -> dict[str, str]:
    return _SHELL_STRINGS.get(locale, _SHELL_STRINGS["en"])


def compute_days_until_show(show_date: date, *, now: datetime | None = None) -> int:
    """Days remaining until ``show_date``, computed at call time (so a
    resend closer to the date reflects a smaller number — PROJECT_BRIEF.md:
    "refreshed if the email is resent closer to the date"). Clamped to a
    minimum of 0 for a show date that has already passed, rather than
    returning a negative number that would render as a nonsensical
    "-3 days to go".
    """
    reference = (now or datetime.now(UTC)).date()
    return max((show_date - reference).days, 0)


def _days_until_show_line(days: int, locale: str) -> str:
    shell = _shell(locale)
    if days <= 0:
        return shell["days_today"]
    suffix = shell["days_suffix_singular"] if days == 1 else shell["days_suffix_plural"]
    return f"{shell['days_prefix']} {days} {suffix}"


def build_placeholder_values(
    *, order: Order, event: Event, show: Show, days_until_show: int
) -> dict[str, str]:
    """Compute the fixed set of placeholder values available to the
    ``order_confirmation_ticket`` template, per PROJECT_BRIEF.md's "buyer
    name, show date, order total, etc.": one buyer-submitted value
    (``buyer_name``) and the rest server-computed from already-trusted
    Event/Show/Order data — never anything else (see
    ``app.services.email_placeholders`` for why that boundary matters).
    """
    return {
        "buyer_name": order.buyer_name,
        "event_name": event.name,
        "show_date": format_date(show.date, order.language),
        "show_time": format_time(show.start_time, order.language),
        "venue_name": show.venue_name,
        "venue_address": show.venue_address,
        "order_total": format_currency(order.total, order.language),
        "days_until_show": str(days_until_show),
    }


@dataclass(frozen=True)
class RenderedEmail:
    """The fully rendered subject/HTML/plain-text content for one send —
    everything ``app.services.ticket_delivery`` needs to hand to
    ``aiosmtplib``."""

    subject: str
    html_body: str
    text_body: str


def _logo_html(theme: Theme | None, event_name: str, locale: str) -> str:
    if theme is None or not theme.logo_path:
        return f'<h2 style="margin:0;font-size:18px;">{html.escape(event_name)}</h2>'
    url = public_url_for(theme.logo_path) or ""
    alt = _shell(locale)["logo_alt"].format(event_name=html.escape(event_name))
    return f'<img src="{html.escape(url)}" alt="{alt}" style="max-height:64px;max-width:220px;display:block;margin:0 auto;" />'


def _ticket_rows_html(tickets: list[Ticket], ticket_types_by_id: dict[str, TicketType], locale: str) -> str:
    shell = _shell(locale)
    rows: list[str] = []
    for ticket in tickets:
        ticket_type = ticket_types_by_id[str(ticket.ticket_type_id)]
        if not ticket.qr_token:
            continue
        qr_uri = qr_data_uri(ticket.qr_token)
        alt = shell["qr_alt"].format(ticket_type=html.escape(ticket_type.name))
        rows.append(
            "<tr>"
            f'<td style="border:1px solid #dddddd;padding:8px;vertical-align:middle;">{html.escape(ticket_type.name)}</td>'
            f'<td style="border:1px solid #dddddd;padding:8px;text-align:center;">'
            f'<img src="{qr_uri}" alt="{alt}" width="120" height="120" style="display:block;margin:0 auto;" />'
            "</td>"
            "</tr>"
        )
    return "\n".join(rows)


def render_order_confirmation_email(
    *,
    template: EmailTemplate | None,
    order: Order,
    event: Event,
    show: Show,
    theme: Theme | None,
    tickets: list[Ticket],
    ticket_types_by_id: dict[str, TicketType],
) -> RenderedEmail:
    """Render the complete order-confirmation/ticket email for ``order``.

    Uses ``template`` if given (an Event's saved ``order_confirmation_ticket``
    ``EmailTemplate`` for ``order.language``), else the built-in
    :data:`DEFAULT_SUBJECT`/:data:`DEFAULT_BODY` for that language. Every
    Ticket in ``tickets`` MUST already have ``qr_token`` set (tickets
    without one are silently skipped from the rendered ticket table — this
    should never happen in practice since ``app.services.ticket_delivery.
    sign_order_tickets`` always signs every ticket before this is called,
    but rendering must never crash a real send over it).

    "Time until the show" (PROJECT_BRIEF.md: "computed at send time and
    also refreshed if the email is resent closer to the date") is computed
    fresh on every call via :func:`compute_days_until_show` — callers never
    need to (and should not) cache/pass a stale value.
    """
    locale = order.language if order.language in DEFAULT_SUBJECT else "en"
    shell = _shell(locale)
    days_until_show = compute_days_until_show(show.date)
    values = build_placeholder_values(order=order, event=event, show=show, days_until_show=days_until_show)

    raw_subject = template.subject if template is not None else DEFAULT_SUBJECT[locale]
    raw_body = template.body if template is not None else DEFAULT_BODY[locale]
    subject = render_placeholders(raw_subject, values, escape_html=False)
    body_content = render_placeholders(raw_body, values, escape_html=True)

    primary = theme.primary_color if theme is not None else _DEFAULT_PRIMARY
    secondary = theme.secondary_color if theme is not None else _DEFAULT_SECONDARY
    font_stack = FONT_STACKS.get(theme.font_choice, _DEFAULT_FONT_STACK) if theme is not None else _DEFAULT_FONT_STACK

    days_line = _days_until_show_line(days_until_show, locale)
    ticket_rows = _ticket_rows_html(tickets, ticket_types_by_id, locale)
    event_name_escaped = html.escape(event.name)

    # Table-based layout with inline styles throughout (no <style> block) —
    # the standard cross-client-compatible pattern PROJECT_BRIEF.md calls
    # for. Only the FIXED theme fields (primary/secondary/font) are used,
    # never Theme.custom_css. Text color is always `primary` on `secondary`
    # background — the one color pairing Theme's own AA contrast check
    # (app.services.contrast.check_theme_contrast) actually evaluates as a
    # text/background pair — so this shell never risks an unvetted
    # color-on-color combination; the accent color is intentionally not
    # used for any text here, only left available to `frontend-theming`'s
    # future visual pass.
    html_body = f"""<!DOCTYPE html>
<html lang="{html.escape(locale)}">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:{secondary};font-family:{font_stack};color:{primary};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{secondary};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background-color:{secondary};border:1px solid #dddddd;">
<tr><td style="padding:20px;text-align:center;">
{_logo_html(theme, event.name, locale)}
</td></tr>
<tr><td style="padding:0 24px 8px;">
<h1 style="margin:0 0 16px;font-size:22px;color:{primary};">{html.escape(shell["heading"])}</h1>
{body_content}
<p style="margin:16px 0;padding:12px;border:2px solid {primary};font-size:16px;"><strong>{html.escape(days_line)}</strong></p>
<h2 style="margin:24px 0 8px;font-size:17px;color:{primary};">{html.escape(shell["tickets_heading"])}</h2>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
<tr>
<th scope="col" style="border:1px solid #dddddd;padding:8px;text-align:left;">{html.escape(shell["ticket_type_column"])}</th>
<th scope="col" style="border:1px solid #dddddd;padding:8px;text-align:center;">{html.escape(shell["qr_column"])}</th>
</tr>
{ticket_rows}
</table>
<p style="margin-top:16px;font-size:13px;">{html.escape(shell["attachment_note"])}</p>
</td></tr>
<tr><td style="padding:16px;text-align:center;font-size:12px;">{html.escape(shell["footer"].format(event_name=event_name_escaped))}</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""

    text_lines = [
        f"{shell['heading']}: {event.name}",
        "",
        _html_to_plain_text(body_content),
        "",
        days_line,
        "",
        shell["tickets_heading"] + ":",
    ]
    for ticket in tickets:
        ticket_type = ticket_types_by_id.get(str(ticket.ticket_type_id))
        if ticket_type is not None:
            text_lines.append(f"- {ticket_type.name}")
    text_lines += ["", shell["attachment_note"]]
    text_body = "\n".join(text_lines)

    return RenderedEmail(subject=subject, html_body=html_body, text_body=text_body)


_SAMPLE_SHOW_DATE = date(2026, 12, 18)


def render_email_template_preview(
    *, event: Event, theme: Theme | None, language: str, subject: str, body: str
) -> RenderedEmail:
    """Render draft (not-yet-saved) ``subject``/``body`` values against
    representative SAMPLE placeholder data and the Event's REAL theme —
    per PROJECT_BRIEF.md's "a preview using real theme colors/logo before
    saving". Nothing is persisted or looked up beyond ``event``/``theme``
    (both already loaded by the caller); every Order/Show/Ticket value used
    here is synthetic sample data, never a real buyer's.

    Mirrors ``app.services.theme_preview.build_theme_preview``'s "construct
    a throwaway, non-persisted instance and reuse the real render path"
    pattern — this calls the exact same
    :func:`render_order_confirmation_email` the real send path uses, so a
    preview can never drift from what an actual email would look like.
    """
    sample_order = Order(
        event_id=event.id,
        buyer_name="Jamie Sample",
        buyer_email="jamie@example.com",
        buyer_address="1 Example Street",
        total=Decimal("42.50"),
        language=language,
    )
    sample_show = Show(
        event_id=event.id,
        date=_SAMPLE_SHOW_DATE,
        doors_time=time(19, 30),
        start_time=time(20, 0),
        venue_name="Sample Venue",
        venue_address="1 Example Street, Sample Town",
        capacity=100,
    )
    sample_ticket_type = TicketType(show_id=sample_show.id, name="Adult", price=Decimal("42.50"), quantity_available=100)
    sample_ticket = Ticket(order_id=sample_order.id, ticket_type_id=sample_ticket_type.id)
    sample_ticket.qr_token = "PREVIEW-SAMPLE-TOKEN"
    sample_ticket.ticket_type = sample_ticket_type

    template = EmailTemplate(event_id=event.id, language=language, template_type="preview", subject=subject, body=body)

    return render_order_confirmation_email(
        template=template,
        order=sample_order,
        event=event,
        show=sample_show,
        theme=theme,
        tickets=[sample_ticket],
        ticket_types_by_id={str(sample_ticket_type.id): sample_ticket_type},
    )


def _html_to_plain_text(fragment: str) -> str:
    """Best-effort, dependency-free HTML-snippet-to-plain-text conversion
    for the admin-authored ``body`` content only (never the whole email
    shell — the plain-text alternative is otherwise built compositionally
    from known-safe programmatic strings, see
    :func:`render_order_confirmation_email`).

    ``fragment`` here has already been through
    ``app.services.email_placeholders.render_placeholders`` with
    ``escape_html=True``, so any buyer-controlled value inside it is
    already HTML-entity-escaped literal text — this function unescapes
    entities back to plain characters (since the destination is now plain
    text, not HTML) and strips the admin-authored tags (``<p>``,
    ``<strong>``, etc. — the only tags this content can realistically
    contain, since it went through the same escaping as everything else).
    """
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text)
    return " ".join(text.split())
