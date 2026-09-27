"""Local-disk storage for theme logo/background images (``Settings.uploads_dir``,
its own docker volume). The Theme row stores only the relative path.
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
"""File signatures used to check the real format, since the client's
``Content-Type`` is trivially spoofed. WEBP is checked separately (RIFF).
"""

_ALLOWED_CONTENT_TYPES: Final[dict[str, str]] = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/webp": "webp",
}
"""Allowed types → stored extension. No SVG: it can carry scripts and would
need its own sanitizer.
"""


def _sniff_format(data: bytes) -> str | None:
    """``"png"``, ``"jpeg"``, ``"webp"`` or ``None``, from magic bytes."""
    for fmt, signature in _MAGIC_BYTES_BY_FORMAT.items():
        if data.startswith(signature):
            return fmt
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


async def save_theme_image(
    *, event_id: uuid.UUID, kind: str, upload: UploadFile
) -> str:
    """Validate and store an upload; return its relative path.

    The filename is a fresh UUID (never the client's), so no path traversal.
    Declared type and sniffed bytes must agree, and the read stops one byte past
    the size limit so oversized uploads aren't buffered. Raises ``HTTPException``.
    """
    settings = get_settings()
    declared_ext = _ALLOWED_CONTENT_TYPES.get((upload.content_type or "").lower())
    if declared_ext is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unsupported image type. Allowed: PNG, JPEG, WEBP.",
        )

    max_bytes = settings.theme_upload_max_bytes
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Image exceeds the maximum allowed size of {max_bytes} bytes.",
        )
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Uploaded file is empty.")

    sniffed_ext = _sniff_format(data)
    if sniffed_ext is None or sniffed_ext != declared_ext:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="File content does not match its declared image type.",
        )

    event_dir = Path(settings.uploads_dir) / "themes" / str(event_id)
    event_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{kind}-{uuid.uuid4().hex}.{sniffed_ext}"
    (event_dir / filename).write_bytes(data)

    return f"themes/{event_id}/{filename}"


def delete_theme_image(relative_path: str | None) -> None:
    """Best-effort delete; a missing file or ``None`` is a no-op."""
    if not relative_path:
        return
    settings = get_settings()
    full_path = Path(settings.uploads_dir) / relative_path
    full_path.unlink(missing_ok=True)


def public_url_for(relative_path: str | None) -> str | None:
    """Public ``/uploads`` URL for a stored path, or ``None``."""
    if not relative_path:
        return None
    return f"/uploads/{relative_path}"
