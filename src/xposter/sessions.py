"""Persistent-session lifecycle: verification, parking and keepalive refreshes.

The persistent Chrome profile is the real credential. Everything here protects
it: the store lock is held for the whole browser session (two Chrome processes
on one ``user-data-dir`` corrupt it), the profile is only written back after a
run that was verified healthy and ended cleanly, and a challenge, lock or rate
limit parks automation immediately instead of retrying.

Two rules are enforced on every path. A cookie value never appears in a log
line, an exception or :class:`SessionInfo`; the only things that may leave this
module are handles, timestamps, booleans and error kinds. And verification is
never skipped silently: a posting run that cannot prove which account it is on
gets an exception, because posting as the wrong account is unrecoverable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, NoReturn, Protocol, cast
from urllib.parse import urlsplit

from playwright._impl._api_structures import SetCookieParam
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from . import guard, selectors
from .browser import (
    PlainLogin,
    chrome_version,
    find_chrome,
    new_page,
    open_context,
    resolve_display,
)
from .config import Display, KeepalivePolicy, XPosterConfig
from .errors import (
    AccountMismatchError,
    AccountRestrictedError,
    BrowserUnavailableError,
    ChallengeError,
    NotLoggedInError,
    RateLimitedError,
    SessionError,
    UiChangedError,
)
from .humanize import Humanizer
from .store import SessionStore

__all__ = [
    "AuthedPage",
    "ContextHook",
    "LoginState",
    "RefreshResult",
    "SessionInfo",
    "authenticated_page",
    "challenge_visible",
    "detect_login_state",
    "login",
    "park_on",
    "read_handle",
    "refresh",
    "reset_profile",
    "utcnow",
    "verify",
]

_logger = logging.getLogger(__name__)

# Detection is a read-only probe on an already loaded page, so it must not add
# seconds to every run: 750 ms is enough for a selector to appear, and anything
# slower is indistinguishable from a changed UI.
_PROBE_TIMEOUT_MS = 750
# ``--force`` bypasses the normal keepalive interval but not this floor: a cron
# that passes the flag every minute must not turn a keepalive into polling.
_FORCED_REFRESH_FLOOR_S = 600.0
_FORCED_REFRESH_TOO_SOON = "forced refresh too soon: min 10 minutes between forced refreshes"
_NAV_ANCHOR_SELECTORS = ("nav a[href]", '[role="navigation"] a[href]')
_COOKIE_KEYS = (
    "name",
    "value",
    "url",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
    "partitionKey",
)

# Hrefs shaped like "/<segment>" are shared by profiles and app routes, so the
# routes are rejected explicitly before a segment can be mistaken for a handle.
_RESERVED_ROUTES = frozenset(
    {
        "about",
        "account",
        "bookmarks",
        "communities",
        "compose",
        "connect_people",
        "download",
        "explore",
        "followers",
        "following",
        "grok",
        "hashtag",
        "help",
        "home",
        "i",
        "intent",
        "jobs",
        "lists",
        "login",
        "logout",
        "messages",
        "notifications",
        "premium",
        "premium_sign_up",
        "privacy",
        "rules",
        "search",
        "settings",
        "share",
        "signup",
        "spaces",
        "status",
        "tos",
        "tweet",
        "verified",
        "verified-choose",
    }
)
_X_HOSTS = frozenset(
    {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
)
_HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,15}")
_HANDLE_IN_TEXT_RE = re.compile(r"(?<![\w-])@([A-Za-z0-9_]{1,15})(?![\w-])")

_DEFAULT_PARK_REASONS = {
    "challenge": "X is showing a verification challenge; automation is parked.",
    "locked": "X is showing a locked or restricted account notice; automation is parked.",
    "rate_limit": "X is limiting this session; automation is parked.",
}


class _PlaywrightManager(Protocol):
    """The two methods :func:`authenticated_page` needs from ``async_playwright``.

    A protocol keeps the launch seam injectable without dragging in Playwright's
    concrete context-manager class, which is not part of the public API.
    """

    async def __aenter__(self) -> Playwright: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...


ContextHook = Callable[[BrowserContext], Awaitable[None]]
"""Test seam: installed on the context before any navigation happens."""


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Cookie-free description of a verified session, safe to log or display.

    Only the handle, timestamps and the browser identity are exposed; cookie
    values never enter this object.
    """

    handle: str
    verified_at: str
    chrome_version: str | None
    auth_expires_at: str | None
    display: str


@dataclass(frozen=True, slots=True)
class AuthedPage:
    """Everything a step needs while the profile lock is held."""

    context: BrowserContext
    page: Page
    handle: str
    display: Display
    store: SessionStore


class LoginState(StrEnum):
    """What the page currently proves about the saved session."""

    LOGGED_IN = "logged_in"
    LOGGED_OUT = "logged_out"
    LOCKED = "locked"
    CHALLENGE = "challenge"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Outcome of a keepalive check, including why it did or did not run."""

    refreshed: bool
    reason: str
    handle: str | None
    next_due: str | None


def utcnow() -> datetime:
    """Current UTC time; tests inject a fixed clock to pin keepalive cut-offs."""
    return datetime.now(UTC)


async def detect_login_state(page: Page) -> LoginState:
    """Classify what ``page`` shows about the login state, without raising.

    The URL is checked first because a redirect to a login or access route is
    the most reliable signal. The challenge selectors are checked before the
    logged-in indicator on purpose: a captcha can overlay an otherwise signed-in
    page, and proceeding in that state is exactly what must never happen. A
    closed or crashed page is reported as :attr:`LoginState.UNKNOWN` so callers
    can make the safe decision instead of crashing on a teardown race.
    """
    try:
        state, _ = await _probe_login(page)
    except Exception:
        _logger.debug("could not determine the login state", exc_info=True)
        return LoginState.UNKNOWN
    return state


async def read_handle(page: Page) -> str | None:
    """Read the signed-in handle from the navigation, or ``None`` if hidden.

    The profile link carries the handle in its href on both X clients; the
    account switcher text is the fallback because older markup renders
    ``@handle`` there even when the profile link is not a plain anchor.
    """
    handle = await _profile_handle(page)
    if handle is not None:
        return handle
    return await _switcher_handle(page)


def park_on(exc: BaseException, store: SessionStore) -> None:
    """Park automation when ``exc`` is one of the never-retry failures.

    The posting step calls this from its own failure paths, so the mapping from
    exception class to parked ``kind`` lives here next to detection. Everything
    else is a no-op: retryable and programming errors must not park an account
    that is perfectly healthy. An existing marker is never rewritten, which
    keeps the original reason and ``since`` for the operator.
    """
    kind = _park_kind(exc)
    if kind is None or store.parked() is not None:
        return
    reason = str(exc).strip() or _DEFAULT_PARK_REASONS[kind]
    store.park(reason, kind=kind)


@asynccontextmanager
async def authenticated_page(
    config: XPosterConfig,
    store: SessionStore,
    *,
    playwright_factory: Callable[[], _PlaywrightManager] = async_playwright,
    display: Display | None = None,
    context_hook: ContextHook | None = None,
    check_login: bool = True,
    goto: str = "/home",
) -> AsyncIterator[AuthedPage]:
    """Open the persistent profile under the lock and yield a verified page.

    Ordering matters. The parked check happens before the lock because a parked
    account must not even start Chrome; the hook runs before navigation so the
    offline fake can install its routes on a pristine context; and the backup
    login is only restored when the profile is empty, because restoring cookies
    into a live profile could overwrite newer session state.
    """
    store.raise_if_parked()
    with store.lock():
        resolved = display if display is not None else resolve_display(config)
        restore_backup = _profile_is_empty(config.profile_dir)
        async with (
            playwright_factory() as playwright,
            open_context(playwright, config, display=resolved) as context,
        ):
            if restore_backup:
                await _restore_cookies(context, store)
            if context_hook is not None:
                await context_hook(context)

            page = await new_page(context)
            await _goto(page, config, goto)

            handle = ""
            if check_login:
                handle = await _require_healthy_login(page, config, store)

            authed = AuthedPage(
                context=context,
                page=page,
                handle=handle,
                display=resolved,
                store=store,
            )
            try:
                yield authed
            except BaseException as exc:
                park_on(exc, store)
                raise
            else:
                if check_login:
                    await _remember_success(context, store, handle)


async def verify(config: XPosterConfig, store: SessionStore, **kw: Any) -> SessionInfo:
    """Open a session, prove it is healthy and describe it without cookies.

    Keyword arguments are forwarded to :func:`authenticated_page`, which is how
    tests inject an offline context and a fixed display.
    """
    async with authenticated_page(config, store, **kw) as authed:
        handle = authed.handle
        display = authed.display

    chrome_path = find_chrome(config)
    version = chrome_version(chrome_path) if chrome_path is not None else None
    summary = store.summarize()
    return SessionInfo(
        handle=handle,
        verified_at=_iso(utcnow()),
        chrome_version=version,
        auth_expires_at=summary.auth_expires_at,
        display=display.value,
    )


async def login(
    config: XPosterConfig,
    store: SessionStore,
    *,
    on_status: Callable[[str], None] | None = None,
    plain_login: PlainLogin | None = None,
    **kw: Any,
) -> SessionInfo:
    """Run the one-time human sign-in, then verify what it produced.

    The human window is a plain Chrome process in a worker thread, never a
    Playwright browser: login is the most scrutinised moment and must not look
    automated. The store lock is held for the whole life of that window, not
    only for the verification that follows: otherwise a simultaneous login or
    a posting run that starts at the same moment could open the same profile
    before Chrome writes its ``SingletonLock``, and two Chromes on one
    ``user-data-dir`` corrupt it. The lock is released before ``verify``, which
    takes it itself.
    """
    store.raise_if_parked()
    runner = plain_login if plain_login is not None else PlainLogin(config)
    with store.lock():
        await asyncio.to_thread(runner.run, on_status=on_status)
    try:
        info = await verify(config, store, **kw)
    except NotLoggedInError as exc:
        raise NotLoggedInError(
            f"{exc} The login window was closed before sign-in finished; run "
            "`xposter login` again and quit Chrome only after X shows your home timeline."
        ) from exc
    store.update_meta(logged_in_at=_iso(utcnow()), handle=info.handle)
    return info


def reset_profile(config: XPosterConfig, store: SessionStore, *, keep_backup: bool = True) -> None:
    """Repair a corrupt Chrome profile, optionally keeping the login backup.

    A crash or a concurrent run can leave a profile Chrome refuses to launch.
    :meth:`SessionStore.wipe_session` already knows how to clear it; this
    function adds the one thing an operator needs before that: a private copy
    of the last credential at ``storage_state.json.pre-reset``, so the profile
    can be reset without forcing a new human login. Restoring that copy is a
    deliberate manual action, never automatic.

    The store lock is held for the whole operation: resetting a profile while a
    posting run is using it would delete the ``user-data-dir`` under a live
    Chrome. Parking, the ledger and artifacts are untouched because they explain
    the failure and record what was already posted.
    """
    with store.lock():
        if keep_backup:
            _copy_private(
                config.storage_state_path,
                config.storage_state_path.with_name("storage_state.json.pre-reset"),
            )
        store.wipe_session()


async def refresh(
    config: XPosterConfig,
    store: SessionStore,
    *,
    force: bool = False,
    clock: Callable[[], datetime] = utcnow,
    humanizer: Humanizer | None = None,
    rng: random.Random | None = None,
    **kw: Any,
) -> RefreshResult:
    """Keep an idle session warm with one read-only visit, when it is due.

    Activity is limited to loading ``/home`` and scrolling the timeline: likes,
    follows, reposts and posts are separately prohibited and would create writes
    the operator never asked for. The decision to run is made before a browser
    starts, so a not-due call is free.

    Scheduled timers must call this **without** ``force``: normal calls honour
    ``keepalive.min_interval_s`` (12 hours by default). ``force`` exists for an
    operator who wants a manual check now and is still floored at ten minutes,
    so a well-meaning cron that passes the flag cannot turn the keepalive into
    polling. ``rng`` makes the browse duration reproducible in tests; production
    callers leave it unset and the humanizer's own RNG is used.
    """
    store.raise_if_parked()
    meta = store.read_meta()
    now = _as_utc(clock())
    last = _parse_timestamp(meta.get("last_refresh_at"))
    interval = config.keepalive.min_interval_s
    age = (now - last).total_seconds() if last is not None else None

    if force:
        if age is not None and age < _FORCED_REFRESH_FLOOR_S and last is not None:
            return RefreshResult(
                refreshed=False,
                reason=_FORCED_REFRESH_TOO_SOON,
                handle=_meta_handle(meta),
                next_due=_iso(last + timedelta(seconds=_FORCED_REFRESH_FLOOR_S)),
            )
        due = True
    else:
        due = last is None or (age is not None and age >= interval)

    if not due:
        return RefreshResult(
            refreshed=False,
            reason="not due",
            handle=_meta_handle(meta),
            next_due=_iso(last + timedelta(seconds=interval)) if last is not None else None,
        )

    reader = humanizer if humanizer is not None else Humanizer(config.human)
    async with authenticated_page(config, store, check_login=True, goto="/home", **kw) as authed:
        await _read_timeline(authed.page, reader, config.keepalive, rng)
        handle = authed.handle

    finished = _as_utc(clock())
    store.update_meta(last_refresh_at=_iso(finished))
    return RefreshResult(
        refreshed=True,
        reason="forced" if force else "due",
        handle=handle,
        next_due=_iso(finished + timedelta(seconds=interval)),
    )


async def _probe_login(page: Page) -> tuple[LoginState, guard.Verdict | None]:
    """Return the login state plus the guard verdict that proved it, if any."""
    verdict = guard.classify_url(page.url)
    state = _login_state(verdict)
    if state is not None:
        return state, verdict

    if await challenge_visible(page):
        return LoginState.CHALLENGE, None
    if await selectors.is_present(page, "logged_in_indicator", timeout_ms=_PROBE_TIMEOUT_MS):
        return LoginState.LOGGED_IN, None
    if await selectors.is_present(page, "logged_out_indicator", timeout_ms=_PROBE_TIMEOUT_MS):
        return LoginState.LOGGED_OUT, None

    text = await page.inner_text("body", timeout=_PROBE_TIMEOUT_MS)
    verdict = guard.classify_text(text)
    state = _login_state(verdict)
    if state is not None:
        return state, verdict
    return LoginState.UNKNOWN, None


async def challenge_visible(page: Page) -> bool:
    """True when a human-verification dialog or iframe is on the page now.

    This is the selector half of :func:`detect_login_state`, exposed so posting
    loops can apply the exact same probe while waiting for a response. A
    challenge whose text lives only inside the Arkose iframe, or that has no
    text in the top document, must park the run just like a visible dialog.

    The probe is a snapshot of the loaded page (``locate_all`` counts, no
    waiting beyond a few hundred milliseconds) rather than a poll: callers that
    need to wait already do so in their own loop. A closed page returns
    ``False`` instead of raising, so a teardown race cannot mask the real error
    that is being handled.
    """
    try:
        for name in ("challenge_dialog", "challenge_iframe"):
            for locator in await selectors.locate_all(page, name):
                try:
                    if await locator.is_visible():
                        return True
                except PlaywrightError:
                    continue
    except Exception:
        return False
    return False


def _login_state(verdict: guard.Verdict | None) -> LoginState | None:
    """Map a guard verdict to a page-level login state.

    Going through :func:`guard.to_exception` keeps the class-to-state mapping in
    one place: the same exceptions that stop a run also decide what the status
    command reports.
    """
    if verdict is None:
        return None
    exc = guard.to_exception(verdict)
    if isinstance(exc, NotLoggedInError):
        return LoginState.LOGGED_OUT
    if isinstance(exc, ChallengeError):
        return LoginState.CHALLENGE
    if isinstance(exc, (AccountRestrictedError, RateLimitedError)):
        return LoginState.LOCKED
    return None


async def _require_healthy_login(page: Page, config: XPosterConfig, store: SessionStore) -> str:
    """Prove the page is signed in as the expected account and return its handle."""
    state, verdict = await _probe_login(page)
    if state is LoginState.LOGGED_OUT:
        raise NotLoggedInError(
            "X redirected to the login flow, so the saved session is gone; run `xposter login`."
        )
    if state in (LoginState.LOCKED, LoginState.CHALLENGE):
        _raise_parked(state, verdict, store)
    if state is LoginState.UNKNOWN:
        raise SessionError(
            "Could not confirm that X is signed in (the page shows no recognisable login "
            "state); run `xposter login` to sign in again."
        )

    handle = await read_handle(page)
    if handle is None:
        raise UiChangedError(
            "X is signed in but xposter could not read the account handle; the navigation "
            "markup probably changed. Update the selectors before posting."
        )
    if config.handle and handle.lower() != config.handle.lower():
        raise AccountMismatchError(
            f"X is signed in as @{handle}, but the configured handle is @{config.handle}; "
            "sign in with the expected account or change XPOSTER_HANDLE."
        )
    return handle


def _raise_parked(
    state: LoginState, verdict: guard.Verdict | None, store: SessionStore
) -> NoReturn:
    """Park the store and raise the guard exception matching the page state."""
    exc = guard.to_exception(verdict) if verdict is not None else None
    if exc is None or _park_kind(exc) is None:
        if state is LoginState.CHALLENGE:
            exc = ChallengeError(_DEFAULT_PARK_REASONS["challenge"])
        else:
            exc = AccountRestrictedError(_DEFAULT_PARK_REASONS["locked"])
    park_on(exc, store)
    raise exc


async def _goto(page: Page, config: XPosterConfig, goto: str) -> None:
    target = f"{config.base_url.rstrip('/')}/{goto.lstrip('/')}"
    try:
        await page.goto(target, wait_until="domcontentloaded")
    except PlaywrightError as exc:
        raise BrowserUnavailableError(
            f"Could not open {target}: {exc}. Check the network connection and that no "
            "other Chrome window is using the xposter profile."
        ) from exc


async def _remember_success(context: BrowserContext, store: SessionStore, handle: str) -> None:
    """Refresh the backup credential and metadata after a clean, healthy run.

    Failing to capture the state must not turn a successful run into an error:
    the persistent profile still holds the same cookies and the previous backup
    is left untouched. The log line names the failure type only, never content.
    """
    try:
        state = await context.storage_state()
    except PlaywrightError as exc:
        _logger.warning("could not capture the session state: %s", type(exc).__name__)
        return
    try:
        store.save_state(cast("Mapping[str, Any]", state))
    except (OSError, ValueError) as exc:
        _logger.warning("could not refresh the session backup: %s", exc)
    try:
        store.update_meta(handle=handle, last_verified_at=_iso(utcnow()))
    except OSError as exc:
        _logger.warning("could not update the session metadata: %s", exc)


async def _restore_cookies(context: BrowserContext, store: SessionStore) -> None:
    """Restore the backup cookie jar into an empty profile.

    A missing profile is what a first run after a wiped or moved home looks
    like. The backup still proves the login, so its cookies are loaded before
    the first navigation; only known cookie fields are forwarded.
    """
    state = store.load_state()
    if state is None:
        return
    cookies = _plausible_cookies(state)
    if not cookies:
        return
    try:
        await context.add_cookies(cast("list[SetCookieParam]", cookies))
    except (PlaywrightError, TypeError, ValueError) as exc:
        _logger.warning("could not restore the session backup: %s", type(exc).__name__)


def _plausible_cookies(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("cookies")
    if not isinstance(raw, list):
        return []
    cookies: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        if not isinstance(item.get("name"), str) or not isinstance(item.get("value"), str):
            continue
        if "url" not in item and "domain" not in item:
            continue
        cookies.append({key: item[key] for key in _COOKIE_KEYS if key in item})
    return cookies


def _copy_private(source: Path, target: Path) -> None:
    """Atomically copy a credential with mode 0600; a missing source is a no-op.

    The pre-reset copy holds the same account credential as the original, so it
    is written through a same-directory temporary file and ``os.replace``; a
    crash can never leave a truncated copy for an operator to restore.
    """
    try:
        payload = source.read_bytes()
    except FileNotFoundError:
        return
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


async def _profile_handle(page: Page) -> str | None:
    """Read the profile link, then fall back to links scoped to the navigation.

    ``nav_profile_link`` is parameterised by the handle in the selector table,
    so the offline-free lookup uses ``locate_all`` (which never waits) and then
    scans anchors inside ``<nav>``. The nav scope is deliberate: timeline
    anchors also point at ``/<handle>`` but to *other* accounts, and reading a
    stranger's handle as our own would be worse than not reading one at all.
    """
    try:
        links = await selectors.locate_all(page, "nav_profile_link")
    except UiChangedError:
        links = []
    for link in links:
        try:
            href = await link.get_attribute("href")
        except PlaywrightError:
            continue
        handle = _handle_from_href(href)
        if handle is not None:
            return handle
    return await _nav_handle(page)


async def _nav_handle(page: Page) -> str | None:
    for selector in _NAV_ANCHOR_SELECTORS:
        try:
            anchors = await page.locator(selector).all()
        except PlaywrightError:
            continue
        for anchor in anchors:
            try:
                text = await anchor.inner_text()
            except PlaywrightError:
                text = ""
            handle = _handle_from_switcher_text(text)
            if handle is not None:
                return handle
            try:
                href = await anchor.get_attribute("href")
            except PlaywrightError:
                continue
            handle = _handle_from_href(href)
            if handle is not None:
                return handle
    return None


async def _switcher_handle(page: Page) -> str | None:
    try:
        switcher = await selectors.locate(
            page, "account_switcher_button", timeout_ms=_PROBE_TIMEOUT_MS
        )
    except (UiChangedError, PlaywrightError):
        return None
    try:
        text = await switcher.first.inner_text()
    except PlaywrightError:
        text = ""
    if not text:
        try:
            text = await switcher.first.get_attribute("aria-label") or ""
        except PlaywrightError:
            text = ""
    return _handle_from_switcher_text(text)


def _handle_from_href(href: str | None) -> str | None:
    """Extract a handle from a profile href such as ``/olsa_test``.

    Reserved app routes share the ``/<segment>`` shape, so they are rejected by
    name; anything that is not a legal X handle is rejected by pattern. Absolute
    links are accepted only on X's own hosts, so a link to another site can
    never be mistaken for the account.
    """
    if not href:
        return None
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        host = parsed.netloc.rsplit("@", 1)[-1].split(":", 1)[0].lower()
        if host not in _X_HOSTS:
            return None
        path = parsed.path
    else:
        path = href.split("?", 1)[0].split("#", 1)[0]

    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return None
    segment = segments[0].lstrip("@")
    if not segment or segment.lower() in _RESERVED_ROUTES:
        return None
    if _HANDLE_RE.fullmatch(segment) is None:
        return None
    return segment


def _handle_from_switcher_text(text: str) -> str | None:
    match = _HANDLE_IN_TEXT_RE.search(text)
    return match.group(1) if match is not None else None


def _browse_duration(
    humanizer: Humanizer, rng: random.Random | None, policy: KeepalivePolicy
) -> float:
    """Draw the keepalive duration from the injected or humanizer RNG.

    The humanizer owns the seeded RNG for the whole session, so its stream is
    the right source when no explicit one is passed; ``random.Random()`` is only
    the last resort for a humanizer-like object without that attribute. Using
    the module-level RNG would make two runs share global state and tests
    impossible to pin.
    """
    source = rng
    if source is None:
        candidate = getattr(humanizer, "_rng", None)
        source = candidate if isinstance(candidate, random.Random) else random.Random()
    low, high = policy.browse_seconds
    return source.uniform(min(low, high), max(low, high))


async def _read_timeline(
    page: Page,
    humanizer: Humanizer,
    policy: KeepalivePolicy,
    rng: random.Random | None = None,
) -> None:
    """Scroll the home timeline for a randomised interval without any clicks.

    The duration is drawn once and every pause is bounded by the remaining
    budget, so a scroll plan can never stretch a keepalive far beyond the policy
    or leave a test hanging when the configured window is zero.
    """
    target = _browse_duration(humanizer, rng, policy)
    started = time.monotonic()

    def remaining() -> float:
        return target - (time.monotonic() - started)

    def bounded(seconds: float) -> Awaitable[None]:
        return asyncio.sleep(min(max(seconds, 0.0), max(remaining(), 0.0)))

    while remaining() > 0:
        plan = humanizer.scroll_plan()
        if not plan:
            await humanizer.pause("think", sleep=bounded)
            continue
        for distance, pause_s in plan:
            if remaining() <= 0:
                break
            await page.mouse.wheel(0, distance)
            await bounded(pause_s)


def _park_kind(exc: BaseException) -> str | None:
    if isinstance(exc, ChallengeError):
        return "challenge"
    if isinstance(exc, AccountRestrictedError):
        return "locked"
    if isinstance(exc, RateLimitedError):
        return "rate_limit"
    return None


def _meta_handle(meta: Mapping[str, Any]) -> str | None:
    value = meta.get("handle")
    return value if isinstance(value, str) and value else None


def _profile_is_empty(profile_dir: Path) -> bool:
    try:
        return next(profile_dir.iterdir(), None) is None
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
