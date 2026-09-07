# Security Policy

Beacon (this repo) handles buyer PII and payment flows (via Mollie), so
security reports matter even before v1 ships.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Instead, report privately via one of:

- GitHub's [private vulnerability reporting](../../security/advisories/new)
  for this repository (preferred — go to the "Security" tab → "Report a
  vulnerability").
- Email the maintainer directly at the address on the
  [basmulder03 GitHub profile](https://github.com/basmulder03).

Please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce (proof-of-concept code/requests if possible)
- Any suggested remediation, if you have one

## What to expect

- Acknowledgement of your report within a reasonable timeframe.
- An assessment of severity and, where valid, a fix developed on a private
  branch before public disclosure.
- Credit in the changelog/release notes, unless you prefer to remain
  anonymous.

## Scope

Particularly interested in reports involving:

- Authentication/authorization bypass (admin, scanner, or agent API-key
  roles)
- Payment/webhook handling (Mollie signature verification, idempotency)
- Secrets handling (SMTP/Mollie credentials at rest)
- QR ticket token forgery or replay
- SQL injection / raw query construction
- PII exposure or GDPR-relevant data handling issues
