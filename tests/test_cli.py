"""Command-line contract tests: every command, every exit code, no browser."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

import xposter.cli as cli
from xposter import (
    AccountMismatchError,
    AccountRestrictedError,
    BrowserUnavailableError,
    ChallengeError,
    ConfigurationError,
    Display,
    LedgerCorruptError,
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
    Thread,
    Tweet,
    UiChangedError,
    ValidationError,
    XPosterConfig,
    XPosterError,
)
from xposter.diagnose import DomReport, DomRow, DraftsReport, Tell, make_calibration_png
from xposter.ledger import Ledger
from xposter.pacing import Pacer
from xposter.sessions import RefreshResult, SessionInfo
from xposter.splitter import weighted_length
from xposter.store import ParkInfo

_ENV = (
    "XPOSTER_HOME",
    "XPOSTER_HANDLE",
    "XPOSTER_DISPLAY",
    "XPOSTER_STRATEGY",
    "XPOSTER_CHROME_PATH",
    "XPOSTER_MAX_MENTIONS_PER_THREAD",
)

_CANARY = "canary-cookie-value-never-print-me"


def _summary() -> SessionSummary:
    return SessionSummary(
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


def _info() -> SessionInfo:
    return SessionInfo(
        handle="olsa",
        verified_at="2026-11-09T12:00:00Z",
        chrome_version="156.0.0.0",
        auth_expires_at="2027-01-01T00:00:00Z",
        display="headless",
    )


def _posted(n: int = 2) -> PostedThread:
    tweets = tuple(
        PostedTweet(
            index=index,
            id=str(100 + index),
            url=f"https://x.com/olsa/status/{100 + index}",
            text_sha256="a" * 64,
        )
        for index in range(n)
    )
    return PostedThread(
        thread_hash="b" * 64,
        handle="olsa",
        strategy="composer",
        posted_at="2026-11-09T12:00:00Z",
        tweets=tweets,
        complete=True,
    )


def _write_png(path: Path, size: int = 16) -> bytes:
    data = make_calibration_png(size, size)
    path.write_bytes(data)
    return data


def _write_cookie_state(home: Path, token: str) -> None:
    state = {
        "cookies": [
            {
                "name": "auth_token",
                "value": token,
                "domain": ".x.com",
                "path": "/",
                "expires": 1999999999,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            },
            {
                "name": "ct0",
                "value": token,
                "domain": ".x.com",
                "path": "/",
                "expires": 1999999999,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            },
        ],
        "origins": [],
    }
    path = home / "storage_state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    path.chmod(0o600)


def _seed_partial(home: Path, thread_hash: str) -> None:
    ledger = Ledger(home / "ledger.json")
    ledger.begin(thread_hash, strategy="replies", handle="olsa", n_tweets=2)
    ledger.record_tweet(
        thread_hash,
        PostedTweet(
            index=0,
            id="111",
            url="https://x.com/olsa/status/111",
            text_sha256="a" * 64,
        ),
    )
    ledger.mark_unknown(thread_hash, "the click may have landed")


@dataclass
class FakeBackend:
    """Mutable knobs and recorded calls for the fake XPoster."""

    calls: list[str] = field(default_factory=list)
    instances: int = 0
    poster_config: XPosterConfig | None = None
    allow_alt_text: bool = False
    summary: SessionSummary = field(default_factory=_summary)
    park: ParkInfo | None = None
    unpark_result: bool = True
    login_info: SessionInfo = field(default_factory=_info)
    verify_info: SessionInfo = field(default_factory=_info)
    refresh_result: RefreshResult = field(
        default_factory=lambda: RefreshResult(
            refreshed=True,
            reason="keepalive interval elapsed",
            handle="olsa",
            next_due="2026-11-10T12:00:00Z",
        )
    )
    post_result: PostedThread = field(default_factory=_posted)
    delete_result: list[str] = field(default_factory=lambda: ["111", "222"])
    thread: Any = None
    post_kwargs: dict[str, bool] = field(default_factory=dict)
    deleted: PostedThread | None = None
    reset_path: Path = field(default_factory=lambda: Path("/tmp/ledger.json.corrupt"))
    logout_keep_backup: bool | None = None
    errors: dict[str, BaseException] = field(default_factory=dict)

    def raise_for(self, name: str) -> None:
        error = self.errors.get(name)
        if error is not None:
            raise error


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("XPOSTER_HOME", str(home))
    return home


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    backend = FakeBackend()

    class FakePoster:
        def __init__(self, config: XPosterConfig, *, allow_alt_text: bool = False) -> None:
            backend.instances += 1
            backend.poster_config = config
            backend.allow_alt_text = allow_alt_text
            self.config = config

        def session_status(self) -> SessionSummary:
            backend.calls.append("session_status")
            backend.raise_for("session_status")
            return backend.summary

        def parked(self) -> ParkInfo | None:
            backend.calls.append("parked")
            backend.raise_for("parked")
            return backend.park

        def unpark(self) -> bool:
            backend.calls.append("unpark")
            backend.raise_for("unpark")
            return backend.unpark_result

        @property
        def ledger(self) -> Ledger:
            return Ledger(self.config.ledger_path)

        @property
        def pacer(self) -> Pacer:
            return Pacer(self.config.pacing, self.ledger)

        async def login(self, *, on_status: Any = None) -> SessionInfo:
            backend.calls.append("login")
            if on_status is not None:
                on_status("fake login progress")
            backend.raise_for("login")
            return backend.login_info

        async def verify(self) -> SessionInfo:
            backend.calls.append("verify")
            backend.raise_for("verify")
            return backend.verify_info

        async def refresh(self, *, force: bool = False) -> RefreshResult:
            backend.calls.append(f"refresh:{force}")
            backend.raise_for("refresh")
            return backend.refresh_result

        async def post_thread(
            self,
            thread: Any,
            *,
            force: bool = False,
            discard_recorded: bool = False,
            dry_run: bool = False,
        ) -> PostedThread:
            backend.calls.append("post_thread")
            backend.thread = thread
            backend.post_kwargs = {
                "force": force,
                "discard_recorded": discard_recorded,
                "dry_run": dry_run,
            }
            backend.raise_for("post_thread")
            return backend.post_result

        def reset_ledger(self) -> Path:
            backend.calls.append("reset_ledger")
            backend.raise_for("reset_ledger")
            return backend.reset_path

        def logout(self, *, keep_backup: bool = True) -> None:
            backend.calls.append("logout")
            backend.logout_keep_backup = keep_backup
            backend.raise_for("logout")

        async def delete_thread(self, posted: PostedThread) -> list[str]:
            backend.calls.append("delete_thread")
            backend.deleted = posted
            backend.raise_for("delete_thread")
            return backend.delete_result

    monkeypatch.setattr(cli, "XPoster", FakePoster)
    return backend


def invoke(*args: str, input: str | None = None) -> Result:
    return CliRunner().invoke(cli.app, list(args), input=input)


def _plain_help(output: str) -> str:
    """Flatten Rich's box-drawing table so help phrases stay searchable."""
    translated = output.translate({ord(char): " " for char in "│─╭╮╰╯"})
    return " ".join(translated.split())


def test_help_lists_every_command() -> None:
    result = invoke("--help")

    assert result.exit_code == 0
    for name in (
        "login",
        "status",
        "verify",
        "refresh",
        "unpark",
        "post",
        "delete",
        "ledger",
        "doctor",
    ):
        assert name in result.output


def test_global_options_build_the_config(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("--handle", "@olsa", "--premium", "--display", "visible", "status")

    assert result.exit_code == 0
    assert fake.poster_config is not None
    assert fake.poster_config.handle == "olsa"
    assert fake.poster_config.max_tweet_weight == 25000
    assert fake.poster_config.display is Display.VISIBLE
    assert fake.poster_config.home == cli_home


def test_home_option_overrides_the_environment(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    other = tmp_path / "other"

    result = invoke("--home", str(other), "--strategy", "replies", "status")

    assert result.exit_code == 0
    assert fake.poster_config is not None
    assert fake.poster_config.home == other
    assert fake.poster_config.strategy.value == "replies"


def test_login_explains_the_one_time_flow(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("login")

    assert result.exit_code == 0
    assert "cmd+q" in result.output.lower()
    assert "fake login progress" in result.output
    assert "@olsa" in result.output
    assert "156.0.0.0" in result.output
    assert "2027-01-01" in result.output
    assert "doctor --dom" in result.output
    assert "automation rules" in result.output
    assert fake.calls == ["login"]


def test_logout_deletes_the_profile_after_confirmation(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("logout", "--yes")

    assert result.exit_code == 0
    assert "saved Chrome profile" in result.output
    assert "xposter login" in result.output
    assert fake.logout_keep_backup is True
    assert "logout" in fake.calls


def test_logout_no_backup_drops_the_cookie_copy(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("logout", "--yes", "--no-backup")

    assert result.exit_code == 0
    assert fake.logout_keep_backup is False
    assert "backup is deleted" in result.output


def test_logout_declined_keeps_the_profile(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("logout", input="n\n")

    assert result.exit_code == 0
    assert "Cancelled; nothing was deleted." in result.output
    assert "logout" not in fake.calls


def test_refresh_help_tells_timers_to_call_plain_refresh(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("refresh", "--help")

    flat = _plain_help(result.output)
    assert result.exit_code == 0
    assert "timers must call plain" in flat
    assert "cadence is respected" in flat


def test_status_is_offline_and_reports_counts(cli_home: Path, fake: FakeBackend) -> None:
    ledger = Ledger(cli_home / "ledger.json")
    ledger.begin("a" * 64, strategy="composer", handle="olsa", n_tweets=1)
    ledger.record_tweet("a" * 64, _posted(1).tweets[0])
    ledger.complete("a" * 64, _posted(1))
    ledger.begin("b" * 64, strategy="composer", handle="olsa", n_tweets=2)

    result = invoke("status")

    assert result.exit_code == 0
    assert "Session: ok (@olsa)" in result.output
    assert "Parked: no" in result.output
    assert "Next post allowed:" in result.output
    assert "Ledger: 2 entries (posted 1, pending 1, unknown 0, failed 0)." in result.output
    assert set(fake.calls) <= {"session_status", "parked"}


def test_status_json_is_single_object(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("status", "--json")

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["session"]["handle"] == "olsa"
    assert payload["parked"] is None
    assert payload["ledger"]["entries"] == 0
    assert "next_allowed_at" in payload["pacing"]


def test_status_reports_parked_state(cli_home: Path, fake: FakeBackend) -> None:
    fake.park = ParkInfo(
        reason="Verify you are a human", kind="challenge", since="2026-11-09T10:00:00Z", detail=""
    )

    result = invoke("status")

    assert result.exit_code == 0
    assert "Parked: yes since 2026-11-09T10:00:00Z" in result.output
    assert "Verify you are a human" in result.output


def test_verify_prints_the_session(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("verify")

    assert result.exit_code == 0
    assert "Session valid as @olsa." in result.output
    assert "2026-11-09T12:00:00Z" in result.output
    assert fake.calls == ["verify"]


def test_refresh_reports_whether_it_ran(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("refresh", "--force")

    assert result.exit_code == 0
    assert "Session refreshed for @olsa." in result.output
    assert "2026-11-10T12:00:00Z" in result.output
    assert fake.calls == ["refresh:True"]

    fake.refresh_result = RefreshResult(
        refreshed=False, reason="not due", handle="olsa", next_due=None
    )
    result = invoke("refresh")
    assert result.exit_code == 0
    assert "not needed" in result.output
    assert "not due" in result.output


def test_unpark_without_a_marker_does_nothing(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("unpark")

    assert result.exit_code == 0
    assert "Not parked" in result.output
    assert "unpark" not in fake.calls


def test_unpark_declined_keeps_the_marker(cli_home: Path, fake: FakeBackend) -> None:
    fake.park = ParkInfo("challenge", "challenge", "2026-11-09T10:00:00Z", "")

    result = invoke("unpark", input="n\n")

    assert result.exit_code == 0
    assert "Left parked; nothing changed." in result.output
    assert "unpark" not in fake.calls


def test_unpark_clears_after_confirmation(cli_home: Path, fake: FakeBackend) -> None:
    fake.park = ParkInfo("challenge", "challenge", "2026-11-09T10:00:00Z", "")

    result = invoke("unpark", "--yes")

    assert result.exit_code == 0
    assert "Parked state cleared." in result.output
    assert "unpark" in fake.calls


def test_post_previews_splits_and_prints_urls(cli_home: Path, fake: FakeBackend) -> None:
    long_text = " ".join(f"word{index}" for index in range(200))

    result = invoke("post", "--text", long_text, "--yes")

    assert result.exit_code == 0
    assert "Preview:" in result.output
    assert "/280" in result.output
    assert fake.thread is not None
    assert len(fake.thread.tweets) > 1
    total = len(fake.thread.tweets)
    assert fake.thread.tweets[0].text.endswith(f"1/{total}")
    assert all(weighted_length(tweet.text) <= 280 for tweet in fake.thread.tweets)
    assert fake.post_kwargs == {
        "force": False,
        "discard_recorded": False,
        "dry_run": False,
    }
    assert "https://x.com/olsa/status/100" in result.output
    assert result.output.strip().splitlines()[-1] == "https://x.com/olsa/status/100"


def test_post_declined_posts_nothing(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("post", "--text", "hello", input="n\n")

    assert result.exit_code == 0
    assert "Cancelled; nothing was posted." in result.output
    assert "post_thread" not in fake.calls


def test_post_dry_run_skips_confirmation(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("post", "--text", "hello", "--dry-run")

    assert result.exit_code == 0
    assert "Dry run: nothing was posted." in result.output
    assert fake.post_kwargs == {
        "force": False,
        "discard_recorded": False,
        "dry_run": True,
    }


def test_post_force_is_passed_to_the_poster(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("post", "--text", "hello", "--force", "--yes")

    assert result.exit_code == 0
    assert fake.post_kwargs == {
        "force": True,
        "discard_recorded": False,
        "dry_run": False,
    }


def test_post_force_help_documents_recovery_not_repeat(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("post", "--help")

    flat = _plain_help(result.output)
    assert result.exit_code == 0
    assert "Recovery only, not a repeat tool" in flat
    assert "on a partially posted thread it CONTINUES after the recorded tweets" in flat
    assert "it never re-posts them" in flat
    assert "--key" in flat
    assert "week-2026-W45" in flat


def test_post_key_sets_thread_key(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("post", "--text", "hello", "--key", "week-2026-W45", "--yes")

    assert result.exit_code == 0
    assert fake.thread is not None
    assert fake.thread.key == "week-2026-W45"


def test_post_discard_recorded_shows_ids_and_passes_flag(cli_home: Path, fake: FakeBackend) -> None:
    thread = Thread((Tweet("hello"),), key="")
    _seed_partial(cli_home, thread.content_hash)

    result = invoke("post", "--text", "hello", "--discard-recorded", "--yes")

    assert result.exit_code == 0
    assert "111" in result.output
    assert "https://x.com/olsa/status/111" in result.output
    assert fake.post_kwargs == {
        "force": False,
        "discard_recorded": True,
        "dry_run": False,
    }


def test_post_discard_recorded_needs_confirmation(cli_home: Path, fake: FakeBackend) -> None:
    thread = Thread((Tweet("hello"),), key="")
    _seed_partial(cli_home, thread.content_hash)

    result = invoke("post", "--text", "hello", "--discard-recorded", input="n\n")

    assert result.exit_code == 0
    assert "Cancelled; nothing was posted." in result.output
    assert "post_thread" not in fake.calls


def test_post_partial_entry_preview_lists_recorded_urls(cli_home: Path, fake: FakeBackend) -> None:
    thread = Thread((Tweet("hello"),), key="")
    _seed_partial(cli_home, thread.content_hash)

    result = invoke("post", "--text", "hello", "--yes")

    assert result.exit_code == 0
    assert "records" in result.output
    assert "111" in result.output
    assert "https://x.com/olsa/status/111" in result.output


def test_post_warns_when_headless(
    cli_home: Path, fake: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "resolve_display", lambda config: Display.HEADLESS)

    result = invoke("post", "--text", "hello", "--yes")

    assert result.exit_code == 0
    assert "HeadlessChrome user agent is visible to X" in result.stderr


def test_post_headless_warning_is_skipped_for_explicit_headless_yes(
    cli_home: Path, fake: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "resolve_display", lambda config: Display.HEADLESS)

    result = invoke("--display", "headless", "post", "--text", "hello", "--yes")

    assert result.exit_code == 0
    assert "HeadlessChrome" not in result.stderr


def test_post_headless_warning_absent_for_visible(
    cli_home: Path, fake: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "resolve_display", lambda config: Display.VISIBLE)

    result = invoke("post", "--text", "hello", "--yes")

    assert result.exit_code == 0
    assert "HeadlessChrome" not in result.stderr


def test_post_json_keeps_stdout_machine_readable(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("post", "--text", "hello", "--yes", "--json")

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["handle"] == "olsa"
    assert payload["tweets"][0]["url"] == "https://x.com/olsa/status/100"
    assert "Preview:" not in result.stdout
    assert "Preview:" in result.stderr


def test_post_thread_json_parses_strings_and_objects(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    picture = assets / "pic.png"
    picture.write_bytes(make_calibration_png(16, 16))
    source = tmp_path / "thread.json"
    source.write_text(
        json.dumps(["first tweet", {"text": "second tweet", "media": ["assets/pic.png"]}]),
        encoding="utf-8",
    )

    result = invoke("post", "--thread-json", str(source), "--yes")

    assert result.exit_code == 0
    assert fake.thread is not None
    assert fake.thread.texts == ("first tweet", "second tweet")
    assert fake.thread.tweets[1].media == (picture,)
    assert fake.thread.tweets[0].media == ()


def test_post_thread_json_rejects_bad_json(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    source = tmp_path / "thread.json"
    source.write_text("{not json", encoding="utf-8")

    result = invoke("post", "--thread-json", str(source), "--yes")

    assert result.exit_code == 2
    assert "not valid JSON" in result.output
    assert "post_thread" not in fake.calls


def test_post_first_file_is_verbatim(cli_home: Path, fake: FakeBackend, tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    first.write_text("Pinned index post\n", encoding="utf-8")
    rest = " ".join(f"detail{index}" for index in range(120))

    result = invoke("post", "--text", rest, "--first-file", str(first), "--yes")

    assert result.exit_code == 0
    assert fake.thread is not None
    assert fake.thread.tweets[0].text == "Pinned index post"


def test_post_rejects_ambiguous_sources(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("post", "--text", "hello", "--file", "somewhere.txt", "--yes")

    assert result.exit_code == 2
    assert "exactly one" in result.output
    assert "post_thread" not in fake.calls


def test_post_rejects_missing_media(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("post", "--text", "hello", "--media", "missing.png", "--yes")

    assert result.exit_code == 2
    assert "not found" in result.output


def test_post_no_numbering_leaves_text_alone(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("post", "--text", "hello world", "--no-numbering", "--yes")

    assert result.exit_code == 0
    assert fake.thread is not None
    assert fake.thread.texts == ("hello world",)
    assert "numbering off" in result.output


def test_post_repeated_media_attaches_to_tweet_one(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    first.write_bytes(make_calibration_png(16, 16))
    second.write_bytes(make_calibration_png(16, 16))

    result = invoke(
        "post",
        "--text",
        "hello",
        "--media",
        str(first),
        "--media",
        str(second),
        "--yes",
    )

    assert result.exit_code == 0
    assert fake.thread is not None
    assert fake.thread.tweets[0].media == (first, second)
    assert "media: one.png" in result.output
    assert "two.png" in result.output
    assert "KB, no alt" in result.output


def test_post_media_index_targets_a_later_tweet(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    picture = tmp_path / "later.png"
    _write_png(picture)
    source = tmp_path / "thread.json"
    source.write_text(json.dumps(["first", "second"]), encoding="utf-8")

    result = invoke("post", "--thread-json", str(source), "--media", f"2:{picture}", "--yes")

    assert result.exit_code == 0
    assert fake.thread is not None
    assert fake.thread.tweets[0].media == ()
    assert fake.thread.tweets[1].media == (picture,)


def test_post_plain_alt_texts_fill_tweet_one_images(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    _write_png(first)
    _write_png(second)

    result = invoke(
        "post",
        "--text",
        "hello",
        "--media",
        str(first),
        "--media",
        str(second),
        "--alt",
        "first image",
        "--alt",
        "second image",
        "--allow-alt",
        "--yes",
    )

    assert result.exit_code == 0
    assert fake.thread is not None
    assert fake.thread.tweets[0].alt_texts == ("first image", "second image")
    assert fake.allow_alt_text is True
    assert "alt" in result.output


def test_post_qualified_alt_text_targets_one_image(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    picture = tmp_path / "later.png"
    _write_png(picture)
    source = tmp_path / "thread.json"
    source.write_text(
        json.dumps(["first", {"text": "second", "media": [str(picture)]}]),
        encoding="utf-8",
    )

    result = invoke(
        "post",
        "--thread-json",
        str(source),
        "--alt",
        "2.1:described later",
        "--allow-alt",
        "--yes",
    )

    assert result.exit_code == 0
    assert fake.thread is not None
    assert fake.thread.tweets[1].alt_texts == ("described later",)
    assert fake.allow_alt_text is True


def test_post_thread_json_reads_aligned_alt_list(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    _write_png(first)
    _write_png(second)
    source = tmp_path / "thread.json"
    source.write_text(
        json.dumps(
            [
                {
                    "text": "one",
                    "media": [str(first), str(second)],
                    "alt": ["one alt", "two alt"],
                }
            ]
        ),
        encoding="utf-8",
    )

    result = invoke("post", "--thread-json", str(source), "--allow-alt", "--yes")

    assert result.exit_code == 0
    assert fake.thread is not None
    assert fake.thread.tweets[0].alt_texts == ("one alt", "two alt")
    assert fake.allow_alt_text is True


def test_post_thread_json_rejects_too_many_alt_texts(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    picture = tmp_path / "only.png"
    _write_png(picture)
    source = tmp_path / "thread.json"
    source.write_text(
        json.dumps([{"text": "one", "media": [str(picture)], "alt": ["a", "b"]}]),
        encoding="utf-8",
    )

    result = invoke("post", "--thread-json", str(source), "--yes")

    assert result.exit_code == 2
    assert "align them one-to-one" in result.output


def test_post_preview_shows_media_sizes_and_alt(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    picture = tmp_path / "described.png"
    _write_png(picture)

    result = invoke(
        "post",
        "--text",
        "hello",
        "--media",
        str(picture),
        "--alt",
        "a description",
        "--allow-alt",
        "--dry-run",
    )

    assert result.exit_code == 0
    assert "described.png" in result.output
    assert "KB" in result.output
    assert "alt" in result.output
    assert "will be refused" not in result.output
    assert fake.allow_alt_text is True
    assert fake.thread is not None
    assert fake.thread.tweets[0].media == (picture,)


def test_post_refuses_alt_text_without_the_allow_flag(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    picture = tmp_path / "described.png"
    _write_png(picture)

    result = invoke(
        "post",
        "--text",
        "hello",
        "--media",
        str(picture),
        "--alt",
        "a description",
        "--yes",
    )

    assert result.exit_code == 2
    assert "Preview:" in result.output
    assert "alt, 5 MiB limit" in result.output
    assert "will be refused" in result.output
    assert "alt text is not verified against the live site yet" in result.output
    assert "allow_alt_text=True / --allow-alt" in result.output
    assert "post_thread" not in fake.calls
    assert fake.instances == 0


def test_post_thread_json_alt_text_requires_the_allow_flag(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    picture = tmp_path / "described.png"
    _write_png(picture)
    source = tmp_path / "thread.json"
    source.write_text(
        json.dumps([{"text": "one", "media": [str(picture)], "alt": ["from json"]}]),
        encoding="utf-8",
    )

    result = invoke("post", "--thread-json", str(source), "--yes")

    assert result.exit_code == 2
    assert "alt, 5 MiB limit" in result.output
    assert "will be refused" in result.output
    assert "alt text is not verified against the live site yet" in result.output
    assert "allow_alt_text=True / --allow-alt" in result.output
    assert "post_thread" not in fake.calls
    assert fake.instances == 0


def test_post_blank_alt_text_never_requires_the_allow_flag(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    picture = tmp_path / "described.png"
    _write_png(picture)

    result = invoke(
        "post",
        "--text",
        "hello",
        "--media",
        str(picture),
        "--alt",
        "",
        "--yes",
    )

    assert result.exit_code == 0
    assert fake.thread is not None
    assert fake.thread.tweets[0].alt_texts == ("",)
    assert fake.allow_alt_text is False
    assert "post_thread" in fake.calls
    assert "will be refused" not in result.output
    assert "no alt" in result.output


@pytest.mark.parametrize("blank", ["", "   "])
def test_post_thread_json_blank_alt_never_requires_the_allow_flag(
    cli_home: Path, fake: FakeBackend, tmp_path: Path, blank: str
) -> None:
    picture = tmp_path / "described.png"
    _write_png(picture)
    source = tmp_path / "thread.json"
    source.write_text(
        json.dumps([{"text": "one", "media": [str(picture)], "alt": [blank]}]),
        encoding="utf-8",
    )

    result = invoke("post", "--thread-json", str(source), "--yes")

    assert result.exit_code == 0
    assert fake.thread is not None
    assert fake.thread.tweets[0].alt_texts == (blank,)
    assert fake.allow_alt_text is False
    assert "post_thread" in fake.calls
    assert "will be refused" not in result.output
    assert "no alt" in result.output


def test_post_allow_alt_flag_reaches_the_poster(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("post", "--text", "hello", "--allow-alt", "--yes")

    assert result.exit_code == 0
    assert fake.allow_alt_text is True
    assert "post_thread" in fake.calls


def test_post_defaults_to_alt_text_disabled(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("post", "--text", "hello", "--yes")

    assert result.exit_code == 0
    assert fake.allow_alt_text is False
    assert "post_thread" in fake.calls


def test_post_surfaces_the_posters_alt_validation_error(cli_home: Path, fake: FakeBackend) -> None:
    fake.errors["post_thread"] = ValidationError(
        "alt text is not verified against the live site yet; pass "
        "allow_alt_text=True / --allow-alt to try it on a test account"
    )

    result = invoke("post", "--text", "hello", "--yes")

    assert result.exit_code == 2
    assert "alt text is not verified against the live site yet" in result.output
    assert "--allow-alt" in result.output
    assert "post_thread" in fake.calls


def test_post_preview_lists_mentions_after_media_and_summarises_the_cap(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    picture = tmp_path / "described.png"
    _write_png(picture)
    source = tmp_path / "thread.json"
    source.write_text(
        json.dumps(
            [
                {"text": "hello @Alice and @bob", "media": [str(picture)]},
                {"text": "again @ALICE"},
            ]
        ),
        encoding="utf-8",
    )

    result = invoke("post", "--thread-json", str(source), "--dry-run")

    assert result.exit_code == 0
    lines = result.output.splitlines()
    media_index = next(i for i, line in enumerate(lines) if "media:" in line)
    first_mentions = next(
        i for i, line in enumerate(lines) if line.strip() == "mentions: @alice, @bob"
    )
    assert first_mentions > media_index
    mention_lines = [line.strip() for line in lines if line.strip().startswith("mentions: @")]
    assert mention_lines == ["mentions: @alice, @bob", "mentions: @alice"]
    assert "mentions: 2 distinct (cap 5)" in result.output
    assert (
        "mentions notify those accounts; only tag accounts that expect it "
        "(X Automation Rules)" in result.output
    )
    assert fake.poster_config is not None
    assert fake.poster_config.max_mentions_per_thread == 5
    assert fake.thread is not None


def test_post_mention_cap_exceeded_warns_loudly_and_refuses_up_front(
    cli_home: Path, fake: FakeBackend
) -> None:
    text = " ".join(f"@user{index}" for index in range(6))

    result = invoke("post", "--text", text, "--dry-run")

    assert result.exit_code == 2
    assert "mentions: 6 distinct (cap 5)" in result.output
    assert (
        "!! 6 distinct @mentions exceed max_mentions_per_thread=5; "
        "X treats unsolicited automated mentions as spam" in result.output
    )
    assert "mentions notify those accounts" not in result.output
    assert "6 distinct @mentions exceed max_mentions_per_thread=5." in result.output
    assert "https://help.x.com/en/rules-and-policies/x-automation" in result.output
    assert "post_thread" not in fake.calls
    assert fake.instances == 0


def test_max_mentions_override_raises_the_cap_for_one_run(
    cli_home: Path, fake: FakeBackend
) -> None:
    text = " ".join(f"@user{index}" for index in range(6))

    result = invoke("--max-mentions", "6", "post", "--text", text, "--dry-run")

    assert result.exit_code == 0
    assert "mentions: 6 distinct (cap 6)" in result.output
    assert "mentions notify those accounts" in result.output
    assert "!!" not in result.output
    assert fake.poster_config is not None
    assert fake.poster_config.max_mentions_per_thread == 6
    assert "post_thread" in fake.calls


def test_max_mentions_override_can_lower_the_cap(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("--max-mentions", "1", "post", "--text", "@a @b", "--dry-run")

    assert result.exit_code == 2
    assert "mentions: 2 distinct (cap 1)" in result.output
    assert (
        "!! 2 distinct @mentions exceed max_mentions_per_thread=1; "
        "X treats unsolicited automated mentions as spam" in result.output
    )
    assert "post_thread" not in fake.calls
    assert fake.instances == 0


def test_post_self_mentions_are_not_counted(cli_home: Path, fake: FakeBackend) -> None:
    text = "@olsa " + " ".join(f"@user{index}" for index in range(5))

    result = invoke("--handle", "olsa", "post", "--text", text, "--dry-run")

    assert result.exit_code == 0
    mention_lines = [
        line.strip()
        for line in result.output.splitlines()
        if line.strip().startswith("mentions: @")
    ]
    assert mention_lines == ["mentions: @user0, @user1, @user2, @user3, @user4"]
    assert "mentions: 5 distinct (cap 5)" in result.output
    assert "mentions notify those accounts" in result.output

    lower_case = invoke("--handle", "OLSA", "post", "--text", "hi @Olsa and @alice", "--dry-run")
    assert lower_case.exit_code == 0
    assert "mentions: 1 distinct (cap 5)" in lower_case.output
    assert "mentions: @alice" in lower_case.output


def test_post_email_addresses_are_not_mentions(cli_home: Path, fake: FakeBackend) -> None:
    emails = invoke(
        "post",
        "--text",
        "write to alice@example.com or bob@example.org",
        "--dry-run",
    )

    assert emails.exit_code == 0
    assert "mentions: 0 distinct (cap 5)" in emails.output
    assert not any(line.strip().startswith("mentions: @") for line in emails.output.splitlines())
    assert "mentions notify those accounts" not in emails.output

    mixed = invoke(
        "post",
        "--text",
        "hello @alice, mail bob@example.com",
        "--dry-run",
    )

    assert mixed.exit_code == 0
    assert "mentions: @alice" in mixed.output
    assert "mentions: 1 distinct (cap 5)" in mixed.output


def test_post_surfaces_the_posters_mention_validation_error(
    cli_home: Path, fake: FakeBackend
) -> None:
    fake.errors["post_thread"] = ValidationError(
        "2 distinct @mentions exceed max_mentions_per_thread=5. X treats "
        "unsolicited automated mentions as spam "
        "(https://help.x.com/en/rules-and-policies/x-automation); mention only "
        "accounts that expect it, or raise the cap deliberately on a test account."
    )

    result = invoke("post", "--text", "hello @alice", "--dry-run")

    assert result.exit_code == 2
    assert "2 distinct @mentions exceed max_mentions_per_thread=5." in result.output
    assert "https://help.x.com/en/rules-and-policies/x-automation" in result.output
    assert "post_thread" in fake.calls


def test_max_mentions_help_documents_test_accounts(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("--help")

    flat = _plain_help(result.output)
    assert result.exit_code == 0
    assert "--max-mentions" in flat
    assert "meant for test accounts" in flat


def test_doctor_offline_report_shows_the_mention_cap(
    cli_home: Path, fake: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.diagnose, "find_chrome", lambda config: "/fake/chrome")
    monkeypatch.setattr(cli.diagnose, "chrome_version", lambda path: "156.0.0.0")
    monkeypatch.setattr(cli.diagnose, "resolve_display", lambda config: Display.HEADLESS)

    result = invoke("doctor")

    assert result.exit_code == 0
    assert "max_mentions_per_thread=5" in result.output

    overridden = invoke("--max-mentions", "2", "doctor")

    assert overridden.exit_code == 0
    assert "max_mentions_per_thread=2" in overridden.output


def test_post_preview_media_limits_never_replace_the_character_limit(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    _write_png(first)
    _write_png(second)
    source = tmp_path / "thread.json"
    source.write_text(
        json.dumps(
            [
                {"text": "tweet one", "media": [str(first)]},
                {"text": "tweet two", "media": [str(second)]},
            ]
        ),
        encoding="utf-8",
    )

    result = invoke("post", "--thread-json", str(source), "--dry-run")

    assert result.exit_code == 0
    numbered = [
        line.strip()
        for line in result.output.splitlines()
        if line.strip().startswith(("1/2", "2/2"))
    ]
    assert len(numbered) == 2
    for line in numbered:
        assert "/280" in line
        assert "MiB" not in line
    media_lines = [line for line in result.output.splitlines() if "media:" in line]
    assert len(media_lines) == 2
    assert all("5 MiB limit" in line for line in media_lines)
    assert fake.thread is not None
    assert [tweet.media for tweet in fake.thread.tweets] == [(first,), (second,)]


def test_post_dry_run_attaches_media_for_real(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    picture = tmp_path / "dry.png"
    _write_png(picture)

    result = invoke("post", "--text", "hello", "--media", str(picture), "--dry-run")

    assert result.exit_code == 0
    assert fake.post_kwargs["dry_run"] is True
    assert fake.thread is not None
    assert fake.thread.tweets[0].media == (picture,)


def test_post_oversize_media_is_loud_and_exits_two(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    picture = tmp_path / "huge.png"
    data = make_calibration_png(16, 16)
    picture.write_bytes(data + b"\x00" * (5 * 1024 * 1024 + 1 - len(data)))

    result = invoke("post", "--text", "hello", "--media", str(picture), "--yes")

    assert result.exit_code == 2
    assert "over X's 5 MiB image limit" in result.output
    assert "KB" in result.output
    assert "post_thread" not in fake.calls


def test_post_unsupported_media_is_loud_and_exits_two(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    picture = tmp_path / "notes.txt"
    picture.write_text("not an image", encoding="utf-8")

    result = invoke("post", "--text", "hello", "--media", str(picture), "--yes")

    assert result.exit_code == 2
    assert "unsupported media type" in result.output
    assert "X accepts only PNG, JPEG, GIF or WebP" in result.output
    assert "post_thread" not in fake.calls


def test_post_media_rejects_a_tweet_past_the_thread(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    picture = tmp_path / "later.png"
    _write_png(picture)

    result = invoke("post", "--text", "hello", "--media", f"2:{picture}", "--yes")

    assert result.exit_code == 2
    assert "references tweet 2" in result.output


def test_post_alt_rejects_an_image_past_the_media(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    picture = tmp_path / "one.png"
    _write_png(picture)

    result = invoke(
        "post", "--text", "hello", "--media", str(picture), "--alt", "1.2:too far", "--yes"
    )

    assert result.exit_code == 2
    assert "image 2" in result.output


def test_post_help_documents_media_and_alt_specs(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("post", "--help")

    flat = _plain_help(result.output)
    assert result.exit_code == 0
    assert "N:PATH" in flat
    assert "N.M:TEXT" in flat
    assert "--alt" in flat
    assert "--allow-alt" in flat
    assert "Edit image description" in flat
    assert "not been fully observed yet" in flat
    assert "try it on a test account first" in flat


def test_post_reads_stdin_with_dash(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("post", "--file", "-", "--yes", input="hello from stdin\n")

    assert result.exit_code == 0
    assert fake.thread is not None
    assert fake.thread.texts == ("hello from stdin",)


def test_post_premium_raises_the_weight_limit(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("--premium", "post", "--text", "word " * 80, "--yes")

    assert result.exit_code == 0
    assert fake.poster_config is not None
    assert fake.poster_config.max_tweet_weight == 25000
    assert fake.thread is not None
    assert len(fake.thread.tweets) == 1


def test_post_dry_run_json_keeps_stdout_clean(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("post", "--text", "hello", "--dry-run", "--json")

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["strategy"] == "composer"
    assert "Dry run: nothing was posted." in result.stderr
    assert "Preview:" in result.stderr


def test_delete_builds_posted_thread_from_urls_and_ids(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke(
        "delete",
        "https://x.com/olsa/status/111?ref=test",
        "222",
        "--yes",
    )

    assert result.exit_code == 0
    assert fake.deleted is not None
    assert [tweet.id for tweet in fake.deleted.tweets] == ["111", "222"]
    assert fake.deleted.tweets[0].url == "https://x.com/olsa/status/111"
    assert "Deleted 111." in result.output
    assert "Deleted 222." in result.output


def test_delete_declined_deletes_nothing(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("delete", "111", input="n\n")

    assert result.exit_code == 0
    assert "Cancelled; nothing was deleted." in result.output
    assert "delete_thread" not in fake.calls


def test_ledger_lists_recent_entries(cli_home: Path, fake: FakeBackend) -> None:
    ledger = Ledger(cli_home / "ledger.json")
    posted = _posted(1)
    ledger.begin("a" * 64, strategy="composer", handle="olsa", n_tweets=1)
    ledger.record_tweet("a" * 64, posted.tweets[0])
    ledger.complete("a" * 64, posted)

    result = invoke("ledger")

    assert result.exit_code == 0
    assert "posted" in result.output
    assert "aaaaaaaa" in result.output
    assert "https://x.com/olsa/status/100" in result.output
    assert "unpark" not in fake.calls
    assert fake.calls == []


def test_ledger_json(cli_home: Path, fake: FakeBackend) -> None:
    ledger = Ledger(cli_home / "ledger.json")
    posted = _posted(1)
    ledger.begin("a" * 64, strategy="composer", handle="olsa", n_tweets=1)
    ledger.record_tweet("a" * 64, posted.tweets[0])
    ledger.complete("a" * 64, posted)

    result = invoke("ledger", "--json", "--limit", "5")

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload[0]["thread_hash"] == "a" * 64
    assert payload[0]["status"] == "posted"
    assert payload[0]["first_url"] == "https://x.com/olsa/status/100"


def test_ledger_reset_corrupt_quarantines_and_prints_path(
    cli_home: Path, fake: FakeBackend
) -> None:
    fake.reset_path = cli_home / "ledger.json.corrupt"

    result = invoke("ledger", "--reset-corrupt", "--yes")

    assert result.exit_code == 0
    assert str(fake.reset_path) in result.output
    assert "reset_ledger" in fake.calls


def test_ledger_reset_corrupt_needs_confirmation(cli_home: Path, fake: FakeBackend) -> None:
    result = invoke("ledger", "--reset-corrupt", input="n\n")

    assert result.exit_code == 0
    assert "Cancelled; the ledger was not changed." in result.output
    assert "reset_ledger" not in fake.calls


def test_ledger_reset_corrupt_json(cli_home: Path, fake: FakeBackend) -> None:
    fake.reset_path = cli_home / "ledger.json.corrupt"

    result = invoke("ledger", "--reset-corrupt", "--yes", "--json")

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["quarantined"] == str(fake.reset_path)


def test_ledger_reset_corrupt_without_a_file_is_not_an_error(
    cli_home: Path, fake: FakeBackend
) -> None:
    fake.errors["reset_ledger"] = FileNotFoundError("no ledger")

    result = invoke("ledger", "--reset-corrupt", "--yes")

    assert result.exit_code == 0
    assert "No corrupt ledger file was found" in result.output


def test_ledger_command_reports_a_corrupt_file(cli_home: Path, fake: FakeBackend) -> None:
    (cli_home / "ledger.json").write_text("{broken", encoding="utf-8")

    result = invoke("ledger")

    assert result.exit_code == 6
    assert "ledger.json" in result.output
    assert "restore" in result.output
    assert "--reset-corrupt" in result.output


@pytest.mark.parametrize(
    ("error", "code", "needle"),
    [
        (ValidationError("bad input"), 2, "Validation error"),
        (ConfigurationError("bad config"), 2, "Configuration error"),
        (NotLoggedInError("no session"), 3, "xposter login"),
        (AccountMismatchError("wrong account"), 3, "Account mismatch"),
        (SessionError("session problem"), 3, "Session error"),
        (ParkedError("challenge shown", "2026-11-09T10:00:00Z"), 4, "xposter unpark"),
        (ChallengeError("captcha"), 4, "xposter unpark"),
        (AccountRestrictedError("locked"), 4, "xposter unpark"),
        (RateLimitedError("slow down", retry_after_s=30.0), 4, "waiting"),
        (PacingError("too soon", 600.0), 5, "Next allowed at"),
        (
            PostOutcomeUnknownError(
                "unknown", (PostedTweet(0, "999", "https://x.com/olsa/status/999", "c" * 64),)
            ),
            6,
            "999",
        ),
        (LedgerCorruptError("unreadable", path=Path("/tmp/ledger.json")), 6, "--reset-corrupt"),
        (UiChangedError("composer moved", Path("/tmp/artifacts/run")), 7, "doctor --dom"),
        (BrowserUnavailableError("no chrome"), 8, "Chrome"),
        (SessionBusyError("locked profile"), 8, "other xposter run"),
        (XPosterError("generic failure"), 1, "xposter error"),
        (RuntimeError("boom"), 1, "Unexpected RuntimeError"),
    ],
)
def test_exit_codes(
    cli_home: Path,
    fake: FakeBackend,
    error: BaseException,
    code: int,
    needle: str,
) -> None:
    fake.errors["verify"] = error

    result = invoke("verify")

    assert result.exit_code == code
    assert needle in result.output


def test_unknown_outcome_prints_ids_before_force(cli_home: Path, fake: FakeBackend) -> None:
    posted = (PostedTweet(0, "999", "https://x.com/olsa/status/999", "c" * 64),)
    fake.errors["verify"] = PostOutcomeUnknownError("unknown outcome", posted)

    result = invoke("verify")

    assert result.exit_code == 6
    assert "999" in result.output
    assert "profile" in result.output
    assert "--force" in result.output


def test_ledger_corrupt_names_file_and_both_options(
    cli_home: Path, fake: FakeBackend, tmp_path: Path
) -> None:
    path = tmp_path / "ledger.json"
    fake.errors["verify"] = LedgerCorruptError("not valid JSON", path=path)

    result = invoke("verify")

    assert result.exit_code == 6
    assert str(path) in result.output
    assert "restore" in result.output
    assert "ledger --reset-corrupt" in result.output


def test_secret_values_never_appear_in_status_doctor_or_ledger(
    cli_home: Path, fake: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_cookie_state(cli_home, _CANARY)

    monkeypatch.setattr(cli.diagnose, "find_chrome", lambda config: "/fake/chrome")
    monkeypatch.setattr(cli.diagnose, "chrome_version", lambda path: "156.0.0.0")
    monkeypatch.setattr(cli.diagnose, "resolve_display", lambda config: Display.HEADLESS)

    for args in (("status",), ("doctor",), ("ledger",), ("status", "--json")):
        result = invoke(*args)
        assert result.exit_code == 0, args
        assert _CANARY not in result.output, args


def test_verbose_traceback_redacts_a_chained_cookie_canary(
    cli_home: Path, fake: FakeBackend
) -> None:
    _write_cookie_state(cli_home, _CANARY)
    try:
        raise ValueError(f"inner failure auth_token={_CANARY}")
    except ValueError as inner:
        outer = RuntimeError(f"outer failure carrying {_CANARY}")
        outer.__cause__ = inner
    fake.errors["verify"] = outer

    result = invoke("-v", "verify")

    assert result.exit_code == 1
    assert _CANARY not in result.output
    assert "[redacted]" in result.output.lower()
    assert "ValueError" in result.output
    assert "Traceback" in result.output


def test_doctor_fails_on_loose_home_permissions(
    cli_home: Path, fake: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli_home.chmod(0o755)
    monkeypatch.setattr(cli.diagnose, "find_chrome", lambda config: "/fake/chrome")
    monkeypatch.setattr(cli.diagnose, "chrome_version", lambda path: "156.0.0.0")
    monkeypatch.setattr(cli.diagnose, "resolve_display", lambda config: Display.HEADLESS)

    result = invoke("doctor")

    assert result.exit_code == 1
    assert "[fail] permissions" in result.output
    assert str(cli_home) in result.output
    assert "0755" in result.output


def test_doctor_runs_live_checks_only_with_flags(
    cli_home: Path, fake: FakeBackend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.diagnose, "find_chrome", lambda config: "/fake/chrome")
    monkeypatch.setattr(cli.diagnose, "chrome_version", lambda path: "156.0.0.0")
    monkeypatch.setattr(cli.diagnose, "resolve_display", lambda config: Display.HEADLESS)

    async def fake_tells(config: XPosterConfig, store: Any, **kw: Any) -> list[Tell]:
        return [Tell("navigator.webdriver", "false", "ok", "not advertised")]

    async def fake_dom(config: XPosterConfig, store: Any, **kw: Any) -> DomReport:
        return DomReport(
            rows=(DomRow("composer_textarea_0", None, 0, True),),
            artifacts=tmp_path / "artifacts",
        )

    monkeypatch.setattr(cli.diagnose, "tells_report", fake_tells)
    monkeypatch.setattr(cli.diagnose, "dom_report", fake_dom)

    result = invoke("doctor", "--tells", "--dom")

    assert result.exit_code == 0
    assert "Offline checks:" in result.output
    assert "Browser tells (live):" in result.output
    assert "navigator.webdriver" in result.output
    assert "DOM calibration" in result.output
    assert "MISSING" in result.output
    assert fake.instances == 0


def test_doctor_dom_with_media_passes_the_opted_in_flag(
    cli_home: Path, fake: FakeBackend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.diagnose, "find_chrome", lambda config: "/fake/chrome")
    monkeypatch.setattr(cli.diagnose, "chrome_version", lambda path: "156.0.0.0")
    monkeypatch.setattr(cli.diagnose, "resolve_display", lambda config: Display.HEADLESS)
    seen: dict[str, Any] = {}

    async def fake_dom(config: XPosterConfig, store: Any, **kw: Any) -> DomReport:
        seen.update(kw)
        return DomReport(
            rows=(DomRow("composer_media_preview", None, 0, False),),
            artifacts=tmp_path / "artifacts",
        )

    monkeypatch.setattr(cli.diagnose, "dom_report", fake_dom)

    result = invoke("doctor", "--dom", "--with-media")

    assert result.exit_code == 0
    assert seen["with_media"] is True


def test_doctor_drafts_prints_leftovers_and_manual_cleanup(
    cli_home: Path, fake: FakeBackend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.diagnose, "find_chrome", lambda config: "/fake/chrome")
    monkeypatch.setattr(cli.diagnose, "chrome_version", lambda path: "156.0.0.0")
    monkeypatch.setattr(cli.diagnose, "resolve_display", lambda config: Display.HEADLESS)

    async def fake_drafts(config: XPosterConfig, store: Any, **kw: Any) -> DraftsReport:
        return DraftsReport(
            rendered=True,
            drafts=["xposter calibration two"],
            artifacts=tmp_path / "drafts-artifacts",
            panel_opened=True,
        )

    monkeypatch.setattr(cli.diagnose, "drafts_report", fake_drafts)

    result = invoke("doctor", "--drafts")

    assert result.exit_code == 0
    assert "Drafts audit" in result.output
    assert "Drafts found: 1" in result.output
    assert "LEFTOVER FROM CALIBRATION" in result.output
    assert "Open the composer and click Drafts" in result.output
    assert "Click Edit" in result.output
    assert "Delete" in result.output
    assert fake.instances == 0
