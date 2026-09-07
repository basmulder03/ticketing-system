"""Seed the local dev database with demo data.

Entrypoint stub for Milestone 0 — ``backend-builder`` should implement this
once Event/Show/TicketType/EventConfig models exist (Milestone 1), so that
`docker-compose up` (or `./scripts/dev-up.sh`) leaves the app immediately
explorable per the brief's Developer Experience requirements:

    "One-command local bootstrap: a single script or `docker-compose up`
    that starts the app, DB, and Mailpit together, runs migrations, and
    seeds at least one demo Event/Show/TicketType..."

Expected shape once implemented:
    - Create one demo Event (draft or published) with an EventConfig
      pointing SMTP at the local Mailpit sink and Mollie at the test-key
      placeholder (see ``Settings.seed_*`` in ``app/core/config.py`` for
      the values to use).
    - Create at least one Show under that Event.
    - Create at least one TicketType under that Show.
    - Be idempotent — safe to re-run against an already-seeded DB (e.g.
      upsert by a stable slug/name) so ``./scripts/dev-reseed.sh`` works.

Run via: `docker-compose exec app python scripts/seed.py`
(also wired into `./scripts/dev-up.sh` / `./scripts/dev-reseed.sh`).
"""

import asyncio


async def seed() -> None:
    """Populate demo data. No-op until models exist (Milestone 1+)."""
    print(
        "[seed] No models defined yet (Milestone 0 skeleton only) — "
        "nothing to seed. backend-builder: implement demo Event/Show/"
        "TicketType creation here starting Milestone 1."
    )


if __name__ == "__main__":
    asyncio.run(seed())
