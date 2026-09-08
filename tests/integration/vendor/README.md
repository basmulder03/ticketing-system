# Vendored: axe-core

`axe.min.js` is axe-core v4.10.2 (https://github.com/dequelabs/axe-core),
downloaded verbatim from jsDelivr (`https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js`).

Licensed under the Mozilla Public License 2.0 (see the file's own header
comment) — MPL 2.0 is compatible with this repo's MIT license for this
use (an unmodified, test-only tool injected into a browser page at test
time, never linked into or shipped with the application itself).

Test-only: this file is injected into headless-browser pages by the
`axe_page`/`run_axe` fixtures in `tests/integration/conftest.py` to run
automated WCAG 2.1 AA checks against the real rendered public-site HTML
(see `tests/integration/test_public_site_accessibility.py`), per
`PROJECT_BRIEF.md`'s Testing section. It is never served by the app and
never ships in the production runtime image.

To update: replace this file with a newer `axe.min.js` from
`https://cdn.jsdelivr.net/npm/axe-core@<version>/axe.min.js` and bump the
version noted above.
