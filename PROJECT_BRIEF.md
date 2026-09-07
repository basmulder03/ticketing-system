# Project Brief: Beacon — Generic Event Ticketing Platform

## Goal
Build a lightweight, self-hosted, open-source ticketing web app for live theater/music events. This is a generic, reusable platform, not a single-event tool — "Christmas Passion" by Het Kruispunt Landsmeer is simply the first real Event configured in it, exercised as the initial use case, not something baked into the product's identity, naming, or structure. Every requirement in this brief should be read as "the platform supports this," never "this event needs this." Single small application: public ticket site + backoffice + door-scanning app, no microservices.

## Open Source & Licensing
- License: MIT — simplest permissive option, standard choice for a small self-hostable tool like this, imposes no obligations on people who deploy or fork it. (Apache 2.0 is the fallback if explicit patent-grant language is ever wanted, but MIT is the right default here.) A proper `LICENSE` file with the full MIT text and copyright line (`Copyright (c) <year> basmulder03`) goes at the repo root — tied to the GitHub handle rather than a personal name, since that's already the public identity this repo is published under.
- Required docs at repo root, in addition to `PROJECT_BRIEF.md`:
  - `README.md` — what the project is, quickstart (local dev bootstrap), screenshots/description once available, link to docs, license badge
  - `CONTRIBUTING.md` — branch/PR workflow (per the Repository & Workflow section below), coding standards (KISS/DRY, type hints, docstrings, testing requirement), how to run the test suite locally
  - `CODE_OF_CONDUCT.md` — standard Contributor Covenant, since this is public
  - `SECURITY.md` — how to responsibly report a vulnerability privately (not as a public GitHub issue) — especially relevant given this handles payments and buyer PII
  - `CHANGELOG.md` — Keep a Changelog format, updated per release/notable PR
  - `.github/ISSUE_TEMPLATE/` and `.github/PULL_REQUEST_TEMPLATE.md` — basic templates so issues/PRs from anyone (including future agent-driven ones) stay structured
- Since this becomes public: never commit real secrets, sample `.env.example` files only, and a clear note in the README that EventConfig credentials (SMTP, Mollie) are runtime configuration, not something checked into the repo.

## Stack
- Backend: FastAPI (Python)
- DB: PostgreSQL
- Frontend: Server-rendered Jinja2 + HTMX (no SPA framework)
- QR generation: `qrcode` (Python)
- PDF generation (tickets, invoices): `weasyprint` (HTML → PDF, themeable)
- Email: SMTP is per-event configurable (host, port, encryption mode, username/password, sender name/address) — not hardcoded to one.com. One.com (`send.one.com`, port 465 SSL or 587 STARTTLS) is simply the default/example provider for the first event; any standard SMTP provider must work. Use `aiosmtplib`. Locally/in dev, point SMTP config at a sink instead (e.g. Mailpit or MailHog) so no real email ever leaves the dev environment
- Payments: Mollie Payments API + webhooks (online); door payments handled on a separately-operated SumUp terminal, reconciled via manual mark-as-paid — no SumUp integration in this app
- Hosting target: an existing small Hetzner VPS with an active docker-compose setup already running on it (not one.com hosting — one.com is only email/domain). See Deployment below — this is not a greenfield deploy target, coordinate with what's already there.

## Deployment (existing Hetzner VPS)
- Target infrastructure already exists: a small Hetzner VPS with a docker-compose file already active on it (presumably running other services too). This app should be designed to be added to that setup, not to assume it owns the whole machine.
- Resource-conscious by default: this is one of Hetzner's smallest VPS tiers, so keep container images lean (slim Python base images, no unnecessary build tooling in the runtime image), avoid running anything memory-hungry beyond what's needed (Postgres is fine but keep its configuration modest — no need for tuning meant for high-traffic workloads), and avoid adding services (extra caches, queues, etc.) unless a requirement in this brief actually needs one.
- `devops-agent` should treat the production compose file as something to extend, not replace: check for likely existing concerns on a shared VPS (a reverse proxy already handling TLS/routing — e.g. Caddy or Traefik — and other services' ports already in use) before assuming this app owns port 80/443 or needs its own reverse proxy. Ask/flag rather than guess if the existing compose setup isn't visible/accessible to inspect.
- The sales-live traffic spike (Milestone 9's load test) should specifically be evaluated against this VPS's actual small resource ceiling, not a generic assumption — if the smallest Hetzner tier can't comfortably handle a realistic spike, flag that as a finding rather than silently hoping it holds up.

## Internationalization
- English and Dutch supported from the first milestone, not retrofitted later
- All user-facing text (public site, emails, ticket PDFs, invoice PDFs) goes through a translation layer (e.g. `gettext`/`.po` files or a simple key-based JSON dictionary per language) — no hardcoded strings in templates
- Buyer selects language on the public site (or it's inferred from browser locale with a visible override); their chosen language is stored on the Order and used for confirmation email, ticket PDF, and invoice PDF
- Backoffice UI itself can stay English-only for v1 unless stated otherwise — only the buyer-facing surface needs both languages guaranteed at launch
- Date/time and currency formatting follow the selected locale (e.g. "18 december 2026, 20:00" vs "December 18, 2026, 8:00 PM")

## Core entities
- Event (top-level container; has one Theme, one EventConfig)
- EventConfig (belongs to an Event: SMTP settings, sender name/address, Mollie API keys (test/live), invoice/company details & numbering prefix, sales-live datetime, enabled payment methods) — everything operational is scoped here, nothing is a single global setting shared across all events
- Show (belongs to an Event: date, doors_time, start_time, venue name, venue address, capacity)
- TicketType (belongs to a Show: name, price, service_fee toggle, quantity_available)
- Order (buyer info, status, payment_method: mollie | door, total)
- Ticket (belongs to an Order + TicketType: unique HMAC-signed QR token, scanned_at, scanned_by)
- Invoice (belongs to an Order: sequential number, PDF)
- Theme (belongs to an Event: colors, logo, background image, font, optional sandboxed custom CSS)
- AdminUser (role: admin | scanner)
- AgentAccount (role: agent; named, API-key-based, scoped to content management only)
- AuditLogEntry (actor_type: human | ai_agent, actor identifier, action, target, timestamp)

## Required functionality

### Auth & Users
- Admin login, hashed passwords (argon2 or bcrypt), optional 2FA
- Three roles: Admin (full access), Scanner (scan endpoint only), Agent (API-only, content management — see AI/Agent Access below)
- Session timeout
- Audit log of all backoffice changes (who/what/when), viewable in backoffice

### AI/Agent Access & Content Management
- A separate, API-key-based auth path (distinct from the human admin login) for AI agents/skills that manage content — e.g. drafting/updating event descriptions, ticket type copy, email template content, theme text fields
- Agent API keys are scoped: they can read/write content-type data (events, shows, ticket types, theme fields, email template content) but cannot touch payment credentials, SMTP credentials, admin user management, financial data, or the audit log itself
- Every agent API key is tied to a named Agent account (not a shared/anonymous key), so actions can always be attributed to a specific integration
- Every change made via an Agent key is written to the audit log with an explicit actor type of "AI agent" plus the agent's name/identifier — never recorded as if a human admin made it, and never silently merged into a generic "system" actor
- Rate limiting and scope restrictions on agent keys same as any other credential (see Security & Ops); keys revocable individually from the backoffice without affecting other agents or human admins
- Human review/publish step remains available where it matters (e.g. an agent can draft an event description or email copy, but publishing it live can be configured to require admin approval) — decide per content type whether agent changes go live immediately or land as a draft; in practice, agent-authored content naturally fits the draft/published model described under Draft & Preview, so this doesn't need a separate mechanism

### Event & Theming
- Create/edit events, each with its own Theme and EventConfig
- Theme = fixed fields by default: primary/secondary/accent colors, logo, background/hero image, font choice
- Theme applies to: public landing page, ticket PDF, invoice PDF, and the shell/branding of all outgoing emails (colors, logo)
- Optional "advanced" custom CSS override, but ONLY scoped to the public landing page (never applied to ticket/invoice PDFs, which always use fixed theme fields for guaranteed compliance)
- Custom CSS must be sanitized/sandboxed server-side before storage/render: strip `@import`, external `url()` references, `position: fixed` abuse, and anything targeting elements outside the event's content container
- Automated AA contrast check against the fixed theme fields; show a warning banner in backoffice if custom CSS is active (since arbitrary CSS can't be reliably auto-audited)
- "Duplicate theme from previous event" convenience action
- Live theme preview in backoffice before publishing

### Per-Event Configuration
- Every operational setting lives on the Event, not globally: SMTP (host/port/encryption/credentials/sender identity), Mollie API keys (test + live), invoice/company details (name, address, VAT number, numbering prefix), and which payment methods are enabled
- Reason: this app is meant to be reused across future events, which may be run by different people, use different email accounts, or even different Mollie merchant accounts entirely — nothing should force a second event to share the first event's provider setup
- "Copy configuration from previous event" convenience action (mirrors the theme-duplication action) so setting up a similar recurring event doesn't mean re-entering everything from scratch
- A connection test action for SMTP (send a test email) and Mollie (verify the API key) directly from the config screen, so misconfiguration is caught before the event goes live rather than at the first real order
- All credentials in EventConfig are encrypted at rest per the usual secrets handling (see Security & Ops)

### Show & Ticket Management
- CRUD for shows under an event (date, doors time, start time, venue, capacity)
- CRUD for ticket types per show (name, price, capacity)
- Per-ticket-type toggle: price includes Mollie service fee vs. price excludes it (fee added at checkout)
- Live sold/remaining counter per ticket type
- Sales-live datetime setting per event/show — drives the public countdown
- Manual pause/resume sales override

### Public Site
- Landing page rendered using the event's active Theme
- Countdown component: pre-sale state before sales-live datetime, auto-switches to buy flow at that moment; use an ARIA-live region so screen readers announce the state change
- Show/date picker → ticket type + quantity selector with live stock
- Venue location shown as an embedded map on the show/event page (e.g. OpenStreetMap or Google Maps embed keyed off the venue address stored on the Show), with a plain-text address fallback/link for anyone who can't or doesn't want to load the map widget
- Checkout form: buyer name, email, address (for invoicing)
- Payment method choice at checkout: Mollie or pay-at-door (only shown if enabled for that show)
- Order confirmation page
- Must meet WCAG 2.1 AA: semantic HTML, correct heading order, visible focus states, keyboard navigability, labeled form fields, accessible error messaging, verified color contrast

### SEO & Discoverability
- Standard SEO: unique title/meta description per event/show page, canonical URLs, `sitemap.xml`, `robots.txt`, clean semantic URLs (not query-string-only routing for public pages)
- Open Graph / Twitter Card meta tags per event so links shared on social media or in chat apps render a proper preview (title, description, image from the event's theme/logo or a hero image)
- Structured data: `schema.org/Event` JSON-LD on every show page (name, startDate, location, offers/pricing, availability) — this is what lets Google show rich event snippets in search results
- Machine-readable content for LLM-based search/answer engines follows the same discipline as good SEO: real semantic HTML (not JS-only rendering — server-rendered Jinja2 already satisfies this), the same structured data above, and factual, unambiguous page text (clear dates, prices, venue) since LLM crawlers and answer engines parse the same structured/semantic signals as search engines, not a separate special format
- Fast page load on the public site (minimal JS, server-rendered HTML, optimized images) — this is both an SEO ranking factor and an accessibility/low-end-device concern, so the two goals reinforce each other here

### Draft & Preview
- Every Event, Show, and Theme has a `draft` / `published` status, independent of whether its production integrations (SMTP, Mollie) are configured yet
- A draft is fully viewable via a shareable, unguessable preview link (public but unlisted — not indexed, `noindex` meta tag, excluded from the sitemap) so it can be shown to stakeholders (e.g. the church board, the band) before anything goes live
- Preview mode works without real SMTP/Mollie credentials: checkout can be exercised in a simulated/sandbox mode (Mollie test-mode key if present, otherwise a clearly-labeled "test checkout" that doesn't process a real charge and doesn't send real email — routes through the local SMTP sink or a no-op if outside dev), so the full buyer journey can be reviewed before production credentials exist
- Publishing an event is an explicit action (not automatic once a draft looks complete) and is blocked with a clear message if required production config (SMTP, Mollie live key if online payment is enabled) is missing — you can't accidentally go live half-configured, but you also don't need those credentials just to build and share a preview

### Payments (Mollie — online)
- Create Mollie payment, redirect to Mollie Checkout
- Webhook endpoint with signature verification
- Idempotent webhook handling — a retried webhook must never double-issue tickets or double-count stock
- Release reserved stock on failed/expired/canceled payment
- Backoffice setting for test vs. live Mollie API key, per event, kept separate per environment

### Pay at the Door
- No payment-terminal integration — keeps this simple. The app's job is to clearly display the amount due (respecting the service-fee toggle) so staff can charge it on their own SumUp terminal exactly as they normally would
- Order marked `pending_door` at checkout when this payment method is chosen; ticket issued with an "unpaid — settle at door" flag
- Staff use the manual mark-as-paid action (below) after charging on the terminal to confirm the order and trigger ticket/invoice delivery
- No SumUp API/account credentials, transaction references, or webhook handling needed for this app at all

### Manual Payment Handling
- Backoffice action to manually mark any order as paid, regardless of original payment method — covers door card payments (via a separately-operated SumUp terminal), bank transfers, true cash, corrections, etc.
- Manual mark-as-paid requires selecting a reason/method and is written to the audit log (who marked it, when, why)
- Marking an order paid triggers the same downstream effects as an automatic Mollie payment confirmation: invoice generation, and — if not already sent — ticket email dispatch

### Printing
- Every ticket and every invoice has a print-friendly view (clean print CSS: no navigation chrome, correct page size/margins, QR code sized to scan reliably off paper)
- Backoffice: "print" action on a single order (ticket + invoice together) and a batch-print action for multiple/all orders of a show (e.g. printing a full run ahead of an event, or reprinting for someone who lost their ticket)
- Printing works directly from the browser (print stylesheet), no separate desktop app required

### Sharing
- Every shareable surface gets a direct, copyable link: the event/show landing page, a specific ticket type (deep link that pre-selects it in the picker), and any draft preview link (already covered under Draft & Preview)
- Copy-link buttons in both the public site (share this show) and backoffice (share this preview/draft) — no manual URL construction needed
- Social share buttons on the public site (share to WhatsApp/Facebook/email at minimum, since that's how most ticket links actually circulate for a community event like this) — these ride on the Open Graph tags already defined under SEO & Discoverability so shared links render properly
- Order confirmation/ticket links are never made shareable in this way — those stay behind the buyer's unique signed access (email link/QR), since sharing them would leak someone else's ticket or PII

### Responsive & Multi-Device Display
- Public site and backoffice both fully responsive across mobile, tablet, laptop, and desktop breakpoints — standard responsive design, mobile-first, tested at common breakpoints (~375px, ~768px, ~1024px, ~1440px+)
- Additional large-display mode for beamer/TV use (e.g. a countdown or "doors open at" screen projected in a venue lobby or church hall): a dedicated, simple full-screen view — large typography, high contrast, no interactive elements or navigation chrome — reachable via its own URL so it can be left open on a screen unattended
- Scanning app UI specifically optimized for a phone held at the door (large tap targets, minimal chrome, one-handed use), separate consideration from the general responsive rules since its usage context is narrower
- Backoffice dashboard/stats view should remain usable on a tablet at minimum, since checking sales from a phone in the moment is a realistic use case, even if the primary backoffice experience is laptop/desktop-oriented

### Ticket Generation & Delivery
- Each ticket gets a unique HMAC-signed QR token (not a sequential/guessable ID)
- PDF ticket rendered using the event's theme, QR embedded
- Sent via the event's configured SMTP settings with PDF attached
- Backoffice action to resend a ticket
- Email content is editable per event/language in the backoffice, not hardcoded: subject and body for each email type (order confirmation/ticket, invoice, door-payment reminder, etc.), with placeholder variables (buyer name, show date, order total, etc.) and a preview using real theme colors/logo before saving
- Ticket confirmation email includes a "time until the show" element (e.g. "X days to go" or a countdown-style line), computed at send time and also refreshed if the email is resent closer to the date — this is a simple server-rendered value, not a live-updating countdown, since email clients can't run JS/CSS animations reliably
- Email HTML must meet WCAG 2.1 AA within the real constraints of email rendering: sufficient color contrast on all text/background combinations (including against the theme's colors), a logical reading order in the underlying HTML (screen readers on email don't benefit from visual-only ordering), real semantic structure (proper heading tags, not styled `<div>`s pretending to be headings), descriptive alt text on the logo and QR code image, and no meaning conveyed by color alone
- Build the email as table-based HTML with inlined CSS (the standard approach for cross-client compatibility — Outlook desktop, Gmail, Apple Mail, etc. all support this reliably), test across major clients before launch, and always include a plain-text fallback version of every email for clients/screen readers that prefer it

### Invoicing
- Auto-generate invoice PDF on payment confirmation (or door payment reconciliation)
- Sequential invoice numbering with configurable prefix, set per event in EventConfig
- Company/VAT details editable per event in EventConfig, used on that event's invoice template
- Emailed alongside the ticket; resend/re-download action in backoffice

### Scanning App
- Mobile-browser camera QR scanning (no native app), accessible to Scanner-role accounts
- Validates: signature authenticity, correct show/date, not already scanned
- Distinct third UI state beyond pass/fail: if the ticket is valid but its order is unpaid (`pending_door` and not yet settled), the scanner sees an explicit "NOT PAID — collect payment before entry" warning, visually and unmistakably different from a clean pass — this must not be mistaken for a valid scan
- Scanning an unpaid ticket does not silently let someone through; staff must resolve payment (triggering the manual mark-as-paid / door-payment flow) before the entry is treated as complete
- Clear pass/fail UI usable in low-light venue entrance conditions
- Degrade gracefully on flaky connectivity (don't hard-fail the whole flow on a slow network)

### Stats & Reporting
- Dashboard: sales per show/ticket type, revenue split online vs. door, gross vs. net of fees, scan-in rate (sold vs. used)
- CSV export for accounting

### Security & Ops (apply throughout, not a separate bolt-on)
- All secrets (Mollie key, SMTP password) encrypted at rest, never logged or exposed client-side
- HTTPS + HSTS
- CSRF protection on all backoffice forms
- Parameterized queries / ORM only — no raw string-built SQL
- Rate limiting on checkout and scan endpoints
- Transactional/row-locked stock decrement on checkout to prevent overselling under concurrent buyers (critical at the sales-live moment)
- Automated database backups, with a tested restore procedure
- GDPR-conscious: minimal PII retention, deletion-on-request process, privacy policy page
- Alerting on failed payments or failed email sends

## Testing
- Automated tests are part of the definition of done for every milestone, not a separate pass at the end — the coding agent writes tests alongside each feature it builds, not after everything is finished
- Unit tests: business logic that can be isolated — pricing/fee calculation, QR token signing/verification, invoice numbering, capacity/stock decrement logic, i18n string resolution, theme CSS sanitization
- Integration tests: full request/response flows against a real (test) database — checkout creating an order, webhook handling (including idempotency — sending the same webhook twice must be explicitly tested), scan validation (valid/already-scanned/wrong-show/unpaid states), email content generation (rendered against Mailpit or captured output, not just "did send() get called")
- Concurrency test: the stock-decrement race condition explicitly covered — simulate concurrent checkouts for the last remaining ticket and assert exactly one succeeds, since this is the single most consequence-heavy bug class in a system built around a sales-live moment
- Accessibility tests: automated AA checks (e.g. axe-core) run against public pages and email HTML as part of the test suite, not only as a manual pre-launch audit
- Payment/webhook tests run against Mollie's test mode, never real transactions
- End-to-end test of at least the critical path (browse → checkout → pay (test mode) → receive ticket in Mailpit → scan it successfully) so a single test proves the whole system still works together, not just its parts in isolation
- Tests run automatically on every change (CI, or at minimum a pre-commit/pre-push hook if no CI is set up yet) — a milestone isn't "done" if its tests aren't wired into that loop
- What's explicitly out of scope for automation: visual/pixel-perfect design review, manual print-output inspection (paper size/QR scannability off real paper), and real cross-email-client rendering checks (Outlook/Gmail/Apple Mail) — these need a human at least once before launch, note them as manual checklist items rather than skipping them silently

## Developer Experience
- Local dev environment must be fully runnable without any real external services: no real SMTP, no real Mollie, no real domain
- SMTP sink for local dev: run Mailpit (or MailHog) alongside the app (e.g. via docker-compose), point local SMTP config at it, and surface a link/README note to its web UI so any email the app sends can be inspected instantly without a real inbox
- Mollie: use Mollie's test-mode API key locally by default (already covered under Payments), never require live keys for local dev
- Hot reload: backend auto-reloads on file change (`uvicorn --reload` or equivalent), templates/HTMX views reflect changes without a manual restart, and any frontend assets (CSS) rebuild/refresh automatically
- One-command local bootstrap: a single script or `docker-compose up` that starts the app, DB, and Mailpit together, runs migrations, and seeds at least one demo Event/Show/TicketType so there's something to click through immediately
- This matters especially when an autonomous coding agent is building this: the agent should be able to start the stack, make a change, and see the running result (page, email in Mailpit, generated PDF) within the same working session without extra manual setup steps — treat "can I see what I just built, right now" as a first-class requirement, not an afterthought
- README should document: how to start the dev stack, where to view sent emails (Mailpit URL), how to reset/reseed the DB, and how each event's SMTP/Mollie configuration is set and overridden between dev and real credentials

## Build order (please follow this sequence)
0. Foundations: project skeleton, DB, encrypted config/secrets storage, admin auth + roles (Admin/Scanner/Agent), agent API-key auth path, audit log scaffold with actor-type distinction, staging/prod deploy split, i18n scaffolding (translation layer wired in from the first template, EN + NL string files even if mostly empty), local dev stack with hot reload + Mailpit SMTP sink + one-command bootstrap with seed data
1. Backoffice core: Event CRUD, EventConfig (SMTP/Mollie/invoice settings per event, with connection-test actions), Show CRUD (incl. venue address), TicketType CRUD, sales-live datetime setting, Agent account management (create/revoke API keys, scope content-management access)
1.5. Event Theming: theme model, backoffice theme editor with live preview and AA contrast check, sanitized custom-CSS override
2. Public site (no payment yet): themed landing page, countdown, show/ticket picker with live stock, embedded venue map, SEO essentials (meta tags, Open Graph, schema.org/Event JSON-LD, sitemap/robots.txt), draft/published status with shareable unguessable preview links, shareable/copyable links for shows and ticket types, checkout form creating a "pending" order, responsive layout across mobile/tablet/laptop/desktop, accessibility pass
3. Payments: Mollie payment creation, webhook + signature verification + idempotency, stock release on failure, test-key flow end-to-end, simulated/sandbox checkout path for preview mode when production keys aren't configured yet
4. Ticket generation & delivery: signed QR tokens, themed PDF tickets in the buyer's chosen language, print-friendly view, per-event SMTP delivery, resend action
5. Invoicing: PDF generation on payment confirmation, localized to buyer's language, sequential numbering, company/VAT settings, email delivery, print-friendly view, resend action
6. Pay at the door + manual payment handling: door payment method showing amount due, `pending_door` issuance, backoffice manual mark-as-paid action with audit logging
7. Scanning app: camera QR scan, validation logic, pass/fail/unpaid-warning UI, scanner-role access, mobile-optimized one-handed layout
8. Stats & reporting: dashboard, scan-in rate, CSV export, tablet-usable layout
9. Hardening & launch prep: rate limiting, backup/restore test, alerting, full AA audit, full responsive audit across breakpoints, large-display (beamer/TV) countdown view, switch to live Mollie keys, load-test the sales-live moment

## Repository & Workflow
- GitHub repo owner: `basmulder03`. Repo creation is the coding agent's job (via `gh repo create basmulder03/<name>` — assume `gh` is already authenticated with correct permissions; don't attempt to configure auth yourself), created as a **public** repository since this is open source. Suggested repo name: `beacon` (or `beacon-tickets` if `beacon` is taken) — reflects the guiding-star motif from the original poster without tying the name to Christmas Passion specifically. Final naming call belongs to the repo owner — propose and confirm rather than assuming silently.
- This brief file lives at the repo root as `PROJECT_BRIEF.md` once the repo exists, so it's the first thing anyone (human or agent) opening the repo sees. The subagent configs live at `.claude/agents/*.md` per Claude Code's convention.
- Code style: KISS and DRY are non-negotiable defaults — prefer the simplest implementation that satisfies a requirement over a clever or speculatively "flexible" one; extract shared logic instead of duplicating it, but don't abstract prematurely for hypothetical future needs that aren't in this brief.
- Full type hints on all Python code (function signatures, class attributes, return types) — this isn't optional polish, treat it the same as the testing requirement. Consider wiring `mypy` (or `pyright`) into CI once the devops setup exists.
- Documentation is a required step of "done" for every milestone, same standing as tests: docstrings on all public functions/classes/modules explaining purpose and non-obvious behavior, inline comments only where the "why" isn't obvious from the code itself, and the README/PROJECT_BRIEF kept in sync with what's actually been built (update the brief's status if scope shifts during implementation, don't let it silently go stale).
- Git workflow: small, frequent, atomic commits — each commit should represent one coherent change, not a milestone-sized dump. Use conventional commit prefixes (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`) so history stays scannable.
- Every unit of work goes through a pull request — a feature branch per milestone-step (not necessarily per whole milestone if a milestone is large), never direct commits to `main`. Each PR title and description should be specific and traceable: state what was built/changed, why, and which milestone/requirement from this brief it addresses, so reviewing the PR list later reconstructs the project history without needing to re-read every diff.
- Git worktrees: not required for strictly sequential work (one milestone, one branch, one agent at a time — normal branch switching is simpler). Use a worktree per active branch only when two or more subagents are genuinely working in parallel on the same milestone (e.g. `frontend-theming` and `content-i18n` both touching Milestone 2 at once) — this avoids them colliding in the same working directory. Treat it as a tool to reach for when parallelism is actually happening, not a standing practice.

## Instruction to the coding agent
Before Milestone 0: create the GitHub repository (`gh repo create basmulder03/<agreed-name>`), place this brief at the repo root as `PROJECT_BRIEF.md`, and set up the `.claude/agents/` subagent configs. Then start Milestone 0. Treat automated tests and documentation as part of finishing a milestone, not follow-up tasks — a milestone isn't complete until its tests exist and pass, and its code/README/brief are documented and current. Work in small commits and per-feature pull requests with specific, traceable titles/descriptions, never large undocumented dumps to `main`. After each milestone, stop and summarize what was built, what was tested and how, and any decisions/assumptions made, before proceeding to the next milestone. Flag immediately if any requirement above conflicts with another (e.g. accessibility vs. a specific design choice) rather than silently choosing one.

## Suggested subagent split
This project is built with a set of scoped Claude Code subagents (see the accompanying `agents/` folder) rather than one agent holding the entire app in context at once. Each subagent's file (`.claude/agents/<name>.md`) defines its exact responsibilities and boundaries:

- `backend-builder` — models, routes, business logic, Mollie/SMTP integration
- `frontend-theming` — templates, theming engine, responsive/print CSS, large-display view
- `content-i18n` — EN/NL translation strings, email/PDF copy, SEO/structured-data text
- `accessibility-auditor` — WCAG 2.1 AA review gate, run after any public-facing or email work
- `security-reviewer` — review gate for auth, secrets, webhooks, agent-access scoping
- `test-writer` — unit/integration/e2e tests, invoked alongside or right after implementation
- `devops-agent` — local dev stack (docker-compose, Mailpit, hot reload, seed data), CI, deploy

Orchestration pattern per milestone: implementation agents (`backend-builder`, `frontend-theming`, `content-i18n`) build the milestone's scope, `test-writer` adds coverage for what was just built, then `accessibility-auditor` and/or `security-reviewer` review before the milestone is marked complete — only invoke the review agents relevant to what that milestone actually touched, to avoid unnecessary review passes on unrelated work. Keeping each agent's context limited to its own concern (not the whole codebase) is the main token-efficiency lever here — an implementation agent shouldn't need to read the theming CSS to add a route, and the accessibility auditor shouldn't need to read backend business logic to check contrast ratios.

## Agent Communication & Conciseness
Subagents don't talk to each other directly — the orchestrator (main agent) delegates a task, gets a result back, and decides what to hand the next subagent. Keep this loop cheap:

- **Handoff, not history.** When one subagent's output feeds into another (e.g. `backend-builder` finishes a feature that `test-writer` needs to cover), pass a short structured summary — what changed, which files, what needs testing/reviewing and why — not the full conversation that produced it. The next agent reads code and a summary, not a transcript.
- **Every subagent ends its turn with a brief structured handoff**, not free-form prose: what was done, files touched, what (if anything) is flagged for another agent, and any assumption made. This is fixed in each agent's own config file below so it's automatic, not something the orchestrator has to remind it of each time.
- **Reference, don't paste.** Point at file paths and line ranges instead of quoting large code blocks back and forth between agents — everyone can read the actual file.
- **The orchestrator holds the milestone state**, not each subagent — a subagent shouldn't need to re-derive "what milestone are we on" or "what's already built" from scratch; it's told what it needs for its specific task.
- **Review agents (`accessibility-auditor`, `security-reviewer`) report findings as a short list of concrete issues**, not a narrative review essay — each finding: what's wrong, where, and what needs to change.
