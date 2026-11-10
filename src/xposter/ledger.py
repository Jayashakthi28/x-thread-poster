"""JSON ledger: proves what was posted, and survives a crash mid-click.

The ledger is the single source of truth for idempotency. A thread is written
as ``PENDING`` after the session is verified and before the composer is
touched, a click barrier (``clicking_at``) is fsynced immediately before the
Post button is clicked, every tweet id is recorded as soon as it exists, and
an ``UNKNOWN`` row quarantines a click whose outcome cannot be verified.
``history`` and ``previous_completed`` keep earlier attempts visible even
after a forced re-run.

Losing that evidence would let a rerun double-post, so every write is atomic:
temp file, file fsync, ``os.replace``, then the parent-directory fsync, with
mode 0600. A corrupt ledger is never silently discarded: it raises
:class:`LedgerCorruptError` until the operator inspects it or explicitly calls
:meth:`Ledger.reset_corrupt`.

Process-level exclusion is the caller's job (the session lock); this class
does no file locking of its own. Every method re-reads the file, which is
cheap at this volume and keeps separate ``Ledger`` instances from serving
stale snapshots.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from .errors import LedgerCorruptError
from .models import PostedThread, PostedTweet

_MAX_ERROR_CHARS = 500
_LEDGER_VERSION = 2

_SECRET_RE = re.compile(
    r"(?i)\b(?P<key>auth_token|ct0|password|secret|api[_-]?key)\b(?P<sep>\s*[:=]\s*)\S+"
    r"|\b(?P<scheme>bearer)\b(?P<space>\s+)\S+"
)


def utcnow() -> datetime:
    """Return the current time as an aware UTC datetime.

    This is the default clock for the ledger and the pacer. Tests inject a
    fake clock instead, so no timing logic reads the wall clock implicitly.
    """
    return datetime.now(UTC)


def _dt_to_str(value: datetime) -> str:
    """Serialise an aware datetime as ISO 8601 in UTC."""
    if value.tzinfo is None:
        raise ValueError("Refusing to serialise a naive datetime; pass an aware UTC datetime.")
    return value.astimezone(UTC).isoformat()


def _parse_dt(raw: str) -> datetime:
    """Parse a stored timestamp, treating a naive value as UTC."""
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _redact(match: re.Match[str]) -> str:
    key = match.group("key")
    if key is not None:
        return f"{key}{match.group('sep')}[redacted]"
    return f"{match.group('scheme')}{match.group('space')}[redacted]"


def _clean_error(raw: str) -> str:
    """Redact credential-shaped fragments and truncate to 500 characters.

    Error text is persisted and may be shown in logs or a portal, so a stray
    cookie value must never survive a crash. Callers should still pass a
    redacted message; this is the last line of defence, not the only one.
    """
    return _SECRET_RE.sub(_redact, raw)[:_MAX_ERROR_CHARS]


def _fsync_dir(path: Path) -> None:
    """Make the preceding rename durable; some platforms refuse to fsync dirs."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(descriptor)
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)


class LedgerStatus(StrEnum):
    """Lifecycle of one thread in the ledger."""

    PENDING = "pending"
    POSTED = "posted"
    UNKNOWN = "unknown"
    FAILED = "failed"


class Decision(StrEnum):
    """What a caller should do with a thread, given the ledger."""

    NEW = "new"
    ALREADY_POSTED = "already_posted"
    RESUME = "resume"
    BLOCKED_UNKNOWN = "blocked_unknown"


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """Immutable snapshot of one thread's ledger record."""

    thread_hash: str
    status: LedgerStatus
    strategy: str
    handle: str
    n_tweets: int
    started_at: str
    updated_at: str
    posted: tuple[PostedTweet, ...]
    error: str
    completed: PostedThread | None
    clicking_at: str = ""
    history: tuple[str, ...] = ()
    previous_completed: PostedThread | None = None


@dataclass(slots=True)
class _State:
    """Mutable on-disk shape, rebuilt on every read and written atomically."""

    entries: dict[str, LedgerEntry] = field(default_factory=dict)
    events: list[tuple[datetime, int, str]] = field(default_factory=list)
    not_before: datetime | None = None


def _entry_to_json(entry: LedgerEntry) -> dict[str, object]:
    return {
        "thread_hash": entry.thread_hash,
        "status": entry.status.value,
        "strategy": entry.strategy,
        "handle": entry.handle,
        "n_tweets": entry.n_tweets,
        "started_at": entry.started_at,
        "updated_at": entry.updated_at,
        "posted": [tweet.to_json() for tweet in entry.posted],
        "error": entry.error,
        "completed": entry.completed.to_json() if entry.completed is not None else None,
        "clicking_at": entry.clicking_at,
        "history": list(entry.history),
        "previous_completed": (
            entry.previous_completed.to_json() if entry.previous_completed is not None else None
        ),
    }


def _entry_from_json(raw: Mapping[str, Any]) -> LedgerEntry:
    posted_raw = raw.get("posted", [])
    posted: tuple[PostedTweet, ...] = ()
    if isinstance(posted_raw, list):
        posted = tuple(
            PostedTweet.from_json(cast(Mapping[str, object], item))
            for item in posted_raw
            if isinstance(item, dict)
        )
    completed_raw = raw.get("completed")
    completed = (
        PostedThread.from_json(cast(Mapping[str, object], completed_raw))
        if isinstance(completed_raw, dict)
        else None
    )
    previous_completed_raw = raw.get("previous_completed")
    previous_completed = (
        PostedThread.from_json(cast(Mapping[str, object], previous_completed_raw))
        if isinstance(previous_completed_raw, dict)
        else None
    )
    history_raw = raw.get("history", [])
    history = (
        tuple(item for item in history_raw if isinstance(item, str))
        if isinstance(history_raw, list)
        else ()
    )
    return LedgerEntry(
        thread_hash=str(raw["thread_hash"]),
        status=LedgerStatus(str(raw["status"])),
        strategy=str(raw.get("strategy", "")),
        handle=str(raw.get("handle", "")),
        n_tweets=int(raw.get("n_tweets", len(posted))),
        started_at=str(raw.get("started_at", "")),
        updated_at=str(raw.get("updated_at", "")),
        posted=posted,
        error=str(raw.get("error", "")),
        completed=completed,
        clicking_at=str(raw.get("clicking_at", "")),
        history=history,
        previous_completed=previous_completed,
    )


class Ledger:
    """Atomic JSON store of thread outcomes and pacing events.

    The file is deliberately boring: a version, a mapping keyed by thread
    content hash, a flat event list and one optional ``not_before`` timestamp.
    A non-empty file that cannot be parsed raises :class:`LedgerCorruptError`
    instead of being discarded, because its entries are the only local proof of
    what was posted. ``reset_corrupt()`` is the explicit operator override.
    """

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._path = path
        self._clock = clock

    @property
    def path(self) -> Path:
        """Location of the JSON file (useful for logs and tests)."""
        return self._path

    def get(self, thread_hash: str) -> LedgerEntry | None:
        """Return the recorded entry for ``thread_hash``, if any."""
        return self._load().entries.get(thread_hash)

    def decide(
        self,
        thread_hash: str,
        *,
        force: bool = False,
        discard_recorded: bool = False,
    ) -> Decision:
        """Classify a thread before any browser work happens.

        ``force`` re-opens a finished or unknown thread but never loses proven
        tweet ids: an unknown thread with recorded tweets still resumes.
        ``discard_recorded`` is the operator's explicit assertion that those
        partial tweets were deleted by hand; only then may a partial unknown
        thread start over as NEW. It implies the forced path and is ignored on
        non-partial entries.
        """
        entry = self.get(thread_hash)
        if entry is None:
            return Decision.NEW
        if entry.status is LedgerStatus.POSTED:
            return Decision.NEW if force else Decision.ALREADY_POSTED
        unknown_like = entry.status is LedgerStatus.UNKNOWN or (
            entry.status is LedgerStatus.PENDING and bool(entry.clicking_at)
        )
        if unknown_like:
            if entry.posted:
                if discard_recorded:
                    return Decision.NEW
                return Decision.RESUME if force else Decision.BLOCKED_UNKNOWN
            return Decision.NEW if force else Decision.BLOCKED_UNKNOWN
        if entry.status is LedgerStatus.PENDING:
            return Decision.RESUME if entry.posted else Decision.NEW
        return Decision.RESUME if entry.posted else Decision.NEW

    def begin(
        self,
        thread_hash: str,
        *,
        strategy: str,
        handle: str,
        n_tweets: int,
        discard_recorded: bool = False,
    ) -> LedgerEntry:
        """Write PENDING before any click, so a crash leaves evidence.

        Posted tweets survive every prior status except a fresh run: a POSTED
        entry being force-reposted, or ``discard_recorded`` after the operator
        deleted the partial thread by hand. A previous ``completed`` record is
        copied into ``previous_completed`` and summarised in ``history`` rather
        than being lost.
        """
        state = self._load()
        now = _dt_to_str(self._now())
        existing = state.entries.get(thread_hash)
        fresh = existing is None or existing.status is LedgerStatus.POSTED or discard_recorded
        if existing is None:
            posted: tuple[PostedTweet, ...] = ()
            started_at = now
            history: tuple[str, ...] = ()
            previous_completed: PostedThread | None = None
        else:
            posted = () if fresh else existing.posted
            started_at = now if fresh else existing.started_at
            history = existing.history
            if existing.posted or existing.completed is not None:
                ordered = sorted(existing.posted, key=lambda item: item.index)
                ids = ",".join(item.id for item in ordered) or "none"
                prefix = "forced re-run" if fresh else "resume"
                history = (
                    *history,
                    f"{prefix} at {now}: previous status {existing.status.value}, ids {ids}",
                )
            previous_completed = existing.previous_completed
            if existing.completed is not None:
                previous_completed = existing.completed
        entry = LedgerEntry(
            thread_hash=thread_hash,
            status=LedgerStatus.PENDING,
            strategy=strategy,
            handle=handle,
            n_tweets=n_tweets,
            started_at=started_at,
            updated_at=now,
            posted=posted,
            error="",
            completed=None,
            clicking_at="",
            history=history,
            previous_completed=previous_completed,
        )
        state.entries[thread_hash] = entry
        self._save(state)
        return entry

    def record_tweet(self, thread_hash: str, tweet: PostedTweet) -> LedgerEntry:
        """Record one posted tweet immediately and count a pacing event.

        Replaying the same index (a crash between the click and the write) is
        idempotent: the record is replaced, no second event is counted.
        """
        state = self._load()
        existing = state.entries.get(thread_hash)
        if existing is None:
            raise KeyError(
                f"Thread {thread_hash} was not begun; call begin() before record_tweet()."
            )
        now = self._now()
        already_recorded = any(item.index == tweet.index for item in existing.posted)
        merged = [item for item in existing.posted if item.index != tweet.index]
        merged.append(tweet)
        posted = tuple(sorted(merged, key=lambda item: item.index))
        status = (
            existing.status
            if existing.status in (LedgerStatus.POSTED, LedgerStatus.UNKNOWN)
            else LedgerStatus.PENDING
        )
        entry = replace(
            existing,
            status=status,
            updated_at=_dt_to_str(now),
            posted=posted,
            n_tweets=max(existing.n_tweets, len(posted)),
        )
        state.entries[thread_hash] = entry
        if not already_recorded:
            state.events.append((now, 1, "tweet"))
        self._save(state)
        return entry

    def complete(self, thread_hash: str, posted: PostedThread) -> LedgerEntry:
        """Mark a thread POSTED with the provider's final record."""
        state = self._load()
        now = _dt_to_str(self._now())
        existing = state.entries.get(thread_hash)
        tweets = tuple(posted.tweets) or (existing.posted if existing is not None else ())
        entry = LedgerEntry(
            thread_hash=thread_hash,
            status=LedgerStatus.POSTED,
            strategy=posted.strategy or (existing.strategy if existing is not None else ""),
            handle=posted.handle or (existing.handle if existing is not None else ""),
            n_tweets=len(tweets) if tweets else (existing.n_tweets if existing is not None else 0),
            started_at=(
                existing.started_at
                if existing is not None and existing.started_at
                else posted.posted_at
            ),
            updated_at=now,
            posted=tweets,
            error="",
            completed=posted,
            clicking_at="",
            history=existing.history if existing is not None else (),
            previous_completed=(existing.previous_completed if existing is not None else None),
        )
        state.entries[thread_hash] = entry
        self._save(state)
        return entry

    def mark_clicking(self, thread_hash: str) -> LedgerEntry:
        """Persist and fsync the click barrier *before* Post is clicked.

        A crash after the click now leaves ``PENDING`` with ``clicking_at`` set,
        which :meth:`decide` treats like UNKNOWN. Clearing it is only allowed
        when the poster proved that no request was dispatched.
        """
        state = self._load()
        existing = state.entries.get(thread_hash)
        if existing is None:
            raise KeyError(
                f"Thread {thread_hash} was not begun; call begin() before mark_clicking()."
            )
        now = _dt_to_str(self._now())
        entry = replace(existing, clicking_at=now, updated_at=now)
        state.entries[thread_hash] = entry
        self._save(state)
        return entry

    def clear_clicking(self, thread_hash: str) -> LedgerEntry:
        """Clear the click barrier after proving nothing was dispatched.

        This covers a click that failed before any request left the browser
        (missing box, covered button); it must never be used when the click may
        have landed.
        """
        state = self._load()
        existing = state.entries.get(thread_hash)
        if existing is None:
            raise KeyError(
                f"Thread {thread_hash} was not begun; call begin() before clear_clicking()."
            )
        entry = replace(existing, clicking_at="", updated_at=_dt_to_str(self._now()))
        state.entries[thread_hash] = entry
        self._save(state)
        return entry

    def reset_corrupt(self) -> Path:
        """Move an unreadable ledger to a timestamped ``.corrupt-<ts>`` file.

        Only an operator can accept losing the recorded history, so the CLI
        asks for confirmation before calling this. Returns the new path.
        """
        stamp = self._now().strftime("%Y%m%dT%H%M%S%fZ")
        target = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        os.replace(self._path, target)
        _fsync_dir(self._path.parent)
        return target

    def mark_unknown(self, thread_hash: str, note: str) -> LedgerEntry:
        """Quarantine a thread whose click may have landed; blocks blind retry."""
        return self._mark(thread_hash, LedgerStatus.UNKNOWN, note)

    def mark_failed(self, thread_hash: str, error: str) -> LedgerEntry:
        """Record a failure; a thread with posted tweets stays resumable."""
        return self._mark(thread_hash, LedgerStatus.FAILED, error)

    def resume_from(self, thread_hash: str) -> tuple[PostedTweet, ...]:
        """Return already-posted tweets in index order, ready to skip on resume."""
        entry = self.get(thread_hash)
        if entry is None:
            return ()
        return tuple(sorted(entry.posted, key=lambda item: item.index))

    def add_event(self, *, tweets: int = 1, kind: str = "tweet") -> None:
        """Append a pacing event (UTC timestamp, tweet count, kind).

        ``kind="thread"`` events count threads for the daily cap; the ledger
        also counts completed entries, so normal callers need not emit them.
        """
        if tweets < 0:
            raise ValueError("A pacing event cannot have a negative tweet count.")
        if not kind:
            raise ValueError("A pacing event needs a non-empty kind.")
        state = self._load()
        state.events.append((self._now(), tweets, kind))
        self._save(state)

    def events_since(self, since: datetime) -> list[tuple[datetime, int, str]]:
        """Return events at or after ``since`` as ``(utc_ts, tweets, kind)``."""
        if since.tzinfo is None:
            raise ValueError("events_since() needs an aware datetime.")
        cutoff = since.astimezone(UTC)
        return [event for event in self._load().events if event[0] >= cutoff]

    def last_thread_completed_at(self) -> datetime | None:
        """Latest completion timestamp across POSTED entries, or ``None``."""
        latest: datetime | None = None
        for entry in self._load().entries.values():
            if entry.completed is None:
                continue
            try:
                when = _parse_dt(entry.completed.posted_at)
            except ValueError:
                continue
            if latest is None or when > latest:
                latest = when
        return latest

    def not_before(self) -> datetime | None:
        """Persisted earliest time the next thread may start."""
        return self._load().not_before

    def set_not_before(self, when: datetime | None) -> None:
        """Persist the jittered earliest next-thread time (``None`` clears it)."""
        state = self._load()
        state.not_before = None if when is None else when.astimezone(UTC)
        self._save(state)

    def prune(self, keep_days: int = 120) -> None:
        """Drop old pacing events and old FAILED entries.

        POSTED, UNKNOWN and PENDING entries are never removed: they are the
        duplicate-post evidence and the quarantine markers. A FAILED entry that
        carries ``previous_completed`` is kept too, because that field may be
        the only remaining proof of an earlier successful post.
        """
        if keep_days < 0:
            raise ValueError("keep_days must be >= 0.")
        cutoff = self._now() - timedelta(days=keep_days)
        state = self._load()
        state.events = [event for event in state.events if event[0] >= cutoff]
        kept: dict[str, LedgerEntry] = {}
        for thread_hash, entry in state.entries.items():
            if entry.status is LedgerStatus.FAILED and entry.previous_completed is None:
                try:
                    stale = _parse_dt(entry.updated_at) < cutoff
                except ValueError:
                    stale = False
                if stale:
                    continue
            kept[thread_hash] = entry
        state.entries = kept
        self._save(state)

    def all_entries(self) -> list[LedgerEntry]:
        """Every entry, ordered by start time then hash for stable output."""
        return sorted(
            self._load().entries.values(), key=lambda item: (item.started_at, item.thread_hash)
        )

    def _mark(self, thread_hash: str, status: LedgerStatus, note: str) -> LedgerEntry:
        state = self._load()
        now = _dt_to_str(self._now())
        existing = state.entries.get(thread_hash)
        if existing is None:
            entry = LedgerEntry(
                thread_hash=thread_hash,
                status=status,
                strategy="",
                handle="",
                n_tweets=0,
                started_at=now,
                updated_at=now,
                posted=(),
                error=_clean_error(note),
                completed=None,
            )
        else:
            entry = replace(
                existing,
                status=status,
                updated_at=now,
                error=_clean_error(note),
            )
        state.entries[thread_hash] = entry
        self._save(state)
        return entry

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Ledger clock must return a timezone-aware datetime.")
        return value.astimezone(UTC)

    def _load(self) -> _State:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _State()
        except UnicodeDecodeError:
            raise self._corrupt_error() from None
        if not text:
            return _State()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            raise self._corrupt_error() from None
        if not isinstance(raw, dict):
            raise self._corrupt_error()
        data = cast(dict[str, Any], raw)
        state = _State()
        entries_raw = data.get("entries")
        if isinstance(entries_raw, dict):
            for key, item in cast(dict[str, Any], entries_raw).items():
                if not isinstance(item, dict):
                    continue
                try:
                    entry = _entry_from_json(cast(Mapping[str, Any], item))
                except (KeyError, TypeError, ValueError):
                    continue
                state.entries[str(key)] = entry
        events_raw = data.get("events")
        if isinstance(events_raw, list):
            for item in events_raw:
                if not isinstance(item, dict):
                    continue
                event = cast(dict[str, Any], item)
                tweets = event.get("tweets", 1)
                kind = event.get("kind", "tweet")
                if isinstance(tweets, bool) or not isinstance(tweets, int) or tweets < 0:
                    continue
                if not isinstance(kind, str) or not kind:
                    continue
                try:
                    when = _parse_dt(str(event["ts"]))
                except (KeyError, TypeError, ValueError):
                    continue
                state.events.append((when, tweets, kind))
        state.events.sort(key=lambda item: item[0])
        not_before_raw = data.get("not_before")
        if isinstance(not_before_raw, str):
            try:
                state.not_before = _parse_dt(not_before_raw)
            except ValueError:
                state.not_before = None
        return state

    def _save(self, state: _State) -> None:
        payload: dict[str, object] = {
            "version": _LEDGER_VERSION,
            "entries": {
                thread_hash: _entry_to_json(entry) for thread_hash, entry in state.entries.items()
            },
            "events": [
                {"ts": _dt_to_str(when), "tweets": count, "kind": kind}
                for when, count, kind in state.events
            ],
            "not_before": _dt_to_str(state.not_before) if state.not_before is not None else None,
        }
        data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self._path)
            _fsync_dir(self._path.parent)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                temp_path.unlink()
            raise

    def _corrupt_error(self) -> LedgerCorruptError:
        return LedgerCorruptError(
            f"The posting ledger {self._path} exists but cannot be read. It is the only "
            "proof of what has already been posted, so this run is blocked rather than "
            "risk posting a duplicate. Inspect or restore the file, or run "
            "`xposter ledger --reset-corrupt` to move it aside and start a fresh ledger.",
            path=self._path,
        )
