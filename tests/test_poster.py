"""End-to-end tests for the posting pipeline against an offline fake x.com.

Every test launches the real headless Chrome on a temporary profile, lets
``FakeX`` answer every x.com request in-process, and drives ``XPoster`` through
the same code path production uses. The human timing is seeded with near-zero
pauses so a scenario takes seconds; no request ever leaves the machine.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Keyboard, Locator, Page, async_playwright

from tests.fake_x import ARTIFACT_CANARY, FakeTweet, FakeX, Scenario
from tests.fake_x.fixtures import open_fake_context
from xposter import selectors
from xposter.browser import find_chrome, new_page, open_context
from xposter.config import (
    Display,
    HumanPolicy,
    PacingPolicy,
    Strategy,
    XPosterConfig,
)
from xposter.errors import (
    AccountRestrictedError,
    ChallengeError,
    LedgerCorruptError,
    NotLoggedInError,
    PacingError,
    ParkedError,
    PostOutcomeUnknownError,
    PostRejectedError,
    RateLimitedError,
    UiChangedError,
    ValidationError,
)
from xposter.humanize import Humanizer
from xposter.ledger import Decision, Ledger, LedgerStatus
from xposter.media import MAX_IMAGE_BYTES
from xposter.models import PostedThread, PostedTweet, Thread, Tweet
from xposter.poster import (
    ClickNotDispatchedError,
    ResponseTiming,
    XPoster,
    _normalise_editor_text,
    _read_editor_text,
    _ResponseWatcher,
    _typed_mismatch_summary,
    human_click,
)
from xposter.splitter import split_with_first
from xposter.store import SessionStore

_CHROME = find_chrome(XPosterConfig())

# A real 1x1 PNG: Chrome must decode the preview (naturalWidth > 0) before the
# upload wait can succeed, so synthetic magic bytes are not enough for browser
# media tests.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg=="
)

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        _CHROME is None,
        reason="Google Chrome is not installed; posting tests drive a real Chrome",
    ),
]

_COOKIE_VALUE = "test-auth-token-do-not-leak"
_CT0_VALUE = "test-ct0-do-not-leak"


@pytest.fixture
def poster_config(tmp_path: Path) -> XPosterConfig:
    """A temporary home with real headless Chrome and nearly instant pauses."""
    assert _CHROME is not None
    home = tmp_path / "home"
    home.mkdir()
    return XPosterConfig(
        home=home,
        handle="olsa_test",
        display=Display.HEADLESS,
        chrome_path=_CHROME,
        default_timeout_ms=4_000,
        human=HumanPolicy(
            typing_delay_ms=(1, 3),
            pre_post_pause_s=(0.0, 0.02),
            between_tweet_pause_s=(0.0, 0.02),
            action_pause_s=(0.0, 0.02),
            think_pause_s=(0.0, 0.02),
            scroll_before_post=False,
            seed=1,
        ),
        pacing=PacingPolicy(
            min_gap_between_threads_s=0.0,
            max_threads_per_day=6,
            max_tweets_per_day=40,
            max_tweets_per_hour=12,
            quiet_hours=None,
            jitter=0.0,
        ),
    )


def seed_session(config: XPosterConfig) -> None:
    """Write a saved cookie state so the browser has a session to open.

    The fake serves the signed-in shell regardless, but production reads the
    backup after an empty profile and a clean run rewrites it, so the tests
    exercise that path too.
    """
    SessionStore(config).save_state(
        {
            "cookies": [
                {
                    "name": "auth_token",
                    "value": _COOKIE_VALUE,
                    "domain": ".x.com",
                    "path": "/",
                    "expires": 4102444800.0,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                },
                {
                    "name": "ct0",
                    "value": _CT0_VALUE,
                    "domain": ".x.com",
                    "path": "/",
                    "expires": 4102444800.0,
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                },
            ],
            "origins": [],
        }
    )


async def no_sleep(seconds: float) -> None:
    """Accept a pause without waiting; the durations themselves still run."""
    return None


def forbidden_factory() -> Any:
    """Prove a code path never reaches the browser."""
    raise AssertionError("Chrome must not be launched on this path")


def make_poster(
    config: XPosterConfig,
    fake: FakeX,
    *,
    playwright_factory: Any = None,
    allow_alt_text: bool = False,
) -> XPoster:
    """Build a poster wired to the offline fake with instant human timing."""
    kwargs: dict[str, Any] = {
        "context_hook": fake.install,
        "sleep": no_sleep,
        "allow_alt_text": allow_alt_text,
    }
    if playwright_factory is not None:
        kwargs["playwright_factory"] = playwright_factory
    return XPoster(config, **kwargs)


def make_thread(
    *texts: str,
    key: str = "poster-test",
    media: tuple[Path, ...] = (),
) -> Thread:
    """Build a thread whose first tweet carries optional media."""
    tweets = [Tweet(texts[0], media=media)]
    tweets.extend(Tweet(text) for text in texts[1:])
    return Thread(tuple(tweets), key=key)


def text_hash(text: str) -> str:
    """The record hash the poster is expected to store for one tweet."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chain_record(index: int, tweet_id: str | None = None) -> PostedTweet:
    """A caller-owned prefix record with a deliberately unknown URL."""
    return PostedTweet(
        index=index,
        id=tweet_id if tweet_id is not None else str(900_000_000_000_000_000 + index),
        url="",
        text_sha256=text_hash(f"tweet {index}"),
    )


def seed_fake_prefix(fake: FakeX, texts: list[str]) -> list[PostedTweet]:
    """Put an accepted prefix on the fake timeline and return its records.

    ``url`` is left empty on purpose: the poster must rebuild it from the
    configured handle and the recorded id when the caller's record has none.
    """
    prefix: list[PostedTweet] = []
    parent: str | None = None
    for index, text in enumerate(texts):
        tweet_id = str(900_000_000_000_000_000 + index)
        fake.tweets.append(
            FakeTweet(
                id=tweet_id,
                text=text,
                author=fake.handle,
                in_reply_to=parent,
                media=(),
            )
        )
        prefix.append(
            PostedTweet(
                index=index,
                id=tweet_id,
                url="",
                text_sha256=text_hash(text),
            )
        )
        parent = tweet_id
    return prefix


@asynccontextmanager
async def open_plain_page(config: XPosterConfig) -> AsyncIterator[Page]:
    """A real Chrome page with no fake routes, for DOM-only checks."""
    async with async_playwright() as playwright, open_context(playwright, config) as context:
        yield await new_page(context)


_CREATE_TWEET_URL = "https://api.x.com/i/api/graphql/query-id/CreateTweet"


def _create_payload(tweet_id: str, *, text: str = "secret tweet text") -> dict[str, Any]:
    """A CreateTweet body with recognisable content to prove logging never leaks it."""
    return {
        "data": {
            "create_tweet": {
                "tweet_results": {"rest_id": tweet_id},
                "note": text,
            }
        }
    }


class _StubResponse:
    """The tiny part of ``playwright.Response`` the watcher needs."""

    def __init__(
        self,
        *,
        body: object = None,
        status: int = 200,
        delay_s: float = 0.0,
        hang: bool = False,
    ) -> None:
        self.url = _CREATE_TWEET_URL
        self.status = status
        self.request = Mock(resource_type="fetch")
        self._body = body
        self._delay_s = delay_s
        self._hang = hang

    async def json(self) -> object:
        if self._hang:
            await asyncio.Event().wait()
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        return self._body


async def test_response_watcher_captures_a_late_body_and_records_timing() -> None:
    watcher = _ResponseWatcher(Mock())
    watcher.start()
    created: list[str] = []
    watcher.on_created = created.append
    watcher._on_response(_StubResponse(body=_create_payload("111"), delay_s=0.2))

    await watcher._drain()

    assert created == ["111"]
    assert len(watcher.timings) == 1
    timing: ResponseTiming = watcher.timings[0]
    assert timing.index == 1
    assert timing.status == 200
    assert timing.ok is True
    assert timing.body_done_s > timing.arrived_s
    assert timing.body_done_s - timing.arrived_s >= 0.15


async def test_response_watcher_times_out_a_hung_body_and_keeps_later_responses(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("xposter.poster._BODY_TIMEOUT_S", 0.05)
    caplog.set_level(logging.WARNING, logger="xposter.poster")
    watcher = _ResponseWatcher(Mock())
    watcher.start()
    created: list[str] = []
    watcher.on_created = created.append
    watcher._on_response(_StubResponse(hang=True))
    watcher._on_response(_StubResponse(body=_create_payload("222")))

    await watcher._drain()

    assert created == ["222"], "a hung body must not stop the next response"
    assert [timing.ok for timing in watcher.timings] == [False, True]
    assert watcher.timings[0].status == 200
    assert watcher.timings[0].body_done_s >= 0.04
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert warnings, "the timeout must be visible in the log"
    message = warnings[0].getMessage()
    assert "timed out" in message
    assert "#1" in message


async def test_response_watcher_info_line_has_only_timing_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="xposter.poster")
    watcher = _ResponseWatcher(Mock())
    watcher.start()
    secret_id = "9988776655"
    secret_text = "DO NOT LOG THIS TEXT"
    watcher._on_response(_StubResponse(body=_create_payload(secret_id, text=secret_text)))

    await watcher._drain()

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "xposter.poster" and record.levelno == logging.INFO
    ]
    assert len(lines) == 1
    line = lines[0]
    assert re.fullmatch(
        r"CreateTweet response #1 status=200 arrived=\+\d+\.\d+s body=\+\d+\.\d+s",
        line,
    ), line
    assert secret_id not in line
    assert secret_text not in line
    assert _COOKIE_VALUE not in line


async def test_composer_posts_a_chain_and_already_posted_stays_offline(
    poster_config: XPosterConfig, fake_x: FakeX
) -> None:
    seed_session(poster_config)
    poster = make_poster(poster_config, fake_x)
    thread = make_thread("First post", "Second post", "Third post")

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    assert posted.strategy == "composer"
    assert [tweet.index for tweet in posted.tweets] == [0, 1, 2]
    assert [tweet.id for tweet in posted.tweets] == [tweet.id for tweet in fake_x.tweets]
    assert [tweet.text for tweet in fake_x.tweets] == list(thread.texts)
    assert [tweet.text_sha256 for tweet in posted.tweets] == [
        text_hash(text) for text in thread.texts
    ]
    for tweet in posted.tweets:
        assert tweet.url == f"https://x.com/olsa_test/status/{tweet.id}"
    replies = [
        request["variables"]["reply"]["in_reply_to_tweet_id"] for request in fake_x.create_requests
    ]
    assert replies == [None, posted.tweets[0].id, posted.tweets[1].id]

    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.POSTED

    visits_before = list(fake_x.visits)
    again = await poster.post_thread(thread)

    assert again == posted
    assert fake_x.visits == visits_before, "an already-posted thread must not touch x.com"
    assert len(fake_x.tweets) == 3


async def test_pacing_blocks_an_immediate_second_thread(
    poster_config: XPosterConfig, fake_x: FakeX
) -> None:
    seed_session(poster_config)
    config = poster_config.with_overrides(
        pacing=replace(poster_config.pacing, min_gap_between_threads_s=1800.0)
    )
    poster = make_poster(config, fake_x)
    await poster.post_thread(make_thread("One", "Two", key="pacing-one"))
    visits_before = list(fake_x.visits)

    with pytest.raises(PacingError) as excinfo:
        await poster.post_thread(make_thread("Another", key="pacing-two"))

    assert excinfo.value.retry_after_s > 0
    assert fake_x.visits == visits_before, "pacing must stop the run before Chrome starts"


async def test_replies_strategy_resumes_after_a_crash(
    poster_config: XPosterConfig, fake_x: FakeX
) -> None:
    seed_session(poster_config)
    config = poster_config.with_overrides(strategy=Strategy.REPLIES)
    poster = make_poster(config, fake_x)
    thread = make_thread("Root post", "Reply one", "Reply two", key="replies")
    pauses = {"between": 0}

    original_pause = poster._pause

    async def crash_on_second_gap(kind: str) -> None:
        if kind == "between_tweet":
            pauses["between"] += 1
            if pauses["between"] == 2:
                raise KeyboardInterrupt
        await original_pause(kind)

    poster._pause = crash_on_second_gap  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        await poster.post_thread(thread)

    interrupted = poster.ledger.get(thread.content_hash)
    assert interrupted is not None
    assert interrupted.status is LedgerStatus.PENDING
    assert interrupted.clicking_at, "the click barrier must survive the interruption"
    assert [tweet.index for tweet in interrupted.posted] == [0, 1]
    assert len(fake_x.tweets) == 2

    # The click barrier is still up, so the ledger demands an explicit force;
    # force resumes after the recorded ids and never re-posts them.
    resumed = await make_poster(config, fake_x).post_thread(thread, force=True)

    assert resumed.complete is True
    assert [tweet.index for tweet in resumed.tweets] == [0, 1, 2]
    assert len(fake_x.tweets) == 3
    assert len(fake_x.create_requests) == 3
    assert (
        fake_x.create_requests[-1]["variables"]["reply"]["in_reply_to_tweet_id"]
        == interrupted.posted[-1].id
    )


async def test_legacy_dom_composer_thread(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = make_thread("Legacy one", "Legacy two", key="legacy")

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    assert [tweet.text for tweet in fake.tweets] == ["Legacy one", "Legacy two"]
    assert [tweet.id for tweet in posted.tweets] == [tweet.id for tweet in fake.tweets]


async def test_media_attaches_to_the_first_tweet(
    poster_config: XPosterConfig, fake_x: FakeX, tmp_path: Path
) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(_PNG_1X1)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake_x)
    thread = make_thread("Post with media", "Reply to it", key="media", media=(image,))

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    assert len(fake_x.create_requests) == 2
    first_media = fake_x.create_requests[0]["variables"]["media"]["media_entities"]
    second_media = fake_x.create_requests[1]["variables"]["media"]["media_entities"]
    assert first_media, "tweet one must carry the uploaded media id"
    assert second_media == []
    assert fake_x.tweets[0].media


async def test_dry_run_leaves_no_tweets_and_no_ledger_entry(
    poster_config: XPosterConfig, fake_x: FakeX
) -> None:
    seed_session(poster_config)
    poster = make_poster(poster_config, fake_x)
    thread = make_thread("Dry one", "Dry two", key="dry")

    posted = await poster.post_thread(thread, dry_run=True)

    assert posted.strategy == "dry-run"
    assert posted.tweets == ()
    assert posted.complete is False
    assert fake_x.tweets == []
    assert fake_x.create_requests == []
    assert poster.ledger.get(thread.content_hash) is None


async def test_duplicate_is_rejected_without_parking(
    poster_config: XPosterConfig, fake_x: FakeX
) -> None:
    seed_session(poster_config)
    poster = make_poster(poster_config, fake_x)
    await poster.post_thread(make_thread("Same words", key="dupe-one"))
    fake_x.set_scenario(Scenario.DUPLICATE)
    duplicate = make_thread("Same words", key="dupe-two")

    with pytest.raises(PostRejectedError) as excinfo:
        await poster.post_thread(duplicate)

    assert excinfo.value.code == 187
    assert poster.parked() is None, "a duplicate is a content error, not a park"
    entry = poster.ledger.get(duplicate.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.FAILED


async def test_daily_limit_parks_and_the_next_call_never_launches_chrome(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(scenario=Scenario.DAILY_LIMIT)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)

    with pytest.raises(RateLimitedError):
        await poster.post_thread(make_thread("Too much", key="daily"))

    info = poster.parked()
    assert info is not None
    assert info.kind == "rate_limit"
    visits_before = list(fake.visits)
    parked_poster = make_poster(poster_config, fake, playwright_factory=forbidden_factory)
    with pytest.raises(ParkedError):
        await parked_poster.post_thread(make_thread("Again", key="daily-two"))
    assert fake.visits == visits_before


async def test_automated_flag_parks(poster_config: XPosterConfig, fake_x_factory: Any) -> None:
    fake = fake_x_factory(scenario=Scenario.AUTOMATED)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)

    with pytest.raises(ChallengeError):
        await poster.post_thread(make_thread("Looks automated", key="automated"))

    assert poster.parked() is not None


async def test_challenge_parks(poster_config: XPosterConfig, fake_x_factory: Any) -> None:
    fake = fake_x_factory(scenario=Scenario.CHALLENGE)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)

    with pytest.raises(ChallengeError):
        await poster.post_thread(make_thread("Challenge me", key="challenge"))

    assert poster.parked() is not None
    info = poster.parked()
    assert info is not None
    assert info.kind == "challenge"


async def test_logged_out_raises_not_logged_in(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(scenario=Scenario.LOGGED_OUT)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)

    with pytest.raises(NotLoggedInError):
        await poster.post_thread(make_thread("Hello", key="logged-out"))


async def test_locked_account_parks(poster_config: XPosterConfig, fake_x_factory: Any) -> None:
    fake = fake_x_factory(scenario=Scenario.LOCKED)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)

    with pytest.raises(AccountRestrictedError):
        await poster.post_thread(make_thread("Hello", key="locked"))

    info = poster.parked()
    assert info is not None
    assert info.kind == "locked"


async def test_ui_drift_saves_artifacts_without_cookies(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(scenario=Scenario.UI_DRIFT)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = make_thread("First", "Second", key="drift")

    with pytest.raises(UiChangedError) as excinfo:
        await poster.post_thread(thread)

    artifacts = excinfo.value.artifacts
    assert artifacts is not None
    assert artifacts.is_dir()
    screenshot = artifacts / "screenshot.png"
    assert screenshot.is_file() and screenshot.stat().st_size > 0
    for path in artifacts.iterdir():
        if path.is_file():
            payload = path.read_bytes()
            assert _COOKIE_VALUE.encode() not in payload
            assert _CT0_VALUE.encode() not in payload
            assert ARTIFACT_CANARY.encode() not in payload
            assert path.stat().st_mode & 0o777 == 0o600, path
    html = (artifacts / "page.html").read_text(encoding="utf-8")
    assert "[REDACTED]" in html, "hidden inputs and csrf meta values must be redacted"
    assert 'name="csrf-token"' in html
    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.FAILED


async def test_fail_at_n_is_unknown_and_blocks_retry_until_force(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(scenario=Scenario.FAIL_AT_N, fail_at=2)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = make_thread("One", "Two", "Three", key="fail-at-n")

    with pytest.raises(PostOutcomeUnknownError) as excinfo:
        await poster.post_thread(thread)

    assert [tweet.index for tweet in excinfo.value.posted] == [0]
    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.UNKNOWN

    with pytest.raises(PostOutcomeUnknownError, match="unknown outcome"):
        await poster.post_thread(thread)
    assert [tweet.text for tweet in fake.tweets] == ["One"]

    # The composer cannot continue a partial thread; force must not silently
    # re-post the recorded tweet.
    with pytest.raises(PostOutcomeUnknownError, match="discard-recorded"):
        await poster.post_thread(thread, force=True)
    assert [tweet.text for tweet in fake.tweets] == ["One"]
    assert len({tweet.text for tweet in fake.tweets}) == len(fake.tweets)

    # The operator deleted the partial tweet; discard_recorded starts clean.
    fake.set_scenario(Scenario.OK)
    fake.reset()
    discarded = await poster.post_thread(thread, discard_recorded=True)
    assert discarded.complete is True
    assert [tweet.text for tweet in fake.tweets] == ["One", "Two", "Three"]
    assert len({tweet.text for tweet in fake.tweets}) == len(fake.tweets)


async def test_server_error_once_is_reclicked_exactly_once(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(scenario=Scenario.SERVER_ERROR_ONCE)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)

    posted = await poster.post_thread(make_thread("Transient", key="server-once"))

    assert posted.complete is True
    assert len(fake.create_requests) == 2, "one refused click, one re-click"
    assert len(fake.tweets) == 1


async def test_over_weight_text_is_rejected_before_chrome(
    poster_config: XPosterConfig,
) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    thread = make_thread("a" * 300, key="too-long")

    with pytest.raises(ValidationError, match="weighted count"):
        await poster.post_thread(thread)


async def test_a_thread_longer_than_twenty_five_is_rejected(
    poster_config: XPosterConfig,
) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    thread = Thread(tuple(Tweet(f"Tweet {index}") for index in range(26)), key="long")

    with pytest.raises(ValidationError, match="at most 25"):
        await poster.post_thread(thread)


async def test_delete_thread_removes_tweets_newest_first(
    poster_config: XPosterConfig, fake_x: FakeX
) -> None:
    seed_session(poster_config)
    poster = make_poster(poster_config, fake_x)
    posted = await poster.post_thread(make_thread("To delete one", "To delete two"))

    deleted = await poster.delete_thread(posted)

    assert deleted == [posted.tweets[1].id, posted.tweets[0].id]
    assert fake_x.deleted == deleted
    assert all(tweet.deleted for tweet in fake_x.tweets)


async def test_post_text_keeps_the_first_tweet_verbatim(
    poster_config: XPosterConfig, fake_x: FakeX
) -> None:
    seed_session(poster_config)
    poster = make_poster(poster_config, fake_x)
    first = "Index: this opening post must never be re-flowed"
    body = "word " * 90
    expected = split_with_first(first, body, limit=poster_config.max_tweet_weight)

    posted = await poster.post_text(body, first=first)

    assert posted.complete is True
    assert len(expected) > 1
    assert [tweet.text for tweet in fake_x.tweets] == expected
    assert fake_x.tweets[0].text == first


def test_session_status_is_offline_and_reports_the_saved_state(
    poster_config: XPosterConfig,
) -> None:
    seed_session(poster_config)
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)

    summary = poster.session_status()

    assert summary.present is True
    assert summary.has_auth_token is True
    assert summary.has_ct0 is True
    assert summary.expired is False
    assert poster.parked() is None
    assert poster.unpark() is False


def test_ledger_record_round_trips_through_the_poster(
    poster_config: XPosterConfig,
) -> None:
    """A PostedThread written by the ledger is visible to a fresh poster."""
    ledger = Ledger(poster_config.ledger_path)
    poster = XPoster(poster_config, ledger=ledger, playwright_factory=forbidden_factory)
    thread = make_thread("Round trip")
    tweet = PostedTweet(0, "12345", "https://x.com/olsa_test/status/12345", text_hash("Round trip"))
    ledger.begin(thread.content_hash, strategy="composer", handle="olsa_test", n_tweets=1)
    ledger.complete(
        thread.content_hash,
        PostedThread(
            thread_hash=thread.content_hash,
            handle="olsa_test",
            strategy="composer",
            posted_at="2026-11-09T12:00:00+00:00",
            tweets=(tweet,),
            complete=True,
        ),
    )

    assert poster.ledger.get(thread.content_hash) is not None
    assert poster.parked() is None


@pytest.mark.parametrize("dom", ["new", "legacy"])
async def test_a_two_paragraph_tweet_keeps_the_blank_line(
    poster_config: XPosterConfig, fake_x_factory: Any, dom: str
) -> None:
    """A newline must be Shift+Return, so a blank line can never submit early.

    The fake ignores a bare Return exactly as X's composer treats it as
    submit; if the poster regresses to Return, the blank line disappears and
    this assertion fails. Both client flavours are covered because the legacy
    composer is a contenteditable, not a textarea.
    """
    fake = fake_x_factory(dom=dom)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    text = "First paragraph.\n\nSecond paragraph."

    posted = await poster.post_thread(make_thread(text, key=f"paragraphs-{dom}"))

    assert posted.complete is True
    assert fake.tweets[0].text == text
    assert posted.tweets[0].text_sha256 == text_hash(text)


_STICKY_PAGE = """
<!doctype html><html><body style="margin:0">
  <div id="cover" style="position:sticky;top:0;height:300px;z-index:10;
       background:rgba(255,0,0,0.95)"></div>
  <button id="target" style="display:block;margin:1000px 0 0 100px;
       width:240px;height:60px">Click me</button>
  <div style="height:3000px"></div>
  <script>
    window.clicks = 0;
    document.getElementById('target').addEventListener('click', () => {
      window.clicks += 1;
    });
    window.scrollTo(0, 1150);
  </script>
</body></html>
"""

_ALWAYS_COVERED_PAGE = """
<!doctype html><html><body style="margin:0">
  <div id="cover" style="position:fixed;inset:0;z-index:10;
       background:rgba(255,0,0,0.95)"></div>
  <button id="target" style="position:absolute;top:200px;left:100px;
       width:240px;height:60px">Click me</button>
  <script>
    window.clicks = 0;
    document.getElementById('target').addEventListener('click', () => {
      window.clicks += 1;
    });
  </script>
</body></html>
"""


async def test_human_click_scrolls_out_from_under_a_sticky_overlay(
    poster_config: XPosterConfig,
) -> None:
    humanizer = Humanizer(poster_config.human)
    async with open_plain_page(poster_config) as page:
        await page.set_content(_STICKY_PAGE)
        target = page.locator("#target")
        box = await target.bounding_box()
        assert box is not None
        covered_by = await page.evaluate(
            "([x, y]) => document.elementFromPoint(x, y)?.id",
            [box["x"] + 10, box["y"] + 10],
        )
        assert covered_by == "cover", "the fixture must start with the target covered"

        with patch.object(
            Locator, "click", side_effect=AssertionError("fell back to locator.click")
        ):
            point = await human_click(page, target, humanizer, sleep=no_sleep, timeout_ms=3_000)

        assert await page.evaluate("window.clicks") == 1
        assert point[1] > 300, "the click must move after the element is centred"


async def test_human_click_never_clicks_through_an_overlay(
    poster_config: XPosterConfig,
) -> None:
    humanizer = Humanizer(poster_config.human)
    async with open_plain_page(poster_config) as page:
        await page.set_content(_ALWAYS_COVERED_PAGE)
        target = page.locator("#target")

        with pytest.raises(PlaywrightError):
            await human_click(page, target, humanizer, sleep=no_sleep, timeout_ms=1_200)

        assert await page.evaluate("window.clicks") == 0, "the overlay must never be clicked"


_FOCAL_PAGE = """
<!doctype html><html><body><main>
  <article data-fx-article="unrelated" itemprop="SocialMediaPosting">
    <a href="/olsa_test/status/111">permalink</a>
    <button aria-label="Reply" onclick="window.clicked='unrelated'">Reply</button>
    <button aria-label="More" onclick="window.clicked='unrelated-more'">More</button>
  </article>
  <article data-fx-article="target" itemprop="SocialMediaPosting">
    <a href="/olsa_test/status/222">permalink</a>
    <button aria-label="Reply" onclick="window.clicked='target'">Reply</button>
    <button aria-label="More" onclick="window.clicked='target-more'">More</button>
  </article>
</main></body></html>
"""


async def test_focal_article_scopes_reply_and_more_to_the_target_status(
    poster_config: XPosterConfig,
) -> None:
    """An unrelated article first in the DOM must not receive the action."""
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    async with open_plain_page(poster_config) as page:
        await page.set_content(_FOCAL_PAGE)

        scope = await poster._focal_scope(page, "222")
        assert await scope.get_attribute("data-fx-article") == "target"
        reply = await selectors.locate(scope, "tweet_reply_button", timeout_ms=600)
        await reply.click()
        assert await page.evaluate("window.clicked") == "target"

        fallback = await poster._focal_scope(page, "999")
        assert await fallback.get_attribute("data-fx-article") == "unrelated"


async def test_a_successful_thread_leaks_no_cookie_value_anywhere(
    poster_config: XPosterConfig,
    fake_x: FakeX,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The canary cookie lives only in the credential file, nowhere observable."""
    caplog.set_level(logging.DEBUG)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake_x)

    posted = await poster.post_thread(make_thread("Nothing secret here", key="no-secrets"))

    assert posted.complete is True
    canaries = (_COOKIE_VALUE, _CT0_VALUE)

    for record in caplog.records:
        message = record.getMessage()
        for canary in canaries:
            assert canary not in message, f"{record.name} logged a cookie value"

    for payload in (json.dumps(posted.to_json()), json.dumps(poster.session_status().to_json())):
        for canary in canaries:
            assert canary not in payload

    state_files = [
        poster_config.ledger_path,
        poster_config.meta_path,
        poster_config.parked_path,
    ]
    artifacts = poster_config.artifacts_dir
    if artifacts.exists():
        state_files.extend(path for path in artifacts.rglob("*") if path.is_file())
    existing = [path for path in state_files if path.is_file()]
    assert poster_config.ledger_path in existing
    assert poster_config.meta_path in existing
    for path in existing:
        payload = path.read_bytes()
        for canary in canaries:
            assert canary.encode() not in payload, path

    # Control: the canary really is in the saved credential, so the assertions
    # above would have caught a copy of it anywhere else.
    assert _COOKIE_VALUE.encode() in poster_config.storage_state_path.read_bytes()


async def test_a_dispatched_click_interrupted_before_the_response_blocks_the_rerun(
    poster_config: XPosterConfig, fake_x: FakeX
) -> None:
    """C1: the click barrier is on disk before the click, not only in memory."""
    seed_session(poster_config)
    poster = make_poster(poster_config, fake_x)
    thread = make_thread("Interrupted after dispatch", key="click-crash")
    original_click = poster._click
    clicks = {"count": 0}

    async def click_then_crash(page: Page, locator: Locator) -> None:
        clicks["count"] += 1
        await original_click(page, locator)
        if clicks["count"] == 2:
            raise KeyboardInterrupt

    poster._click = click_then_crash  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        await poster.post_thread(thread)

    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.PENDING
    assert entry.clicking_at, "the click barrier must be fsynced before the click"
    assert entry.posted == ()

    blocked = make_poster(poster_config, fake_x, playwright_factory=forbidden_factory)
    with pytest.raises(PostOutcomeUnknownError, match="unknown outcome"):
        await blocked.post_thread(thread)


async def test_a_pre_dispatch_click_failure_is_not_unknown(
    poster_config: XPosterConfig, fake_x: FakeX
) -> None:
    """A click that provably never reached the page clears the barrier."""
    seed_session(poster_config)
    poster = make_poster(poster_config, fake_x)
    thread = make_thread("Never dispatched", key="pre-dispatch")
    original_click = poster._click
    clicks = {"count": 0}

    async def fail_on_the_submit(page: Page, locator: Locator) -> None:
        clicks["count"] += 1
        if clicks["count"] == 2:
            raise ClickNotDispatchedError("the button detached before the pointer moved")
        await original_click(page, locator)

    poster._click = fail_on_the_submit  # type: ignore[method-assign]

    with pytest.raises(UiChangedError):
        await poster.post_thread(thread)

    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.FAILED
    assert entry.clicking_at == ""
    assert fake_x.tweets == []


async def test_an_iframe_only_challenge_parks(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    """D1: the challenge has no text in the top document, only an Arkose iframe."""
    fake = fake_x_factory(scenario=Scenario.CHALLENGE_IFRAME)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)

    with pytest.raises(ChallengeError):
        await poster.post_thread(make_thread("Challenge", key="iframe-challenge"))

    assert poster.parked() is not None


async def test_http_403_with_automation_body_parks(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    """D3: HTTP 403 + body code 226 is a behavioural block, not a login problem."""
    fake = fake_x_factory(scenario=Scenario.FORBIDDEN_226)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)

    with pytest.raises(ChallengeError):
        await poster.post_thread(make_thread("Blocked", key="403-226"))

    assert poster.parked() is not None
    assert len(fake.create_requests) == 1, "a park must never be retried"


async def test_http_403_with_html_body_parks(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    """D3: an unparseable 403 is a challenge, not a session to re-login."""
    fake = fake_x_factory(scenario=Scenario.FORBIDDEN_HTML)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)

    with pytest.raises(ChallengeError):
        await poster.post_thread(make_thread("Blocked", key="403-html"))

    assert poster.parked() is not None
    assert len(fake.create_requests) == 1


async def test_unknown_error_code_parks(poster_config: XPosterConfig, fake_x_factory: Any) -> None:
    """D2: an unrecognised code (261 read-only mode) must park, never retry."""
    fake = fake_x_factory(scenario=Scenario.UNKNOWN_CODE)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)

    with pytest.raises(ChallengeError):
        await poster.post_thread(make_thread("Read only", key="unknown-code"))

    assert poster.parked() is not None
    assert len(fake.create_requests) == 1


async def test_a_status_post_mentioning_captcha_does_not_park(
    poster_config: XPosterConfig, fake_x: FakeX
) -> None:
    """D4: text is classified strictly and only inside alert containers."""
    canary_id = "1000000000000000123"
    fake_x.tweets.append(
        FakeTweet(
            id=canary_id,
            text="A thread about captcha wording and how products authenticate",
            author=fake_x.handle,
            in_reply_to=None,
            media=(),
        )
    )
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)

    async with open_fake_context(poster_config, fake_x) as (context, page):
        await page.goto(f"https://x.com/{fake_x.handle}/status/{canary_id}")
        body = await page.inner_text("body")
        assert "captcha" in body.lower()
        assert "authenticate" in body.lower()
        assert await poster._page_verdict(page) is None


@pytest.mark.parametrize("dom", ["new", "legacy"])
async def test_alt_text_is_applied_and_echoed_in_the_request(
    poster_config: XPosterConfig, fake_x_factory: Any, tmp_path: Path, dom: str
) -> None:
    """F3: the alt editor is used before posting, and the text reaches X."""
    fake = fake_x_factory(dom=dom)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake, allow_alt_text=True)
    image = tmp_path / "image.png"
    image.write_bytes(_PNG_1X1)
    thread = Thread(
        (Tweet("Described image", media=(image,), alt_texts=("A tiny test pixel",)),),
        key=f"alt-{dom}",
    )

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    entities = fake.create_requests[0]["variables"]["media"]["media_entities"]
    assert entities[0]["alt_text"] == "A tiny test pixel"


async def test_a_corrupt_ledger_blocks_and_reset_ledger_repairs(
    poster_config: XPosterConfig,
) -> None:
    poster_config.ledger_path.write_bytes(b"{ this is not a ledger")
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    thread = make_thread("Corrupt", key="corrupt")

    with pytest.raises(LedgerCorruptError):
        await poster.post_thread(thread)

    moved = poster.reset_ledger()

    assert moved.exists()
    assert not poster_config.ledger_path.exists()
    assert poster.ledger.decide(thread.content_hash) is Decision.NEW


def test_logout_keeps_the_credential_backup_by_default(
    poster_config: XPosterConfig,
) -> None:
    seed_session(poster_config)
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)

    poster.logout()

    assert list(poster_config.profile_dir.iterdir()) == []
    assert not poster_config.storage_state_path.exists()
    pre_reset = poster_config.storage_state_path.with_name("storage_state.json.pre-reset")
    assert pre_reset.is_file()


@pytest.mark.parametrize("dom", ["new", "legacy"])
async def test_emoji_flag_and_combining_sequences_type_exactly(
    poster_config: XPosterConfig, fake_x_factory: Any, dom: str
) -> None:
    """Real key events must reproduce astral/ZWJ/combining text exactly."""
    fake = fake_x_factory(dom=dom)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    text = "Family 👨\u200d👩\u200d👧\u200d👦 flag 🇫🇷 combining e\u0301"
    thread = make_thread(text, key=f"emoji-{dom}")

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    assert fake.tweets[0].text == text


async def test_a_dropped_character_fails_before_the_click(
    poster_config: XPosterConfig, fake_x: FakeX
) -> None:
    """A read-back mismatch is a pre-click failure: FAILED, not UNKNOWN."""
    seed_session(poster_config)
    poster = make_poster(poster_config, fake_x)
    original_type = poster._type

    async def drop_the_last_character(page: Page, text: str) -> None:
        await original_type(page, text[:-1])

    poster._type = drop_the_last_character  # type: ignore[method-assign]
    thread = make_thread("Dropped character", key="typed-mismatch")

    with pytest.raises(UiChangedError) as excinfo:
        await poster.post_thread(thread)

    assert fake_x.tweets == []
    assert fake_x.create_requests == []
    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.FAILED
    assert entry.clicking_at == ""
    artifacts = excinfo.value.artifacts
    assert artifacts is not None
    mismatch = artifacts / "typed_mismatch.txt"
    assert mismatch.is_file()
    assert mismatch.stat().st_mode & 0o777 == 0o600
    summary = mismatch.read_text(encoding="utf-8")
    assert "first_difference_index" in summary
    assert "Dropped character" in summary


def test_normalise_editor_text_handles_nbsp_and_editor_artifacts() -> None:
    assert _normalise_editor_text("a\u00a0b") == "a b"
    assert _normalise_editor_text("line one\r\nline two") == "line one\nline two"
    assert _normalise_editor_text("line one\n") == "line one"
    assert _normalise_editor_text("line one\n\n") == "line one\n"
    assert _normalise_editor_text("zero\u200bwidth\ufeff") == "zerowidth"
    assert _normalise_editor_text("e\u0301") == "\u00e9"
    assert _normalise_editor_text("👨\u200d👩\u200d👧") == "👨\u200d👩\u200d👧"


def test_typed_mismatch_summary_reports_lengths_and_first_difference() -> None:
    summary = _typed_mismatch_summary("hello", "help")

    assert "expected_length: 5" in summary
    assert "actual_length: 4" in summary
    assert "first_difference_index: 3" in summary
    assert "hello" in summary


async def test_keyboard_insert_text_is_never_used(
    poster_config: XPosterConfig, fake_x: FakeX
) -> None:
    """Paste-style insertion leaves Post disabled on the live React composer."""
    seed_session(poster_config)
    poster = make_poster(poster_config, fake_x)

    with patch.object(
        Keyboard, "insert_text", side_effect=AssertionError("insert_text must not be used")
    ) as spy:
        posted = await poster.post_thread(make_thread("Real keys only", key="no-paste"))

    assert posted.complete is True
    assert spy.call_count == 0


async def test_submit_shortcut_fallback_posts_when_the_button_is_covered(
    poster_config: XPosterConfig, fake_x: FakeX
) -> None:
    """The opt-in keyboard fallback submits when both click paths are blocked."""
    seed_session(poster_config)
    poster = XPoster(
        poster_config,
        context_hook=fake_x.install,
        sleep=no_sleep,
        submit_shortcut_fallback=True,
    )
    original_click = poster._click
    clicks = {"count": 0}

    async def cover_the_submit(page: Page, locator: Locator) -> None:
        clicks["count"] += 1
        if clicks["count"] == 2:
            await page.evaluate(
                """() => {
                    const cover = document.createElement('div');
                    cover.style.cssText =
                        'position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,0.01)';
                    document.body.appendChild(cover);
                }"""
            )
        await original_click(page, locator)

    poster._click = cover_the_submit  # type: ignore[method-assign]

    posted = await poster.post_thread(make_thread("Shortcut submit", key="shortcut"))

    assert posted.complete is True
    assert [tweet.text for tweet in fake_x.tweets] == ["Shortcut submit"]


async def test_legacy_modal_thread_types_only_into_the_modal(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    """The live classic page has an inline composer and a modal with the same id."""
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    background_texts: list[list[str]] = []
    original_verify = poster._verify_typed_text

    async def verify_and_inspect_background(
        page: Page, editor: Locator, expected: str, attempt: Any
    ) -> None:
        background_texts.append(
            await page.evaluate(
                """() => Array.from(document.querySelectorAll(
                    '[data-fx-inline-composer] [data-testid="tweetTextarea_0"]'
                )).map((element) => element.innerText)"""
            )
        )
        await original_verify(page, editor, expected, attempt)

    poster._verify_typed_text = verify_and_inspect_background  # type: ignore[method-assign]
    thread = make_thread("Modal first", "Modal second", key="legacy-modal")

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    assert [tweet.text for tweet in fake.tweets] == ["Modal first", "Modal second"]
    assert len(background_texts) == 2
    assert all(texts == [""] for texts in background_texts), "the inline composer must stay empty"
    assert len(fake.create_requests) == 2


async def test_legacy_dry_run_uses_the_modal_and_leaves_nothing(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = make_thread("Dry modal", key="legacy-dry")

    posted = await poster.post_thread(thread, dry_run=True)

    assert posted.strategy == "dry-run"
    assert posted.tweets == ()
    assert fake.tweets == []
    assert fake.create_requests == []


async def test_ui_drift_screenshot_covers_only_the_viewport(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    """A failure artifact must not be a multi-megabyte full-page image."""
    fake = fake_x_factory(dom="legacy", scenario=Scenario.UI_DRIFT)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    viewport: dict[str, float] = {}
    original_capture = poster._capture_artifacts

    async def capture_with_viewport(page: Page, label: str) -> Path | None:
        viewport["height"] = await page.evaluate("window.innerHeight")
        viewport["dpr"] = await page.evaluate("window.devicePixelRatio")
        return await original_capture(page, label)

    poster._capture_artifacts = capture_with_viewport  # type: ignore[method-assign]

    with pytest.raises(UiChangedError) as excinfo:
        await poster.post_thread(make_thread("One", "Two", key="screenshot-viewport"))

    artifacts = excinfo.value.artifacts
    assert artifacts is not None
    screenshot = artifacts / "screenshot.png"
    payload = screenshot.read_bytes()
    height = int.from_bytes(payload[20:24], "big")
    assert viewport["height"] > 0
    assert height <= viewport["height"] * viewport["dpr"] + 20


_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_DRAFTJS_EDITOR = _FIXTURES_DIR / "live_draftjs_editor_2026-11-10.html"
_DRAFTJS_EDITOR_THREE = _FIXTURES_DIR / "live_draftjs_editor_2026-11-10_three_paragraphs.html"
_DRAFTJS_TEXT = (
    "[xposter dry-run 2026-11-10] First tweet, paragraph one.\n\nParagraph two of the first tweet."
)


async def _draft_fixture_text(page: Page, fixture: Path) -> str:
    await page.set_content(fixture.read_text(encoding="utf-8"))
    editor = page.locator('[data-testid="tweetTextarea_0"]')
    return await _read_editor_text(editor)


async def test_read_editor_text_reconstructs_real_draftjs_blocks(
    poster_config: XPosterConfig,
) -> None:
    """The live markup must read back as the logical text, not innerText's extra newline."""
    async with open_plain_page(poster_config) as page:
        await page.set_content(_DRAFTJS_EDITOR.read_text(encoding="utf-8"))
        editor = page.locator('[data-testid="tweetTextarea_0"]')

        assert await _read_editor_text(editor) == _DRAFTJS_TEXT
        assert await editor.inner_text() != _DRAFTJS_TEXT


async def test_read_editor_text_handles_three_draftjs_paragraphs(
    poster_config: XPosterConfig,
) -> None:
    expected = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    async with open_plain_page(poster_config) as page:
        assert await _draft_fixture_text(page, _DRAFTJS_EDITOR_THREE) == expected


@pytest.mark.parametrize(
    ("case", "variant"),
    [
        ("dropped_character", _DRAFTJS_TEXT.replace("First tweet", "Frst tweet", 1)),
        ("duplicated_character", _DRAFTJS_TEXT.replace("First tweet", "Firstt tweet", 1)),
        ("missing_blank_line", _DRAFTJS_TEXT.replace("\n\n", "\n", 1)),
        ("extra_blank_line", _DRAFTJS_TEXT.replace("\n\n", "\n\n\n", 1)),
    ],
)
async def test_draftjs_readback_rejects_inexact_text(
    poster_config: XPosterConfig, case: str, variant: str
) -> None:
    """Exact comparison must catch a character change and blank-line changes."""
    async with open_plain_page(poster_config) as page:
        actual = await _draft_fixture_text(page, _DRAFTJS_EDITOR)

    assert _normalise_editor_text(actual) != _normalise_editor_text(variant), case


async def test_legacy_editor_uses_draftjs_blocks_and_posts_the_logical_text(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    text = "Paragraph one.\n\nParagraph two."
    structures: list[list[dict[str, Any]]] = []
    original_verify = poster._verify_typed_text

    async def verify_and_inspect(page: Page, editor: Locator, expected: str, attempt: Any) -> None:
        structures.append(
            await page.evaluate(
                """() => Array.from(document.querySelectorAll(
                    '[data-fx-composer] [data-testid="tweetTextarea_0"] [data-block="true"]'
                )).map((block) => ({
                    text: block.textContent || '',
                    placeholders: block.querySelectorAll('br[data-text="true"]').length,
                    spans: block.querySelectorAll('span[data-text="true"]').length,
                }))"""
            )
        )
        await original_verify(page, editor, expected, attempt)

    poster._verify_typed_text = verify_and_inspect  # type: ignore[method-assign]

    posted = await poster.post_thread(make_thread(text, key="draft-blocks"))

    assert posted.complete is True
    assert fake.tweets[0].text == text
    blocks = structures[0]
    assert [block["text"] for block in blocks] == ["Paragraph one.", "", "Paragraph two."]
    assert blocks[1]["placeholders"] == 1
    assert blocks[0]["spans"] == 1
    assert blocks[2]["spans"] == 1


async def test_a_single_image_with_text_posts(
    poster_config: XPosterConfig, fake_x: FakeX, tmp_path: Path
) -> None:
    image = tmp_path / "single.png"
    image.write_bytes(_PNG_1X1)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake_x)
    thread = Thread((Tweet("A single image", media=(image,)),), key="single-image")

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    entities = fake_x.create_requests[0]["variables"]["media"]["media_entities"]
    assert len(entities) == 1 and entities[0]["media_id"]
    assert fake_x.tweets[0].media == ("single.png",)


async def test_four_images_attach_to_one_tweet(
    poster_config: XPosterConfig, fake_x: FakeX, tmp_path: Path
) -> None:
    images = []
    for index in range(4):
        image = tmp_path / f"image-{index}.png"
        image.write_bytes(_PNG_1X1)
        images.append(image)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake_x)
    thread = Thread((Tweet("Four images", media=tuple(images)),), key="four-images")

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    entities = fake_x.create_requests[0]["variables"]["media"]["media_entities"]
    assert len(entities) == 4
    assert len({entity["media_id"] for entity in entities}) == 4
    assert fake_x.tweets[0].media == tuple(image.name for image in images)


async def test_images_and_alt_text_stay_with_their_own_tweet(
    poster_config: XPosterConfig, fake_x: FakeX, tmp_path: Path
) -> None:
    first = tmp_path / "first.png"
    first.write_bytes(_PNG_1X1)
    third_a = tmp_path / "third-a.png"
    third_a.write_bytes(_PNG_1X1)
    third_b = tmp_path / "third-b.png"
    third_b.write_bytes(_PNG_1X1)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake_x, allow_alt_text=True)
    thread = Thread(
        (
            Tweet("First tweet", media=(first,), alt_texts=("first alt",)),
            Tweet("Second tweet"),
            Tweet(
                "Third tweet",
                media=(third_a, third_b),
                alt_texts=("third alt a", "third alt b"),
            ),
        ),
        key="thread-media",
    )

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    bodies = fake_x.create_requests
    assert [len(body["variables"]["media"]["media_entities"]) for body in bodies] == [1, 0, 2]
    first_entities = bodies[0]["variables"]["media"]["media_entities"]
    third_entities = bodies[2]["variables"]["media"]["media_entities"]
    assert [entity["alt_text"] for entity in first_entities] == ["first alt"]
    assert [entity["alt_text"] for entity in third_entities] == ["third alt a", "third alt b"]
    assert [tweet.media for tweet in fake_x.tweets] == [
        ("first.png",),
        (),
        ("third-a.png", "third-b.png"),
    ]


async def test_replies_strategy_attaches_an_image_to_a_reply(
    poster_config: XPosterConfig, fake_x: FakeX, tmp_path: Path
) -> None:
    image = tmp_path / "reply.png"
    image.write_bytes(_PNG_1X1)
    seed_session(poster_config)
    config = poster_config.with_overrides(strategy=Strategy.REPLIES)
    poster = make_poster(config, fake_x)
    thread = Thread(
        (Tweet("Root post"), Tweet("Reply with an image", media=(image,))),
        key="reply-media",
    )

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    bodies = fake_x.create_requests
    assert len(bodies) == 2
    assert bodies[0]["variables"]["media"]["media_entities"] == []
    entities = bodies[1]["variables"]["media"]["media_entities"]
    assert len(entities) == 1 and entities[0]["media_id"]
    assert fake_x.tweets[1].media == ("reply.png",)


async def test_a_slow_upload_still_posts(
    poster_config: XPosterConfig, fake_x_factory: Any, tmp_path: Path
) -> None:
    fake = fake_x_factory(upload_ms=2500)
    image = tmp_path / "slow.png"
    image.write_bytes(_PNG_1X1)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = Thread((Tweet("Slow upload", media=(image,)),), key="slow-upload")

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    assert [tweet.text for tweet in fake.tweets] == ["Slow upload"]
    assert fake.tweets[0].media == ("slow.png",)


def _short_upload_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("xposter.poster._UPLOAD_TIMEOUT_BASE_S", 0.6)
    monkeypatch.setattr("xposter.poster._UPLOAD_TIMEOUT_PER_MIB_S", 0.0)
    monkeypatch.setattr("xposter.poster._UPLOAD_TIMEOUT_CAP_S", 0.6)


async def test_a_never_finishing_upload_fails_before_the_click(
    poster_config: XPosterConfig,
    fake_x_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _short_upload_timeout(monkeypatch)
    fake = fake_x_factory(scenario=Scenario.UPLOAD_HANGS)
    image = tmp_path / "hang.png"
    image.write_bytes(_PNG_1X1)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = Thread((Tweet("Hanging upload", media=(image,)),), key="upload-hang")

    with pytest.raises(UiChangedError, match="media upload did not finish"):
        await poster.post_thread(thread)

    assert fake.tweets == []
    assert fake.create_requests == []
    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.FAILED
    assert entry.clicking_at == ""


async def test_a_failed_upload_keeps_post_disabled_and_never_posts(
    poster_config: XPosterConfig,
    fake_x_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _short_upload_timeout(monkeypatch)
    fake = fake_x_factory(scenario=Scenario.UPLOAD_FAILS)
    image = tmp_path / "fail.png"
    image.write_bytes(_PNG_1X1)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = Thread((Tweet("Failed upload", media=(image,)),), key="upload-fail")

    with pytest.raises(UiChangedError, match="media upload did not finish"):
        await poster.post_thread(thread)

    assert fake.tweets == []
    assert fake.create_requests == []
    assert poster.ledger.get(thread.content_hash).status is LedgerStatus.FAILED


def _bad_media_case(tmp_path: Path, case: str) -> list[Path]:
    if case == "wrong_magic":
        path = tmp_path / "mismatch.jpg"
        path.write_bytes(_PNG_1X1)
        return [path]
    if case == "too_big":
        path = tmp_path / "too-big.png"
        with path.open("wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")
            handle.truncate(MAX_IMAGE_BYTES + 1)
        return [path]
    if case == "gif_mix":
        gif = tmp_path / "anim.gif"
        gif.write_bytes(b"GIF89a" + b"\x00" * 16)
        still = tmp_path / "still.png"
        still.write_bytes(_PNG_1X1)
        return [gif, still]
    if case == "video":
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return [path]
    if case == "duplicate":
        path = tmp_path / "same.png"
        path.write_bytes(_PNG_1X1)
        return [path, path]
    if case == "zero_byte":
        path = tmp_path / "empty.png"
        path.write_bytes(b"")
        return [path]
    raise AssertionError(f"unknown media case {case}")


@pytest.mark.parametrize(
    "case",
    ["wrong_magic", "too_big", "gif_mix", "video", "duplicate", "zero_byte"],
)
async def test_bad_media_is_rejected_before_chrome(
    poster_config: XPosterConfig, tmp_path: Path, case: str
) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    media = _bad_media_case(tmp_path, case)
    thread = Thread((Tweet("Bad media", media=tuple(media)),), key=f"bad-media-{case}")

    with pytest.raises(ValidationError):
        await poster.post_thread(thread)


_LIVE_MEDIA_FIXTURE = _FIXTURES_DIR / "live_media_composer_2026-11-10.html"


async def _load_live_media_page(page: Page) -> None:
    await page.set_content(_LIVE_MEDIA_FIXTURE.read_text(encoding="utf-8"))


async def test_the_real_live_media_fixture_is_ready(poster_config: XPosterConfig) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    async with open_plain_page(poster_config) as page:
        await _load_live_media_page(page)

        assert await poster._count_media_previews(page) == 1
        assert await poster._attachment_progress_count(page) == 0
        assert await poster._previews_loaded(page) is True
        assert await poster._submit_enabled(page) is True
        await poster._wait_for_media_ready(page, before=0, expected=1, timeout_s=2.0)


async def test_a_blob_preview_without_natural_size_is_accepted(
    poster_config: XPosterConfig,
) -> None:
    """The live failure: a rendered blob preview reports 0x0 natural size."""
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    async with open_plain_page(poster_config) as page:
        await _load_live_media_page(page)
        await page.evaluate(
            """() => {
                const img = document.querySelector(
                    '[role="dialog"][aria-modal="true"] [data-testid="attachments"] img');
                img.src = 'blob:https://x.com/deadbeef-never-loads';
            }"""
        )
        state = await page.evaluate(
            """() => {
                const img = document.querySelector(
                    '[role="dialog"][aria-modal="true"] [data-testid="attachments"] img');
                const rect = img.getBoundingClientRect();
                return {
                    naturalWidth: img.naturalWidth,
                    width: rect.width,
                    edit: !!img.closest('[role="group"][aria-label="Media"]')
                        ?.querySelector('button[aria-label="Edit media"]'),
                };
            }"""
        )
        assert state["naturalWidth"] == 0
        assert state["width"] > 0
        assert state["edit"] is True

        assert await poster._previews_loaded(page) is True
        await poster._wait_for_media_ready(page, before=0, expected=1, timeout_s=2.0)


async def test_a_progressbar_inside_attachments_blocks_readiness(
    poster_config: XPosterConfig,
) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    async with open_plain_page(poster_config) as page:
        await _load_live_media_page(page)
        await page.evaluate(
            """() => {
                const bar = document.createElement('div');
                bar.setAttribute('role', 'progressbar');
                document.querySelector(
                    '[role="dialog"][aria-modal="true"] [data-testid="attachments"]'
                ).appendChild(bar);
            }"""
        )

        assert await poster._attachment_progress_count(page) == 1
        with pytest.raises(UiChangedError, match="upload indicator"):
            await poster._wait_for_media_ready(page, before=0, expected=1, timeout_s=0.3)


async def test_a_decoded_preview_with_a_disabled_post_is_not_ready(
    poster_config: XPosterConfig,
) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    async with open_plain_page(poster_config) as page:
        await _load_live_media_page(page)
        await page.evaluate(
            """() => {
                const button = document.querySelector(
                    '[role="dialog"][aria-modal="true"] [data-testid="tweetButton"]');
                button.disabled = true;
            }"""
        )

        assert await poster._previews_loaded(page) is True
        assert await poster._submit_enabled(page) is False
        with pytest.raises(UiChangedError) as excinfo:
            await poster._wait_for_media_ready(page, before=0, expected=1, timeout_s=0.3)
        assert "post_enabled=False" in str(excinfo.value)


async def test_upload_timeout_diagnostics_name_each_image_and_the_button(
    poster_config: XPosterConfig,
) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    async with open_plain_page(poster_config) as page:
        await _load_live_media_page(page)
        await page.evaluate(
            """() => {
                const img = document.querySelector(
                    '[role="dialog"][aria-modal="true"] [data-testid="attachments"] img');
                img.src = 'blob:https://x.com/deadbeef-never-loads';
                document.querySelector(
                    '[role="dialog"][aria-modal="true"] [data-testid="tweetButton"]'
                ).disabled = true;
            }"""
        )

        with pytest.raises(UiChangedError) as excinfo:
            await poster._wait_for_media_ready(page, before=0, expected=1, timeout_s=0.3)

    message = str(excinfo.value)
    assert "src=blob" in message
    assert "complete=" in message
    assert "natural=0x0" in message
    assert "bbox=" in message
    assert "post_enabled=False" in message
    assert "Post was not clicked" in message


async def test_preview_count_ignores_timeline_and_avatar_images(
    poster_config: XPosterConfig,
) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    async with open_plain_page(poster_config) as page:
        await _load_live_media_page(page)
        total = await page.evaluate("() => document.querySelectorAll('img').length")

        assert total > 1
        assert await poster._count_media_previews(page) == 1


async def test_images_in_both_editors_of_a_thread(
    poster_config: XPosterConfig, fake_x_factory: Any, tmp_path: Path
) -> None:
    fake = fake_x_factory(dom="legacy")
    first = tmp_path / "first-editor.png"
    first.write_bytes(_PNG_1X1)
    second = tmp_path / "second-editor.png"
    second.write_bytes(_PNG_1X1)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = Thread(
        (Tweet("First editor", media=(first,)), Tweet("Second editor", media=(second,))),
        key="two-editors",
    )

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    bodies = fake.create_requests
    assert [len(body["variables"]["media"]["media_entities"]) for body in bodies] == [1, 1]
    assert [tweet.media for tweet in fake.tweets] == [
        ("first-editor.png",),
        ("second-editor.png",),
    ]


_LIVE_COMPOSE_FIXTURE = _FIXTURES_DIR / "live_compose_page_2026-11-10.html"
_PNG_DATA_URI = "data:image/png;base64," + base64.b64encode(_PNG_1X1).decode("ascii")


async def test_real_compose_fixture_counts_no_media_before_any_attachment(
    poster_config: XPosterConfig,
) -> None:
    """The live bug: an avatar inside the modal must not be the baseline."""
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    async with open_plain_page(poster_config) as page:
        await page.set_content(_LIVE_COMPOSE_FIXTURE.read_text(encoding="utf-8"))

        avatars = await page.evaluate(
            """() => Array.from(document.querySelectorAll(
                '[role="dialog"][aria-modal="true"] img'
            )).filter((img) => img.getAttribute('alt')).length"""
        )
        assert avatars >= 1, "the fixture must contain the avatar that caused the bug"
        assert await poster._count_media_previews(page) == 0


async def test_an_attachment_appearing_moves_the_baseline_from_zero_to_one(
    poster_config: XPosterConfig,
) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    async with open_plain_page(poster_config) as page:
        await page.set_content(_LIVE_COMPOSE_FIXTURE.read_text(encoding="utf-8"))
        await page.evaluate(
            """() => {
                const modal = document.querySelector('[role="dialog"][aria-modal="true"]');
                const box = document.createElement('div');
                box.setAttribute('data-testid', 'attachments');
                box.style.display = 'none';
                modal.appendChild(box);
            }"""
        )
        assert await poster._count_media_previews(page) == 0

        await page.evaluate(
            """(src) => {
                const box = document.querySelector('[data-testid="attachments"]');
                box.style.display = '';
                const image = document.createElement('img');
                image.src = src;
                box.appendChild(image);
            }""",
            _PNG_DATA_URI,
        )

        assert await poster._count_media_previews(page) == 1
        assert await poster._previews_loaded(page) is True


async def test_the_media_fixture_counts_exactly_one_preview(
    poster_config: XPosterConfig,
) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    async with open_plain_page(poster_config) as page:
        await page.set_content(_LIVE_MEDIA_FIXTURE.read_text(encoding="utf-8"))

        assert await poster._count_media_previews(page) == 1


async def test_uncountable_attachment_is_reported_quickly(
    poster_config: XPosterConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A visible attachment that the counter cannot see must not look silent."""
    monkeypatch.setattr("xposter.poster._MEDIA_UNCOUNTED_GRACE_S", 0.6)
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    async with open_plain_page(poster_config) as page:
        await page.set_content(_LIVE_MEDIA_FIXTURE.read_text(encoding="utf-8"))
        await page.evaluate(
            """() => {
                const box = document.querySelector('[data-testid="attachments"]');
                box.removeAttribute('data-testid');
                const group = box.querySelector('[role="group"]');
                group.removeAttribute('aria-label');
                group.removeAttribute('role');
            }"""
        )

        assert await poster._count_media_previews(page) == 0
        assert await poster._has_attachment_controls(page) is True
        assert await poster._submit_enabled(page) is True

        with pytest.raises(UiChangedError) as excinfo:
            await poster._wait_for_media_ready(page, before=0, expected=1, timeout_s=30.0)

    message = str(excinfo.value)
    assert "attachment appeared but could not be counted" in message
    assert "post_enabled=True" in message
    assert "Post was not clicked" in message


async def test_fake_empty_composer_counts_no_media_despite_the_avatar(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    async with open_fake_context(poster_config, fake) as (_context, page):
        await page.goto("https://x.com/compose/post")

        avatars = await page.evaluate(
            """() => Array.from(document.querySelectorAll(
                '[role="dialog"][aria-modal="true"] img'
            )).filter((img) => img.getAttribute('alt')).length"""
        )
        assert avatars >= 1
        assert await poster._count_media_previews(page) == 0
        assert await poster._attachment_progress_count(page) == 0


async def test_two_images_in_one_tweet_add_two_previews(
    poster_config: XPosterConfig, fake_x_factory: Any, tmp_path: Path
) -> None:
    fake = fake_x_factory(dom="legacy")
    images = []
    for index in range(2):
        image = tmp_path / f"pair-{index}.png"
        image.write_bytes(_PNG_1X1)
        images.append(image)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    samples: list[int] = []
    original_attach = poster._attach_media

    async def traced_attach(page: Page, media: Any, alt_texts: Any = ()) -> None:
        samples.append(await poster._count_media_previews(page))
        await original_attach(page, media, alt_texts)
        samples.append(await poster._count_media_previews(page))

    poster._attach_media = traced_attach  # type: ignore[method-assign]
    thread = Thread((Tweet("Two images", media=tuple(images)),), key="two-images")

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    assert samples == [0, 2]
    entities = fake.create_requests[0]["variables"]["media"]["media_entities"]
    assert len(entities) == 2
    assert fake.tweets[0].media == ("pair-0.png", "pair-1.png")


async def test_images_in_both_editors_accumulate_the_preview_count(
    poster_config: XPosterConfig, fake_x_factory: Any, tmp_path: Path
) -> None:
    fake = fake_x_factory(dom="legacy")
    first = tmp_path / "accumulate-1.png"
    first.write_bytes(_PNG_1X1)
    second = tmp_path / "accumulate-2.png"
    second.write_bytes(_PNG_1X1)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    samples: list[int] = []
    original_attach = poster._attach_media

    async def traced_attach(page: Page, media: Any, alt_texts: Any = ()) -> None:
        samples.append(await poster._count_media_previews(page))
        await original_attach(page, media, alt_texts)
        samples.append(await poster._count_media_previews(page))

    poster._attach_media = traced_attach  # type: ignore[method-assign]
    thread = Thread(
        (Tweet("Editor one", media=(first,)), Tweet("Editor two", media=(second,))),
        key="accumulate",
    )

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    assert samples == [0, 1, 1, 2]


async def test_blank_alt_text_never_opens_the_alt_editor(
    poster_config: XPosterConfig, fake_x_factory: Any, tmp_path: Path
) -> None:
    """Blank/whitespace alt entries must not touch any alt UI (the live failure)."""
    fake = fake_x_factory(dom="legacy")
    images = []
    for index in range(2):
        image = tmp_path / f"blank-{index}.png"
        image.write_bytes(_PNG_1X1)
        images.append(image)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    samples: list[dict[str, Any]] = []
    first = {"done": False}
    original_attach = poster._attach_media

    async def traced_attach(page: Page, media: Any, alt_texts: Any = ()) -> None:
        await original_attach(page, media, alt_texts)
        samples.append(
            {
                "clicks": await page.evaluate("() => window.__fxAltAddClicks"),
                "intro": await page.evaluate("() => window.__fxAltIntro"),
            }
        )
        if not first["done"]:
            first["done"] = True
            # Reproduce the live count: only one description control is left
            # while the next batch's skip is already 1.
            await page.evaluate(
                """() => {
                    const control = document.querySelector('[data-testid="altTextWrapper"]');
                    if (control) control.remove();
                }"""
            )

    poster._attach_media = traced_attach  # type: ignore[method-assign]
    thread = Thread(
        (
            Tweet("One", media=(images[0],), alt_texts=("",)),
            Tweet("Two", media=(images[1],), alt_texts=("   ",)),
        ),
        key="blank-alt",
    )

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    assert samples == [
        {"clicks": 0, "intro": {"sure": 0, "no": 0}},
        {"clicks": 0, "intro": {"sure": 0, "no": 0}},
    ]
    assert [tweet.media for tweet in fake.tweets] == [
        ("blank-0.png",),
        ("blank-1.png",),
    ]


async def test_alt_text_for_the_second_image_only(
    poster_config: XPosterConfig, fake_x_factory: Any, tmp_path: Path
) -> None:
    fake = fake_x_factory(dom="legacy", scenario=Scenario.ALT_INTRO)
    first = tmp_path / "alt-first.png"
    first.write_bytes(_PNG_1X1)
    second = tmp_path / "alt-second.png"
    second.write_bytes(_PNG_1X1)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake, allow_alt_text=True)
    samples: list[dict[str, Any]] = []
    original_attach = poster._attach_media

    async def traced_attach(page: Page, media: Any, alt_texts: Any = ()) -> None:
        await original_attach(page, media, alt_texts)
        samples.append(
            {
                "clicks": await page.evaluate("() => window.__fxAltAddClicks"),
                "intro": await page.evaluate("() => window.__fxAltIntro"),
            }
        )

    poster._attach_media = traced_attach  # type: ignore[method-assign]
    thread = Thread(
        (
            Tweet("One", media=(first,), alt_texts=("",)),
            Tweet("Two", media=(second,), alt_texts=("second image",)),
        ),
        key="alt-second-only",
    )

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    assert samples[0]["clicks"] == 0
    assert samples[1] == {"clicks": 1, "intro": {"sure": 0, "no": 1}}
    bodies = fake.create_requests
    assert "alt_text" not in bodies[0]["variables"]["media"]["media_entities"][0]
    assert bodies[1]["variables"]["media"]["media_entities"][0]["alt_text"] == "second image"


async def test_the_alt_intro_is_dismissed_with_no_thanks(
    poster_config: XPosterConfig, fake_x_factory: Any, tmp_path: Path
) -> None:
    fake = fake_x_factory(dom="legacy", scenario=Scenario.ALT_INTRO)
    image = tmp_path / "intro.png"
    image.write_bytes(_PNG_1X1)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake, allow_alt_text=True)
    counters: dict[str, Any] = {}
    original_attach = poster._attach_media

    async def traced_attach(page: Page, media: Any, alt_texts: Any = ()) -> None:
        await original_attach(page, media, alt_texts)
        counters.update(
            await page.evaluate(
                """() => ({
                    clicks: window.__fxAltAddClicks,
                    intro: window.__fxAltIntro,
                })"""
            )
        )

    poster._attach_media = traced_attach  # type: ignore[method-assign]
    thread = Thread(
        (Tweet("With description", media=(image,), alt_texts=("described image",)),),
        key="alt-intro",
    )

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    assert counters == {"clicks": 1, "intro": {"sure": 0, "no": 1}}
    entities = fake.create_requests[0]["variables"]["media"]["media_entities"]
    assert entities[0]["alt_text"] == "described image"


async def test_alt_text_requires_allow_alt_text_before_any_browser(
    poster_config: XPosterConfig, tmp_path: Path
) -> None:
    image = tmp_path / "guard.png"
    image.write_bytes(_PNG_1X1)
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    thread = Thread(
        (Tweet("With alt", media=(image,), alt_texts=("description",)),),
        key="alt-guard",
    )

    with pytest.raises(ValidationError, match="alt text is not verified against the live site yet"):
        await poster.post_thread(thread)


async def test_a_missing_alt_editor_fails_pre_click_as_failed(
    poster_config: XPosterConfig, fake_x_factory: Any, tmp_path: Path
) -> None:
    fake = fake_x_factory(dom="legacy", scenario=Scenario.ALT_UI_MISSING)
    image = tmp_path / "missing-editor.png"
    image.write_bytes(_PNG_1X1)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake, allow_alt_text=True)
    thread = Thread(
        (Tweet("With alt", media=(image,), alt_texts=("description",)),),
        key="alt-missing",
    )

    with pytest.raises(UiChangedError):
        await poster.post_thread(thread)

    assert fake.tweets == []
    assert fake.create_requests == []
    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.FAILED
    assert entry.clicking_at == ""


def test_normalise_editor_text_ignores_only_trailing_spaces() -> None:
    assert _normalise_editor_text("a @bob \n\nnext ") == _normalise_editor_text("a @bob\n\nnext")
    assert _normalise_editor_text("line\t") != _normalise_editor_text("line")
    assert _normalise_editor_text("a  b") != _normalise_editor_text("a b")
    assert _normalise_editor_text("a\nb") != _normalise_editor_text("a b")
    assert _normalise_editor_text("Hi @bob") != _normalise_editor_text("Hi @alice")
    assert _normalise_editor_text("#AI") != _normalise_editor_text("#ML")


async def test_a_mid_sentence_mention_posts_exactly(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    text = "Hi @example_user, welcome aboard."
    thread = make_thread(text, key="mention-mid")

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    assert fake.tweets[0].text == text


async def test_a_line_final_mention_before_a_blank_line_keeps_the_blank_line(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    intended = "Hello @example_user\n\nNew paragraph."
    thread = make_thread(intended, key="mention-blank-line")

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    posted_text = fake.tweets[0].text
    assert posted_text.count("\n") == 2, "both newlines must survive"
    assert _normalise_editor_text(posted_text) == _normalise_editor_text(intended)
    first_line = posted_text.split("\n", 1)[0]
    assert first_line.startswith("Hello @example_user")
    assert first_line[len("Hello @example_user") :] in {" ", "\u00a0"}


async def test_a_mention_at_the_very_end_of_a_tweet_is_safe(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    intended = "Thanks @example_user"
    thread = make_thread(intended, key="mention-end")

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    assert _normalise_editor_text(fake.tweets[0].text) == _normalise_editor_text(intended)
    assert fake.tweets[0].text.startswith(intended)


@pytest.mark.parametrize(
    ("token", "intended"),
    [
        ("#AI", "#AI\n\nNext paragraph."),
        ("$XYZ", "$XYZ\n\nNext paragraph."),
    ],
)
async def test_a_line_final_hashtag_or_cashtag_is_safe(
    poster_config: XPosterConfig, fake_x_factory: Any, token: str, intended: str
) -> None:
    fake = fake_x_factory(dom="legacy")
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = make_thread(intended, key=f"typeahead-{token.strip('$#')}")

    posted = await poster.post_thread(thread)

    assert posted.complete is True
    posted_text = fake.tweets[0].text
    assert posted_text.count("\n") == 2
    assert _normalise_editor_text(posted_text) == _normalise_editor_text(intended)


async def test_a_mention_followed_by_punctuation_gets_no_extra_space(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    intended = "Hi @example_user, welcome."
    typed: list[str] = []
    original_type = Keyboard.type

    async def spy(instance: Keyboard, text: str) -> None:
        typed.append(text)
        await original_type(instance, text)

    with patch.object(Keyboard, "type", spy):
        posted = await poster.post_thread(make_thread(intended, key="mention-punctuation"))

    assert posted.complete is True
    assert fake.tweets[0].text == intended
    assert "@example_user " not in "".join(typed), "no guard space after a non-final token"


async def test_without_the_typeahead_guard_the_readback_stops_the_run(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    """The old behaviour: Shift+Enter selects the mention suggestion."""
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)

    async def unguarded_type(page: Page, text: str) -> None:
        for step in poster._humanizer.typing_plan(text):
            chunk = step.text
            if not chunk:
                continue
            if chunk in ("\n", "\r", "\r\n"):
                await page.keyboard.press("Shift+Enter")
            else:
                await page.keyboard.type(chunk)
            await poster._sleep(step.delay_ms / 1000.0)

    poster._type = unguarded_type  # type: ignore[method-assign]
    thread = make_thread("Hello @example_user\n\nNew paragraph.", key="mention-old")

    with pytest.raises(UiChangedError, match="did not keep the text"):
        await poster.post_thread(thread)

    assert fake.tweets == []
    assert fake.create_requests == []
    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.FAILED


async def test_the_mention_cap_refuses_six_distinct_handles_before_chrome(
    poster_config: XPosterConfig,
) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    thread = make_thread("@one @two @three @four @five @six", key="mentions-six")

    with pytest.raises(ValidationError, match="6 distinct @mentions exceed"):
        await poster.post_thread(thread)


async def test_five_distinct_mentions_are_allowed(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    text = "@one @two @three @four @five"

    posted = await poster.post_thread(make_thread(text, key="mentions-five"))

    assert posted.complete is True
    assert fake.tweets[0].text.startswith("@one @two @three @four @five")


async def test_a_repeated_mention_counts_once(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    text = "@one @two @three @four @five @one"

    posted = await poster.post_thread(make_thread(text, key="mentions-repeat"))

    assert posted.complete is True


async def test_the_posting_accounts_own_mention_is_not_counted(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    text = "@olsa_test @one @two @three @four @five"

    posted = await poster.post_thread(make_thread(text, key="mentions-self"))

    assert posted.complete is True


async def test_email_addresses_do_not_count_as_mentions(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    text = "Mail foo@example.com or @one"

    posted = await poster.post_thread(make_thread(text, key="mentions-email"))

    assert posted.complete is True


async def test_chain_posts_a_fresh_thread_through_the_composer(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = make_thread("Chain one", "Chain two", "Chain three", key="chain-fresh")
    calls: list[PostedTweet] = []
    post_all_clicks: list[str] = []
    original_click = poster._click

    async def count_post_all(page: Page, locator: Locator) -> None:
        try:
            label = (await locator.inner_text()).strip()
        except PlaywrightError:
            label = ""
        if label == "Post all":
            post_all_clicks.append(label)
        await original_click(page, locator)

    poster._click = count_post_all  # type: ignore[method-assign]

    posted = await poster.post_chain(thread, on_posted=calls.append)

    assert posted.strategy == "composer"
    assert posted.complete is True
    assert posted.handle == "olsa_test"
    assert post_all_clicks == ["Post all"], "one batch click, exactly as post_thread"
    assert [tweet.index for tweet in posted.tweets] == [0, 1, 2]
    assert [tweet.id for tweet in posted.tweets] == [tweet.id for tweet in fake.tweets]
    assert [tweet.url for tweet in posted.tweets] == [
        f"https://x.com/olsa_test/status/{tweet.id}" for tweet in posted.tweets
    ]
    assert [tweet.text_sha256 for tweet in posted.tweets] == [
        text_hash(text) for text in thread.texts
    ]
    assert [tweet.index for tweet in calls] == [0, 1, 2]
    assert len({tweet.index for tweet in calls}) == 3, "each id is reported once"
    assert [tweet.id for tweet in calls] == [tweet.id for tweet in fake.tweets]
    assert [tweet.text for tweet in fake.tweets] == list(thread.texts)
    assert [
        request["variables"]["reply"]["in_reply_to_tweet_id"] for request in fake.create_requests
    ] == [None, fake.tweets[0].id, fake.tweets[1].id]

    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.POSTED
    assert entry.strategy == "composer"
    assert [tweet.id for tweet in entry.posted] == [tweet.id for tweet in fake.tweets]
    assert entry.completed == posted


async def test_chain_uses_the_composer_even_when_config_says_replies(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    config = poster_config.with_overrides(strategy=Strategy.REPLIES)
    poster = make_poster(config, fake)

    posted = await poster.post_chain(make_thread("One", "Two", key="chain-strategy"))

    assert posted.strategy == "composer"
    assert posted.complete is True
    assert len(fake.create_requests) == 2
    assert fake.create_requests[1]["variables"]["reply"]["in_reply_to_tweet_id"] == (
        fake.tweets[0].id
    )


async def test_chain_tolerates_a_slow_last_create_tweet_response(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = make_thread("Chain one", "Chain two", "Chain three", key="chain-slow-last")
    original_create = fake._create_tweet
    seen = {"count": 0}

    async def slow_last(route: Any, request: Any) -> None:
        seen["count"] += 1
        if seen["count"] == 3:
            await asyncio.sleep(1.5)
        await original_create(route, request)

    fake._create_tweet = slow_last  # type: ignore[method-assign]
    calls: list[PostedTweet] = []

    posted = await poster.post_chain(thread, on_posted=calls.append)

    assert posted.complete is True, "a slow body must not turn a success into UNKNOWN"
    assert seen["count"] == 3
    assert [tweet.index for tweet in calls] == [0, 1, 2]
    assert [tweet.id for tweet in calls] == [tweet.id for tweet in fake.tweets]
    assert len(fake.tweets) == 3


async def test_chain_composer_callback_failure_still_records_every_id(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = make_thread("Chain one", "Chain two", "Chain three", key="chain-callback-batch")
    calls: list[PostedTweet] = []
    callback_error = RuntimeError("the pipeline could not persist the tweet id")

    def on_posted(tweet: PostedTweet) -> None:
        calls.append(tweet)
        raise callback_error

    with pytest.raises(PostOutcomeUnknownError) as excinfo:
        await poster.post_chain(thread, on_posted=on_posted)

    # The click already dispatched the whole batch, so every id is captured and
    # recorded even though the callback failed on the first one.
    assert excinfo.value.__cause__ is callback_error
    assert [tweet.index for tweet in excinfo.value.posted] == [0, 1, 2]
    assert [tweet.id for tweet in excinfo.value.posted] == [tweet.id for tweet in fake.tweets]
    assert [tweet.index for tweet in calls] == [0], "the callback is not called again"
    assert len(fake.tweets) == 3
    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.UNKNOWN
    assert [tweet.id for tweet in entry.posted] == [tweet.id for tweet in fake.tweets]


async def test_chain_composer_fail_at_n_reports_the_captured_prefix(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy", scenario=Scenario.FAIL_AT_N, fail_at=2)
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = make_thread("Chain one", "Chain two", "Chain three", key="chain-fail-at-n")
    calls: list[PostedTweet] = []

    with pytest.raises(PostOutcomeUnknownError) as excinfo:
        await poster.post_chain(thread, on_posted=calls.append)

    assert [tweet.index for tweet in calls] == [0]
    assert [tweet.index for tweet in excinfo.value.posted] == [0]
    assert [tweet.id for tweet in excinfo.value.posted] == [fake.tweets[0].id]
    assert "response timings:" in str(excinfo.value)
    assert [tweet.text for tweet in fake.tweets] == ["Chain one"]
    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.UNKNOWN
    assert [tweet.index for tweet in entry.posted] == [0]


async def test_chain_refuses_a_26_tweet_thread_before_the_browser(
    poster_config: XPosterConfig,
) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    thread = Thread(tuple(Tweet(f"tweet {index}") for index in range(26)), key="chain-26")

    with pytest.raises(ValidationError, match="at most 25"):
        await poster.post_chain(thread)


async def test_chain_resumes_from_a_caller_owned_prefix(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    prefix = seed_fake_prefix(fake, ["Chain one", "Chain two"])
    thread = make_thread("Chain one", "Chain two", "Chain three", key="chain-resume")
    calls: list[PostedTweet] = []
    typed: list[str] = []
    original_type = poster._type

    async def record_type(page: Page, text: str) -> None:
        typed.append(text)
        await original_type(page, text)

    poster._type = record_type  # type: ignore[method-assign]

    posted = await poster.post_chain(thread, already_posted=prefix, on_posted=calls.append)

    assert posted.strategy == "replies", "the live-unverified resume path must be explicit"
    assert posted.complete is True
    assert typed == ["Chain three"], "the recorded prefix must never be retyped"
    assert len(fake.tweets) == 3
    assert fake.tweets[2].text == "Chain three"
    assert fake.tweets[2].in_reply_to == prefix[-1].id
    assert len(fake.create_requests) == 1
    request = fake.create_requests[0]["variables"]
    assert request["tweet_text"] == "Chain three"
    assert request["reply"]["in_reply_to_tweet_id"] == prefix[-1].id
    assert any(url.endswith(f"/status/{prefix[-1].id}") for url in fake.visits)
    assert [tweet.index for tweet in calls] == [2]
    assert [tweet.id for tweet in posted.tweets] == [
        prefix[0].id,
        prefix[1].id,
        fake.tweets[2].id,
    ]
    assert [tweet.url for tweet in posted.tweets] == [
        f"https://x.com/olsa_test/status/{tweet.id}" for tweet in posted.tweets
    ]


async def test_chain_with_a_complete_prefix_returns_without_a_browser(
    poster_config: XPosterConfig,
) -> None:
    thread = make_thread("Chain one", "Chain two", key="chain-complete-prefix")
    prefix = seed_fake_prefix(FakeX(dom="legacy"), ["Chain one", "Chain two"])
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)

    posted = await poster.post_chain(thread, already_posted=prefix)

    assert posted.complete is True
    assert posted.strategy == "replies"
    assert [tweet.id for tweet in posted.tweets] == [tweet.id for tweet in prefix]
    assert [tweet.url for tweet in posted.tweets] == [
        f"https://x.com/olsa_test/status/{tweet.id}" for tweet in prefix
    ]
    assert poster.ledger.get(thread.content_hash) is None


async def test_chain_ignores_an_unknown_ledger_entry(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = make_thread("Chain one", "Chain two", "Chain three", key="chain-unknown")
    poster.ledger.begin(thread.content_hash, strategy="replies", handle="olsa_test", n_tweets=3)
    poster.ledger.mark_unknown(thread.content_hash, "a previous attempt may have landed")

    posted = await poster.post_chain(thread)

    assert posted.complete is True
    assert len(fake.tweets) == 3
    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.POSTED


async def test_chain_ignores_a_posted_ledger_entry_and_keeps_it_as_history(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = make_thread("Chain one", "Chain two", "Chain three", key="chain-posted")
    previous = PostedThread(
        thread_hash=thread.content_hash,
        handle="olsa_test",
        strategy="replies",
        posted_at="2026-11-10T00:00:00+00:00",
        tweets=tuple(chain_record(index) for index in range(3)),
        complete=True,
    )
    poster.ledger.begin(thread.content_hash, strategy="replies", handle="olsa_test", n_tweets=3)
    poster.ledger.complete(thread.content_hash, previous)

    posted = await poster.post_chain(thread)

    assert posted.complete is True
    assert len(fake.tweets) == 3
    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.POSTED
    assert entry.previous_completed == previous
    assert [tweet.id for tweet in entry.posted] == [tweet.id for tweet in fake.tweets]
    assert all(fresh.id != old.id for old, fresh in zip(previous.tweets, entry.posted, strict=True))


async def test_chain_refuses_a_parked_store_before_the_browser(
    poster_config: XPosterConfig,
) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    poster.store.park("captcha", kind="challenge")

    with pytest.raises(ParkedError):
        await poster.post_chain(make_thread("Chain one", key="chain-parked"))


async def test_chain_pacing_blocks_before_the_browser(poster_config: XPosterConfig) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    poster.ledger.set_not_before(datetime.now(UTC) + timedelta(hours=1))

    with pytest.raises(PacingError) as excinfo:
        await poster.post_chain(make_thread("Chain one", "Chain two", key="chain-pacing"))

    assert excinfo.value.retry_after_s > 0


async def test_chain_pacing_counts_only_the_remaining_tweets(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    config = poster_config.with_overrides(
        pacing=replace(poster_config.pacing, max_tweets_per_day=2)
    )
    poster = make_poster(config, fake)
    prefix = seed_fake_prefix(fake, ["Chain one", "Chain two"])
    thread = make_thread("Chain one", "Chain two", "Chain three", key="chain-remaining")

    # The whole 3-tweet thread cannot fit max_tweets_per_day=2, but the single
    # remaining tweet can; a total-count check would raise here.
    posted = await poster.post_chain(thread, already_posted=prefix)

    assert posted.complete is True
    assert len(fake.tweets) == 3


async def test_chain_callback_failure_stops_after_the_accepted_id(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    prefix = seed_fake_prefix(fake, ["Chain one"])
    thread = make_thread("Chain one", "Chain two", "Chain three", key="chain-callback")
    calls: list[PostedTweet] = []
    callback_error = RuntimeError("the pipeline could not persist the tweet id")

    def on_posted(tweet: PostedTweet) -> None:
        calls.append(tweet)
        raise callback_error

    with pytest.raises(PostOutcomeUnknownError) as excinfo:
        await poster.post_chain(thread, already_posted=prefix, on_posted=on_posted)

    assert excinfo.value.__cause__ is callback_error
    assert [tweet.index for tweet in excinfo.value.posted] == [0, 1]
    assert [tweet.id for tweet in excinfo.value.posted] == [
        prefix[0].id,
        fake.tweets[1].id,
    ]
    assert [tweet.index for tweet in calls] == [1]
    assert [tweet.text for tweet in fake.tweets] == ["Chain one", "Chain two"]
    assert len(fake.create_requests) == 1
    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.UNKNOWN
    assert [tweet.index for tweet in entry.posted] == [0, 1]


async def test_chain_callback_xposter_error_is_re_raised_after_the_batch_settles(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = make_thread("Chain one", "Chain two", key="chain-callback-xposter")
    sentinel = ValidationError("the caller rejected this id")

    def on_posted(tweet: PostedTweet) -> None:
        raise sentinel

    with pytest.raises(ValidationError) as excinfo:
        await poster.post_chain(thread, on_posted=on_posted)

    assert excinfo.value is sentinel
    assert len(fake.tweets) == 2, "the composer batch still settles"
    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.UNKNOWN
    assert [tweet.id for tweet in entry.posted] == [tweet.id for tweet in fake.tweets]


@pytest.mark.parametrize(
    ("prefix", "match"),
    [
        ([chain_record(0), chain_record(2)], "accepted prefix in order"),
        ([chain_record(1), chain_record(0)], "accepted prefix in order"),
        ([chain_record(0, "12a")], "ASCII digit strings"),
        (
            [chain_record(0), chain_record(1), chain_record(2), chain_record(3)],
            "cannot be longer than the thread",
        ),
    ],
    ids=["gap", "wrong-order", "non-digit-id", "too-long"],
)
async def test_chain_rejects_an_invalid_prefix_before_the_browser(
    poster_config: XPosterConfig,
    prefix: list[PostedTweet],
    match: str,
) -> None:
    poster = XPoster(poster_config, playwright_factory=forbidden_factory)
    thread = make_thread("Chain one", "Chain two", "Chain three", key="chain-invalid")

    with pytest.raises(ValidationError, match=match):
        await poster.post_chain(thread, already_posted=prefix)


async def test_chain_composer_attaches_media_to_each_tweet(
    poster_config: XPosterConfig, fake_x_factory: Any, tmp_path: Path
) -> None:
    root_image = tmp_path / "root.png"
    reply_image = tmp_path / "reply.png"
    root_image.write_bytes(_PNG_1X1)
    reply_image.write_bytes(_PNG_1X1)
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = Thread(
        (
            Tweet("Root with an image", media=(root_image,)),
            Tweet("Reply with an image", media=(reply_image,)),
            Tweet("Plain reply"),
        ),
        key="chain-media",
    )

    posted = await poster.post_chain(thread)

    assert posted.strategy == "composer"
    assert posted.complete is True
    bodies = fake.create_requests
    assert [len(body["variables"]["media"]["media_entities"]) for body in bodies] == [1, 1, 0]
    assert [tweet.media for tweet in fake.tweets] == [("root.png",), ("reply.png",), ()]


async def test_chain_crash_after_a_click_leaves_the_barrier_up_and_can_be_resumed(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    prefix = seed_fake_prefix(fake, ["Chain one"])
    thread = make_thread("Chain one", "Chain two", "Chain three", key="chain-crash")
    original_pause = poster._pause
    gaps = {"count": 0}

    async def crash_on_second_gap(kind: str) -> None:
        if kind == "between_tweet":
            gaps["count"] += 1
            if gaps["count"] == 2:
                raise KeyboardInterrupt
        await original_pause(kind)

    poster._pause = crash_on_second_gap  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        await poster.post_chain(thread, already_posted=prefix)

    interrupted = poster.ledger.get(thread.content_hash)
    assert interrupted is not None
    assert interrupted.status is LedgerStatus.PENDING
    assert interrupted.clicking_at, "the click barrier must survive the interruption"
    assert [tweet.index for tweet in interrupted.posted] == [0, 1]
    assert len(fake.tweets) == 2
    accepted = tuple(interrupted.posted)

    # The caller owns idempotency: a PENDING entry with a raised barrier must not
    # block the next chain call, unlike post_thread, which would demand --force.
    resumed = await make_poster(poster_config, fake).post_chain(thread, already_posted=accepted)

    assert resumed.strategy == "replies"
    assert resumed.complete is True
    assert [tweet.index for tweet in resumed.tweets] == [0, 1, 2]
    assert len(fake.tweets) == 3
    assert (
        fake.create_requests[1]["variables"]["reply"]["in_reply_to_tweet_id"]
        == interrupted.posted[-1].id
    )
    assert poster.ledger.get(thread.content_hash).status is LedgerStatus.POSTED


async def test_chain_readback_mismatch_keeps_the_prefix_and_posts_nothing_further(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    prefix = seed_fake_prefix(fake, ["Chain one", "Chain two"])
    thread = make_thread("Chain one", "Chain two", "Chain three", key="chain-readback")
    original_type = poster._type

    async def type_wrong_text(page: Page, text: str) -> None:
        await original_type(page, f"{text}!")

    poster._type = type_wrong_text  # type: ignore[method-assign]

    with pytest.raises(UiChangedError, match="did not keep the text") as excinfo:
        await poster.post_chain(thread, already_posted=prefix)

    assert excinfo.value.artifacts is not None
    assert [tweet.text for tweet in fake.tweets] == ["Chain one", "Chain two"]
    assert fake.create_requests == []
    entry = poster.ledger.get(thread.content_hash)
    assert entry is not None
    assert entry.status is LedgerStatus.FAILED
    assert [tweet.id for tweet in entry.posted] == [prefix[0].id, prefix[1].id]


async def test_chain_seeds_the_ledger_with_exactly_the_caller_prefix(
    poster_config: XPosterConfig, fake_x_factory: Any
) -> None:
    fake = fake_x_factory(dom="legacy")
    seed_session(poster_config)
    poster = make_poster(poster_config, fake)
    thread = make_thread("Chain one", "Chain two", "Chain three", key="chain-stale")
    poster.ledger.begin(thread.content_hash, strategy="replies", handle="olsa_test", n_tweets=3)
    for index in range(3):
        poster.ledger.record_tweet(thread.content_hash, chain_record(index))
    poster.ledger.mark_unknown(thread.content_hash, "a stale attempt")
    prefix = seed_fake_prefix(fake, ["Chain one"])
    observed: list[int] = []

    def on_posted(tweet: PostedTweet) -> None:
        entry = poster.ledger.get(thread.content_hash)
        assert entry is not None
        observed.extend(item.index for item in entry.posted)
        raise RuntimeError("stop after inspecting the seeded record")

    with pytest.raises(PostOutcomeUnknownError):
        await poster.post_chain(thread, already_posted=prefix, on_posted=on_posted)

    # Stale indexes the caller did not accept are gone; the seeded record holds
    # exactly the prefix plus the tweet this run just captured.
    assert observed == [0, 1]
