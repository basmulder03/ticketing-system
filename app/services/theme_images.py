"""Local-filesystem storage for Theme logo/background images.

No object storage integration: PROJECT_BRIEF.md explicitly says to "avoid
adding services unless a requirement actually needs one", and this app
targets a single small Hetzner VPS — a dedicated directory on local disk
(``Settings.uploads_dir``, mounted as its own docker volume so it survives
container rebuilds — see ``docker-compose.yml``) is the simplest correct
choice (KISS). Only a relative path is ever stored on the ``Theme`` row
(see ``app.models.theme.Theme``); this module is the single place that
turns an uploaded file into that path (and back into bytes on delete).
"""

import uuid
from pathlib import Path
from typing import Final

from fastapi import HTTPException, UploadFile, status

from app.core.config import get_settings

_MAGIC_BYTES_BY_FORMAT: Final[dict[str, bytes]] = {
    "png": b"\x89PNG\r\n\x1a\n",
    "jpeg": b"\xff\xd8\xff",
}
"""Magic-byte signatures used to sniff the real file format, independent of
the client-supplied ``Content-Type`` header (which is trivially spoofable —
defense in depth against a mislabeled/malicious upload). WEBP is checked
separately below (RIFF container with a WEBP fourcc, not a fixed prefix)."""

_ALLOWED_CONTENT_TYPES: Final[dict[str, str]] = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/webp": "webp",
}
"""Allowed upload content types, mapped to the stored file extension. SVG is
deliberately excluded even though it's a common "logo" format: an SVG file
can embed ``<script>``/event-handler content and would need its own
sanitizer (the same class of risk this milestone's CSS sanitizer exists
for) — out of scope for this milestone; flagged in the handoff."""


def _sniff_format(data: bytes) -> str | None:
    """Best-effort detection of the real image format from its magic bytes.

    Returns ``"png"``, ``"jpeg"``, ``"webp"``, or ``None`` if the content
    doesn't match any allowed signature.
    """
    for fmt, signature in _MAGIC_BYTES_BY_FORMAT.items():
        if data.startswith(signature):
            return fmt
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


async def save_theme_image(
    *, event_id: uuid.UUID, kind: str, upload: UploadFile
) -> str:
    """Validate and persist an uploaded theme image, returning its stored
    relative path (to be written onto ``Theme.logo_path`` /
    ``Theme.background_image_path``).

    ``kind`` is a short label (``"logo"`` or ``"background"``) used only to
    namespace the generated filename for readability; it is not
    attacker-controlled input that ends up in a path (the filename itself
    is always a freshly generated UUID, never derived from the client's
    original filename, which sidesteps path-traversal/weird-character
    concerns entirely).

    Validates both the declared ``Content-Type`` AND the actual file bytes
    (magic-number sniffing) — the two must agree on the same format — and
    the size, read up to one byte past the configured limit so an
    oversized upload is rejected without buffering an unbounded amount of
    attacker-controlled data into memory first.

    Raises ``HTTPException`` (422/400) for any validation failure.
    """
    settings = get_settings()
    declared_ext = _ALLOWED_CONTENT_TYPES.get((upload.content_type or "").lower())
    if declared_ext is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unsupported image type. Allowed: PNG, JPEG, WEBP.",
        )

    max_bytes = settings.theme_upload_max_bytes
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Image exceeds the maximum allowed size of {max_bytes} bytes.",
        )
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Uploaded file is empty.")

    sniffed_ext = _sniff_format(data)
    if sniffed_ext is None or sniffed_ext != declared_ext:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="File content does not match its declared image type.",
        )

    event_dir = Path(settings.uploads_dir) / "themes" / str(event_id)
    event_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{kind}-{uuid.uuid4().hex}.{sniffed_ext}"
    (event_dir / filename).write_bytes(data)

    return f"themes/{event_id}/{filename}"


def delete_theme_image(relative_path: str | None) -> None:
    """Delete a previously stored theme image file, if it exists.

    Silently no-ops if ``relative_path`` is ``None`` or the file is already
    gone — deletion is best-effort cleanup, not something a request should
    fail over (a dangling file on disk is a minor cleanup issue, not a
    correctness or security problem).
    """
    if not relative_path:
        return
    settings = get_settings()
    full_path = Path(settings.uploads_dir) / relative_path
    full_path.unlink(missing_ok=True)


def public_url_for(relative_path: str | None) -> str | None:
    """Map a stored relative path to the public URL it's served from (see
    the ``/uploads`` static mount in ``app.main``). Returns ``None`` if no
    image is set."""
    if not relative_path:
        return None
    return f"/uploads/{relative_path}"
