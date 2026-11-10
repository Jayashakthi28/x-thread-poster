"""Validation of image attachments before any browser work starts.

Fixtures are generated with stdlib magic bytes; no Pillow and no real camera
files are needed. Oversized files are sparse so the test suite never writes
several megabytes to disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xposter.errors import ValidationError
from xposter.media import MAX_GIF_BYTES, MAX_IMAGE_BYTES, validate_media

_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF"
_GIF = b"GIF89a"
_WEBP = b"RIFF" + (32).to_bytes(4, "little") + b"WEBP"
_MP4 = b"\x00\x00\x00\x18ftypmp42"


def make_image(tmp_path: Path, name: str, magic: bytes, size: int = 64) -> Path:
    path = tmp_path / name
    path.write_bytes(magic + b"\x00" * max(0, size - len(magic)))
    return path


def make_sparse(tmp_path: Path, name: str, magic: bytes, size: int) -> Path:
    path = tmp_path / name
    with path.open("wb") as handle:
        handle.write(magic)
        handle.truncate(size)
    return path


@pytest.mark.parametrize(
    ("name", "magic"),
    [
        ("pixel.png", _PNG),
        ("pixel.jpg", _JPEG),
        ("pixel.jpeg", _JPEG),
        ("pixel.gif", _GIF),
        ("pixel.webp", _WEBP),
    ],
)
def test_accepts_the_four_supported_image_types(tmp_path: Path, name: str, magic: bytes) -> None:
    validate_media([make_image(tmp_path, name, magic)], tweet_number=1)


def test_rejects_extension_and_magic_disagreement(tmp_path: Path) -> None:
    path = make_image(tmp_path, "renamed.jpg", _PNG)

    with pytest.raises(ValidationError, match=r"Tweet 2.*renamed\.jpg.*contents are PNG"):
        validate_media([path], tweet_number=2)


def test_rejects_unrecognised_bytes(tmp_path: Path) -> None:
    path = make_image(tmp_path, "not-an-image.png", b"plain text")

    with pytest.raises(ValidationError, match="magic bytes"):
        validate_media([path], tweet_number=1)


def test_rejects_video_extensions_with_a_clear_message(tmp_path: Path) -> None:
    path = make_image(tmp_path, "clip.mp4", _MP4)

    with pytest.raises(ValidationError, match="video upload is not supported yet"):
        validate_media([path], tweet_number=1)


def test_rejects_video_magic_behind_an_image_extension(tmp_path: Path) -> None:
    path = make_image(tmp_path, "clip.png", _MP4)

    with pytest.raises(ValidationError, match="video upload is not supported yet"):
        validate_media([path], tweet_number=1)


def test_image_size_limit_is_five_mib(tmp_path: Path) -> None:
    too_big = make_sparse(tmp_path, "big.png", _PNG, MAX_IMAGE_BYTES + 1)

    with pytest.raises(ValidationError, match="over X's 5.0 MiB limit"):
        validate_media([too_big], tweet_number=3)

    at_limit = make_sparse(tmp_path, "limit.png", _PNG, MAX_IMAGE_BYTES)
    validate_media([at_limit], tweet_number=3)


def test_gif_size_limit_is_fifteen_mib(tmp_path: Path) -> None:
    too_big = make_sparse(tmp_path, "big.gif", _GIF, MAX_GIF_BYTES + 1)

    with pytest.raises(ValidationError, match="over X's 15.0 MiB limit"):
        validate_media([too_big], tweet_number=1)

    at_limit = make_sparse(tmp_path, "limit.gif", _GIF, MAX_GIF_BYTES)
    validate_media([at_limit], tweet_number=1)


def test_rejects_a_gif_mixed_with_another_image(tmp_path: Path) -> None:
    paths = [
        make_image(tmp_path, "a.gif", _GIF),
        make_image(tmp_path, "b.png", _PNG),
    ]

    with pytest.raises(ValidationError, match="mixes a GIF"):
        validate_media(paths, tweet_number=1)


def test_rejects_zero_byte_files(tmp_path: Path) -> None:
    path = tmp_path / "empty.png"
    path.write_bytes(b"")

    with pytest.raises(ValidationError, match="empty"):
        validate_media([path], tweet_number=1)


def test_rejects_duplicate_paths(tmp_path: Path) -> None:
    path = make_image(tmp_path, "same.png", _PNG)
    alias = tmp_path / "." / "same.png"

    with pytest.raises(ValidationError, match="same media file more than once"):
        validate_media([path, alias], tweet_number=1)


def test_rejects_more_than_four_images(tmp_path: Path) -> None:
    paths = [make_image(tmp_path, f"{index}.png", _PNG) for index in range(5)]

    with pytest.raises(ValidationError, match="at most 4"):
        validate_media(paths, tweet_number=4)


def test_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        validate_media([tmp_path / "gone.png"], tweet_number=1)
