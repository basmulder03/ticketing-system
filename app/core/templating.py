"""Jinja2 template environment for server-rendered backoffice HTML pages.

Milestone 1.5 scope: backoffice-only. Public-site templates (Milestone 2)
will get their own environment wired in by `frontend-theming` at that
point, themed per-Event rather than using this fixed backoffice look —
never share this environment with public-facing rendering.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
