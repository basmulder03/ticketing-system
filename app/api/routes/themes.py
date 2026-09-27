"""Theme routes: get/upsert, logo/background upload, copying from another
event, and live preview. Admin or agent.
"""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin_or_agent
from app.api.routes._utils import parse_uuid_or_404
from app.core.css_sanitizer import sanitize_custom_css
from app.db.session import get_session
from app.models.event import Event
from app.models.theme import Theme
from app.schemas.theme import (
    ContrastPairOut,
    ContrastReportOut,
    ContrastSuggestionOut,
    ThemeOut,
    ThemePreviewRequest,
    ThemePreviewResponse,
    ThemeUpdateRequest,
)
from app.services.audit import record_audit_entry
from app.services.contrast import ThemeContrastReport, check_theme_contrast
from app.services.theme_images import delete_theme_image, public_url_for, save_theme_image
from app.services.theme_preview import build_theme_preview

router = APIRouter(prefix="/api/v1/events/{event_id}/theme", tags=["theme"])

_COPYABLE_FIELDS = ("primary_color", "secondary_color", "accent_color", "font_choice", "custom_css", "status")
"""Fields copied between events. Not the images: sharing a file would tie the
two events' storage together, so images are re-uploaded per event.
"""


def _contrast_report_out(report: ThemeContrastReport) -> ContrastReportOut:
    return ContrastReportOut(
        pairs=[
            ContrastPairOut(
                label=p.label,
                foreground=p.foreground,
                background=p.background,
                ratio=p.ratio,
                passes_normal_text=p.passes_normal_text,
                passes_large_text=p.passes_large_text,
            )
            for p in report.pairs
        ],
        all_pass_normal_text=report.all_pass_normal_text,
        all_pass_large_text=report.all_pass_large_text,
    )


def _to_out(theme: Theme) -> ThemeOut:
    report = check_theme_contrast(
        primary_color=theme.primary_color, secondary_color=theme.secondary_color, accent_color=theme.accent_color
    )
    return ThemeOut(
        id=str(theme.id),
        event_id=str(theme.event_id),
        primary_color=theme.primary_color,
        secondary_color=theme.secondary_color,
        accent_color=theme.accent_color,
        logo_url=public_url_for(theme.logo_path),
        background_image_url=public_url_for(theme.background_image_path),
        font_choice=theme.font_choice,
        custom_css=theme.custom_css,
        is_custom_css_active=bool(theme.custom_css),
        status=theme.status,
        contrast_report=_contrast_report_out(report),
        created_at=theme.created_at,
        updated_at=theme.updated_at,
    )


async def _get_event_or_404(session: AsyncSession, event_id: str) -> Event:
    parsed_id = parse_uuid_or_404(event_id, detail="Event not found.")
    event = await session.get(Event, parsed_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")
    return event


async def _get_theme_or_404(session: AsyncSession, event: Event) -> Theme:
    result = await session.execute(select(Theme).where(Theme.event_id == event.id))
    theme = result.scalar_one_or_none()
    if theme is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This event has no theme yet. PUT to this endpoint to create one.",
        )
    return theme


@router.get("")
async def get_theme(
    event_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> ThemeOut:
    """The event's theme with a fresh contrast report."""
    event = await _get_event_or_404(session, event_id)
    theme = await _get_theme_or_404(session, event)
    return _to_out(theme)


@router.put("")
async def upsert_theme(
    event_id: str,
    body: ThemeUpdateRequest,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> ThemeOut:
    """Create or update the theme; omitted fields stay unchanged. ``custom_css``
    is sanitized before storage (never stored raw); an empty result is ``NULL``.
    """
    event = await _get_event_or_404(session, event_id)
    result = await session.execute(select(Theme).where(Theme.event_id == event.id))
    theme = result.scalar_one_or_none()
    created = theme is None
    if theme is None:
        theme = Theme(event_id=event.id)
        session.add(theme)

    changes = body.model_dump(exclude_unset=True)
    if "custom_css" in changes:
        changes["custom_css"] = sanitize_custom_css(changes["custom_css"] or "") or None
    for field, value in changes.items():
        setattr(theme, field, value)

    await session.flush()
    await record_audit_entry(
        session,
        principal,
        action="theme.create" if created else "theme.update",
        target_type="Theme",
        target_id=str(theme.id),
        detail={"event_id": str(event.id), **{k: str(v) for k, v in changes.items()}},
    )
    await session.commit()
    await session.refresh(theme)
    return _to_out(theme)


@router.put("/logo")
async def upload_theme_logo(
    event_id: str,
    file: UploadFile = File(...),
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> ThemeOut:
    """Replace the logo (creating the theme if needed); the old file is then deleted."""
    event = await _get_event_or_404(session, event_id)
    result = await session.execute(select(Theme).where(Theme.event_id == event.id))
    theme = result.scalar_one_or_none()
    created = theme is None
    if theme is None:
        theme = Theme(event_id=event.id)
        session.add(theme)
        await session.flush()

    new_path = await save_theme_image(event_id=event.id, kind="logo", upload=file)
    old_path = theme.logo_path
    theme.logo_path = new_path
    await record_audit_entry(
        session,
        principal,
        action="theme.create" if created else "theme.logo.upload",
        target_type="Theme",
        target_id=str(theme.id),
        detail={"event_id": str(event.id), "logo_path": new_path},
    )
    await session.commit()
    await session.refresh(theme)
    delete_theme_image(old_path)
    return _to_out(theme)


@router.delete("/logo")
async def delete_theme_logo(
    event_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> ThemeOut:
    """Clear the logo and delete its file."""
    event = await _get_event_or_404(session, event_id)
    theme = await _get_theme_or_404(session, event)
    old_path = theme.logo_path
    theme.logo_path = None
    await record_audit_entry(
        session, principal, action="theme.logo.delete", target_type="Theme", target_id=str(theme.id),
        detail={"event_id": str(event.id)},
    )
    await session.commit()
    await session.refresh(theme)
    delete_theme_image(old_path)
    return _to_out(theme)


@router.put("/background")
async def upload_theme_background(
    event_id: str,
    file: UploadFile = File(...),
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> ThemeOut:
    """Like :func:`upload_theme_logo`, for the background image."""
    event = await _get_event_or_404(session, event_id)
    result = await session.execute(select(Theme).where(Theme.event_id == event.id))
    theme = result.scalar_one_or_none()
    created = theme is None
    if theme is None:
        theme = Theme(event_id=event.id)
        session.add(theme)
        await session.flush()

    new_path = await save_theme_image(event_id=event.id, kind="background", upload=file)
    old_path = theme.background_image_path
    theme.background_image_path = new_path
    await record_audit_entry(
        session,
        principal,
        action="theme.create" if created else "theme.background.upload",
        target_type="Theme",
        target_id=str(theme.id),
        detail={"event_id": str(event.id), "background_image_path": new_path},
    )
    await session.commit()
    await session.refresh(theme)
    delete_theme_image(old_path)
    return _to_out(theme)


@router.delete("/background")
async def delete_theme_background(
    event_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> ThemeOut:
    """Clear the background and delete its file."""
    event = await _get_event_or_404(session, event_id)
    theme = await _get_theme_or_404(session, event)
    old_path = theme.background_image_path
    theme.background_image_path = None
    await record_audit_entry(
        session, principal, action="theme.background.delete", target_type="Theme", target_id=str(theme.id),
        detail={"event_id": str(event.id)},
    )
    await session.commit()
    await session.refresh(theme)
    delete_theme_image(old_path)
    return _to_out(theme)


@router.post("/copy-from/{source_event_id}")
async def copy_theme(
    event_id: str,
    source_event_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> ThemeOut:
    """Copy another event's colors, font, custom CSS and status (not images)."""
    target_event = await _get_event_or_404(session, event_id)
    source_event = await _get_event_or_404(session, source_event_id)
    if target_event.id == source_event.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot copy an event's theme onto itself."
        )

    source_theme = await _get_theme_or_404(session, source_event)

    result = await session.execute(select(Theme).where(Theme.event_id == target_event.id))
    target_theme = result.scalar_one_or_none()
    created = target_theme is None
    if target_theme is None:
        target_theme = Theme(event_id=target_event.id)
        session.add(target_theme)

    for field in _COPYABLE_FIELDS:
        setattr(target_theme, field, getattr(source_theme, field))

    await session.flush()
    await record_audit_entry(
        session,
        principal,
        action="theme.copy_from",
        target_type="Theme",
        target_id=str(target_theme.id),
        detail={
            "target_event_id": str(target_event.id),
            "source_event_id": str(source_event.id),
            "created": created,
        },
    )
    await session.commit()
    await session.refresh(target_theme)
    return _to_out(target_theme)


@router.post("/preview")
async def preview_theme(
    event_id: str,
    body: ThemePreviewRequest,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> ThemePreviewResponse:
    """Preview unsaved values; nothing is stored. Custom CSS goes through the same
    sanitizer as saving. ``event_id`` only checks access; no Theme row is needed.
    """
    await _get_event_or_404(session, event_id)
    result = build_theme_preview(
        primary_color=body.primary_color,
        secondary_color=body.secondary_color,
        accent_color=body.accent_color,
        font_choice=body.font_choice,
        custom_css=body.custom_css,
    )
    return ThemePreviewResponse(
        sanitized_custom_css=result.sanitized_custom_css,
        is_custom_css_active=result.is_custom_css_active,
        contrast_report=_contrast_report_out(result.contrast_report),
        contrast_suggestions={
            label: ContrastSuggestionOut(field_name=s.field_name, suggested_color=s.suggested_color)
            for label, s in result.contrast_suggestions.items()
        },
        preview_css=result.preview_css,
        sample_html=result.sample_html,
    )
