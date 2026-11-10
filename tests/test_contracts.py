"""Contract tests for the shared models, config and errors.

These tests pin the names, fields and defaults that every other module is
written against, so a change here is a deliberate API change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import xposter
from xposter import (
    AccountMismatchError,
    AccountRestrictedError,
    BrowserUnavailableError,
    ChallengeError,
    ConfigurationError,
    Display,
    HumanPolicy,
    NotLoggedInError,
    PacingError,
    ParkedError,
    PostedThread,
    PostedTweet,
    PostOutcomeUnknownError,
    RateLimitedError,
    SessionBusyError,
    SessionError,
    SessionSummary,
    Strategy,
    Thread,
    Tweet,
    UiChangedError,
    ValidationError,
    XPosterConfig,
    XPosterError,
    default_home,
)

ENV_VARS = (
    "XPOSTER_HOME",
    "XPOSTER_HANDLE",
    "XPOSTER_DISPLAY",
    "XPOSTER_STRATEGY",
    "XPOSTER_CHROME_PATH",
    "XPOSTER_MAX_MENTIONS_PER_THREAD",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_tweet_defaults_and_thread_texts() -> None:
    tweet = Tweet("hello")
    assert tweet.media == ()
    assert tweet.alt_texts == ()

    thread = Thread((Tweet("one"), Tweet("two")), key="demo")
    assert thread.texts == ("one", "two")


def test_thread_rejects_empty() -> None:
    with pytest.raises(ValidationError, match="at least one tweet"):
        Thread(tweets=())


def test_content_hash_is_stable_and_covers_media_names_and_sizes(tmp_path: Path) -> None:
    one_dir = tmp_path / "one"
    other_dir = tmp_path / "other"
    one_dir.mkdir()
    other_dir.mkdir()
    (one_dir / "a.png").write_bytes(b"1234")
    (other_dir / "a.png").write_bytes(b"5678")

    first = Thread((Tweet("hello", media=(one_dir / "a.png",)),), key="k")
    same = Thread((Tweet("hello", media=(other_dir / "a.png",)),), key="k")

    assert first.content_hash == first.content_hash
    assert len(first.content_hash) == 64
    assert first.content_hash == same.content_hash

    missing = Thread((Tweet("hello", media=(tmp_path / "missing.png",)),), key="k")
    assert len(missing.content_hash) == 64
    assert missing.content_hash != first.content_hash


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param(
            Thread((Tweet("same"),), key="k1"),
            Thread((Tweet("same"),), key="k2"),
            id="key",
        ),
        pytest.param(
            Thread((Tweet("one"),), key="k"),
            Thread((Tweet("two"),), key="k"),
            id="text",
        ),
        pytest.param(
            Thread((Tweet("same", media=(Path("one.png"),)),), key="k"),
            Thread((Tweet("same", media=(Path("two.png"),)),), key="k"),
            id="media-name",
        ),
        pytest.param(
            Thread((Tweet("same", alt_texts=("a cat",)),), key="k"),
            Thread((Tweet("same", alt_texts=("a dog",)),), key="k"),
            id="alt-text",
        ),
    ],
)
def test_content_hash_detects_changes(left: Thread, right: Thread) -> None:
    assert left.content_hash != right.content_hash


def test_content_hash_changes_when_media_size_changes(tmp_path: Path) -> None:
    media = tmp_path / "a.png"
    media.write_bytes(b"a")
    thread = Thread((Tweet("same", media=(media,)),), key="k")
    before = thread.content_hash

    media.write_bytes(b"a much larger file")

    assert thread.content_hash != before


def test_content_hash_changes_when_alt_text_changes(tmp_path: Path) -> None:
    media = tmp_path / "a.png"
    media.write_bytes(b"a")
    no_alt = Thread((Tweet("same", media=(media,)),), key="k")
    with_alt = Thread((Tweet("same", media=(media,), alt_texts=("a cat",)),), key="k")
    changed = Thread((Tweet("same", media=(media,), alt_texts=("a dog",)),), key="k")

    assert no_alt.content_hash != with_alt.content_hash
    assert with_alt.content_hash != changed.content_hash


def test_posted_tweet_round_trip() -> None:
    tweet = PostedTweet(
        index=2,
        id="123",
        url="https://x.com/olsa/status/123",
        text_sha256="ab" * 32,
    )
    assert PostedTweet.from_json(tweet.to_json()) == tweet


def test_posted_thread_round_trip() -> None:
    posted = PostedThread(
        thread_hash="a" * 64,
        handle="olsa",
        strategy="composer",
        posted_at="2026-11-09T12:00:00Z",
        tweets=(
            PostedTweet(0, "1", "https://x.com/olsa/status/1", "b" * 64),
            PostedTweet(1, "2", "https://x.com/olsa/status/2", "c" * 64),
        ),
        complete=True,
    )

    restored = PostedThread.from_json(json.loads(json.dumps(posted.to_json())))

    assert restored == posted
    assert restored.first_url == "https://x.com/olsa/status/1"


def test_posted_thread_first_url_is_empty_when_nothing_posted() -> None:
    partial = PostedThread(
        thread_hash="d" * 64,
        handle="olsa",
        strategy="replies",
        posted_at="2026-11-09T12:00:00Z",
        tweets=(),
        complete=False,
    )

    assert partial.first_url == ""


def test_session_summary_round_trip() -> None:
    summary = SessionSummary(
        present=True,
        has_auth_token=True,
        has_ct0=True,
        auth_expires_at="2027-01-01T00:00:00Z",
        days_left=52,
        expired=False,
        handle="olsa",
        last_verified_at="2026-11-09T12:00:00Z",
        parked=False,
    )
    empty = SessionSummary(
        present=False,
        has_auth_token=False,
        has_ct0=False,
        auth_expires_at=None,
        days_left=None,
        expired=True,
        handle=None,
        last_verified_at=None,
        parked=True,
    )

    for item in (summary, empty):
        restored = SessionSummary.from_json(json.loads(json.dumps(item.to_json())))
        assert restored == item


def test_config_path_properties(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = XPosterConfig(home=home)

    assert config.profile_dir == home / "profile"
    assert config.storage_state_path == home / "storage_state.json"
    assert config.meta_path == home / "session.json"
    assert config.parked_path == home / "parked.json"
    assert config.ledger_path == home / "ledger.json"
    assert config.lock_path == home / "session.lock"
    assert config.artifacts_dir == home / "artifacts"


def test_config_defaults(clean_env: None) -> None:
    config = XPosterConfig()

    assert config.home == Path.home() / ".olsa" / "x"
    assert config.handle is None
    assert config.display is Display.AUTO
    assert config.strategy is Strategy.COMPOSER
    assert config.chrome_path is None
    assert config.human == HumanPolicy()
    assert config.human.seed is None
    assert config.pacing.min_gap_between_threads_s == 1800.0
    assert config.keepalive.min_interval_s == 43200.0
    assert config.max_tweet_weight == 280
    assert config.max_media_per_tweet == 4
    assert config.max_mentions_per_thread == 5
    assert config.default_timeout_ms == 30000
    assert config.keep_artifacts == 10
    assert config.base_url == "https://x.com"


def test_with_overrides_returns_copy(tmp_home: XPosterConfig) -> None:
    changed = tmp_home.with_overrides(handle="olsa", display=Display.VISIBLE)

    assert changed.handle == "olsa"
    assert changed.display is Display.VISIBLE
    assert changed.home == tmp_home.home
    assert changed.human == tmp_home.human
    assert tmp_home.handle is None


def test_tmp_home_fixture_is_deterministic(tmp_home: XPosterConfig, tmp_path: Path) -> None:
    assert tmp_home.home == tmp_path / "x"
    assert tmp_home.human.seed == 1


def test_from_env_reads_all_variables(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XPOSTER_HOME", str(tmp_path / "x"))
    monkeypatch.setenv("XPOSTER_HANDLE", "@olsa")
    monkeypatch.setenv("XPOSTER_DISPLAY", "visible")
    monkeypatch.setenv("XPOSTER_STRATEGY", "replies")
    monkeypatch.setenv("XPOSTER_CHROME_PATH", "/Applications/Chrome")

    config = XPosterConfig.from_env()

    assert config.home == tmp_path / "x"
    assert config.handle == "olsa"
    assert config.display is Display.VISIBLE
    assert config.strategy is Strategy.REPLIES
    assert config.chrome_path == "/Applications/Chrome"


def test_from_env_blank_means_unset(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.setenv(name, "   ")

    config = XPosterConfig.from_env()

    assert config.home == Path.home() / ".olsa" / "x"
    assert config.handle is None
    assert config.display is Display.AUTO
    assert config.strategy is Strategy.COMPOSER
    assert config.chrome_path is None


def test_from_env_reads_max_mentions(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPOSTER_MAX_MENTIONS_PER_THREAD", "2")

    assert XPosterConfig.from_env().max_mentions_per_thread == 2


def test_from_env_rejects_invalid_max_mentions(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XPOSTER_MAX_MENTIONS_PER_THREAD", "many")

    with pytest.raises(ConfigurationError, match="XPOSTER_MAX_MENTIONS_PER_THREAD"):
        XPosterConfig.from_env()


def test_from_env_rejects_invalid_display(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPOSTER_DISPLAY", "windowed")

    with pytest.raises(ConfigurationError, match="XPOSTER_DISPLAY"):
        XPosterConfig.from_env()


def test_from_env_rejects_invalid_strategy(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XPOSTER_STRATEGY", "threads")

    with pytest.raises(ConfigurationError, match="XPOSTER_STRATEGY"):
        XPosterConfig.from_env()


def test_default_home_honours_env(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XPOSTER_HOME", str(tmp_path / "custom"))

    assert default_home() == tmp_path / "custom"


def test_default_home_blank_falls_back(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XPOSTER_HOME", "   ")

    assert default_home() == Path.home() / ".olsa" / "x"


def test_error_hierarchy() -> None:
    for error in (
        ConfigurationError,
        BrowserUnavailableError,
        ValidationError,
        SessionBusyError,
        SessionError,
        ParkedError,
        ChallengeError,
        AccountRestrictedError,
        RateLimitedError,
        PacingError,
        UiChangedError,
        PostOutcomeUnknownError,
    ):
        assert issubclass(error, XPosterError)

    assert issubclass(NotLoggedInError, SessionError)
    assert issubclass(AccountMismatchError, SessionError)


def test_parked_error_attributes() -> None:
    parked = ParkedError(
        "Challenge shown; run `xposter unpark` after solving it.", "2026-11-09T10:00:00Z"
    )

    assert parked.reason.startswith("Challenge shown")
    assert parked.since == "2026-11-09T10:00:00Z"
    assert ParkedError("reason").since is None


def test_rate_limited_error_attributes() -> None:
    limited = RateLimitedError("Rate limited; park and retry manually later.", retry_after_s=90.0)

    assert limited.retry_after_s == 90.0
    assert RateLimitedError("Rate limited.").retry_after_s is None


def test_pacing_error_attributes() -> None:
    pacing = PacingError("Only 10 minutes since the last thread; wait.", 600.0)

    assert pacing.retry_after_s == 600.0
    assert str(pacing) == "Only 10 minutes since the last thread; wait."


def test_ui_changed_error_attributes(tmp_path: Path) -> None:
    artifacts = tmp_path / "shot.png"
    error = UiChangedError("Composer button missing; inspect artifacts.", artifacts)

    assert error.artifacts == artifacts
    assert UiChangedError("missing").artifacts is None


def test_post_outcome_unknown_error_attributes() -> None:
    posted = (PostedTweet(0, "1", "https://x.com/olsa/status/1", "e" * 64),)
    error = PostOutcomeUnknownError(
        "Click may have landed; check the profile before retrying.", posted
    )

    assert error.posted == posted
    assert PostOutcomeUnknownError("unknown").posted == ()


def test_public_exports() -> None:
    assert xposter.__version__ == "0.1.0"
    for name in xposter.__all__:
        assert hasattr(xposter, name), name
