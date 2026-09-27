"""Expires stale orders that nothing else will ever resolve.

An abandoned Mollie checkout may never produce a webhook, so without this
its tickets would hold stock forever (phantom "sold out"). Two policies:

- ``pending`` (Mollie): expired after ``Settings.pending_order_ttl_hours``.
- ``pending_door``: never time-boxed — booking early is normal. Expired only
  once its show has started.

Both go through ``release_order_stock`` (locking, idempotency, never touching
paid orders) as ``SYSTEM_PRINCIPAL``. Runs from an in-process background
loop (so no external scheduler is needed) and from
``scripts/expire_stale_orders.py`` for cron.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import async_session_factory
from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.order_payment import SYSTEM_PRINCIPAL, release_order_stock

logger = logging.getLogger(__name__)

DEFAULT_PENDING_ORDER_TTL: timedelta = timedelta(hours=6)
"""Fallback only; tune ``Settings.pending_order_ttl_hours`` instead."""

_PENDING_STALE_REASON = "stale pending order swept after TTL"
_PENDING_DOOR_STALE_REASON = "pending door order swept after its show's start time passed"


@dataclass(frozen=True)
class OrderExpirySweepResult:
    """Expired order ids from one sweep, split by kind."""

    expired_pending_order_ids: list[uuid.UUID] = field(default_factory=list)
    """Mollie ``pending`` orders past the TTL."""

    expired_pending_door_order_ids: list[uuid.UUID] = field(default_factory=list)
    """``pending_door`` orders whose show has started."""

    @property
    def total_expired(self) -> int:
        """Total across both kinds."""
        return len(self.expired_pending_order_ids) + len(self.expired_pending_door_order_ids)


async def _find_stale_pending_order_ids(session: AsyncSession, *, cutoff_utc: datetime) -> list[uuid.UUID]:
    """``pending`` orders created before ``cutoff_utc``. No lock here —
    ``release_order_stock`` locks and re-checks.
    """
    result = await session.execute(
        select(Order.id).where(Order.status == OrderStatus.PENDING).where(Order.created_at < cutoff_utc)
    )
    return [row[0] for row in result.all()]


async def _find_lapsed_pending_door_order_ids(session: AsyncSession, *, local_now: datetime) -> list[uuid.UUID]:
    """``pending_door`` orders whose show started before ``local_now``.

    ``local_now`` is naive local time: show dates/times are stored naive and the
    app assumes venue and server share a timezone. Compared in Python; the
    candidate set is small.
    """
    result = await session.execute(
        select(Order.id, Show.date, Show.start_time)
        .join(Ticket, Ticket.order_id == Order.id)
        .join(TicketType, TicketType.id == Ticket.ticket_type_id)
        .join(Show, Show.id == TicketType.show_id)
        .where(Order.status == OrderStatus.PENDING_DOOR)
        .distinct()
    )
    lapsed_ids: list[uuid.UUID] = []
    for order_id, show_date, show_start_time in result.all():
        show_start = datetime.combine(show_date, show_start_time)
        if show_start <= local_now:
            lapsed_ids.append(order_id)
    return lapsed_ids


async def sweep_stale_orders(
    session: AsyncSession,
    *,
    now: datetime,
    pending_ttl: timedelta = DEFAULT_PENDING_ORDER_TTL,
) -> OrderExpirySweepResult:
    """Expire every stale ``pending``/``pending_door`` order. Doesn't commit.

    ``now`` must be tz-aware; taking it as a parameter keeps this deterministic
    for tests. Safe to run concurrently with itself — the lock and re-check in
    ``release_order_stock`` make repeats no-ops.
    """
    cutoff_utc = now.astimezone(UTC) - pending_ttl
    local_now = now.astimezone().replace(tzinfo=None)

    pending_ids = await _find_stale_pending_order_ids(session, cutoff_utc=cutoff_utc)
    for order_id in pending_ids:
        await release_order_stock(
            session,
            order_id=order_id,
            new_status=OrderStatus.EXPIRED,
            principal=SYSTEM_PRINCIPAL,
            reason=_PENDING_STALE_REASON,
        )

    pending_door_ids = await _find_lapsed_pending_door_order_ids(session, local_now=local_now)
    for order_id in pending_door_ids:
        await release_order_stock(
            session,
            order_id=order_id,
            new_status=OrderStatus.EXPIRED,
            principal=SYSTEM_PRINCIPAL,
            reason=_PENDING_DOOR_STALE_REASON,
        )

    return OrderExpirySweepResult(
        expired_pending_order_ids=pending_ids,
        expired_pending_door_order_ids=pending_door_ids,
    )


async def run_order_expiry_sweep_once(session: AsyncSession) -> OrderExpirySweepResult:
    """Run a sweep with the real current time and configured TTL, then commit."""
    settings = get_settings()
    result = await sweep_stale_orders(
        session,
        now=datetime.now(UTC),
        pending_ttl=timedelta(hours=settings.pending_order_ttl_hours),
    )
    await session.commit()
    return result


async def run_order_expiry_background_loop(*, stop_event: asyncio.Event) -> None:
    """Sweep every ``pending_door_order_sweep_interval_minutes`` until
    ``stop_event`` is set (started from ``app.main``'s lifespan).

    Skips a tick if the previous sweep is still running. A failed sweep is
    logged and swallowed — letting it raise would kill the task until restart.
    Returns promptly on shutdown instead of being cancelled mid-transaction.
    """
    settings = get_settings()
    interval = timedelta(minutes=settings.pending_door_order_sweep_interval_minutes)
    sweep_in_progress = False

    while not stop_event.is_set():
        if sweep_in_progress:
            logger.warning("order_expiry: previous sweep still running, skipping this tick.")
        else:
            sweep_in_progress = True
            try:
                async with async_session_factory() as session:
                    result = await run_order_expiry_sweep_once(session)
                if result.total_expired:
                    logger.info(
                        "order_expiry: expired %d pending + %d pending_door order(s).",
                        len(result.expired_pending_order_ids),
                        len(result.expired_pending_door_order_ids),
                    )
            except Exception:
                logger.exception("order_expiry: sweep failed, will retry on the next interval.")
            finally:
                sweep_in_progress = False

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval.total_seconds())
        except TimeoutError:
            pass  # normal case: interval elapsed, loop again.
