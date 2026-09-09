# Load-testing the sales-live moment

Milestone 9 ("Hardening & launch prep") calls for load-testing `POST
/api/v1/public/checkout` — the highest-consequence endpoint in this app,
per `PROJECT_BRIEF.md`: many distinct buyers racing for the same limited
pool of tickets the instant sales open.

`tests/integration/test_checkout_concurrency.py` already proves
**correctness** under concurrency (8 concurrent in-process requests via
`asyncio.gather`, asserting the row lock in
`app.services.stock.reserve_stock` never oversells) — that test runs in CI
on every push. This directory is different on purpose: it's a manual,
occasional, pre-launch **capacity/performance** exercise against a REAL
running instance (real HTTP connections, real Postgres, real latency),
meant to be run by a human operator before an actual event's sales-live
moment — **not** wired into CI (see `.github/workflows/ci.yml`, untouched
by this).

## Tool choice: Locust

[Locust](https://locust.io) (Python) over [k6](https://k6.io) (Go/JS):
this is a Python-first project end-to-end, Locust's scenario is just
another Python file (no new language in the toolbox), and its live web UI
is genuinely useful for *watching* RPS/latency/failure-rate charts update
in real time during an actual pre-launch test — the brief specifically
asks for a tool a human operator can run and read results from, not just a
CI-friendly pass/fail.

It's kept out of the main `dev` dependency group deliberately: it's a
one-off/occasional operational tool, not something every contributor
running `pytest`/`mypy` needs installed. It lives in its own
`pyproject.toml` optional-dependency group instead:

```bash
pip install -e ".[loadtest]"
# or, even lighter — locustfile.py imports nothing from this repo's `app`
# package, only the stdlib + locust, so a plain `pip install locust` in
# any Python 3.12 environment works too.
```

`seed_loadtest_data.py` (the companion seed script, see below) is the one
piece that *does* need the real `beacon` package importable — same as
`scripts/seed.py` — so run it inside the app container or an activated
native venv, not an ad hoc `pip install locust`-only environment.

## Step by step

### 1. Start the stack you're testing against

Local dev stack (either `./scripts/dev-up.sh` or `./scripts/dev-native-up.sh`
— see the root `README.md`/`CONTRIBUTING.md`), or point `--host` at a real
staging deployment (see "Staging / the real VPS" below). Never run this
against production with real inventory.

### 2. Raise the checkout rate limit — read this, it will silently invalidate your results otherwise

`POST /api/v1/public/checkout` is rate-limited **per client IP**
(`app.core.rate_limit.checkout_rate_limiter`, default
`CHECKOUT_RATE_LIMIT_PER_MINUTE=10` — see `.env.example`). That's correct
and desirable in production, where hundreds of *real* buyers each have
their own IP. It is **not** representative of a load-testing tool: Locust
sends every virtual user's request from this one machine (effectively one
source IP), so with the default limit, requests 11+ within any rolling
minute get `429`s that have nothing to do with the app's real checkout/
stock-lock capacity — they'd swamp your results and look like a capacity
ceiling that isn't real.

Before running a real test, raise the limit for the instance under test
(dev-only override — never do this in real production):

```bash
# docker-compose (edit .env or export before `docker-compose up`):
CHECKOUT_RATE_LIMIT_PER_MINUTE=100000

# dev-native-up.sh:
CHECKOUT_RATE_LIMIT_PER_MINUTE=100000 ./scripts/dev-native-up.sh
```

Revert it afterward — don't leave a raised limit committed or running
anywhere real. The Locust summary (see below) calls out any `429`s it saw
as a loud reminder in case you forget this step.

### 3. Seed a scarce TicketType

```bash
# inside the app container:
docker-compose exec app python scripts/loadtest/seed_loadtest_data.py seed --quantity 100

# or natively:
SEED_SMTP_HOST=localhost .venv/bin/python scripts/loadtest/seed_loadtest_data.py seed --quantity 100
```

This creates (idempotently) a dedicated, `PUBLISHED` "Load Test Event"
(slug `loadtest`) with a `door`-only `EventConfig`, then — every time you
run `seed` — a **fresh** `PUBLISHED` Show + one TicketType with
`quantity_available` set to whatever you passed. `door` payment is used
deliberately: it exercises the exact same row-locked
`app.services.stock.reserve_stock` path as every other payment method,
without also depending on network calls to Mollie's API (which would
conflate this app's own capacity with Mollie sandbox latency/rate limits —
`tests/integration/test_checkout_concurrency.py` makes the same call).

The script prints the new `TICKET_TYPE_ID` and a ready-to-copy `locust`
command.

Pick `--quantity` deliberately small relative to how many virtual users
you're about to run (e.g. 100 tickets vs. 300 virtual users) — the whole
point is guaranteeing real contention on one shared pool, not diffuse load
across plenty of headroom.

### 4. Run the load test

```bash
TICKET_TYPE_ID=<uuid from step 3> \
CHECKOUT_TOTAL_TICKETS=100 \
locust -f scripts/loadtest/locustfile.py \
  --host=http://localhost:8000 \
  --headless --users 300 --spawn-rate 300 --run-time 30s
```

- `--users 300 --spawn-rate 300` spawns all 300 virtual buyers
  near-instantly — modeling the actual sales-live burst, not a gradual
  ramp.
- Each virtual user makes exactly **one** checkout attempt (1 ticket, for
  the shared `TICKET_TYPE_ID`) and then goes quiet for the rest of the run
  — see `locustfile.py`'s `SalesLiveBuyer` docstring for why (a real buyer
  gets one attempt, not a retry loop, at this moment).
- Omit `--headless` to get Locust's web UI at <http://localhost:8089>
  instead, for watching live charts during the run — start it there and
  set users/spawn-rate/host in the browser form.
- Scale `--users`/`--quantity` up together to find where the app starts
  degrading (e.g. 100/300, then 100/1000, then 500/2000 — watch latency
  and error rate, not just whether it "worked").

### 5. Verify the ground truth (never trust Locust's counts alone for oversell)

```bash
docker-compose exec app python scripts/loadtest/seed_loadtest_data.py status --ticket-type-id <uuid>
```

Prints `quantity_available`, live `sold` count, and `remaining` — flags
loudly if `sold > quantity_available` (an oversell — see "What to look
for" below).

### 6. Clean up

The load-test Event/Show/TicketTypes live under the fixed `loadtest` slug,
separate from `scripts/seed.py`'s demo data, so they won't collide with it
— but they do accumulate one Show+TicketType per `seed` invocation. If
you've been testing against the shared local dev stack, either leave it
(harmless, clearly labeled "Load Test Event — do not use for real sales"
in the backoffice) or run `./scripts/dev-reset-db.sh` for a full reset +
reseed if you want a clean slate. If you tested against your own
disposable stack, just tear it down (`docker-compose down -v` on whatever
compose project you started for this).

Don't forget step 2's rate-limit override — revert it before leaving the
instance running for anyone else.

## Interpreting results

Locust prints its own summary table (requests/sec, response-time
percentiles, failure count) plus this scenario's own summary printed at
the end of the run (see `locustfile.py`'s `test_stop` listener) breaking
down `201`/`409`/`429`/other counts.

**What "good" looks like** — no absolute target numbers are prescribed
here (this app has no production traffic history to calibrate against);
here's what to actually look at and why:

- **Zero oversells, always.** `201` count must never exceed
  `quantity_available`; `scripts/loadtest/seed_loadtest_data.py status`
  (step 5) is the authoritative check, not Locust's own counters (Locust
  only sees HTTP responses — the DB is the source of truth). This is a
  hard pass/fail: any oversell is a critical correctness bug, not a
  "tune it later" performance finding.
- **`409` count should be exactly `(checkout attempts) − quantity_available`**,
  never fewer. If it's *lower* than expected, some requests are
  succeeding that shouldn't be (an oversell, covered above); this is the
  main sanity check the row lock is holding under real concurrent HTTP
  load the way `test_checkout_concurrency.py` already proved it does
  in-process.
- **`429` count should be zero.** Any non-zero count almost always means
  step 2 (raising `CHECKOUT_RATE_LIMIT_PER_MINUTE`) was skipped or
  reverted mid-test — it's an artifact of this test tool sharing one
  source IP, not a real capacity signal. `locustfile.py`'s summary flags
  this loudly.
- **Latency, split by outcome, not lumped together.** A successful (`201`)
  checkout does slightly more work than a `409` (it also inserts Order +
  Ticket rows after the row lock succeeds), so expect `201`s to be a bit
  slower on average, purely from that extra DB work — verified against a
  local smoke run (25 users / 20 tickets): median ~370ms for both outcomes,
  no dramatic split. This scenario deliberately uses `door` payment (see
  above), and confirmation email + PDF ticket rendering
  (`app.services.ticket_delivery.send_order_confirmation_email` →
  `weasyprint`, CPU-bound, called directly with no `asyncio.to_thread`/
  executor offload — a real, separate finding worth knowing about) is
  **not** part of a `door` checkout's request path at all — it fires later,
  asynchronously in wall-clock terms, from the separate staff-triggered
  mark-as-paid action (`app.web.routes.orders`), a different moment
  entirely from the sales-live checkout burst this scenario measures. A
  real `mollie`-method checkout (not used here, see above) would instead
  make a synchronous outbound HTTPS call to Mollie's API before returning
  — a different, network-dependent latency source than PDF/SMTP, and still
  no PDF/SMTP work inline (that's deferred to the webhook, later). If you
  specifically want to load-test the PDF+SMTP path's own throughput (e.g.
  a rush of staff mark-as-paid actions, or Mailpit itself as a bottleneck),
  that needs a different scenario against a different endpoint — out of
  scope for "the sales-live moment" this scenario targets.
- **Lock-wait time on `ticket_types`, not just wall-clock latency.** The
  row lock (`SELECT ... FOR UPDATE` in `app.services.stock.reserve_stock`)
  is the intentional serialization point — some queueing there is
  expected and correct, the question is whether it's *reasonable*
  queueing or a real bottleneck at your target concurrency. While a test
  is running, check Postgres directly:
  ```sql
  SELECT pid, wait_event_type, wait_event, query, state,
         now() - query_start AS running_for
  FROM pg_stat_activity
  WHERE wait_event_type = 'Lock' OR state = 'active'
  ORDER BY running_for DESC;
  ```
  (`docker-compose exec db psql -U beacon -d beacon`). A handful of rows
  briefly waiting on `ticket_types`/`tickets` locks is exactly the
  mechanism working as designed; requests queueing for many seconds (or
  timing out) is a real capacity ceiling.
- **Resource ceiling: this targets a small Hetzner VPS tier, not this dev
  machine.** `docker-compose.yml`'s local Postgres runs deliberately modest
  (`shared_buffers=32MB -c max_connections=50`) to mirror that target, but
  your dev machine's CPU/RAM is still almost certainly far larger than the
  actual VPS's. A clean result on a fast laptop does **not** by itself
  confirm the real VPS can handle a realistic spike (`PROJECT_BRIEF.md`
  says exactly this: flag it rather than silently assume it holds up). For
  a more representative number, either run this against a resource-capped
  container (e.g. `docker update --cpus 2 --memory 2g <app-container>`
  before the test, adjusted to the real VPS's actual spec once known) or,
  ideally, against staging on the real VPS itself before a real
  sales-live moment. `devops-agent` / a human with VPS access: the exact
  Hetzner tier's vCPU/RAM spec isn't available to this tooling — worth
  confirming and, if this dev-machine result and the real VPS's headroom
  look meaningfully different, re-running there directly.

## Staging / the real VPS

Same steps, with `--host` pointed at the staging URL instead of
`localhost`, and the rate-limit override (step 2) set via whatever
environment-variable mechanism that deployment uses (see the root
`README.md`'s "Environments & credentials" section) rather than a local
`.env` file. Confirm with whoever operates that VPS before running a real
burst of load against it — it's a shared machine per `PROJECT_BRIEF.md`'s
deployment constraints, potentially running other services too. Never
point this at a real production Event with real ticket inventory — always
a dedicated/staging Event (this script's `loadtest` slug, or equivalent).
