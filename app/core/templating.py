"""Jinja2 environment for backoffice pages. Public pages use
``app.core.public_templating`` instead — keep the two separate."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
