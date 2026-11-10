"""Opt-in live tests against a throwaway X test account.

Nothing here runs by default: ``tests/conftest.py`` skips every test marked
``live`` unless ``XPOSTER_LIVE=1``. Even then these tests refuse to run unless
``XPOSTER_HOME`` points at the test account's Chrome home and ``XPOSTER_HANDLE``
names the account it should be signed in as. A handle named in
``XPOSTER_LIVE_FORBIDDEN_HANDLES`` (comma-separated, default ``olsa``,
``openlifescienceai``, ``olsa_ai``) is always refused.

Run the whole file, in order, against the test account::

    XPOSTER_LIVE=1 XPOSTER_HOME=~/.olsa/x-test XPOSTER_HANDLE=my_test_handle \
        XPOSTER_DISPLAY=visible .venv/bin/pytest tests/test_live.py -s

Safety rules baked into this module:

* no browser starts until the home, the expected handle and an unparked saved
  session have all been checked, and a forbidden handle is refused first;
* only one short thread per test, and tests that touch the composer are held
  ``LIVE_POST_GAP_S`` seconds apart by a module-level guard, so the account is
  never burst;
* every real post is deleted in a ``finally`` through ``delete_thread``; the
  tweet ids are printed to stderr *before* the delete so the operator can finish
  the cleanup by hand when the delete itself fails;
* posting is refused while the test home's ledger holds an ``UNKNOWN`` entry or
  a ``PENDING`` entry with a recorded click, because an unresolved outcome must
  be inspected by a human before another thread is posted;
* every post carries a unique ``[xposter-live-test ...]`` marker in its text and
  as its ``Thread.key``, so repeated live runs are distinct threads and no run
  ever needs ``--force``;
* typing cadence and the between-tweet pause stay at the shipped production
  defaults (``35..120`` ms and ``20..45`` s); only the short pre-post / think /
  action dwells are shortened, so a live run still looks human.

Test 8 reuses the thread posted by test 6 (or 7); running it alone skips with a
clear message instead of silently doing nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Page

import xposter.diagnose as diagnose
import xposter.sessions as sessions
from xposter import (
    Display,
    HumanPolicy,
    KeepalivePolicy,
    PacingPolicy,
    Strategy,
    Thread,
    Tweet,
    ValidationError,
    XPosterConfig,
)
from xposter.browser import Tell, resolve_display
from xposter.ledger import Decision, LedgerStatus
from xposter.models import PostedThread
from xposter.poster import XPoster
from xposter.selectors import SELECTORS, locate
from xposter.splitter import weighted_length
from xposter.store import SessionStore

pytestmark = pytest.mark.live

FORBIDDEN_HANDLES = frozenset({"olsa", "openlifescienceai", "olsa_ai"})
"""Accounts that must never be used by this harness, even by accident."""

PRODUCTION_HOME = Path.home() / ".olsa" / "x"
"""The tool's default home; live tests want a separate throwaway-account home."""

LIVE_POST_GAP_S = 90.0
"""Minimum seconds between two tests that open the composer; never lower it."""

LIVE_HUMAN = HumanPolicy(
    # The typing range is the shipped default on purpose: a live run must not
    # type faster than production, or it would stop being representative.
    typing_delay_ms=(35, 120),
    pre_post_pause_s=(1.0, 2.0),
    # The replies strategy uses this between posts. Keep the production range:
    # a 3-tweet chain then takes 40-90 s, which is not the burst the audit
    # (H4) warned about.
    between_tweet_pause_s=(20.0, 45.0),
    action_pause_s=(0.15, 0.4),
    think_pause_s=(0.6, 1.4),
    scroll_before_post=False,
)
"""Fast enough for a test run, still paced like a slow human."""

LIVE_PACING = PacingPolicy(
    # The 90 s module guard is the binding gap for this throwaway account, so
    # the in-app pacer is set below it and can never deadlock the suite.
    min_gap_between_threads_s=60.0,
    max_threads_per_day=20,
    max_tweets_per_day=200,
    max_tweets_per_hour=60,
    quiet_hours=None,
    jitter=0.0,
)

LIVE_KEEPALIVE = KeepalivePolicy(min_interval_s=3600.0, browse_seconds=(2.0, 4.0))
"""A short, read-only refresh so test 10 does not scroll for a minute."""

CONDITIONAL_COMPOSER_SELECTORS = frozenset(
    {
        # These only exist once the composer is in thread mode or has media
        # attached; dom_report opens an empty composer, so their absence there
        # is expected and reported, not a failure.
        "composer_post_all_button",
        "composer_media_preview",
        "composer_media_remove_button",
        "composer_alt_button",
        "composer_alt_text_input",
        "composer_alt_save_button",
    }
)

_LAST_COMPOSER_AT: float | None = None
"""Monotonic time of the last composer use, shared by every test in the module."""

_POSTED: dict[str, tuple[Thread, PostedThread]] = {}
"""Threads posted by this run, keyed by strategy, so test 8 can reuse one."""


def _announce(message: str) -> None:
    """Tell the operator what is about to happen before it happens."""
    print(f"[live] {message}", file=sys.stderr, flush=True)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _normalise_handle(raw: str) -> str:
    return raw.strip().lstrip("@")


def _forbidden_handles() -> set[str]:
    """The configured list plus the built-in production handles.

    The built-ins are always included: an operator-supplied list can extend the
    refusal, never weaken it.
    """
    raw = _env("XPOSTER_LIVE_FORBIDDEN_HANDLES")
    configured = {item.strip().lstrip("@").lower() for item in raw.split(",") if item.strip()}
    return configured | set(FORBIDDEN_HANDLES)


def _marker(kind: str) -> str:
    """A unique, greppable marker that no real content would ever contain."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"[xposter-live-test {kind} {stamp}]"


def _note_composer_use() -> None:
    """Start the 90 s gap clock as soon as the composer work is over."""
    global _LAST_COMPOSER_AT
    _LAST_COMPOSER_AT = time.monotonic()


async def _respect_composer_gap() -> None:
    """Sleep until the previous composer-using test is at least 90 s old."""
    if _LAST_COMPOSER_AT is None:
        return
    remaining = LIVE_POST_GAP_S - (time.monotonic() - _LAST_COMPOSER_AT)
    if remaining <= 0:
        return
    _announce(
        f"waiting {remaining:.0f}s so composer tests stay at least {LIVE_POST_GAP_S:.0f}s apart"
    )
    await asyncio.sleep(remaining)


def _thread(texts: tuple[str, ...], config: XPosterConfig, *, key: str) -> Thread:
    """Build a thread and fail locally, not on X, if a tweet cannot fit."""
    tweets = tuple(Tweet(text) for text in texts)
    for index, tweet in enumerate(tweets, start=1):
        weight = weighted_length(tweet.text)
        assert weight <= config.max_tweet_weight, (
            f"test tweet {index} weighs {weight} characters, over the "
            f"{config.max_tweet_weight} limit"
        )
    return Thread(tweets=tweets, key=key)


def _normalised(text: str) -> str:
    """Normalise line endings and trailing spaces without hiding blank lines."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


async def _article_text(page: Page, needle: str) -> str:
    """Text of the first tweet article that contains ``needle``.

    Scoping by a unique fragment avoids reading an ancestor or a reply when the
    status page renders the whole conversation.
    """
    articles = page.locator("article")
    for index in range(await articles.count()):
        text = await articles.nth(index).inner_text()
        if needle in text:
            return text
    return ""


async def _profile_text(config: XPosterConfig) -> str:
    store = SessionStore(config)
    async with sessions.authenticated_page(config, store, goto=f"/{config.handle}") as authed:
        await locate(authed.page, "primary_column", timeout_ms=config.default_timeout_ms)
        return await authed.page.inner_text("body")


async def _assert_thread_rendering(
    config: XPosterConfig, posted: PostedThread, texts: tuple[str, ...]
) -> None:
    """Visit every status URL and prove the reply chain and text survived.

    The previous-tweet link is checked as well as the visible text: a link is
    the machine-readable proof of the chain, while the text proves that X kept
    the blank line inside tweet one.
    """
    assert len(posted.tweets) == len(texts)
    store = SessionStore(config)
    async with sessions.authenticated_page(config, store, goto="/home") as authed:
        page = authed.page
        for index, tweet in enumerate(posted.tweets):
            _announce(f"reading back tweet {index + 1}/{len(texts)} at {tweet.url}")
            await page.goto(tweet.url, wait_until="domcontentloaded")
            assert tweet.id in page.url, (
                f"tweet {index + 1} redirected to {page.url}; it may already be missing"
            )
            await locate(page, "tweet_article", timeout_ms=config.default_timeout_ms)
            needle = f"part {index + 1}/{len(texts)}"
            rendered = await _article_text(page, needle)
            assert rendered, (
                f"tweet {index + 1} ({tweet.id}) did not render at {tweet.url}; "
                f"no article contained {needle!r}"
            )
            assert _normalised(texts[index]) in _normalised(rendered), (
                f"tweet {index + 1} text differs on the live page.\n"
                f"expected: {texts[index]!r}\nrendered article: {rendered!r}"
            )
            if index > 0:
                previous = posted.tweets[index - 1]
                links = await page.locator(f'a[href*="/status/{previous.id}"]').count()
                assert links > 0, (
                    f"tweet {index + 1}'s page has no link to the previous tweet "
                    f"{previous.id}; the reply chain did not render"
                )


def _partial_thread(poster: XPoster, thread: Thread) -> PostedThread | None:
    """Recover the ids the ledger recorded for a post that never completed."""
    entry = poster.ledger.get(thread.content_hash)
    if entry is None or not entry.posted:
        return None
    return PostedThread(
        thread_hash=thread.content_hash,
        handle=entry.handle,
        strategy=entry.strategy,
        posted_at=entry.updated_at,
        tweets=tuple(sorted(entry.posted, key=lambda item: item.index)),
        complete=False,
    )


def _refuse_unresolved_ledger(poster: XPoster) -> None:
    """Refuse to add another post while the test home has an unresolved outcome.

    An ``UNKNOWN`` row, or a ``PENDING`` row whose click barrier was written,
    means a previous attempt may already be live on X. Posting anything else
    before a human inspects the account is exactly how a test harness turns
    into an accidental second thread.
    """
    blockers: list[str] = []
    for entry in poster.ledger.all_entries():
        clicked = bool(getattr(entry, "clicking_at", ""))
        if entry.status is LedgerStatus.UNKNOWN:
            blockers.append(f"{entry.thread_hash[:12]} is UNKNOWN")
        elif entry.status is LedgerStatus.PENDING and clicked:
            blockers.append(f"{entry.thread_hash[:12]} is PENDING with a recorded click")
    if blockers:
        pytest.skip(
            "REFUSING to post: the ledger in this test home has an unresolved outcome "
            f"({'; '.join(blockers)}). Inspect the account, delete any partial thread by "
            "hand, then clear that ledger row with the ledger tooling; never use --force."
        )


def _tell(tells: list[Tell], name: str) -> Tell:
    for tell in tells:
        if tell.name == name:
            return tell
    reported = ", ".join(tell.name for tell in tells)
    raise AssertionError(f"the tells probe did not report {name!r}; it reported: {reported}")


def _assert_tells_policy(tells: list[Tell], resolved: Display) -> None:
    """Assert the honest-browser thresholds the live policy depends on.

    ``--display headless`` is allowed to advertise the HeadlessChrome token
    (that is what headless Chrome does), so that one assertion is replaced by a
    loud warning. On visible or Xvfb the token must be absent, and every mode
    must report a real language list and a desktop-sized screen.
    """
    webdriver = _tell(tells, "navigator.webdriver")
    assert webdriver.value == "false", (
        f"navigator.webdriver is {webdriver.value!r}; the browser must not advertise "
        "automation. Run `xposter doctor --tells` and inspect the launch flags."
    )
    assert webdriver.risk == "ok", webdriver.explanation

    user_agent = _tell(tells, "userAgent")
    if resolved in (Display.VISIBLE, Display.XVFB):
        assert "HeadlessChrome" not in user_agent.value, (
            f"the resolved display is {resolved.value}, but the user agent still "
            f"contains HeadlessChrome: {user_agent.value!r}"
        )
        assert user_agent.risk == "ok", user_agent.explanation
    elif "HeadlessChrome" in user_agent.value:
        _announce(
            "WARNING: this run resolved to HEADLESS, so the user agent advertises "
            "HeadlessChrome and X can see it. The HeadlessChrome assertion is "
            "skipped only for headless; run the supervised post with "
            "XPOSTER_DISPLAY=visible."
        )

    languages = _tell(tells, "navigator.languages")
    assert languages.value not in ("", "(empty)", "unavailable"), (
        f"navigator.languages is {languages.value!r}; a normal desktop Chrome reports "
        "a non-empty language list"
    )
    assert languages.risk == "ok", languages.explanation

    screen = _tell(tells, "screen")
    match = re.search(r"(\d+)x(\d+)", screen.value)
    assert match is not None, f"the screen tell is not parseable: {screen.value!r}"
    width, height = int(match.group(1)), int(match.group(2))
    assert width >= 1024 and height >= 768, (
        f"screen {width}x{height} is smaller than the plausible 1024x768 desktop "
        "minimum; headless defaults and tiny VMs look automated"
    )
    assert screen.risk == "ok", screen.explanation


async def _delete_or_report(poster: XPoster, posted: PostedThread) -> bool:
    """Delete a test thread, printing the ids first so manual cleanup is possible."""
    ids = [tweet.id for tweet in posted.tweets]
    _announce(f"deleting {len(ids)} test tweet(s) through the web UI: {ids}")
    if not ids:
        return True
    try:
        deleted = await poster.delete_thread(posted)
    except Exception as exc:
        _announce(
            f"CLEANUP FAILED ({type(exc).__name__}: {exc}); delete these statuses by hand: {ids}"
        )
        return False
    _announce(f"deleted: {deleted}")
    return len(deleted) == len(ids)


@contextlib.asynccontextmanager
async def _posted_and_cleaned(poster: XPoster, thread: Thread) -> AsyncIterator[PostedThread]:
    """Post one real thread and always attempt to delete it.

    A partial failure is cleaned up from the ledger too: any id recorded before
    the exception is a tweet that exists on X and must not be left behind.
    """
    _announce(
        f"posting a real {len(thread.tweets)}-tweet thread with the "
        f"{poster.config.strategy.value} strategy"
    )
    try:
        posted = await poster.post_thread(thread)
    except BaseException:
        partial = _partial_thread(poster, thread)
        if partial is not None:
            _announce(
                "the post failed after X created ids; attempting cleanup before re-raising: "
                f"{[tweet.id for tweet in partial.tweets]}"
            )
            await _delete_or_report(poster, partial)
        raise
    finally:
        _note_composer_use()

    ids = [tweet.id for tweet in posted.tweets]
    _announce(f"posted ids (delete by hand if cleanup fails): {ids}")
    body_failed = False
    try:
        yield posted
    except BaseException:
        body_failed = True
        raise
    finally:
        cleaned = await _delete_or_report(poster, posted)
        if not cleaned and not body_failed:
            raise AssertionError(f"cleanup left test tweets on the account: {ids}")


@pytest.fixture
def live_config() -> XPosterConfig:
    """Refuse to run unless the test account's home and handle are explicit."""
    raw_home = _env("XPOSTER_HOME")
    if not raw_home:
        pytest.skip(
            "live test needs XPOSTER_HOME pointing at the test account's home "
            "(for example ~/.olsa/x-test)"
        )
    handle = _normalise_handle(_env("XPOSTER_HANDLE"))
    if not handle:
        pytest.skip("live test needs XPOSTER_HANDLE set to the test account's handle")
    if handle.lower() in _forbidden_handles():
        pytest.skip(
            f"REFUSING live test for @{handle}: it is in XPOSTER_LIVE_FORBIDDEN_HANDLES; "
            "use a throwaway test account, never a production handle"
        )

    home = Path(raw_home).expanduser()
    config = XPosterConfig.from_env().with_overrides(
        home=home,
        handle=handle,
        human=LIVE_HUMAN,
        pacing=LIVE_PACING,
        keepalive=LIVE_KEEPALIVE,
    )
    store = SessionStore(config)
    summary = store.summarize()
    if not summary.present:
        pytest.skip(f"live test needs a saved session in {home}; run `xposter login` there first")
    if summary.expired:
        pytest.skip(f"the saved session in {home} looks expired; run `xposter login` again")
    parked = store.parked()
    if parked is not None:
        pytest.skip(
            f"REFUSING live test: the session in {home} is parked ({parked.reason}); "
            "resolve it and run `xposter unpark`"
        )
    if home == PRODUCTION_HOME:
        _announce(
            f"warning: {home} looks like the production home; use a throwaway test "
            "account home instead"
        )
    return config


@pytest.fixture
def live_poster(live_config: XPosterConfig) -> XPoster:
    return XPoster(live_config)


async def test_offline_session_status_has_auth_token_and_is_not_parked(
    live_config: XPosterConfig, live_poster: XPoster
) -> None:
    """The saved session is present, has auth_token and is not parked."""
    _announce("reading the offline session summary; no browser will start")
    summary = live_poster.session_status()
    _announce(
        f"present={summary.present} auth_token={summary.has_auth_token} "
        f"ct0={summary.has_ct0} expires={summary.auth_expires_at} "
        f"days_left={summary.days_left} expired={summary.expired} parked={summary.parked}"
    )
    assert summary.present is True
    assert summary.has_auth_token is True
    assert summary.expired is False
    assert summary.parked is False
    expected = (live_config.handle or "").lower()
    if summary.handle is not None:
        assert summary.handle.lower() == expected, (
            f"the saved session records @{summary.handle}, not @{live_config.handle}"
        )


async def test_verify_returns_the_expected_handle(
    live_config: XPosterConfig, live_poster: XPoster
) -> None:
    """A real browser visit proves the session belongs to the expected account."""
    expected = live_config.handle or ""
    _announce(f"opening the saved Chrome profile to verify it is signed in as @{expected}")
    info = await live_poster.verify()
    _announce(f"verified @{info.handle} in {info.display} mode; chrome={info.chrome_version}")
    assert info.handle.lower() == expected.lower()


async def test_dom_calibration_finds_the_composer_selectors(
    live_config: XPosterConfig, live_poster: XPoster
) -> None:
    """Calibrate the selector table against the live composer without posting."""
    before = {entry.thread_hash for entry in live_poster.ledger.all_entries()}
    _announce("opening /compose/post to calibrate selectors; nothing will be posted")
    report = await diagnose.dom_report(live_config, live_poster.store)
    table = "\n".join(diagnose.format_dom_report(report))
    print(table, file=sys.stderr, flush=True)

    after = {entry.thread_hash for entry in live_poster.ledger.all_entries()}
    assert after == before, "DOM calibration changed the ledger; it must never post"
    assert report.artifacts.exists()
    assert report.screenshot is not None and report.screenshot.exists()
    assert report.report_json is not None and report.report_json.exists()

    reported = {row.name for row in report.rows}
    composer = {name for name in SELECTORS if diagnose.is_composer_selector(name)}
    assert composer <= reported, "dom_report did not cover every composer selector"

    conditional = [
        row.name
        for row in report.rows
        if row.missing and row.name in CONDITIONAL_COMPOSER_SELECTORS
    ]
    if conditional:
        _announce("not shown on an empty composer (conditional): " + ", ".join(conditional))

    missing = [
        row.name
        for row in report.rows
        if row.missing
        and diagnose.is_composer_selector(row.name)
        and row.name not in CONDITIONAL_COMPOSER_SELECTORS
    ]
    if missing:
        pytest.fail(
            "composer selectors missing: "
            + ", ".join(missing)
            + "\nInspect the calibration table and artifacts above, then update "
            "xposter.selectors.SELECTORS."
        )


async def test_browser_tells_report_runs(live_config: XPosterConfig, live_poster: XPoster) -> None:
    """The live browser must satisfy the honest-browser policy."""
    resolved = resolve_display(live_config)
    _announce(f"measuring the live browser tells (resolved display: {resolved.value})")
    tells = await diagnose.tells_report(live_config, live_poster.store)
    print("\n".join(diagnose.format_tells(tells)), file=sys.stderr, flush=True)
    _assert_tells_policy(tells, resolved)


async def test_dry_run_thread_leaves_no_tweet_on_the_profile(
    live_config: XPosterConfig,
) -> None:
    """A prompted 2-tweet thread is discarded and never reaches the profile."""
    await _respect_composer_gap()
    config = live_config.with_overrides(strategy=Strategy.COMPOSER)
    poster = XPoster(config)
    marker = _marker("dry-run")
    texts = (
        f"{marker} dry run part 1/2.\n\nThis paragraph must never be posted.",
        f"{marker} dry run part 2/2 would have been the reply.",
    )
    thread = _thread(texts, config, key=marker)

    _announce("rehearsing the 2-tweet thread in the composer without clicking Post")
    try:
        posted = await poster.post_thread(thread, dry_run=True)
    finally:
        _note_composer_use()

    assert posted.tweets == ()
    assert posted.complete is False
    assert posted.strategy == "dry-run"
    assert poster.ledger.get(thread.content_hash) is None, "a dry run must not write the ledger"

    _announce("visiting the profile; the dry-run marker must be absent")
    profile = await _profile_text(config)
    assert marker not in profile, f"the dry-run marker leaked to the profile: {marker}"


async def test_composer_thread_posts_and_renders_a_reply_chain(
    live_config: XPosterConfig,
) -> None:
    """A real 3-tweet composer thread exists, chains and keeps the blank line."""
    config = live_config.with_overrides(strategy=Strategy.COMPOSER)
    poster = XPoster(config)
    _refuse_unresolved_ledger(poster)
    await _respect_composer_gap()
    marker = _marker("composer")
    texts = (
        f"{marker} composer part 1/3.\n\nSecond paragraph after one blank line.",
        f"{marker} composer part 2/3 replies to the first.",
        f"{marker} composer part 3/3 ends the test thread.",
    )
    thread = _thread(texts, config, key=marker)

    with _posted_and_cleaned(poster, thread) as posted:
        assert posted.complete is True
        assert posted.strategy == "composer"
        assert len(posted.tweets) == len(texts)
        assert [tweet.index for tweet in posted.tweets] == [0, 1, 2]
        assert len({tweet.id for tweet in posted.tweets}) == len(texts)
        assert all(tweet.id.isdigit() for tweet in posted.tweets)
        await _assert_thread_rendering(config, posted, texts)

    _POSTED["composer"] = (thread, posted)


async def test_replies_thread_posts_and_renders_a_reply_chain(
    live_config: XPosterConfig,
) -> None:
    """The replies strategy produces the same 3-tweet chain from status pages."""
    config = live_config.with_overrides(strategy=Strategy.REPLIES)
    poster = XPoster(config)
    _refuse_unresolved_ledger(poster)
    await _respect_composer_gap()
    marker = _marker("replies")
    texts = (
        f"{marker} replies part 1/3.\n\nA blank line inside the root post.",
        f"{marker} replies part 2/3 answers the root.",
        f"{marker} replies part 3/3 ends the test thread.",
    )
    thread = _thread(texts, config, key=marker)

    with _posted_and_cleaned(poster, thread) as posted:
        assert posted.complete is True
        assert posted.strategy == "replies"
        assert len(posted.tweets) == len(texts)
        assert [tweet.index for tweet in posted.tweets] == [0, 1, 2]
        assert len({tweet.id for tweet in posted.tweets}) == len(texts)
        assert all(tweet.id.isdigit() for tweet in posted.tweets)
        await _assert_thread_rendering(config, posted, texts)

    _POSTED["replies"] = (thread, posted)


async def test_reposting_the_same_thread_is_idempotent(
    live_config: XPosterConfig,
    live_poster: XPoster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ledger returns the stored record without opening the composer."""
    if not _POSTED:
        pytest.skip(
            "this test reuses the thread posted by test 6 or 7; run the whole module in order"
        )
    name = "composer" if "composer" in _POSTED else "replies"
    thread, original = _POSTED[name]
    _announce(
        f"posting the {name} thread from this run again; the ledger must answer "
        "without touching x.com"
    )
    decision = live_poster.ledger.decide(thread.content_hash)
    assert decision is Decision.ALREADY_POSTED

    def no_browser(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("post_thread opened a browser for an already-posted thread")

    monkeypatch.setattr(sessions, "authenticated_page", no_browser)
    again = await live_poster.post_thread(thread)

    assert again == original, "the stored PostedThread must be returned unchanged"
    assert again.thread_hash == thread.content_hash


async def test_an_over_limit_tweet_is_refused_before_the_browser(
    live_config: XPosterConfig,
    live_poster: XPoster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation happens before Chrome is launched, for every strategy."""
    limit = live_config.max_tweet_weight
    text = "x" * (limit + 1)
    thread = Thread(tweets=(Tweet(text),), key="live-test/over-limit")
    browser_started = False

    def no_browser(*args: Any, **kwargs: Any) -> Any:
        nonlocal browser_started
        browser_started = True
        raise AssertionError("the browser must not start for invalid content")

    monkeypatch.setattr(sessions, "authenticated_page", no_browser)
    _announce(f"posting a {len(text)}-character tweet ({limit} limit); ValidationError must win")
    with pytest.raises(ValidationError):
        await live_poster.post_thread(thread)
    assert browser_started is False


def _parse_stamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def test_refresh_force_updates_last_refresh_at(
    live_config: XPosterConfig, live_poster: XPoster
) -> None:
    """A forced keepalive visits /home read-only and records when it ran."""
    store = live_poster.store
    before = _parse_stamp(store.read_meta().get("last_refresh_at"))
    started = datetime.now(UTC)
    _announce("forcing a keepalive refresh of /home (read-only, no posting)")
    result = await live_poster.refresh(force=True)
    _announce(
        f"refresh: refreshed={result.refreshed} reason={result.reason} next_due={result.next_due}"
    )
    assert result.refreshed is True
    assert result.reason == "forced"
    assert result.handle is not None
    assert result.handle.lower() == (live_config.handle or "").lower()

    after_raw = store.read_meta().get("last_refresh_at")
    assert isinstance(after_raw, str) and after_raw
    after = _parse_stamp(after_raw)
    assert after is not None
    assert after >= started.replace(microsecond=0) - timedelta(seconds=1)
    if before is not None:
        assert after >= before
