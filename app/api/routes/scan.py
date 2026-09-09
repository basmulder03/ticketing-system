"""Door-scanning route (Milestone 7): validates a scanned QR ticket token
against one specific Show and, on a genuine pass, completes entry.

Gated by ``require_scanner_or_admin`` (not ``require_admin_or_agent`` —
scanner-role accounts are never agents, and this route is not
content-type data) — see that dependency's docstring for the exact
scoping rationale. Nested under ``/api/v1/shows/{show_id}`` rather than
also under ``/events/{event_id}``, mirroring
``app.api.routes.ticket_types``'s URL shape (Show-scoped routes there also
don't require the parent Event id in the path — a Show id is already a
unique UUID, so the extra path segment would add nothing but ceremony).

All actual validation/mutation logic lives in ``app.services.scan`` — this
module is intentionally thin (parse/404 the show, call the service, map
its result onto the response schema), matching the split every other
route module in this package uses.
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
    """Validate (and, on a pass, complete entry for) the ticket encoded in
    ``body.token`` against the Show identified by ``show_id``.

    404s if ``show_id`` is malformed or names no existing Show, matching
    every other route in this package (``app.api.routes._utils.
    parse_uuid_or_404``) — this is a routing-level 404, distinct from and
    checked before any of ``app.services.scan.scan_ticket``'s own outcome
    states (which are all HTTP 200s — a failed/unpaid scan is not itself an
    HTTP error, it's a normal, expected result the frontend renders
    directly via ``ScanResponse.outcome``; see that schema).
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
