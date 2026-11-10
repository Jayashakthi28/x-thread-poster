"""Tests for the atomic posting ledger: idempotency, durability and events."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from xposter.errors import LedgerCorruptError
from xposter.ledger import Decision, Ledger, LedgerEntry, LedgerStatus
from xposter.models import PostedThread, PostedTweet

START = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


class FakeClock:
    """Mutable injected clock; tests advance it explicitly."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ledger(tmp_path: Path, clock: FakeClock) -> Ledger:
    return Ledger(tmp_path / "ledger.json", clock=clock)


def tweet(index: int, identifier: str | None = None) -> PostedTweet:
    value = identifier if identifier is not None else str(index)
    return PostedTweet(
        index=index,
        id=value,
        url=f"https://x.com/olsa/status/{value}",
        text_sha256="a" * 64,
    )


def thread(thread_hash: str, *, count: int = 1, at: datetime = START) -> PostedThread:
    return PostedThread(
        thread_hash=thread_hash,
        handle="olsa",
        strategy="composer",
        posted_at=at.isoformat(),
        tweets=tuple(tweet(index) for index in range(count)),
        complete=True,
    )


def begin(ledger: Ledger, thread_hash: str = "thread-1", *, n_tweets: int = 1) -> None:
    ledger.begin(thread_hash, strategy="composer", handle="olsa", n_tweets=n_tweets)


def _seed_missing(ledger: Ledger) -> None:
    return None


def _seed_pending_empty(ledger: Ledger) -> None:
    begin(ledger)


def _seed_pending_partial(ledger: Ledger) -> None:
    begin(ledger, n_tweets=2)
    ledger.record_tweet("thread-1", tweet(0))


def _seed_clicking_empty(ledger: Ledger) -> None:
    begin(ledger)
    ledger.mark_clicking("thread-1")


def _seed_clicking_partial(ledger: Ledger) -> None:
    begin(ledger, n_tweets=2)
    ledger.mark_clicking("thread-1")
    ledger.record_tweet("thread-1", tweet(0))


def _seed_unknown_empty(ledger: Ledger) -> None:
    begin(ledger)
    ledger.mark_unknown("thread-1", "click may have landed")


def _seed_unknown_partial(ledger: Ledger) -> None:
    begin(ledger, n_tweets=2)
    ledger.record_tweet("thread-1", tweet(0))
    ledger.mark_unknown("thread-1", "click may have landed")


def _seed_failed_empty(ledger: Ledger) -> None:
    begin(ledger)
    ledger.mark_failed("thread-1", "composer missing")


def _seed_failed_partial(ledger: Ledger) -> None:
    begin(ledger, n_tweets=2)
    ledger.record_tweet("thread-1", tweet(0))
    ledger.mark_failed("thread-1", "network")


def _seed_posted(ledger: Ledger) -> None:
    begin(ledger)
    ledger.record_tweet("thread-1", tweet(0))
    ledger.complete("thread-1", thread("thread-1"))


STATE_SETUPS: dict[str, Callable[[Ledger], None]] = {
    "missing": _seed_missing,
    "pending-empty": _seed_pending_empty,
    "pending-partial": _seed_pending_partial,
    "clicking-empty": _seed_clicking_empty,
    "clicking-partial": _seed_clicking_partial,
    "unknown-empty": _seed_unknown_empty,
    "unknown-partial": _seed_unknown_partial,
    "failed-empty": _seed_failed_empty,
    "failed-partial": _seed_failed_partial,
    "posted": _seed_posted,
}

# Every row of the decide() contract. "force" is the CLI --force switch;
# "discard" is the operator's assertion that recorded partial tweets were
# deleted by hand.
DECIDE_CASES = [
    ("missing", False, False, Decision.NEW),
    ("missing", True, False, Decision.NEW),
    ("missing", False, True, Decision.NEW),
    ("missing", True, True, Decision.NEW),
    ("pending-empty", False, False, Decision.NEW),
    ("pending-empty", True, False, Decision.NEW),
    ("pending-empty", False, True, Decision.NEW),
    ("pending-empty", True, True, Decision.NEW),
    ("pending-partial", False, False, Decision.RESUME),
    ("pending-partial", True, False, Decision.RESUME),
    ("pending-partial", False, True, Decision.RESUME),
    ("pending-partial", True, True, Decision.RESUME),
    ("clicking-empty", False, False, Decision.BLOCKED_UNKNOWN),
    ("clicking-empty", True, False, Decision.NEW),
    ("clicking-empty", False, True, Decision.BLOCKED_UNKNOWN),
    ("clicking-empty", True, True, Decision.NEW),
    ("clicking-partial", False, False, Decision.BLOCKED_UNKNOWN),
    ("clicking-partial", True, False, Decision.RESUME),
    ("clicking-partial", False, True, Decision.NEW),
    ("clicking-partial", True, True, Decision.NEW),
    ("unknown-empty", False, False, Decision.BLOCKED_UNKNOWN),
    ("unknown-empty", True, False, Decision.NEW),
    ("unknown-empty", False, True, Decision.BLOCKED_UNKNOWN),
    ("unknown-empty", True, True, Decision.NEW),
    ("unknown-partial", False, False, Decision.BLOCKED_UNKNOWN),
    ("unknown-partial", True, False, Decision.RESUME),
    ("unknown-partial", False, True, Decision.NEW),
    ("unknown-partial", True, True, Decision.NEW),
    ("failed-empty", False, False, Decision.NEW),
    ("failed-empty", True, False, Decision.NEW),
    ("failed-empty", False, True, Decision.NEW),
    ("failed-empty", True, True, Decision.NEW),
    ("failed-partial", False, False, Decision.RESUME),
    ("failed-partial", True, False, Decision.RESUME),
    ("failed-partial", False, True, Decision.RESUME),
    ("failed-partial", True, True, Decision.RESUME),
    ("posted", False, False, Decision.ALREADY_POSTED),
    ("posted", True, False, Decision.NEW),
    ("posted", False, True, Decision.ALREADY_POSTED),
    ("posted", True, True, Decision.NEW),
]


@pytest.mark.parametrize(("state", "force", "discard", "expected"), DECIDE_CASES)
def test_decide_truth_table(
    ledger: Ledger,
    state: str,
    force: bool,
    discard: bool,
    expected: Decision,
) -> None:
    STATE_SETUPS[state](ledger)

    assert ledger.decide("thread-1", force=force, discard_recorded=discard) is expected


def test_missing_file_reads_as_empty(ledger: Ledger) -> None:
    assert ledger.get("nothing") is None
    assert ledger.decide("nothing") is Decision.NEW
    assert ledger.all_entries() == []
    assert ledger.not_before() is None
    assert ledger.last_thread_completed_at() is None
    assert ledger.events_since(START - timedelta(days=1)) == []
    assert not ledger.path.exists()


def test_entry_new_fields_default_to_empty(ledger: Ledger) -> None:
    entry = LedgerEntry(
        thread_hash="h",
        status=LedgerStatus.PENDING,
        strategy="composer",
        handle="olsa",
        n_tweets=1,
        started_at="",
        updated_at="",
        posted=(),
        error="",
        completed=None,
    )

    assert entry.clicking_at == ""
    assert entry.history == ()
    assert entry.previous_completed is None


def test_entry_round_trips_through_disk(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "ledger.json"
    first = Ledger(path, clock=clock)
    begin(first, n_tweets=2)
    first.mark_clicking("thread-1")
    first.record_tweet("thread-1", tweet(0))
    first.record_tweet("thread-1", tweet(1))
    completed = first.complete("thread-1", thread("thread-1", count=2))

    reloaded = Ledger(path, clock=clock).get("thread-1")

    assert reloaded == completed
    assert reloaded is not None
    assert reloaded.status is LedgerStatus.POSTED
    assert reloaded.posted == (tweet(0), tweet(1))
    assert reloaded.completed == thread("thread-1", count=2)
    assert reloaded.clicking_at == ""
    assert reloaded.handle == "olsa"
    assert reloaded.strategy == "composer"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["entries"]["thread-1"]["status"] == "posted"
    assert payload["version"] == 2


def test_decide_pending_without_tweets_is_new(ledger: Ledger) -> None:
    begin(ledger)
    assert ledger.decide("thread-1") is Decision.NEW


def test_decide_pending_with_a_tweet_is_resume(ledger: Ledger) -> None:
    begin(ledger, n_tweets=2)
    ledger.record_tweet("thread-1", tweet(0))
    assert ledger.decide("thread-1") is Decision.RESUME


def test_decide_posted_and_force(ledger: Ledger) -> None:
    begin(ledger)
    ledger.complete("thread-1", thread("thread-1"))
    assert ledger.decide("thread-1") is Decision.ALREADY_POSTED
    assert ledger.decide("thread-1", force=True) is Decision.NEW


def test_decide_unknown_without_tweets_and_force(ledger: Ledger) -> None:
    begin(ledger)
    ledger.mark_unknown("thread-1", "click may have landed")
    assert ledger.decide("thread-1") is Decision.BLOCKED_UNKNOWN
    assert ledger.decide("thread-1", force=True) is Decision.NEW


def test_decide_failed_with_nothing_posted_is_new(ledger: Ledger) -> None:
    begin(ledger)
    ledger.mark_failed("thread-1", "composer missing")
    assert ledger.decide("thread-1") is Decision.NEW


def test_decide_failed_with_a_tweet_stays_resume_even_when_forced(ledger: Ledger) -> None:
    begin(ledger, n_tweets=2)
    ledger.record_tweet("thread-1", tweet(0))
    ledger.mark_failed("thread-1", "network")
    assert ledger.decide("thread-1") is Decision.RESUME
    assert ledger.decide("thread-1", force=True) is Decision.RESUME


def test_decide_unknown_partial_never_turns_into_new_on_force(ledger: Ledger) -> None:
    begin(ledger, n_tweets=2)
    ledger.record_tweet("thread-1", tweet(0))
    ledger.mark_unknown("thread-1", "click may have landed")

    assert ledger.decide("thread-1") is Decision.BLOCKED_UNKNOWN
    assert ledger.decide("thread-1", force=True) is Decision.RESUME
    assert ledger.decide("thread-1", force=False, discard_recorded=True) is Decision.NEW


def test_mark_clicking_persists_and_blocks(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "ledger.json"
    ledger = Ledger(path, clock=clock)
    begin(ledger)

    entry = ledger.mark_clicking("thread-1")
    assert entry.clicking_at == START.isoformat()

    reopened = Ledger(path, clock=clock)
    assert reopened.get("thread-1") is not None
    assert reopened.get("thread-1").clicking_at == START.isoformat()
    assert reopened.decide("thread-1") is Decision.BLOCKED_UNKNOWN
    assert reopened.decide("thread-1", force=True) is Decision.NEW


def test_clear_clicking_persists_and_unblocks(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "ledger.json"
    ledger = Ledger(path, clock=clock)
    begin(ledger)
    ledger.mark_clicking("thread-1")

    assert ledger.clear_clicking("thread-1").clicking_at == ""
    reopened = Ledger(path, clock=clock)
    assert reopened.get("thread-1") is not None
    assert reopened.get("thread-1").clicking_at == ""
    assert reopened.decide("thread-1") is Decision.NEW


def test_mark_clicking_requires_a_begin(ledger: Ledger) -> None:
    with pytest.raises(KeyError, match="not begun"):
        ledger.mark_clicking("thread-1")


def test_clear_clicking_requires_a_begin(ledger: Ledger) -> None:
    with pytest.raises(KeyError, match="not begun"):
        ledger.clear_clicking("thread-1")


def test_complete_clears_the_click_barrier(ledger: Ledger) -> None:
    begin(ledger)
    ledger.mark_clicking("thread-1")

    completed = ledger.complete("thread-1", thread("thread-1"))

    assert completed.clicking_at == ""


def test_record_tweet_and_mark_failed_keep_the_click_barrier(ledger: Ledger) -> None:
    begin(ledger, n_tweets=2)
    ledger.mark_clicking("thread-1")

    recorded = ledger.record_tweet("thread-1", tweet(0))
    assert recorded.clicking_at == START.isoformat()

    failed = ledger.mark_failed("thread-1", "network")
    assert failed.clicking_at == START.isoformat()


def test_begin_keeps_posted_tweets_on_resume(ledger: Ledger, clock: FakeClock) -> None:
    first = ledger.begin("thread-1", strategy="replies", handle="olsa", n_tweets=3)
    clock.advance(30)
    ledger.record_tweet("thread-1", tweet(0))
    ledger.mark_failed("thread-1", "network")
    clock.advance(30)

    second = ledger.begin("thread-1", strategy="replies", handle="olsa", n_tweets=3)

    assert second.posted == (tweet(0),)
    assert second.started_at == first.started_at
    assert second.updated_at == clock.now.isoformat()
    assert second.error == ""
    assert second.completed is None
    assert second.status is LedgerStatus.PENDING
    assert second.clicking_at == ""
    assert any("resume" in line and "ids 0" in line for line in second.history)


def test_begin_keeps_posted_tweets_for_pending_partial(ledger: Ledger) -> None:
    first = ledger.begin("thread-1", strategy="composer", handle="olsa", n_tweets=2)
    ledger.record_tweet("thread-1", tweet(0))

    restarted = ledger.begin("thread-1", strategy="composer", handle="olsa", n_tweets=2)

    assert restarted.posted == (tweet(0),)
    assert restarted.started_at == first.started_at
    assert any("previous status pending" in line for line in restarted.history)


def test_begin_keeps_posted_tweets_for_forced_unknown_partial(ledger: Ledger) -> None:
    first = ledger.begin("thread-1", strategy="replies", handle="olsa", n_tweets=2)
    ledger.record_tweet("thread-1", tweet(0))
    ledger.mark_unknown("thread-1", "click may have landed")

    restarted = ledger.begin("thread-1", strategy="replies", handle="olsa", n_tweets=2)

    assert restarted.posted == (tweet(0),)
    assert restarted.started_at == first.started_at
    assert restarted.previous_completed is None
    assert any("previous status unknown" in line for line in restarted.history)


def test_begin_keeps_posted_tweets_for_clicking_partial(ledger: Ledger) -> None:
    begin(ledger, n_tweets=2)
    ledger.mark_clicking("thread-1")
    ledger.record_tweet("thread-1", tweet(0))

    restarted = ledger.begin("thread-1", strategy="composer", handle="olsa", n_tweets=2)

    assert restarted.posted == (tweet(0),)
    assert restarted.clicking_at == ""


def test_begin_discard_recorded_drops_posted_after_a_manual_delete(
    ledger: Ledger, clock: FakeClock
) -> None:
    begin(ledger, n_tweets=2)
    ledger.mark_clicking("thread-1")
    ledger.record_tweet("thread-1", tweet(0))
    ledger.mark_unknown("thread-1", "click may have landed")
    clock.advance(60)

    restarted = ledger.begin(
        "thread-1", strategy="composer", handle="olsa", n_tweets=2, discard_recorded=True
    )

    assert restarted.posted == ()
    assert restarted.started_at == clock.now.isoformat()
    assert restarted.previous_completed is None
    assert any(
        "forced re-run" in line and "previous status unknown" in line and "ids 0" in line
        for line in restarted.history
    )


def test_begin_after_forced_repost_keeps_the_old_completion(
    ledger: Ledger, clock: FakeClock
) -> None:
    begin(ledger)
    ledger.record_tweet("thread-1", tweet(0))
    original = thread("thread-1")
    ledger.complete("thread-1", original)
    clock.advance(60)

    restarted = ledger.begin("thread-1", strategy="composer", handle="olsa", n_tweets=1)

    assert restarted.posted == ()
    assert restarted.completed is None
    assert restarted.previous_completed == original
    assert restarted.started_at == clock.now.isoformat()
    assert len(restarted.history) == 1
    assert "forced re-run" in restarted.history[0]
    assert "previous status posted" in restarted.history[0]
    assert "ids 0" in restarted.history[0]


def test_previous_completed_survives_a_failed_forced_run(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "ledger.json"
    ledger = Ledger(path, clock=clock)
    begin(ledger)
    ledger.record_tweet("thread-1", tweet(0))
    original = thread("thread-1")
    ledger.complete("thread-1", original)
    ledger.begin("thread-1", strategy="composer", handle="olsa", n_tweets=1)
    ledger.mark_failed("thread-1", "network")

    reloaded = Ledger(path, clock=clock).get("thread-1")

    assert reloaded is not None
    assert reloaded.status is LedgerStatus.FAILED
    assert reloaded.previous_completed == original
    assert any("forced re-run" in line for line in reloaded.history)


def test_history_accumulates_across_forced_attempts(ledger: Ledger, clock: FakeClock) -> None:
    begin(ledger)
    ledger.record_tweet("thread-1", tweet(0))
    ledger.complete("thread-1", thread("thread-1"))
    ledger.begin("thread-1", strategy="composer", handle="olsa", n_tweets=1)
    clock.advance(60)
    second = thread("thread-1", at=clock.now)
    ledger.complete("thread-1", second)

    third = ledger.begin("thread-1", strategy="composer", handle="olsa", n_tweets=1)

    assert third.previous_completed == second
    assert len(third.history) == 2
    assert all("forced re-run" in line for line in third.history)


def test_atomic_write_failure_leaves_the_old_file(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ledger.json"
    ledger = Ledger(path, clock=clock)
    begin(ledger)
    before = path.read_bytes()

    def boom(source: object, destination: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("xposter.ledger.os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        ledger.complete("thread-1", thread("thread-1"))

    assert path.read_bytes() == before
    leftovers = [item for item in tmp_path.iterdir() if item.name.endswith(".tmp")]
    assert leftovers == []


def test_click_barrier_is_file_fsynced_then_replaced_then_dir_fsynced(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ledger.json"
    ledger = Ledger(path, clock=clock)
    begin(ledger)
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        events.append("fsync-dir" if stat.S_ISDIR(mode) else "fsync-file")
        real_fsync(descriptor)

    def recording_replace(source: object, destination: object) -> None:
        events.append("replace")
        real_replace(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr("xposter.ledger.os.fsync", recording_fsync)
    monkeypatch.setattr("xposter.ledger.os.replace", recording_replace)

    ledger.mark_clicking("thread-1")

    assert events == ["fsync-file", "replace", "fsync-dir"]


def test_corrupt_json_raises_and_keeps_the_file(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "ledger.json"
    original = "{not json"
    path.write_text(original, encoding="utf-8")
    ledger = Ledger(path, clock=clock)

    with pytest.raises(LedgerCorruptError) as info:
        ledger.get("thread-1")

    assert info.value.path == path
    assert "only proof" in str(info.value)
    assert "xposter ledger --reset-corrupt" in str(info.value)
    assert path.read_text(encoding="utf-8") == original


def test_non_object_top_level_raises(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(LedgerCorruptError):
        Ledger(path, clock=clock).all_entries()

    assert path.read_text(encoding="utf-8") == "[]"


def test_non_utf8_ledger_raises(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "ledger.json"
    path.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(LedgerCorruptError):
        Ledger(path, clock=clock).get("thread-1")

    assert path.read_bytes() == b"\xff\xfe\x00"


def test_zero_byte_file_is_empty_and_usable(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("", encoding="utf-8")
    ledger = Ledger(path, clock=clock)

    assert ledger.all_entries() == []
    assert ledger.decide("thread-1") is Decision.NEW

    begin(ledger)
    assert path.read_text(encoding="utf-8") != ""


def test_reset_corrupt_moves_the_file_to_a_timestamped_name(
    tmp_path: Path, clock: FakeClock
) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{bad", encoding="utf-8")
    ledger = Ledger(path, clock=clock)
    with pytest.raises(LedgerCorruptError):
        ledger.get("thread-1")

    moved = ledger.reset_corrupt()

    assert moved.name.startswith("ledger.json.corrupt-20261109T120000")
    assert moved.parent == tmp_path
    assert moved.read_text(encoding="utf-8") == "{bad"
    assert not path.exists()
    assert ledger.decide("thread-1") is Decision.NEW

    begin(ledger)
    assert path.exists()


def test_malformed_entry_is_skipped_without_losing_the_others(
    ledger: Ledger, tmp_path: Path
) -> None:
    begin(ledger)
    payload = json.loads(ledger.path.read_text(encoding="utf-8"))
    payload["entries"]["broken"] = {"thread_hash": "broken", "status": "not-a-status"}
    ledger.path.write_text(json.dumps(payload), encoding="utf-8")

    assert ledger.get("thread-1") is not None
    assert ledger.get("broken") is None


def test_record_tweet_requires_a_begin(ledger: Ledger) -> None:
    with pytest.raises(KeyError, match="not begun"):
        ledger.record_tweet("thread-1", tweet(0))


def test_record_tweet_replay_is_idempotent(ledger: Ledger) -> None:
    begin(ledger)
    ledger.record_tweet("thread-1", tweet(0))
    entry = ledger.record_tweet("thread-1", tweet(0))

    assert entry.posted == (tweet(0),)
    assert ledger.events_since(START) == [(START, 1, "tweet")]


def test_resume_from_returns_tweets_in_index_order(ledger: Ledger) -> None:
    begin(ledger, n_tweets=3)
    ledger.record_tweet("thread-1", tweet(2, "c"))
    ledger.record_tweet("thread-1", tweet(0, "a"))

    assert ledger.resume_from("thread-1") == (tweet(0, "a"), tweet(2, "c"))
    assert ledger.resume_from("unknown") == ()


def test_events_are_recorded_and_queried(ledger: Ledger, clock: FakeClock) -> None:
    ledger.add_event()
    clock.advance(60)
    ledger.add_event(tweets=3, kind="thread")

    assert ledger.events_since(START) == [
        (START, 1, "tweet"),
        (START + timedelta(seconds=60), 3, "thread"),
    ]
    later = ledger.events_since(START + timedelta(seconds=30))
    assert later == [(START + timedelta(seconds=60), 3, "thread")]

    with pytest.raises(ValueError, match="negative"):
        ledger.add_event(tweets=-1)
    with pytest.raises(ValueError, match="non-empty"):
        ledger.add_event(kind="")


def test_events_since_rejects_naive_datetimes(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="aware"):
        ledger.events_since(datetime(2026, 11, 9, 12, 0))


def test_not_before_round_trip(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "ledger.json"
    ledger = Ledger(path, clock=clock)
    when = START + timedelta(minutes=5)
    ledger.set_not_before(when)

    reopened = Ledger(path, clock=clock)
    assert reopened.not_before() == when

    reopened.set_not_before(None)
    assert Ledger(path, clock=clock).not_before() is None


def test_last_thread_completed_at_uses_the_latest(ledger: Ledger, clock: FakeClock) -> None:
    begin(ledger, "first")
    ledger.complete("first", thread("first", at=START))
    clock.advance(600)
    begin(ledger, "second")
    ledger.complete("second", thread("second", at=clock.now))

    assert ledger.last_thread_completed_at() == clock.now


def test_prune_keeps_posted_unknown_and_pending(ledger: Ledger, clock: FakeClock) -> None:
    begin(ledger, "posted")
    ledger.complete("posted", thread("posted"))
    begin(ledger, "unknown")
    ledger.mark_unknown("unknown", "check the profile")
    begin(ledger, "failed")
    ledger.mark_failed("failed", "boom")
    begin(ledger, "pending")
    ledger.add_event(tweets=1)

    clock.advance(200 * 24 * 3600)
    ledger.add_event(tweets=2)
    ledger.prune(keep_days=120)

    remaining = {entry.thread_hash for entry in ledger.all_entries()}
    assert remaining == {"posted", "unknown", "pending"}
    assert ledger.events_since(START) == [(clock.now, 2, "tweet")]


def test_prune_keeps_failed_entries_that_hold_a_previous_completion(
    ledger: Ledger, clock: FakeClock
) -> None:
    begin(ledger)
    ledger.record_tweet("thread-1", tweet(0))
    original = thread("thread-1")
    ledger.complete("thread-1", original)
    ledger.begin("thread-1", strategy="composer", handle="olsa", n_tweets=1)
    ledger.mark_failed("thread-1", "network")

    clock.advance(200 * 24 * 3600)
    ledger.prune(keep_days=120)

    entry = ledger.get("thread-1")
    assert entry is not None
    assert entry.status is LedgerStatus.FAILED
    assert entry.previous_completed == original


def test_prune_rejects_negative_days(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="keep_days"):
        ledger.prune(keep_days=-1)


def test_ledger_file_is_private(ledger: Ledger) -> None:
    begin(ledger)
    mode = stat.S_IMODE(ledger.path.stat().st_mode)
    assert mode == 0o600


def test_errors_are_redacted_and_truncated(ledger: Ledger) -> None:
    entry = ledger.mark_failed("thread-1", "auth_token=SUPERSECRET " + "x" * 900)

    assert len(entry.error) == 500
    assert "SUPERSECRET" not in entry.error
    assert entry.error.startswith("auth_token=[redacted]")


def test_unknown_notes_are_redacted(ledger: Ledger) -> None:
    entry = ledger.mark_unknown("thread-1", "ct0=ABC123")
    assert entry.error == "ct0=[redacted]"


def test_plain_errors_are_truncated(ledger: Ledger) -> None:
    entry = ledger.mark_failed("thread-1", "y" * 900)
    assert len(entry.error) == 500


def test_marking_a_thread_that_was_never_begun_still_records(ledger: Ledger) -> None:
    entry = ledger.mark_unknown("orphan", "click may have landed")

    assert entry.status is LedgerStatus.UNKNOWN
    assert entry.thread_hash == "orphan"
    assert ledger.decide("orphan") is Decision.BLOCKED_UNKNOWN
