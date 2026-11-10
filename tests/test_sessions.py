"""Tests for the persistent-session lifecycle.

The browser tests launch the installed Google Chrome headless and install the
offline ``tests.fake_x`` route before the first navigation, so no test touches
the network. They are marked ``browser`` and skipped when no real Chrome is
installed. The remaining tests are pure: they cover the exception-to-parking
mapping and the href/text parsing that decides which account a page belongs to.
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from playwright.async_api import BrowserContext

import xposter.sessions as sessions
from tests.fake_x import FakeX, Scenario
from xposter.browser import find_chrome
from xposter.config import Display, KeepalivePolicy, XPosterConfig
from xposter.errors import (
    AccountMismatchError,
    AccountRestrictedError,
    ChallengeError,
    NotLoggedInError,
    ParkedError,
    RateLimitedError,
    SessionBusyError,
)
from xposter.humanize import Humanizer
from xposter.sessions import (
    ContextHook,
    LoginState,
    _browse_duration,
    _handle_from_href,
    _handle_from_switcher_text,
    _iso,
    _read_timeline,
    authenticated_page,
    challenge_visible,
    detect_login_state,
    login,
    park_on,
    refresh,
    reset_profile,
    verify,
)
from xposter.store import SessionStore

_CHROME = find_chrome(XPosterConfig())

requires_chrome = pytest.mark.skipif(
    _CHROME is None,
    reason="Google Chrome is not installed; session browser tests need a real Chrome",
)

_PROBE_COOKIE: dict[str, str] = {
    "name": "xposter_probe",
    "value": "probe-value",
    "url": "https://x.com",
}

_CHALLENGE_SCRIPT = """
document.addEventListener('DOMContentLoaded', () => {
  const dialog = document.createElement('div');
  dialog.setAttribute('role', 'dialog');
  dialog.textContent = 'Verify you are a human';
  document.body.appendChild(dialog);
});
"""


def browser_test(func: Any) -> Any:
    """Mark a test as launching real Chrome and skip it when Chrome is absent."""
    return pytest.mark.browser(requires_chrome(func))


@pytest.fixture
def config(tmp_home: XPosterConfig) -> XPosterConfig:
    """A temporary home plus the real Chrome path in headless mode."""
    assert _CHROME is not None
    return tmp_home.with_overrides(
        chrome_path=_CHROME,
        display=Display.HEADLESS,
        default_timeout_ms=15_000,
    )


def _stored_state() -> dict[str, object]:
    """A good-looking backup credential with one non-secret test cookie."""
    return {
        "cookies": [
            {
                "name": "auth_token",
                "value": "stored-cookie-value",
                "domain": ".x.com",
                "path": "/",
                "expires": 4102444800.0,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


def _hook(fake: FakeX, *, script: str | None = None) -> ContextHook:
    """Install the fake plus a cookie so a clean save has a non-empty jar."""

    async def install(context: BrowserContext) -> None:
        await context.add_cookies([dict(_PROBE_COOKIE)])
        if script is not None:
            await context.add_init_script(script)
        await fake.install(context)

    return install


class _PlainLoginStub:
    """A stand-in for the human login window; no Chrome and no human needed."""

    def __init__(self) -> None:
        self.runs = 0

    def run(self, *, on_status: Callable[[str], None] | None = None, **_kw: object) -> int:
        self.runs += 1
        if on_status is not None:
            on_status("Sign in, then quit Chrome.")
        return 0


class _BlockingPlainLogin(_PlainLoginStub):
    """A login window that stays open until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, *, on_status: Callable[[str], None] | None = None, **_kw: object) -> int:
        self.runs += 1
        self.started.set()
        assert self.release.wait(timeout=20.0)
        return 0


class _RecordingMouse:
    """Records wheel events so a timeline read can be inspected offline."""

    def __init__(self) -> None:
        self.events: list[tuple[int, int]] = []

    async def wheel(self, x: int, y: int) -> None:
        self.events.append((x, y))


class _RecordingPage:
    """The only part of ``Page`` ``_read_timeline`` touches."""

    def __init__(self) -> None:
        self.mouse = _RecordingMouse()


class _FixedRandom(random.Random):
    """A seeded RNG whose duration draw is pinned to one value."""

    def __init__(self, value: float) -> None:
        super().__init__(0)
        self.value = value

    def uniform(self, a: float, b: float) -> float:
        return self.value


# --------------------------------------------------------------------------- pure


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/olsa_test", "olsa_test"),
        ("https://x.com/olsa_test", "olsa_test"),
        ("https://www.x.com/olsa_test/status/123", "olsa_test"),
        ("/olsa_test?ref=nav", "olsa_test"),
        ("/olsa_test/", "olsa_test"),
        ("/home", None),
        ("/i/flow/login", None),
        ("/compose/post", None),
        ("/explore", None),
        ("/", None),
        ("", None),
        (None, None),
        ("https://example.com/olsa_test", None),
        ("//example.com/olsa_test", None),
        ("/handle-with-dashes", None),
        ("/this_handle_is_far_too_long", None),
    ],
)
def test_handle_from_href_parsing(href: str | None, expected: str | None) -> None:
    assert _handle_from_href(href) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@olsa_test", "olsa_test"),
        ("Account menu\n@olsa_test", "olsa_test"),
        ("Log out @olsa_test", "olsa_test"),
        ("Account", None),
        ("Account menu", None),
        ("", None),
        ("@handle-with-dashes", None),
    ],
)
def test_handle_from_switcher_text_parsing(text: str, expected: str | None) -> None:
    assert _handle_from_switcher_text(text) == expected


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (ChallengeError("captcha"), "challenge"),
        (AccountRestrictedError("locked"), "locked"),
        (RateLimitedError("slow down"), "rate_limit"),
    ],
)
def test_park_on_parks_never_retry_failures(
    tmp_home: XPosterConfig, exc: Exception, kind: str
) -> None:
    store = SessionStore(tmp_home)
    park_on(exc, store)
    info = store.parked()
    assert info is not None
    assert info.kind == kind
    assert info.reason == str(exc)


def test_park_on_ignores_retryable_failures(tmp_home: XPosterConfig) -> None:
    store = SessionStore(tmp_home)
    park_on(NotLoggedInError("expired"), store)
    park_on(RuntimeError("boom"), store)
    assert store.parked() is None


def test_park_on_keeps_the_original_marker(tmp_home: XPosterConfig) -> None:
    store = SessionStore(tmp_home)
    store.park("first cause", kind="challenge")
    before = store.parked()
    park_on(AccountRestrictedError("later cause"), store)
    assert store.parked() == before


async def test_parked_store_stops_before_the_browser(
    tmp_home: XPosterConfig, monkeypatch: Any
) -> None:
    store = SessionStore(tmp_home)
    store.park("captcha", kind="challenge")
    started: list[str] = []

    def factory() -> Any:
        started.append("playwright")
        raise AssertionError("the browser must not start while parked")

    def opener(*_args: object, **_kwargs: object) -> Any:
        started.append("open_context")
        raise AssertionError("the profile must not open while parked")

    monkeypatch.setattr(sessions, "open_context", opener)
    with pytest.raises(ParkedError):
        async with authenticated_page(tmp_home, store, playwright_factory=factory):
            pytest.fail("a parked store must not yield a page")
    assert started == []


async def test_lock_contention_raises_busy(tmp_home: XPosterConfig) -> None:
    store = SessionStore(tmp_home)
    started: list[str] = []

    def factory() -> Any:
        started.append("playwright")
        raise AssertionError("the browser must not start while the lock is held")

    with store.lock(), pytest.raises(SessionBusyError):
        async with authenticated_page(tmp_home, store, playwright_factory=factory):
            pytest.fail("a busy lock must not yield a page")
    assert started == []


async def test_login_is_refused_while_the_profile_lock_is_held(
    tmp_home: XPosterConfig,
) -> None:
    store = SessionStore(tmp_home)
    runner = _PlainLoginStub()

    with store.lock(), pytest.raises(SessionBusyError):
        await login(tmp_home, store, plain_login=runner)

    assert runner.runs == 0


async def test_refresh_not_due_does_not_start_the_browser(tmp_home: XPosterConfig) -> None:
    config = tmp_home.with_overrides(
        keepalive=KeepalivePolicy(min_interval_s=7200.0, browse_seconds=(0.0, 0.0))
    )
    store = SessionStore(config)
    store.update_meta(handle="olsa_test", last_refresh_at="2026-11-09T11:00:00Z")
    started: list[str] = []

    def factory() -> Any:
        started.append("playwright")
        raise AssertionError("a not-due refresh must not start the browser")

    result = await refresh(
        config,
        store,
        clock=lambda: datetime(2026, 11, 9, 12, 0, tzinfo=UTC),
        playwright_factory=factory,
    )
    assert result.refreshed is False
    assert result.reason == "not due"
    assert result.handle == "olsa_test"
    assert result.next_due == "2026-11-09T13:00:00Z"
    assert started == []


async def test_refresh_while_parked_raises(tmp_home: XPosterConfig) -> None:
    store = SessionStore(tmp_home)
    store.park("captcha", kind="challenge")
    started: list[str] = []

    def factory() -> Any:
        started.append("playwright")
        raise AssertionError("a parked refresh must not start the browser")

    with pytest.raises(ParkedError):
        await refresh(tmp_home, store, playwright_factory=factory)
    assert started == []


async def test_forced_refresh_is_refused_within_ten_minutes(
    tmp_home: XPosterConfig,
) -> None:
    config = tmp_home.with_overrides(
        keepalive=KeepalivePolicy(min_interval_s=86400.0, browse_seconds=(0.0, 0.0))
    )
    store = SessionStore(config)
    store.update_meta(handle="olsa_test", last_refresh_at="2026-11-09T11:55:00Z")
    started: list[str] = []

    def factory() -> Any:
        started.append("playwright")
        raise AssertionError("a too-soon forced refresh must not start the browser")

    result = await refresh(
        config,
        store,
        force=True,
        clock=lambda: datetime(2026, 11, 9, 12, 0, tzinfo=UTC),
        playwright_factory=factory,
    )

    assert result.refreshed is False
    assert result.reason == ("forced refresh too soon: min 10 minutes between forced refreshes")
    assert result.handle == "olsa_test"
    assert result.next_due == "2026-11-09T12:05:00Z"
    assert started == []


def test_reset_profile_wipes_corrupt_profile_and_keeps_backup(
    tmp_home: XPosterConfig,
) -> None:
    store = SessionStore(tmp_home)
    store.ensure_dirs()
    (tmp_home.profile_dir / "Corrupt").write_text("not a Chrome profile", encoding="utf-8")
    store.save_state(_stored_state())
    saved = tmp_home.storage_state_path.read_bytes()
    store.park("challenge", kind="challenge")
    ledger_text = '{"entries": []}\n'
    tmp_home.ledger_path.write_text(ledger_text, encoding="utf-8")

    reset_profile(tmp_home, store)

    assert list(tmp_home.profile_dir.iterdir()) == []
    assert not tmp_home.storage_state_path.exists()
    assert not tmp_home.meta_path.exists()
    pre_reset = tmp_home.storage_state_path.with_name("storage_state.json.pre-reset")
    assert pre_reset.read_bytes() == saved
    assert pre_reset.stat().st_mode & 0o777 == 0o600
    assert store.parked() is not None
    assert tmp_home.ledger_path.read_text(encoding="utf-8") == ledger_text


def test_reset_profile_without_backup_deletes_the_state(tmp_home: XPosterConfig) -> None:
    store = SessionStore(tmp_home)
    store.ensure_dirs()
    (tmp_home.profile_dir / "Corrupt").write_text("junk", encoding="utf-8")
    store.save_state(_stored_state())

    reset_profile(tmp_home, store, keep_backup=False)

    assert list(tmp_home.profile_dir.iterdir()) == []
    assert not tmp_home.storage_state_path.exists()
    assert not tmp_home.storage_state_path.with_name("storage_state.json.pre-reset").exists()


def test_reset_profile_refuses_while_the_profile_lock_is_held(
    tmp_home: XPosterConfig,
) -> None:
    store = SessionStore(tmp_home)
    store.ensure_dirs()
    corrupt = tmp_home.profile_dir / "Corrupt"
    corrupt.write_text("junk", encoding="utf-8")

    with store.lock(), pytest.raises(SessionBusyError):
        reset_profile(tmp_home, store)

    assert corrupt.exists()


def test_browse_duration_is_deterministic_for_a_seeded_rng(
    tmp_home: XPosterConfig,
) -> None:
    policy = KeepalivePolicy(min_interval_s=60.0, browse_seconds=(1.0, 5.0))
    humanizer = Humanizer(tmp_home.human)

    first = _browse_duration(humanizer, random.Random(7), policy)
    second = _browse_duration(humanizer, random.Random(7), policy)

    assert first == second
    assert 1.0 <= first <= 5.0


def test_browse_duration_defaults_to_the_humanizer_rng(tmp_home: XPosterConfig) -> None:
    policy = KeepalivePolicy(min_interval_s=60.0, browse_seconds=(1.0, 5.0))
    humanizer = Humanizer(tmp_home.human, rng=random.Random(11))

    expected = random.Random(11).uniform(1.0, 5.0)

    assert _browse_duration(humanizer, None, policy) == expected


async def test_read_timeline_uses_the_injected_rng(tmp_home: XPosterConfig) -> None:
    policy = KeepalivePolicy(min_interval_s=60.0, browse_seconds=(0.0, 60.0))
    humanizer = Humanizer(tmp_home.human)

    zero = _RecordingPage()
    await _read_timeline(zero, humanizer, policy, _FixedRandom(0.0))  # type: ignore[arg-type]
    assert zero.mouse.events == []

    positive = _RecordingPage()
    await _read_timeline(positive, humanizer, policy, _FixedRandom(0.05))  # type: ignore[arg-type]
    assert positive.mouse.events


# ----------------------------------------------------------------------- browser


@browser_test
@pytest.mark.parametrize("dom", ["new", "legacy"])
async def test_logged_in_reads_handle_and_saves_state(config: XPosterConfig, dom: str) -> None:
    store = SessionStore(config)
    fake = FakeX(handle="olsa_test", dom=dom, scenario=Scenario.OK)

    async with authenticated_page(config, store, context_hook=_hook(fake)) as authed:
        assert authed.handle == "olsa_test"
        assert authed.display is Display.HEADLESS
        assert authed.store is store
        assert authed.page.url.rstrip("/").endswith("/home")
        assert await challenge_visible(authed.page) is False

    state = store.load_state()
    assert state is not None
    cookies = state["cookies"]
    assert isinstance(cookies, list)
    assert any(cookie.get("name") == _PROBE_COOKIE["name"] for cookie in cookies)
    meta = store.read_meta()
    assert meta["handle"] == "olsa_test"
    assert isinstance(meta["last_verified_at"], str)


@browser_test
async def test_logged_out_raises_and_keeps_the_backup(config: XPosterConfig) -> None:
    store = SessionStore(config)
    store.save_state(_stored_state())
    before = config.storage_state_path.read_bytes()
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.LOGGED_OUT)

    with pytest.raises(NotLoggedInError, match="xposter login"):
        async with authenticated_page(config, store, context_hook=_hook(fake)):
            pytest.fail("a logged-out page must not yield")

    assert config.storage_state_path.read_bytes() == before


@browser_test
async def test_locked_page_parks_the_account(config: XPosterConfig) -> None:
    store = SessionStore(config)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.LOCKED)

    with pytest.raises(AccountRestrictedError):
        async with authenticated_page(config, store, context_hook=_hook(fake)):
            pytest.fail("a locked page must not yield")

    info = store.parked()
    assert info is not None
    assert info.kind == "locked"


@browser_test
async def test_suspended_page_parks_the_account(config: XPosterConfig) -> None:
    store = SessionStore(config)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.SUSPENDED)

    with pytest.raises(AccountRestrictedError):
        async with authenticated_page(config, store, context_hook=_hook(fake)):
            pytest.fail("a suspended page must not yield")

    assert store.parked() is not None


@browser_test
async def test_challenge_dialog_parks_before_the_logged_in_check(
    config: XPosterConfig,
) -> None:
    store = SessionStore(config)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)

    with pytest.raises(ChallengeError):
        async with authenticated_page(
            config, store, context_hook=_hook(fake, script=_CHALLENGE_SCRIPT)
        ):
            pytest.fail("a challenge over a signed-in page must not yield")

    info = store.parked()
    assert info is not None
    assert info.kind == "challenge"


@browser_test
async def test_challenge_visible_finds_the_selector_overlay(config: XPosterConfig) -> None:
    store = SessionStore(config)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)

    async with authenticated_page(
        config, store, check_login=False, context_hook=_hook(fake, script=_CHALLENGE_SCRIPT)
    ) as authed:
        assert await challenge_visible(authed.page) is True


@browser_test
async def test_handle_mismatch_raises_and_does_not_park(config: XPosterConfig) -> None:
    config = config.with_overrides(handle="someone_else")
    store = SessionStore(config)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)

    with pytest.raises(AccountMismatchError, match=r"@olsa_test.*@someone_else"):
        async with authenticated_page(config, store, context_hook=_hook(fake)):
            pytest.fail("the wrong account must not yield")

    assert store.parked() is None
    assert not config.storage_state_path.exists()


@browser_test
async def test_handle_comparison_is_case_insensitive(config: XPosterConfig) -> None:
    config = config.with_overrides(handle="OLSA_TEST")
    store = SessionStore(config)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)

    async with authenticated_page(config, store, context_hook=_hook(fake)) as authed:
        assert authed.handle == "olsa_test"


@browser_test
async def test_verify_returns_cookie_free_info(config: XPosterConfig) -> None:
    store = SessionStore(config)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)

    info = await verify(config, store, context_hook=_hook(fake))

    assert info.handle == "olsa_test"
    assert info.display == Display.HEADLESS.value
    assert info.chrome_version is not None
    assert info.verified_at.endswith("Z")
    assert _PROBE_COOKIE["value"] not in repr(info)


@browser_test
async def test_empty_profile_restores_the_backup_cookies(config: XPosterConfig) -> None:
    store = SessionStore(config)
    store.save_state(_stored_state())
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)
    restored: list[bool] = []

    async def hook(context: BrowserContext) -> None:
        cookies = await context.cookies("https://x.com")
        restored.append(any(cookie["name"] == "auth_token" for cookie in cookies))
        await context.add_cookies([dict(_PROBE_COOKIE)])
        await fake.install(context)

    async with authenticated_page(config, store, context_hook=hook) as authed:
        assert authed.handle == "olsa_test"

    assert restored == [True]


@browser_test
async def test_authenticated_page_restores_the_kept_backup_after_a_reset(
    config: XPosterConfig,
) -> None:
    store = SessionStore(config)
    store.ensure_dirs()
    (config.profile_dir / "Corrupt").write_text("not a Chrome profile", encoding="utf-8")
    store.save_state(_stored_state())
    reset_profile(config, store)

    kept = config.storage_state_path.with_name("storage_state.json.pre-reset")
    os.replace(kept, config.storage_state_path)
    os.chmod(config.storage_state_path, 0o600)

    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)
    restored: list[bool] = []

    async def hook(context: BrowserContext) -> None:
        cookies = await context.cookies("https://x.com")
        restored.append(any(cookie["name"] == "auth_token" for cookie in cookies))
        await context.add_cookies([dict(_PROBE_COOKIE)])
        await fake.install(context)

    async with authenticated_page(config, store, context_hook=hook) as authed:
        assert authed.handle == "olsa_test"

    assert restored == [True]


@browser_test
async def test_a_failing_body_leaves_the_old_backup_untouched(
    config: XPosterConfig,
) -> None:
    store = SessionStore(config)
    store.save_state(_stored_state())
    before = config.storage_state_path.read_bytes()
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)

    with pytest.raises(RuntimeError, match="boom"):
        async with authenticated_page(config, store, context_hook=_hook(fake)):
            raise RuntimeError("boom")

    assert config.storage_state_path.read_bytes() == before
    assert "last_verified_at" not in store.read_meta()


@browser_test
async def test_a_challenge_raised_by_the_body_parks(config: XPosterConfig) -> None:
    store = SessionStore(config)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)

    with pytest.raises(ChallengeError, match="captcha appeared"):
        async with authenticated_page(config, store, context_hook=_hook(fake)):
            raise ChallengeError("captcha appeared while posting")

    info = store.parked()
    assert info is not None
    assert info.kind == "challenge"
    assert info.reason == "captcha appeared while posting"
    assert not config.storage_state_path.exists()


@browser_test
async def test_refresh_due_visits_home_and_stamps_meta(config: XPosterConfig) -> None:
    config = config.with_overrides(
        keepalive=KeepalivePolicy(min_interval_s=3600.0, browse_seconds=(0.0, 0.0))
    )
    store = SessionStore(config)
    stale = _iso(datetime.now(UTC) - timedelta(hours=2))
    store.update_meta(handle="olsa_test", last_refresh_at=stale)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)

    result = await refresh(config, store, context_hook=_hook(fake))

    assert result.refreshed is True
    assert result.reason == "due"
    assert result.handle == "olsa_test"
    assert result.next_due is not None
    assert any(url.rstrip("/").endswith("/home") for url in fake.visits)
    assert store.read_meta()["last_refresh_at"] != stale
    assert store.load_state() is not None


@browser_test
async def test_refresh_force_runs_after_the_ten_minute_floor(config: XPosterConfig) -> None:
    config = config.with_overrides(
        keepalive=KeepalivePolicy(min_interval_s=86400.0, browse_seconds=(0.0, 0.0))
    )
    store = SessionStore(config)
    last = _iso(datetime.now(UTC) - timedelta(minutes=15))
    store.update_meta(handle="olsa_test", last_refresh_at=last)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)

    result = await refresh(config, store, force=True, context_hook=_hook(fake))

    assert result.refreshed is True
    assert result.reason == "forced"
    assert result.handle == "olsa_test"
    assert store.read_meta()["last_refresh_at"] != last


@browser_test
async def test_a_second_login_is_refused_while_the_plain_window_is_open(
    config: XPosterConfig,
) -> None:
    store = SessionStore(config)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)
    first = _BlockingPlainLogin()
    second = _PlainLoginStub()

    task = asyncio.create_task(login(config, store, plain_login=first, context_hook=_hook(fake)))
    started = await asyncio.to_thread(first.started.wait, 20.0)
    assert started, "the first login never opened its plain window"

    with pytest.raises(SessionBusyError):
        await login(config, store, plain_login=second)
    assert second.runs == 0

    first.release.set()
    info = await task
    assert info.handle == "olsa_test"


@browser_test
async def test_login_runs_the_plain_window_then_verifies(config: XPosterConfig) -> None:
    store = SessionStore(config)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)
    runner = _PlainLoginStub()
    statuses: list[str] = []

    info = await login(
        config,
        store,
        on_status=statuses.append,
        plain_login=runner,
        context_hook=_hook(fake),
    )

    assert runner.runs == 1
    assert statuses == ["Sign in, then quit Chrome."]
    assert info.handle == "olsa_test"
    assert info.display == Display.HEADLESS.value
    assert info.verified_at.endswith("Z")
    meta = store.read_meta()
    assert meta["handle"] == "olsa_test"
    assert isinstance(meta["logged_in_at"], str)
    assert isinstance(meta["last_verified_at"], str)


@browser_test
async def test_login_that_ends_logged_out_mentions_the_closed_window(
    config: XPosterConfig,
) -> None:
    store = SessionStore(config)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.LOGGED_OUT)

    with pytest.raises(NotLoggedInError, match="window was closed before sign-in finished"):
        await login(config, store, plain_login=_PlainLoginStub(), context_hook=_hook(fake))

    assert not config.storage_state_path.exists()


@browser_test
async def test_detect_login_state_on_a_closed_page_is_unknown(config: XPosterConfig) -> None:
    store = SessionStore(config)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)

    async with authenticated_page(config, store, context_hook=_hook(fake)) as authed:
        await authed.page.close()
        assert await detect_login_state(authed.page) is LoginState.UNKNOWN
        assert await challenge_visible(authed.page) is False


@browser_test
async def test_lock_is_held_for_the_whole_session(config: XPosterConfig) -> None:
    store = SessionStore(config)
    fake = FakeX(handle="olsa_test", dom="new", scenario=Scenario.OK)

    async with authenticated_page(config, store, context_hook=_hook(fake)):
        with pytest.raises(SessionBusyError), store.lock():
            pytest.fail("the session lock must be held for the whole run")

    with store.lock():
        pass
