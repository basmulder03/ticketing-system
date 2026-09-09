"""One-shot sweep for stale ``pending``/``pending_door`` Orders.

Runs exactly one call to ``app.services.order_expiry.run_order_expiry_sweep_once``
against the configured database and exits — suitable for an external cron
job (e.g. `*/20 * * * *`) once real deployment/scheduling infrastructure
exists for this app. Until then, the SAME sweep also runs automatically
from an in-process background loop wired into the app's own lifespan (see
``app.services.order_expiry.run_order_expiry_background_loop``, started
from ``app.main.create_app``) — this script is a manual/operator-triggered
alternative to that, not a replacement for it; running both is safe (the
sweep is idempotent — see ``app.services.order_payment.release_order_stock``).

Mirrors ``scripts/seed.py``'s structure/conventions (a single `async def`
entry point, run via ``docker-compose exec app python scripts/expire_stale_orders.py``).

Run via: `docker-compose exec app python scripts/expire_stale_orders.py`
"""

import asyncio

from app.db.session import async_session_factory
from app.services.order_expiry import run_order_expiry_sweep_once


async def main() -> None:
    """Run one sweep and print a short operator-facing summary of what it did."""
    async with async_session_factory() as session:
        result = await run_order_expiry_sweep_once(session)

    print(
        f"[expire_stale_orders] Expired {len(result.expired_pending_order_ids)} stale pending "
        f"(Mollie) order(s) and {len(result.expired_pending_door_order_ids)} lapsed pending_door "
        f"order(s) — {result.total_expired} total."
    )


if __name__ == "__main__":
    asyncio.run(main())
