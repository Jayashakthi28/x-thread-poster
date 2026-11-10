"""Local pacing rules: refuse to post too much, too fast, or at a rude hour.

These limits exist even when X would accept the post. They are the tool's own
brake: a handful of threads per week, a gap between threads, and no posting
while the operator is asleep. Every rule is evaluated against injected time and
the ledger, so a day boundary is always a *local* calendar day and tests never
depend on the machine clock.

An oversized thread (more tweets than the daily cap) can never be posted and
raises :class:`ValidationError`, not a pacing wait. A thread larger than the
hourly cap is allowed only when the rolling hour window is empty: an author
thread is published in one go, so splitting it to fit the hourly number would
change the content users see.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo

from .config import PacingPolicy
from .errors import ConfigurationError, PacingError, ValidationError
from .ledger import Ledger, utcnow


def _parse_ts(raw: str) -> datetime:
    """Parse a stored ISO timestamp; a naive value is treated as UTC."""
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _local_timezone() -> tzinfo:
    """The machine's local timezone, used for quiet hours and day boundaries."""
    return datetime.now().astimezone().tzinfo or UTC


class Pacer:
    """Answers "may this thread start now?" from local policy plus the ledger."""

    def __init__(
        self,
        policy: PacingPolicy,
        ledger: Ledger,
        *,
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        if policy.min_gap_between_threads_s < 0:
            raise ConfigurationError("min_gap_between_threads_s must be >= 0.")
        if policy.jitter < 0:
            raise ConfigurationError("jitter must be >= 0.")
        if policy.max_threads_per_day < 1:
            raise ConfigurationError("max_threads_per_day must be at least 1.")
        if policy.max_tweets_per_day < 1:
            raise ConfigurationError("max_tweets_per_day must be at least 1.")
        if policy.max_tweets_per_hour < 1:
            raise ConfigurationError("max_tweets_per_hour must be at least 1.")
        if policy.quiet_hours is not None:
            start, end = policy.quiet_hours
            if not (0 <= start < 24 and 0 <= end < 24):
                raise ConfigurationError("quiet_hours hours must be between 0 and 23.")
        self._policy = policy
        self._ledger = ledger
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()
        self._tz = tz if tz is not None else _local_timezone()

    def check(self, n_tweets: int) -> None:
        """Raise :class:`PacingError` for the longest binding wait, or return.

        ``retry_after_s`` always describes the rule that clears last, so a
        caller that sleeps that long and retries is not bounced by a second
        rule immediately afterwards.
        """
        self._validate(n_tweets)
        now = self._now()
        deadlines = self._deadlines(now, n_tweets)
        if not deadlines:
            return
        rule, until = max(deadlines, key=lambda item: item[1])
        retry_after_s = (until - now).total_seconds()
        if retry_after_s <= 0:
            return
        raise PacingError(self._message(rule, until, retry_after_s), retry_after_s)

    def next_allowed(self, n_tweets: int) -> datetime:
        """Earliest UTC time at which :meth:`check` would pass.

        Rules are re-evaluated at each candidate because pushing past one
        deadline can cross a local midnight or land inside quiet hours.
        """
        self._validate(n_tweets)
        at = self._now()
        for _ in range(32):
            deadlines = self._deadlines(at, n_tweets)
            if not deadlines:
                return at.astimezone(UTC)
            latest = max(until for _, until in deadlines)
            if latest <= at:
                return at.astimezone(UTC)
            at = latest
        return at.astimezone(UTC)

    def record_thread_done(self, n_tweets: int) -> datetime:
        """Persist a jittered earliest next-thread time and return it.

        The random spread keeps the gap from looking metronomic. ``n_tweets``
        is accepted for symmetry with :meth:`check`; the current policy gap is
        independent of thread size.
        """
        if n_tweets < 1:
            raise ValidationError("A completed thread needs at least one tweet.")
        gap_s = self._policy.min_gap_between_threads_s * (
            1.0 + self._rng.uniform(0.0, self._policy.jitter)
        )
        when = self._now() + timedelta(seconds=gap_s)
        self._ledger.set_not_before(when)
        return when

    def _validate(self, n_tweets: int) -> None:
        if n_tweets < 1:
            raise ValidationError("A thread needs at least one tweet.")
        if n_tweets > self._policy.max_tweets_per_day:
            raise ValidationError(
                f"A thread of {n_tweets} tweets can never fit the daily cap of "
                f"{self._policy.max_tweets_per_day}; split it or raise max_tweets_per_day."
            )

    def _deadlines(self, at: datetime, n_tweets: int) -> list[tuple[str, datetime]]:
        policy = self._policy
        deadlines: list[tuple[str, datetime]] = []

        last_done = self._ledger.last_thread_completed_at()
        if last_done is not None:
            deadline = last_done + timedelta(seconds=policy.min_gap_between_threads_s)
            if deadline > at:
                deadlines.append(("minimum gap between threads (min gap)", deadline))

        persisted = self._ledger.not_before()
        if persisted is not None and persisted > at:
            deadlines.append(("persisted not_before", persisted))

        if self._threads_today(at) >= policy.max_threads_per_day:
            deadlines.append(("daily thread cap", self._next_local_midnight(at)))

        if self._tweets_today(at) + n_tweets > policy.max_tweets_per_day:
            deadlines.append(("daily tweet cap", self._next_local_midnight(at)))

        window = self._hour_window(at)
        used = sum(count for _, count in window)
        if used + n_tweets > policy.max_tweets_per_hour and used > 0:
            newest = max(when for when, _ in window)
            deadlines.append(("hourly tweet cap", newest + timedelta(hours=1)))

        quiet_until = self._quiet_deadline(at)
        if quiet_until is not None:
            deadlines.append(("quiet hours", quiet_until))

        return deadlines

    def _threads_today(self, at: datetime) -> int:
        start, end = self._local_day_bounds(at)
        completed = 0
        for entry in self._ledger.all_entries():
            if entry.completed is None:
                continue
            try:
                posted_at = _parse_ts(entry.completed.posted_at)
            except ValueError:
                continue
            if start <= posted_at < end:
                completed += 1
        thread_events = sum(
            1
            for when, _, kind in self._ledger.events_since(start)
            if kind == "thread" and when < end
        )
        return completed + thread_events

    def _tweets_today(self, at: datetime) -> int:
        start, end = self._local_day_bounds(at)
        return sum(
            count
            for when, count, kind in self._ledger.events_since(start)
            if kind == "tweet" and when < end
        )

    def _hour_window(self, at: datetime) -> list[tuple[datetime, int]]:
        start = at - timedelta(hours=1)
        return [
            (when, count)
            for when, count, kind in self._ledger.events_since(start)
            if kind == "tweet" and start < when <= at
        ]

    def _local_day_bounds(self, at: datetime) -> tuple[datetime, datetime]:
        local = at.astimezone(self._tz)
        start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = (start_local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return start_local.astimezone(UTC), end_local.astimezone(UTC)

    def _next_local_midnight(self, at: datetime) -> datetime:
        _, end = self._local_day_bounds(at)
        return end

    def _quiet_deadline(self, at: datetime) -> datetime | None:
        quiet = self._policy.quiet_hours
        if quiet is None:
            return None
        start, end = quiet
        if start == end:
            return None
        local = at.astimezone(self._tz)
        hour = local.hour
        inside = start <= hour < end if start < end else hour >= start or hour < end
        if not inside:
            return None
        days = 1 if start > end and hour >= start else 0
        end_local = (local + timedelta(days=days)).replace(
            hour=end, minute=0, second=0, microsecond=0
        )
        return end_local.astimezone(UTC)

    def _message(self, rule: str, until: datetime, retry_after_s: float) -> str:
        stamp = until.astimezone(self._tz).strftime("%Y-%m-%d %H:%M")
        return (
            f"Local pacing rule '{rule}' blocks this post; "
            f"next allowed at {stamp} local time (in {math.ceil(retry_after_s)} s)."
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Pacer clock must return a timezone-aware datetime.")
        return value.astimezone(UTC)
