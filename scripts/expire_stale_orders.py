"""One-shot sweep of stale ``pending``/``pending_door`` orders, for cron.

The app already runs the same sweep in-process; running both is safe (it's
idempotent).

Run via: `docker-compose exec app python scripts/expire_stale_orders.py`
"""

import asyncio

from app.db.session import async_session_factory
from app.services.order_expiry import run_order_expiry_sweep_once


async def main() -> None:
    """Run one sweep and print a summary."""
    async with async_session_factory() as session:
        result = await run_order_expiry_sweep_once(session)

    print(
        f"[expire_stale_orders] Expired {len(result.expired_pending_order_ids)} stale pending "
        f"(Mollie) order(s) and {len(result.expired_pending_door_order_ids)} lapsed pending_door "
        f"order(s) — {result.total_expired} total."
    )


if __name__ == "__main__":
    asyncio.run(main())
