"""Small shared helpers for the content-CRUD route modules (events, shows,
ticket types, event config) — kept out of ``app.api.deps`` since these are
route-layer conveniences, not auth/principal resolution.
"""

import uuid
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel


def parse_uuid_or_404(raw: str, *, detail: str) -> uuid.UUID:
    """Parse ``raw`` as a UUID, raising a 404 (not a 400) if it isn't one.

    A malformed ID in a path segment is indistinguishable, from the
    client's point of view, from "no such resource" — returning 404 for
    both keeps route behavior consistent regardless of why the lookup
    can't succeed.
    """
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail) from None


def apply_partial_update(instance: Any, body: BaseModel) -> dict[str, Any]:
    """Apply only the fields explicitly present in ``body`` onto ``instance``.

    Uses Pydantic's ``exclude_unset`` so a field omitted from the request
    body is left untouched on ``instance`` (PATCH semantics), rather than
    being reset to the field's schema default. Returns the dict of applied
    changes, handy for building an audit-log ``detail`` payload without
    re-deriving it at each call site.
    """
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(instance, field, value)
    return changes
