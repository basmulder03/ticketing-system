"""Theme routes: get/upsert an Event's Theme, logo/background image upload,
"duplicate theme from previous event", and the draft-values live-preview
endpoint.

Content-type data per PROJECT_BRIEF.md's AI/Agent Access section (theme
fields are explicitly listed as agent-accessible) — every route here uses
``require_admin_or_agent``, same as Event/Show/TicketType, never
``require_admin``.
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
"""Fields duplicated by "duplicate theme from previous event". Deliberately
excludes ``logo_path``/``background_image_path``: copying a reference to
another event's uploaded image file would couple the two events' storage
lifetimes together (deleting/replacing the source event's logo would
silently break the target's), which is surprising and avoidable — operators
re-upload images per event instead. Mirrors ``event_configs.py``'s
``_COPYABLE_FIELDS`` excluding ``sales_live_at`` for an analogous reason."""


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
    """Fetch the given Event's theme, including a freshly computed AA contrast report."""
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
    """Create or update the given Event's theme.

    Only fields explicitly present in the request body are applied. If
    ``custom_css`` is present, it is sanitized (see
    ``app.core.css_sanitizer.sanitize_custom_css``) BEFORE being stored —
    the raw submitted text is never persisted, only the sanitized result
    (an empty/whitespace-only or fully-stripped input is stored as
    ``NULL``, matching ``is_custom_css_active`` semantics).
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
    """Upload (replacing, if present) the theme's logo image. Creates the
    Theme row first if it doesn't exist yet. The previous logo file (if
    any) is deleted from disk after the new one is stored."""
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
    """Clear the theme's logo image and delete the underlying file."""
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
    """Upload (replacing, if present) the theme's background/hero image.
    Same behavior as :func:`upload_theme_logo`, for the background slot."""
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
    """Clear the theme's background image and delete the underlying file."""
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
    """"Duplicate theme from previous event": copy another event's theme
    colors/font/custom CSS/status onto this event's theme.

    Creates this event's Theme if it doesn't exist yet; overwrites the
    copyable fields if it does. Logo/background images are NOT copied — see
    ``_COPYABLE_FIELDS``.
    """
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
    """Render a live preview of arbitrary, not-yet-saved theme values.

    Nothing is persisted by this endpoint. ``custom_css`` in the request
    body goes through the exact same :func:`sanitize_custom_css` call as
    the real save path in :func:`upsert_theme` — the preview path never
    skips or relaxes sanitization. The ``event_id`` path parameter only
    confirms the event exists (and thus the caller's access to it); no
    Theme row is read or required.
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
        preview_css=result.preview_css,
        sample_html=result.sample_html,
    )
