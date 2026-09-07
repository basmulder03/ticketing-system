"""Declarative base class for ORM models.

``backend-builder`` should define models by subclassing ``Base`` here so
Alembic's autogenerate (wired in ``alembic/env.py``) can discover them via
``Base.metadata``.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
