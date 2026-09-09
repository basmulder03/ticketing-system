"""Locust scenario for Milestone 9's "load-test the sales-live moment"
against a REAL running Beacon instance (docker-compose, staging, ...) —
see ``scripts/loadtest/README.md`` for full setup/usage/interpretation.

Deliberately standalone: this file imports nothing from ``app.*`` (only the
stdlib + ``locust``), so running a load test never requires the ``beacon``
package itself to be installed — just ``pip install locust`` (or
``pip install -e ".[loadtest]"``, see pyproject.toml) in whatever
environment you run this from. Seeding the scarce TicketType it targets
*does* need the real app package — that's ``seed_loadtest_data.py``'s job,
run separately against the running instance (e.g. inside its container).

The scenario: every simulated user is a DISTINCT buyer (unique name/email
per request, matching ``app.schemas.order.CheckoutRequest``) making exactly
ONE checkout attempt for 1 ticket of a SHARED, scarce ``TICKET_TYPE_ID`` —
the real worst case this app's row-locked stock reservation
(``app.services.stock.reserve_stock``) exists for: many people racing for
the same limited pool the instant sales open, not generic load spread
across many different ticket types.

Required environment variable:
    TICKET_TYPE_ID   UUID of the TicketType to race for (see
                      ``seed_loadtest_data.py seed``).

Optional:
    CHECKOUT_TOTAL_TICKETS   quantity_available you seeded, purely for the
                              end-of-run summary's expected-failure-count
                              math (default: unknown — summary just shows
                              raw counts if unset).

Run (see README for the full walkthrough, including why
CHECKOUT_RATE_LIMIT_PER_MINUTE must be raised first):

    TICKET_TYPE_ID=<uuid> locust -f scripts/loadtest/locustfile.py \\
        --host=http://localhost:8000 \\
        --headless --users 300 --spawn-rate 300 --run-time 30s

Or omit --headless for Locust's web UI (http://localhost:8089) to watch
live RPS/latency/failure charts during the run.
"""

import os
import sys
import uuid
from collections import Counter

from locust import HttpUser, between, events, task
from locust.env import Environment

_TICKET_TYPE_ID = os.environ.get("TICKET_TYPE_ID")
_CHECKOUT_PATH = "/api/v1/public/checkout"

# Raw outcome counts, keyed by a short label — printed as a summary at the
# end of the run (see the `test_stop` listener below). Module-level and
# unlocked deliberately: Locust's HttpUser tasks run cooperatively on
# gevent greenlets in a single OS thread, so plain dict/Counter mutation
# here is safe without any additional locking.
_OUTCOMES: Counter[str] = Counter()


@events.test_start.add_listener  # type: ignore[untyped-decorator]  # locust's decorator itself is untyped
def _require_ticket_type_id(environment: Environment, **kwargs: object) -> None:
    """Fail fast, with a clear message, rather than letting every virtual
    user's first request 404/crash confusingly if the operator forgot to
    set ``TICKET_TYPE_ID``."""
    if not _TICKET_TYPE_ID:
        print(
            "[loadtest] TICKET_TYPE_ID environment variable is not set. "
            "Seed a scarce TicketType first (scripts/loadtest/seed_loadtest_data.py seed --quantity ...) "
            "and pass its id via TICKET_TYPE_ID=<uuid>. Aborting.",
            file=sys.stderr,
        )
        environment.runner.quit()  # type: ignore[union-attr]
        sys.exit(1)


@events.test_stop.add_listener  # type: ignore[untyped-decorator]  # locust's decorator itself is untyped
def _print_summary(environment: Environment, **kwargs: object) -> None:
    """Print a plain-language outcome summary — what to look for is
    explained in scripts/loadtest/README.md's "Interpreting results"
    section; this just surfaces the raw numbers that section talks about."""
    total = sum(_OUTCOMES.values())
    print("\n[loadtest] ==== Checkout outcome summary ====")
    print(f"[loadtest] total checkout attempts: {total}")
    print(f"[loadtest]   201 created (won a ticket):       {_OUTCOMES['201_created']}")
    print(f"[loadtest]   409 insufficient stock (expected): {_OUTCOMES['409_insufficient_stock']}")
    print(f"[loadtest]   429 rate limited (see README!):    {_OUTCOMES['429_rate_limited']}")
    print(f"[loadtest]   other/unexpected:                  {_OUTCOMES['other']}")
    expected_total = os.environ.get("CHECKOUT_TOTAL_TICKETS")
    if expected_total:
        print(
            f"[loadtest] seeded quantity_available was {expected_total} — 201s above should be "
            f"exactly min({expected_total}, total attempts); anything higher is an oversell (critical bug)."
        )
    if _OUTCOMES["429_rate_limited"]:
        print(
            "[loadtest] *** Got 429s: this almost always means CHECKOUT_RATE_LIMIT_PER_MINUTE was left at "
            "its low real-world default and is throttling this test's virtual buyers (who all share this "
            "machine's one source IP) rather than measuring real capacity — see README before trusting "
            "these numbers. ***"
        )
    print(
        "[loadtest] Ground truth check (run this next): "
        "python scripts/loadtest/seed_loadtest_data.py status --ticket-type-id " + str(_TICKET_TYPE_ID)
    )


class SalesLiveBuyer(HttpUser):
    """One simulated buyer: exactly one checkout attempt for 1 ticket of
    the shared scarce ``TICKET_TYPE_ID``, then idle for the rest of the
    run.

    ``wait_time`` is deliberately long (60s) relative to any realistic
    ``--run-time`` for this scenario (seconds, not minutes) — a real buyer
    at a sales-live moment gets ONE attempt, not a tight retry loop, so
    each virtual user's single task firing once and then going quiet for
    the rest of a short run is the intended behavior, not a bug. If you
    genuinely want sustained (not single-burst) load, lower this — but
    that changes what's being simulated (steady traffic vs. the opening-
    seconds spike this scenario targets).
    """

    host = "http://localhost:8000"
    wait_time = between(60, 60)

    @task
    def checkout_for_scarce_ticket(self) -> None:
        buyer_id = uuid.uuid4().hex[:12]
        payload = {
            "buyer_name": f"Load Test Buyer {buyer_id}",
            "buyer_email": f"loadtest-{buyer_id}@example.test",
            "buyer_address": "1 Sales Live Lane",
            "language": "en",
            "payment_method": "door",
            "items": [{"ticket_type_id": _TICKET_TYPE_ID, "quantity": 1}],
        }
        with self.client.post(
            _CHECKOUT_PATH,
            json=payload,
            name=f"{_CHECKOUT_PATH} (contended ticket type)",
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                _OUTCOMES["201_created"] += 1
                response.success()
            elif response.status_code == 409:
                # A clean loss of the race is a CORRECT outcome for this
                # scenario, not a Locust-reported failure — see README.
                _OUTCOMES["409_insufficient_stock"] += 1
                response.success()
            elif response.status_code == 429:
                _OUTCOMES["429_rate_limited"] += 1
                response.failure("429 rate limited — see CHECKOUT_RATE_LIMIT_PER_MINUTE note in README")
            else:
                _OUTCOMES["other"] += 1
                response.failure(f"unexpected status {response.status_code}: {response.text[:200]}")
