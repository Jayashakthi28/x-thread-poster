"""Immutable value objects shared by every xposter layer.

The models are pure data: they carry no browser or filesystem behaviour, so the
ledger, the CLI and the posting pipeline can all agree on one representation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .errors import ValidationError

_RECORD_SEP = "\x1e"
_MISSING_MEDIA_SIZE = -1


def _media_size(path: Path) -> int:
    """Return a media file's size, or ``-1`` when it is missing/unreadable.

    Hashing must never fail because a file has not been downloaded yet; the
    sentinel keeps the hash stable and still distinguishes "absent" from a real
    file of any size. Only ``stat`` is consulted, never the file bytes.
    """
    try:
        return path.stat().st_size
    except OSError:
        return _MISSING_MEDIA_SIZE


@dataclass(frozen=True, slots=True)
class Tweet:
    """One tweet in a thread, with optional media and matching alt texts."""

    text: str
    media: tuple[Path, ...] = ()
    alt_texts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Thread:
    """A complete thread plus the key that identifies its intended content.

    ``content_hash`` covers the key, each tweet's text, each media file's name
    and byte size, and every alt text. Sizes are read with ``stat`` only, never
    by reading bytes: a re-downloaded image with the same name and size hashes
    identically, while an image replaced under the same name changes the hash.
    Alt text is included because it changes what readers (and screen readers)
    actually receive.
    """

    tweets: tuple[Tweet, ...]
    key: str = ""

    def __post_init__(self) -> None:
        if not self.tweets:
            raise ValidationError(
                "A thread needs at least one tweet; add tweet text before posting."
            )

    @property
    def content_hash(self) -> str:
        """SHA-256 over the key, tweet text, media name+size and alt text."""
        parts: list[str] = ["xposter-thread-v2", self.key]
        for tweet in self.tweets:
            parts.append("tweet")
            parts.append(tweet.text)
            for media in tweet.media:
                parts.append("media")
                parts.append(media.name)
                parts.append(str(_media_size(media)))
            for alt_text in tweet.alt_texts:
                parts.append("alt")
                parts.append(alt_text)
        payload = _RECORD_SEP.join(f"{len(part)}:{part}" for part in parts)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def texts(self) -> tuple[str, ...]:
        """Tweet texts in order, without media."""
        return tuple(tweet.text for tweet in self.tweets)


@dataclass(frozen=True, slots=True)
class PostedTweet:
    """One tweet known to exist on X, as recorded in the ledger."""

    index: int
    id: str
    url: str
    text_sha256: str

    def to_json(self) -> dict[str, object]:
        """Plain JSON-ready mapping."""
        return {
            "index": self.index,
            "id": self.id,
            "url": self.url,
            "text_sha256": self.text_sha256,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> PostedTweet:
        """Rebuild a record written by :meth:`to_json`."""
        return cls(
            index=cast(int, data["index"]),
            id=cast(str, data["id"]),
            url=cast(str, data["url"]),
            text_sha256=cast(str, data["text_sha256"]),
        )


@dataclass(frozen=True, slots=True)
class PostedThread:
    """Ledger record of a thread that was posted, possibly only partially."""

    thread_hash: str
    handle: str
    strategy: str
    posted_at: str
    tweets: tuple[PostedTweet, ...]
    complete: bool

    @property
    def first_url(self) -> str:
        """URL of the first posted tweet, or an empty string when none were posted."""
        return self.tweets[0].url if self.tweets else ""

    def to_json(self) -> dict[str, object]:
        """Plain JSON-ready mapping, with nested tweets as a list."""
        return {
            "thread_hash": self.thread_hash,
            "handle": self.handle,
            "strategy": self.strategy,
            "posted_at": self.posted_at,
            "tweets": [tweet.to_json() for tweet in self.tweets],
            "complete": self.complete,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> PostedThread:
        """Rebuild a record written by :meth:`to_json`."""
        raw_tweets = cast(list[Mapping[str, object]], data["tweets"])
        return cls(
            thread_hash=cast(str, data["thread_hash"]),
            handle=cast(str, data["handle"]),
            strategy=cast(str, data["strategy"]),
            posted_at=cast(str, data["posted_at"]),
            tweets=tuple(PostedTweet.from_json(item) for item in raw_tweets),
            complete=cast(bool, data["complete"]),
        )


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Cookie-free view of the saved session, safe to show in the portal and logs.

    Only booleans, timestamps and the handle are exposed; cookie values never
    enter this object.
    """

    present: bool
    has_auth_token: bool
    has_ct0: bool
    auth_expires_at: str | None
    days_left: int | None
    expired: bool
    handle: str | None
    last_verified_at: str | None
    parked: bool

    def to_json(self) -> dict[str, object]:
        """Plain JSON-ready mapping."""
        return {
            "present": self.present,
            "has_auth_token": self.has_auth_token,
            "has_ct0": self.has_ct0,
            "auth_expires_at": self.auth_expires_at,
            "days_left": self.days_left,
            "expired": self.expired,
            "handle": self.handle,
            "last_verified_at": self.last_verified_at,
            "parked": self.parked,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> SessionSummary:
        """Rebuild a record written by :meth:`to_json`."""
        return cls(
            present=cast(bool, data["present"]),
            has_auth_token=cast(bool, data["has_auth_token"]),
            has_ct0=cast(bool, data["has_ct0"]),
            auth_expires_at=cast(str | None, data.get("auth_expires_at")),
            days_left=cast(int | None, data.get("days_left")),
            expired=cast(bool, data["expired"]),
            handle=cast(str | None, data.get("handle")),
            last_verified_at=cast(str | None, data.get("last_verified_at")),
            parked=cast(bool, data["parked"]),
        )
