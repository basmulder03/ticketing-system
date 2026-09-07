"""Unit tests for ``app.services.theme_images``: magic-byte vs.
declared-content-type validation, size/empty-file rejection, and the
public-URL/delete helpers.

No DB involved — only the local filesystem (via ``tmp_path``) and an
in-memory ``UploadFile``, so this lives in ``tests/unit/`` rather than
``tests/integration/``. ``get_settings`` is monkeypatched per-test (rather
than mutating the real process-wide ``lru_cache``d settings singleton) so
each test gets an isolated ``uploads_dir``/``theme_upload_max_bytes``
without leaking state between tests.
"""

import io
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app.services import theme_images

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _webp_bytes(payload: bytes = b"restofwebpdata") -> bytes:
    return b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + payload


def _upload(data: bytes, content_type: str, filename: str = "upload") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename, headers=Headers({"content-type": content_type}))


@pytest.fixture
def fake_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[int], SimpleNamespace]:
    """Point ``theme_images.get_settings()`` at an isolated tmp uploads dir.

    Returns a factory so a test can override ``theme_upload_max_bytes``
    (default generous: 1 MiB) without needing a second fixture.
    """

    def _make(max_bytes: int = 1024 * 1024) -> SimpleNamespace:
        settings = SimpleNamespace(uploads_dir=str(tmp_path), theme_upload_max_bytes=max_bytes)
        monkeypatch.setattr(theme_images, "get_settings", lambda: settings)
        return settings

    return _make


# --- Valid formats accepted ---


async def test_valid_png_is_accepted_and_written_to_disk(
    fake_settings: Callable[[int], SimpleNamespace],
) -> None:
    settings = fake_settings(1024)
    data = _PNG_MAGIC + b"restofpngdata"
    event_id = uuid.uuid4()

    path = await theme_images.save_theme_image(event_id=event_id, kind="logo", upload=_upload(data, "image/png"))

    assert path.startswith(f"themes/{event_id}/logo-")
    assert path.endswith(".png")
    assert (Path(settings.uploads_dir) / path).read_bytes() == data


async def test_valid_jpeg_is_accepted(fake_settings: Callable[[int], SimpleNamespace]) -> None:
    fake_settings(1024)
    data = _JPEG_MAGIC + b"restofjpegdata"

    path = await theme_images.save_theme_image(
        event_id=uuid.uuid4(), kind="background", upload=_upload(data, "image/jpeg")
    )

    assert path.endswith(".jpeg")


async def test_valid_webp_is_accepted(fake_settings: Callable[[int], SimpleNamespace]) -> None:
    fake_settings(1024)
    data = _webp_bytes()

    path = await theme_images.save_theme_image(event_id=uuid.uuid4(), kind="logo", upload=_upload(data, "image/webp"))

    assert path.endswith(".webp")


# --- Rejections ---


async def test_svg_is_rejected_as_unsupported_content_type(
    fake_settings: Callable[[int], SimpleNamespace],
) -> None:
    fake_settings(1024)
    data = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"

    with pytest.raises(HTTPException) as exc_info:
        await theme_images.save_theme_image(event_id=uuid.uuid4(), kind="logo", upload=_upload(data, "image/svg+xml"))

    assert exc_info.value.status_code == 422


async def test_magic_bytes_not_matching_declared_content_type_is_rejected(
    fake_settings: Callable[[int], SimpleNamespace],
) -> None:
    """Declared ``image/png`` but the actual bytes are a JPEG — the two must
    agree, since the declared ``Content-Type`` header is trivially spoofable."""
    fake_settings(1024)
    data = _JPEG_MAGIC + b"actually-a-jpeg"

    with pytest.raises(HTTPException) as exc_info:
        await theme_images.save_theme_image(event_id=uuid.uuid4(), kind="logo", upload=_upload(data, "image/png"))

    assert exc_info.value.status_code == 422


async def test_unrecognized_bytes_with_a_valid_declared_type_is_rejected(
    fake_settings: Callable[[int], SimpleNamespace],
) -> None:
    fake_settings(1024)
    data = b"this is not an image at all, just plain text padding here"

    with pytest.raises(HTTPException) as exc_info:
        await theme_images.save_theme_image(event_id=uuid.uuid4(), kind="logo", upload=_upload(data, "image/png"))

    assert exc_info.value.status_code == 422


async def test_oversized_upload_is_rejected(fake_settings: Callable[[int], SimpleNamespace]) -> None:
    fake_settings(10)
    data = _PNG_MAGIC + b"way more bytes than the 10 byte cap allows"

    with pytest.raises(HTTPException) as exc_info:
        await theme_images.save_theme_image(event_id=uuid.uuid4(), kind="logo", upload=_upload(data, "image/png"))

    assert exc_info.value.status_code == 413


async def test_empty_file_is_rejected(fake_settings: Callable[[int], SimpleNamespace]) -> None:
    fake_settings(1024)

    with pytest.raises(HTTPException) as exc_info:
        await theme_images.save_theme_image(event_id=uuid.uuid4(), kind="logo", upload=_upload(b"", "image/png"))

    assert exc_info.value.status_code == 422
    assert "empty" in str(exc_info.value.detail).lower()


# --- public_url_for / delete_theme_image ---


def test_public_url_for_none_path_returns_none() -> None:
    assert theme_images.public_url_for(None) is None


def test_public_url_for_prefixes_uploads_mount() -> None:
    assert theme_images.public_url_for("themes/abc/logo-x.png") == "/uploads/themes/abc/logo-x.png"


def test_delete_theme_image_is_a_noop_for_none() -> None:
    theme_images.delete_theme_image(None)  # must not raise


def test_delete_theme_image_removes_the_file(
    fake_settings: Callable[[int], SimpleNamespace], tmp_path: Path
) -> None:
    settings = fake_settings(1024)
    target_dir = Path(settings.uploads_dir) / "themes" / "some-event"
    target_dir.mkdir(parents=True)
    target_file = target_dir / "logo-x.png"
    target_file.write_bytes(_PNG_MAGIC)

    theme_images.delete_theme_image("themes/some-event/logo-x.png")

    assert not target_file.exists()


def test_delete_theme_image_is_a_noop_when_file_already_gone(
    fake_settings: Callable[[int], SimpleNamespace],
) -> None:
    fake_settings(1024)
    theme_images.delete_theme_image("themes/nonexistent/logo-x.png")  # must not raise
