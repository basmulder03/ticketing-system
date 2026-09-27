"""Declarative base for all ORM models; Alembic autogenerate reads ``Base.metadata``."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
