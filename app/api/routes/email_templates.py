"""EmailTemplate routes: list/get/upsert/delete an Event's per-language
email templates, and a draft-values live-preview endpoint (Milestone 4).

Content-type data per PROJECT_BRIEF.md's AI/Agent Access section ("email
template content" is explicitly listed as agent-accessible) — every route
here uses ``require_admin_or_agent``, same as Event/Show/TicketType/Theme,
never ``require_admin``.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import Principal, require_admin_or_agent
from app.api.routes._utils import parse_uuid_or_404
from app.db.session import get_session
from app.i18n import SUPPORTED_LOCALES
from app.models.email_template import EmailTemplate
from app.models.enums import EmailTemplateType
from app.models.event import Event
from app.schemas.email_template import (
    EmailTemplateOut,
    EmailTemplatePreviewRequest,
    EmailTemplatePreviewResponse,
    EmailTemplateUpsertRequest,
)
from app.services.audit import record_audit_entry
from app.services.email_render import render_email_template_preview

router = APIRouter(prefix="/api/v1/events/{event_id}/email-templates", tags=["email-templates"])

_SUPPORTED_TEMPLATE_TYPES = {t.value for t in EmailTemplateType}


def _to_out(template: EmailTemplate) -> EmailTemplateOut:
    return EmailTemplateOut(
        id=str(template.id),
        event_id=str(template.event_id),
        language=template.language,
        template_type=template.template_type,
        subject=template.subject,
        body=template.body,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


async def _get_event_or_404(session: AsyncSession, event_id: str) -> Event:
    parsed_id = parse_uuid_or_404(event_id, detail="Event not found.")
    event = await session.get(Event, parsed_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")
    return event


async def _get_event_with_theme_or_404(session: AsyncSession, event_id: str) -> Event:
    parsed_id = parse_uuid_or_404(event_id, detail="Event not found.")
    result = await session.execute(
        select(Event).where(Event.id == parsed_id).options(selectinload(Event.theme))
    )
    event = result.scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")
    return event


def _validate_template_type(template_type: str) -> str:
    if template_type not in _SUPPORTED_TEMPLATE_TYPES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown email template type.")
    return template_type


def _validate_language(language: str) -> str:
    normalized = language.lower()
    if normalized not in SUPPORTED_LOCALES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unsupported language.")
    return normalized


async def _get_template_or_404(
    session: AsyncSession, *, event_id: uuid.UUID, template_type: str, language: str
) -> EmailTemplate:
    result = await session.execute(
        select(EmailTemplate).where(
            EmailTemplate.event_id == event_id,
            EmailTemplate.template_type == template_type,
            EmailTemplate.language == language,
        )
    )
    template = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No customized template for this type/language yet — the built-in default is used.",
        )
    return template


@router.get("")
async def list_email_templates(
    event_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> list[EmailTemplateOut]:
    """List every customized EmailTemplate row for this event. A
    type/language combination absent from this list has no customization
    yet and falls back to a built-in default at send time (see
    ``app.services.email_render.DEFAULT_SUBJECT``/``DEFAULT_BODY``)."""
    event = await _get_event_or_404(session, event_id)
    result = await session.execute(select(EmailTemplate).where(EmailTemplate.event_id == event.id))
    return [_to_out(t) for t in result.scalars().all()]


@router.get("/{template_type}/{language}")
async def get_email_template(
    event_id: str,
    template_type: str,
    language: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> EmailTemplateOut:
    """Fetch this event's customized template for one type/language pair.
    404s (with a clarifying message) if this combination has never been
    customized — that is an expected, valid state (the built-in default
    applies), not necessarily an error the caller needs to fix."""
    event = await _get_event_or_404(session, event_id)
    template_type = _validate_template_type(template_type)
    language = _validate_language(language)
    template = await _get_template_or_404(session, event_id=event.id, template_type=template_type, language=language)
    return _to_out(template)


@router.put("/{template_type}/{language}")
async def upsert_email_template(
    event_id: str,
    template_type: str,
    language: str,
    body: EmailTemplateUpsertRequest,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> EmailTemplateOut:
    """Create or replace this event's subject/body for one type/language
    pair. Both ``subject``/``body`` are required (see
    ``EmailTemplateUpsertRequest``) — there is no partial-update mode for
    this resource."""
    event = await _get_event_or_404(session, event_id)
    template_type = _validate_template_type(template_type)
    language = _validate_language(language)

    result = await session.execute(
        select(EmailTemplate).where(
            EmailTemplate.event_id == event.id,
            EmailTemplate.template_type == template_type,
            EmailTemplate.language == language,
        )
    )
    template = result.scalar_one_or_none()
    created = template is None
    if template is None:
        template = EmailTemplate(event_id=event.id, template_type=template_type, language=language)
        session.add(template)

    template.subject = body.subject
    template.body = body.body

    await session.flush()
    await record_audit_entry(
        session,
        principal,
        action="email_template.create" if created else "email_template.update",
        target_type="EmailTemplate",
        target_id=str(template.id),
        detail={"event_id": str(event.id), "template_type": template_type, "language": language},
    )
    await session.commit()
    await session.refresh(template)
    return _to_out(template)


@router.delete("/{template_type}/{language}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_email_template(
    event_id: str,
    template_type: str,
    language: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete this event's customization for one type/language pair,
    reverting future sends of that type/language to the built-in default."""
    event = await _get_event_or_404(session, event_id)
    template_type = _validate_template_type(template_type)
    language = _validate_language(language)
    template = await _get_template_or_404(session, event_id=event.id, template_type=template_type, language=language)

    await session.delete(template)
    await record_audit_entry(
        session,
        principal,
        action="email_template.delete",
        target_type="EmailTemplate",
        target_id=str(template.id),
        detail={"event_id": str(event.id), "template_type": template_type, "language": language},
    )
    await session.commit()


@router.post("/preview")
async def preview_email_template(
    event_id: str,
    body: EmailTemplatePreviewRequest,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> EmailTemplatePreviewResponse:
    """Render draft (not-yet-saved) subject/body values against sample
    placeholder data and this event's REAL theme — nothing is persisted.
    See ``app.services.email_render.render_email_template_preview``.
    """
    event = await _get_event_with_theme_or_404(session, event_id)
    rendered = render_email_template_preview(
        event=event,
        theme=event.theme,
        language=body.language,
        subject=body.subject,
        body=body.body,
    )
    return EmailTemplatePreviewResponse(
        subject=rendered.subject, html_body=rendered.html_body, text_body=rendered.text_body
    )
