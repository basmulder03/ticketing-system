"""Ticket-scan route. Scanner or admin only (never agents); the logic lives in
``app.services.scan``.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_scanner_or_admin
from app.api.routes._utils import parse_uuid_or_404
from app.core.rate_limit import rate_limit_dependency, scan_rate_limiter
from app.db.session import get_session
from app.models.show import Show
from app.schemas.scan import ScanRequest, ScanResponse
from app.services.scan import scan_ticket

router = APIRouter(
    prefix="/api/v1/shows/{show_id}/scan",
    tags=["scan"],
    dependencies=[Depends(rate_limit_dependency(scan_rate_limiter))],
)


@router.post("")
async def scan(
    show_id: str,
    body: ScanRequest,
    principal: Principal = Depends(require_scanner_or_admin),
    session: AsyncSession = Depends(get_session),
) -> ScanResponse:
    """Validate ``body.token`` for ``show_id``, completing entry on a pass.

    404 only for an unknown show; every scan outcome (including failures) is a
    200 the frontend renders via ``outcome``.
    """
    parsed_show_id = parse_uuid_or_404(show_id, detail="Show not found.")
    show = await session.get(Show, parsed_show_id)
    if show is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Show not found.")

    result = await scan_ticket(session, token=body.token, show_id=parsed_show_id, principal=principal)
    return ScanResponse(
        outcome=result.outcome,
        message=result.message,
        ticket_id=str(result.ticket_id) if result.ticket_id else None,
        ticket_type_name=result.ticket_type_name,
        buyer_name=result.buyer_name,
        order_id=str(result.order_id) if result.order_id else None,
        order_status=result.order_status,
        amount_due=result.amount_due,
        scanned_at=result.scanned_at,
        scanned_by_name=result.scanned_by_name,
        actual_show_id=str(result.actual_show_id) if result.actual_show_id else None,
        actual_show_label=result.actual_show_label,
    )
