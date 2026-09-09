"""Proactive cleanup for stale ``pending``/``pending_door`` Orders that will
never receive a terminal resolution from any other code path.

The gap this exists to close: ``app.api.routes.public.mollie_webhook``
already correctly releases a ``pending`` Order's reserved stock when Mollie
*tells us* a payment reached a terminal failure state (``expired``/
``failed``/``canceled``). But if a buyer opens the Mollie checkout page and
simply abandons the browser tab, Mollie's own payment session eventually
expires on Mollie's side — and there is no guarantee this app is ever told;
a webhook delivery can be lost, or (in the abandoned-tab case) there may be
no final Mollie-side state change that triggers one at all within any
useful timeframe. Without a proactive sweep, that Order — and the stock its
Tickets hold, see ``app.services.stock.sold_counts_for_ticket_types`` —
would sit ``pending`` indefinitely, which for a limited-capacity show is a
real, consequential bug: phantom "sold out" state from checkouts nobody
ever completed.

Two DELIBERATELY DIFFERENT policies, one per Order kind (see
:func:`sweep_stale_orders`'s docstring for the full reasoning on each):

- ``pending`` (Mollie-method): a fixed TTL from ``Order.created_at``
  (``Settings.pending_order_ttl_hours``) — these are abandoned checkouts,
  and "how long ago did checkout start" is the only signal available.
- ``pending_door``: NOT time-boxed by a fixed TTL at all. A door
  reservation made a week (or more) before the show is completely normal,
  not abandoned — it is only swept once its Show's start time has clearly
  passed, since at that point the reservation is unambiguously moot (the
  buyer never showed up to pay).

Both kinds of expiry are applied via the EXISTING
``app.services.order_payment.release_order_stock`` — this module never
duplicates its row-locking, idempotency, or never-downgrades-a-paid-order
guarantees; it only decides WHICH Order ids are stale and calls that
function once per id, attributed to
``app.services.order_payment.SYSTEM_PRINCIPAL`` (an automated,
non-human-initiated transition, same as Mollie webhook reconciliation).

This module is invoked by two independent mechanisms — see their own
docstrings for why both exist:

- ``scripts/expire_stale_orders.py``: a one-shot script for an external
  cron job, once real deployment/scheduling infra exists.
- :func:`run_order_expiry_background_loop`: an in-process
  ``asyncio`` loop, wired into ``app.main.create_app``'s lifespan, so the
  app is self-contained/correct out of the box with no external scheduler
  required — matching this project's existing "one-command bootstrap, no
  extra infra to hand-configure" developer-experience posture (Mailpit,
  hot reload, etc.).

The sweep itself (:func:`sweep_stale_orders`) takes an explicit ``now``
parameter rather than reading ``datetime.now()``/``datetime.now(UTC)``
internally, matching how ``app.services.email_render.compute_days_until_show``
already handles this in this codebase — so it's a plain, deterministic
async function a test can drive with any fixed instant, with no wall-clock
dependency or monkeypatching required.
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
"""Fallback TTL used only if ``Settings.pending_order_ttl_hours`` can't be
resolved (never expected in practice — the setting has its own default).
Kept as a module-level constant, not just an inline literal, so it has one
place to be found; see ``Settings.pending_order_ttl_hours`` for the actual
reasoning behind the 6-hour figure, which is where operators are expected
to tune it."""

_PENDING_STALE_REASON = "stale pending order swept after TTL"
_PENDING_DOOR_STALE_REASON = "pending door order swept after its show's start time passed"


@dataclass(frozen=True)
class OrderExpirySweepResult:
    """Outcome of one call to :func:`sweep_stale_orders` — which Order ids
    were expired, split by kind, so callers (the script, the background
    loop) can report a meaningful summary."""

    expired_pending_order_ids: list[uuid.UUID] = field(default_factory=list)
    """``pending`` (Mollie) Orders expired for exceeding the TTL."""

    expired_pending_door_order_ids: list[uuid.UUID] = field(default_factory=list)
    """``pending_door`` Orders expired because their Show's start time has
    passed."""

    @property
    def total_expired(self) -> int:
        """Combined count across both kinds, for a one-line summary log/print."""
        return len(self.expired_pending_order_ids) + len(self.expired_pending_door_order_ids)


async def _find_stale_pending_order_ids(session: AsyncSession, *, cutoff_utc: datetime) -> list[uuid.UUID]:
    """Ids of ``pending`` Orders created before ``cutoff_utc`` (a
    tz-aware UTC instant) — candidates for TTL expiry. Plain read, no lock:
    the actual row lock (and re-check of current status) happens inside
    ``release_order_stock`` itself, per its own idempotency discipline."""
    result = await session.execute(
        select(Order.id).where(Order.status == OrderStatus.PENDING).where(Order.created_at < cutoff_utc)
    )
    return [row[0] for row in result.all()]


async def _find_lapsed_pending_door_order_ids(session: AsyncSession, *, local_now: datetime) -> list[uuid.UUID]:
    """Ids of ``pending_door`` Orders whose Show's start time has already
    passed, as of ``local_now`` (a NAIVE local datetime — matching
    ``app.web.routes.public_site._default_beamer_show_id``'s existing
    convention that ``Show.date``/``Show.start_time`` are naive values
    compared directly against naive "now", since this app assumes every
    venue operates in the same timezone as the server).

    Joins through ``Ticket``/``TicketType`` to reach each Order's Show:
    per ``app.models.order.Order``'s docstring, every Ticket in one Order
    belongs to the same Show, so this never needs to reconcile conflicting
    Show dates for a single Order — it just needs any one of its Tickets'
    Shows. Done as a plain read + Python-side date comparison rather than
    a DB-side date/time expression: simpler, avoids cross-dialect date-math
    differences, and the candidate set (Orders someone chose to pay at the
    door) is never large enough for this to matter.
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
    """Find and expire every stale ``pending``/``pending_door`` Order, in
    one call. Does NOT commit — callers control the transaction boundary
    (both the script and the background loop commit once per sweep, after
    this returns), matching ``app.services.order_payment``'s own
    "does not commit" convention.

    ``now`` must be tz-aware (UTC is the codebase's canonical form — see
    ``app.db.mixins.utcnow``). Two different comparisons are derived from
    it, matching two deliberately different TTL policies:

    - ``pending`` (Mollie) Orders: compared against ``Order.created_at``
      (also tz-aware UTC) minus ``pending_ttl``. This is the "abandoned
      checkout" case — a fixed grace period is the only signal available,
      chosen generous enough to never race a real in-progress Mollie
      checkout (see ``Settings.pending_order_ttl_hours`` for the exact
      reasoning).
    - ``pending_door`` Orders: NOT time-boxed by any fixed duration at
      all — ``pending_ttl`` is not used for these. A door reservation made
      well ahead of the show is normal, legitimate buyer intent, not
      neglect. Instead, ``now`` is converted to a naive local datetime
      (via ``now.astimezone().replace(tzinfo=None)``, matching
      ``_default_beamer_show_id``'s existing naive-local-time convention
      for comparing against ``Show.date``/``Show.start_time``) and an Order
      is only swept once its Show's start time has clearly passed — at
      that point the reservation is unambiguously moot, whether or not any
      fixed amount of time has elapsed since it was made.

    Every id found is released via ``app.services.order_payment.
    release_order_stock`` with ``new_status=OrderStatus.EXPIRED`` and
    ``principal=SYSTEM_PRINCIPAL`` — reusing that function's row-locking
    and idempotency guarantees rather than re-implementing them. Safe to
    call repeatedly/concurrently for the same stale Order (e.g. the
    background loop's next tick overlapping a slow prior sweep, or the
    background loop and a manually-run script racing): the underlying
    ``SELECT ... FOR UPDATE`` + re-check in ``release_order_stock`` means a
    second sweep that reaches an already-expired Order sees its
    already-updated status and no-ops, writing no duplicate audit entry.
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
    """Convenience wrapper used by both the standalone script and the
    background loop: calls :func:`sweep_stale_orders` with the real current
    time and this deployment's configured TTL (``Settings.
    pending_order_ttl_hours``), then commits. The one place ``datetime.now``
    is actually read for this feature — kept out of :func:`sweep_stale_orders`
    itself so that function stays a plain, deterministic, unit-testable
    async function (see module docstring).
    """
    settings = get_settings()
    result = await sweep_stale_orders(
        session,
        now=datetime.now(UTC),
        pending_ttl=timedelta(hours=settings.pending_order_ttl_hours),
    )
    await session.commit()
    return result


async def run_order_expiry_background_loop(*, stop_event: asyncio.Event) -> None:
    """In-process background loop, started as an ``asyncio.create_task`` from
    ``app.main.create_app``'s lifespan and cancelled cleanly on shutdown —
    see that module for the wiring. Runs :func:`run_order_expiry_sweep_once`
    on a fixed interval (``Settings.pending_door_order_sweep_interval_minutes``)
    for as long as the app process is up, so a stale Order's held stock is
    reclaimed automatically without requiring an operator to have set up an
    external cron job — this app has no other background-job infrastructure
    (no celery, no APScheduler), and real deployment/scheduling infra is
    explicitly deferred; this loop is what makes the app self-contained and
    correct in the meantime, matching how Mailpit/hot-reload/one-command
    bootstrap are already treated as first-class DX requirements here.

    Guards against overlapping sweeps (e.g. one sweep taking longer than the
    configured interval, most plausible if the DB is briefly slow/unreachable)
    with a simple "skip this tick if the previous sweep is still running"
    flag — sufficient for a single-process app; no distributed locking is
    needed since there is only ever one process running this loop.

    Exits (returns) as soon as ``stop_event`` is set — the lifespan shutdown
    handler sets this and then awaits the task, giving it a chance to finish
    its current sleep/sweep and return cleanly rather than being cancelled
    mid-transaction.

    Failures in one sweep (e.g. a transient DB outage) are logged and
    swallowed, not raised — an uncaught exception here would otherwise kill
    the background task permanently for the rest of the process's lifetime,
    silently disabling stale-order cleanup until the next restart, which is
    worse than skipping one tick and trying again on the next interval.
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
