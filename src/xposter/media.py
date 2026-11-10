"""Validate image attachments before any browser work starts.

X accepts four image types and has three limits that matter to a driver: at
most four images per post, 5 MiB per image (15 MiB for a GIF), and no mixing a
GIF with other images. Every check here is local and cheap, so a bad attachment
fails with an operator-actionable message instead of leaving a half-composed
thread in the browser.

The extension and the magic bytes must agree. A file renamed from ``.png`` to
``.jpg`` is a typical mistake, and X's uploader either rejects it late or shows
a broken preview; catching it here names the tweet and the file before Chrome
is even launched.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .errors import ValidationError

__all__ = [
    "MAX_GIF_BYTES",
    "MAX_IMAGE_BYTES",
    "MAX_MEDIA_PER_TWEET",
    "validate_media",
]

# X's composer limits (docs/research/web-client.md section 6).
MAX_MEDIA_PER_TWEET = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_GIF_BYTES = 15 * 1024 * 1024

_IMAGE_EXTENSIONS = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".gif": "gif",
    ".webp": "webp",
}
_VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".m4v", ".mov", ".webm", ".avi", ".mkv", ".ogv", ".mpeg", ".mpg"}
)
_KIND_LABEL = {
    "png": "PNG",
    "jpeg": "JPEG",
    "gif": "GIF",
    "webp": "WebP",
}


def _sniff(path: Path) -> str | None:
    """Identify the file by magic bytes, or ``None`` when it is not an image.

    ``video`` is returned separately so a video with an image extension gets
    the specific "not supported yet" message rather than a mismatch message.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return None
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"GIF8"):
        return "gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video"
    return None


def _mib(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MiB"


def validate_media(paths: Sequence[Path], *, tweet_number: int) -> None:
    """Raise :class:`ValidationError` for media X would refuse.

    The message always names the tweet and the offending file so the operator
    can fix the source without guessing. ``tweet_number`` is one-based, the
    same numbering used in the other validation errors.
    """
    if len(paths) > MAX_MEDIA_PER_TWEET:
        raise ValidationError(
            f"Tweet {tweet_number} has {len(paths)} media files; X allows at most "
            f"{MAX_MEDIA_PER_TWEET} images per post."
        )

    resolved: list[Path] = []
    for path in paths:
        target = path.expanduser()
        try:
            canonical = target.resolve()
        except OSError:
            canonical = target.absolute()
        if canonical in resolved:
            raise ValidationError(
                f"Tweet {tweet_number} lists the same media file more than once "
                f"({target}); attach each image once."
            )
        resolved.append(canonical)

    kinds: list[str] = []
    for path in paths:
        shown = path.expanduser()
        if not shown.exists():
            raise ValidationError(
                f"Tweet {tweet_number} references media {shown} that does not "
                "exist; fix the path before posting."
            )
        if not shown.is_file():
            raise ValidationError(
                f"Tweet {tweet_number} references media {shown}, which is not a "
                "regular file; point it at an image file."
            )
        suffix = shown.suffix.lower()
        if suffix in _VIDEO_EXTENSIONS:
            raise ValidationError(
                f"Tweet {tweet_number}: video upload is not supported yet "
                f"({shown}); attach a PNG, JPEG, GIF or WebP image instead."
            )
        try:
            size = shown.stat().st_size
        except OSError as exc:
            raise ValidationError(
                f"Tweet {tweet_number}: media {shown} could not be read ({exc}); "
                "check the file permissions."
            ) from exc
        if size == 0:
            raise ValidationError(
                f"Tweet {tweet_number}: media {shown} is empty (0 bytes); export "
                "a real image before posting."
            )

        sniffed = _sniff(shown)
        if sniffed == "video":
            raise ValidationError(
                f"Tweet {tweet_number}: video upload is not supported yet "
                f"({shown}); attach a PNG, JPEG, GIF or WebP image instead."
            )
        expected = _IMAGE_EXTENSIONS.get(suffix)
        if expected is None:
            raise ValidationError(
                f"Tweet {tweet_number}: media {shown} has extension "
                f"{suffix or '(none)'}; X accepts only PNG, JPEG, GIF or WebP images."
            )
        if sniffed is None:
            raise ValidationError(
                f"Tweet {tweet_number}: media {shown} is not a recognised image "
                "(its magic bytes match no PNG, JPEG, GIF or WebP file)."
            )
        if sniffed != expected:
            raise ValidationError(
                f"Tweet {tweet_number}: media {shown} has a {suffix} extension but "
                f"its contents are {_KIND_LABEL[sniffed]}; re-export or rename the file."
            )

        limit = MAX_GIF_BYTES if sniffed == "gif" else MAX_IMAGE_BYTES
        if size > limit:
            kib = "GIF" if sniffed == "gif" else "image"
            raise ValidationError(
                f"Tweet {tweet_number}: media {shown} is {_mib(size)}, over X's "
                f"{_mib(limit)} limit for a {kib}; shrink or re-encode it."
            )
        kinds.append(sniffed)

    if "gif" in kinds and len(kinds) > 1:
        raise ValidationError(
            f"Tweet {tweet_number} mixes a GIF with another image; X does not "
            "allow a GIF and other media in the same post."
        )
