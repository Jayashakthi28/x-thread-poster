"""Tests for local pacing: every rule alone, combined, and next_allowed()."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from xposter.config import PacingPolicy
from xposter.errors import ConfigurationError, PacingError, ValidationError
from xposter.ledger import Ledger
from xposter.models import PostedThread, PostedTweet
from xposter.pacing import Pacer

TZ = timezone(timedelta(hours=5, minutes=30))
START = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)  # 17:30 local


class FakeClock:
    """Mutable injected clock; tests assign or advance ``now``."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def lax_policy(**overrides: object) -> PacingPolicy:
    """A policy whose only active rule is the one a test overrides."""
    values: dict[str, object] = {
        "min_gap_between_threads_s": 0.0,
        "max_threads_per_day": 100,
        "max_tweets_per_day": 1000,
        "max_tweets_per_hour": 1000,
        "quiet_hours": None,
        "jitter": 0.0,
    }
    values.update(overrides)
    return PacingPolicy(**values)


def make_ledger(tmp_path: Path, clock: FakeClock) -> Ledger:
    return Ledger(tmp_path / "ledger.json", clock=clock)


def make_pacer(ledger: Ledger, clock: FakeClock, **overrides: object) -> Pacer:
    return Pacer(lax_policy(**overrides), ledger, clock=clock, rng=random.Random(7), tz=TZ)


def complete_thread(
    ledger: Ledger,
    clock: FakeClock,
    thread_hash: str,
    *,
    n_tweets: int = 1,
    at: datetime | None = None,
) -> None:
    ledger.begin(thread_hash, strategy="composer", handle="olsa", n_tweets=n_tweets)
    records = tuple(
        PostedTweet(
            index=index,
            id=str(index),
            url=f"https://x.com/olsa/status/{index}",
            text_sha256="a" * 64,
        )
        for index in range(n_tweets)
    )
    ledger.complete(
        thread_hash,
        PostedThread(
            thread_hash=thread_hash,
            handle="olsa",
            strategy="composer",
            posted_at=(at or clock.now).isoformat(),
            tweets=records,
            complete=True,
        ),
    )


def assert_next_allowed_agrees(pacer: Pacer, clock: FakeClock, n_tweets: int) -> None:
    """next_allowed() must be exactly the first second check() stops raising."""
    until = pacer.next_allowed(n_tweets)
    assert until.utcoffset() == timedelta(0)

    clock.now = until
    pacer.check(n_tweets)

    clock.now = until - timedelta(seconds=1)
    with pytest.raises(PacingError) as info:
        pacer.check(n_tweets)
    assert info.value.retry_after_s == pytest.approx(1.0)


def test_a_fresh_ledger_allows_posting(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = make_ledger(tmp_path, clock)
    pacer = make_pacer(ledger, clock)

    assert pacer.check(1) is None
    assert pacer.next_allowed(1) == START
    assert pacer.next_allowed(1).tzinfo is UTC


def test_min_gap_between_threads(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = make_ledger(tmp_path, clock)
    complete_thread(ledger, clock, "first")
    pacer = make_pacer(ledger, clock, min_gap_between_threads_s=600)

    with pytest.raises(PacingError) as info:
        pacer.check(1)
    assert info.value.retry_after_s == pytest.approx(600)
    assert "min gap" in str(info.value)
    assert "next allowed" in str(info.value)

    clock.advance(600)
    assert pacer.check(1) is None


def test_persisted_not_before(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = make_ledger(tmp_path, clock)
    ledger.set_not_before(START + timedelta(minutes=5))
    pacer = make_pacer(ledger, clock)

    with pytest.raises(PacingError) as info:
        pacer.check(1)
    assert info.value.retry_after_s == pytest.approx(300)
    assert "not_before" in str(info.value)

    clock.advance(300)
    assert pacer.check(1) is None


def test_daily_thread_cap_waits_for_local_midnight(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = make_ledger(tmp_path, clock)
    complete_thread(ledger, clock, "first")
    pacer = make_pacer(ledger, clock, max_threads_per_day=1)

    with pytest.raises(PacingError) as info:
        pacer.check(1)
    assert info.value.retry_after_s == pytest.approx(6.5 * 3600)
    assert "daily thread cap" in str(info.value)

    clock.advance(6.5 * 3600)
    assert pacer.check(1) is None


def test_thread_events_count_toward_the_daily_cap(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = make_ledger(tmp_path, clock)
    ledger.add_event(kind="thread", tweets=0)
    pacer = make_pacer(ledger, clock, max_threads_per_day=1)

    with pytest.raises(PacingError, match="daily thread cap"):
        pacer.check(1)


def test_daily_tweet_cap(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = make_ledger(tmp_path, clock)
    ledger.add_event(tweets=8)
    pacer = make_pacer(ledger, clock, max_tweets_per_day=10)

    assert pacer.check(2) is None
    with pytest.raises(PacingError) as info:
        pacer.check(3)
    assert info.value.retry_after_s == pytest.approx(6.5 * 3600)
    assert "daily tweet cap" in str(info.value)

    clock.advance(6.5 * 3600)
    assert pacer.check(3) is None


def test_thread_events_do_not_count_as_tweets(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = make_ledger(tmp_path, clock)
    ledger.add_event(tweets=500, kind="thread")
    pacer = make_pacer(ledger, clock, max_tweets_per_day=10)

    assert pacer.check(10) is None


def test_hourly_tweet_cap(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = make_ledger(tmp_path, clock)
    ledger.add_event(tweets=4)
    pacer = make_pacer(ledger, clock, max_tweets_per_hour=5)

    assert pacer.check(1) is None
    with pytest.raises(PacingError) as info:
        pacer.check(2)
    assert info.value.retry_after_s == pytest.approx(3600)
    assert "hourly tweet cap" in str(info.value)

    clock.advance(3600)
    assert pacer.check(2) is None


def test_oversized_thread_fits_an_empty_hour_window(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = make_ledger(tmp_path, clock)
    pacer = make_pacer(ledger, clock, max_tweets_per_hour=5)

    assert pacer.check(10) is None

    ledger.add_event(tweets=1)
    with pytest.raises(PacingError, match="hourly tweet cap"):
        pacer.check(10)


def test_quiet_hours_wrap_midnight(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 11, 9, 18, 0, tzinfo=UTC))  # 23:30 local
    ledger = make_ledger(tmp_path, clock)
    pacer = make_pacer(ledger, clock, quiet_hours=(23, 7))

    with pytest.raises(PacingError) as info:
        pacer.check(1)
    assert info.value.retry_after_s == pytest.approx(7.5 * 3600)
    assert "quiet hours" in str(info.value)

    clock.advance(7.5 * 3600)
    assert pacer.check(1) is None


def test_quiet_hours_wrap_midnight_from_the_early_morning(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 11, 9, 20, 0, tzinfo=UTC))  # 01:30 local
    ledger = make_ledger(tmp_path, clock)
    pacer = make_pacer(ledger, clock, quiet_hours=(23, 7))

    with pytest.raises(PacingError) as info:
        pacer.check(1)
    assert info.value.retry_after_s == pytest.approx(5.5 * 3600)


def test_quiet_hours_same_day_window(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 11, 9, 5, 0, tzinfo=UTC))  # 10:30 local
    ledger = make_ledger(tmp_path, clock)
    pacer = make_pacer(ledger, clock, quiet_hours=(9, 17))

    with pytest.raises(PacingError) as info:
        pacer.check(1)
    assert info.value.retry_after_s == pytest.approx(6.5 * 3600)
    assert "17:00" in str(info.value)


def test_quiet_hours_can_be_disabled(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 11, 9, 18, 0, tzinfo=UTC))  # 23:30 local
    ledger = make_ledger(tmp_path, clock)
    pacer = make_pacer(ledger, clock, quiet_hours=None)

    assert pacer.check(1) is None


def test_combined_rules_report_the_longest_wait(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 11, 9, 18, 0, tzinfo=UTC))  # 23:30 local
    ledger = make_ledger(tmp_path, clock)
    complete_thread(ledger, clock, "first")
    pacer = make_pacer(
        ledger, clock, max_threads_per_day=1, min_gap_between_threads_s=60, quiet_hours=(23, 7)
    )

    with pytest.raises(PacingError) as info:
        pacer.check(1)
    assert info.value.retry_after_s == pytest.approx(7.5 * 3600)
    assert "quiet hours" in str(info.value)


def test_next_allowed_agrees_with_check_for_each_rule(tmp_path: Path) -> None:
    scenarios: list[tuple[Pacer, FakeClock, int]] = []

    clock = FakeClock()
    ledger = make_ledger(tmp_path / "gap", clock)
    complete_thread(ledger, clock, "first")
    scenarios.append((make_pacer(ledger, clock, min_gap_between_threads_s=600), clock, 1))

    clock = FakeClock()
    ledger = make_ledger(tmp_path / "threadcap", clock)
    complete_thread(ledger, clock, "first")
    scenarios.append((make_pacer(ledger, clock, max_threads_per_day=1), clock, 1))

    clock = FakeClock()
    ledger = make_ledger(tmp_path / "tweetcap", clock)
    ledger.add_event(tweets=10)
    scenarios.append((make_pacer(ledger, clock, max_tweets_per_day=10), clock, 1))

    clock = FakeClock()
    ledger = make_ledger(tmp_path / "hourly", clock)
    ledger.add_event(tweets=4)
    scenarios.append((make_pacer(ledger, clock, max_tweets_per_hour=5), clock, 2))

    clock = FakeClock(datetime(2026, 11, 9, 18, 0, tzinfo=UTC))
    ledger = make_ledger(tmp_path / "quiet", clock)
    scenarios.append((make_pacer(ledger, clock, quiet_hours=(23, 7)), clock, 1))

    for pacer, fake_clock, n_tweets in scenarios:
        assert_next_allowed_agrees(pacer, fake_clock, n_tweets)


def test_record_thread_done_applies_bounded_jitter(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = make_ledger(tmp_path, clock)
    pacer = make_pacer(ledger, clock, min_gap_between_threads_s=1000, jitter=0.25)

    first = pacer.record_thread_done(1)
    assert START + timedelta(seconds=1000) <= first <= START + timedelta(seconds=1250)
    assert ledger.not_before() == first


def test_record_thread_done_is_reproducible_with_a_seed(tmp_path: Path) -> None:
    clock = FakeClock()
    policy = lax_policy(min_gap_between_threads_s=1000, jitter=0.25)

    first = Pacer(
        policy, make_ledger(tmp_path / "a", clock), clock=clock, rng=random.Random(42), tz=TZ
    ).record_thread_done(1)
    second = Pacer(
        policy, make_ledger(tmp_path / "b", clock), clock=clock, rng=random.Random(42), tz=TZ
    ).record_thread_done(1)

    assert first == second


def test_record_thread_done_without_jitter_is_exact(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = make_ledger(tmp_path, clock)
    pacer = make_pacer(ledger, clock, min_gap_between_threads_s=1000, jitter=0.0)

    assert pacer.record_thread_done(1) == START + timedelta(seconds=1000)


def test_a_thread_larger_than_the_daily_cap_can_never_fit(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = make_ledger(tmp_path, clock)
    pacer = make_pacer(ledger, clock, max_tweets_per_day=5)

    with pytest.raises(ValidationError, match="can never fit"):
        pacer.check(6)
    with pytest.raises(ValidationError, match="can never fit"):
        pacer.next_allowed(6)
    with pytest.raises(ValidationError, match="at least one tweet"):
        pacer.check(0)
    with pytest.raises(ValidationError, match="at least one tweet"):
        pacer.record_thread_done(0)


def test_invalid_quiet_hours_are_rejected(tmp_path: Path) -> None:
    clock = FakeClock()
    ledger = make_ledger(tmp_path, clock)

    with pytest.raises(ConfigurationError, match="quiet_hours"):
        make_pacer(ledger, clock, quiet_hours=(24, 7))
    with pytest.raises(ConfigurationError, match="jitter"):
        make_pacer(ledger, clock, jitter=-0.1)
