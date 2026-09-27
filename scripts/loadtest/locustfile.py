"""Locust scenario for the sales-live rush, against a real running instance
(see ``scripts/loadtest/README.md``).

Standalone: imports only the stdlib and ``locust``. Every virtual user is a
distinct buyer making exactly one checkout for 1 ticket of the same scarce
``TICKET_TYPE_ID`` — many people racing for one limited pool, the case the
row-locked stock reservation exists for.

Required env: ``TICKET_TYPE_ID`` (from ``seed_loadtest_data.py seed``).
Optional: ``CHECKOUT_TOTAL_TICKETS`` (the seeded quantity, for the summary).

Raise ``CHECKOUT_RATE_LIMIT_PER_MINUTE`` first (see README), then:

    TICKET_TYPE_ID=<uuid> locust -f scripts/loadtest/locustfile.py \
        --host=http://localhost:8000 \
        --headless --users 300 --spawn-rate 300 --run-time 30s

Omit ``--headless`` for Locust's web UI (http://localhost:8089).
"""

import os
import sys
import uuid
from collections import Counter

from locust import HttpUser, between, events, task
from locust.env import Environment

_TICKET_TYPE_ID = os.environ.get("TICKET_TYPE_ID")
_CHECKOUT_PATH = "/api/v1/public/checkout"

# Outcome counts for the end-of-run summary. No locking needed: Locust users
# run as greenlets in one OS thread.
_OUTCOMES: Counter[str] = Counter()


@events.test_start.add_listener  # type: ignore[untyped-decorator]  # locust's decorator itself is untyped
def _require_ticket_type_id(environment: Environment, **kwargs: object) -> None:
    """Fail fast with a clear message if ``TICKET_TYPE_ID`` isn't set."""
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
    """Print raw outcome counts (see the README's "Interpreting results")."""
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
    """One buyer, one checkout attempt, then idle. The long ``wait_time`` is
    intentional: a real buyer gets one shot, not a retry loop.
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
                # Losing the race cleanly is a correct outcome, not a failure.
                _OUTCOMES["409_insufficient_stock"] += 1
                response.success()
            elif response.status_code == 429:
                _OUTCOMES["429_rate_limited"] += 1
                response.failure("429 rate limited — see CHECKOUT_RATE_LIMIT_PER_MINUTE note in README")
            else:
                _OUTCOMES["other"] += 1
                response.failure(f"unexpected status {response.status_code}: {response.text[:200]}")
