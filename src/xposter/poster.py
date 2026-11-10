"""Post a thread through the real X web client, idempotently and at human pace.

The poster is the only module that combines the browser, the ledger, pacing and
the human-timing plans. Two invariants drive the whole design:

* **Never double-post.** The ledger is written as ``PENDING`` before the first
  click, every captured tweet id is recorded immediately, and an outcome that
  cannot be proven becomes ``UNKNOWN`` instead of being retried. The browser is
  not even started when the ledger already knows the answer.
* **Stop, never evade.** A challenge, lock, suspension or rate limit parks the
  tool and raises a typed error. Only an HTTP 5xx on the very first click may be
  retried once, because the server proved it created nothing.

Tweet ids are read passively from the ``CreateTweet`` responses. Scraping the
DOM or the success toast would race the client's own render and break on
localisation.

The classic composer is Draft.js: each line is a ``div[data-block="true"]`` and
each run of text is a ``span[data-text="true"]``. ``innerText`` is unreliable
there — a blank line is an empty block containing only ``br[data-text]``, which
adds an extra newline — so the pre-Post read-back reconstructs the logical text
from the blocks and joins them with a single newline.

Typing a line-final ``@handle``, ``#hashtag`` or ``$cashtag`` opens X's
typeahead; a following Return would select the suggestion instead of inserting
a line break. The typer therefore sends one plain space after such a token
(a space cannot belong to it), so the posted text may legitimately end a line
with a trailing space. The read-back ignores trailing spaces at the end of
each line and at the very end of the text, and nothing else.

The two strategies deliberately differ in cadence. ``Strategy.COMPOSER`` mirrors
X's own **Post all** burst: one submit and N sequential requests, exactly what a
person using the thread composer produces. ``Strategy.REPLIES`` spends the full
20-45 s between tweets (plus typing and dwell), so a 25-tweet thread takes
roughly 20+ minutes; it is the human-like choice for a long thread, at the cost
of many more page loads.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import random
import sys
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from playwright._impl._api_structures import FloatRect
from playwright.async_api import BrowserContext, Locator, Page, Response, async_playwright
from playwright.async_api import Error as PlaywrightError

from . import guard, selectors, sessions, splitter
from .config import Strategy, XPosterConfig
from .errors import (
    ChallengeError,
    LedgerCorruptError,
    NotLoggedInError,
    PostOutcomeUnknownError,
    PostRejectedError,
    RateLimitedError,
    UiChangedError,
    ValidationError,
    XPosterError,
)
from .humanize import Humanizer
from .ledger import Decision, Ledger, utcnow
from .media import validate_media
from .mentions import ends_with_typeahead_token, extract_mentions
from .models import PostedThread, PostedTweet, SessionSummary, Thread, Tweet
from .pacing import Pacer
from .sessions import RefreshResult, SessionInfo
from .store import ParkInfo, SessionStore

__all__ = ["ClickNotDispatchedError", "ContextHook", "XPoster", "human_click"]

ContextHook = Callable[[BrowserContext], Awaitable[None]]

# X's composer caps a thread at 25 posts; the client refuses to add the 26th.
_MAX_THREAD_TWEETS = 25
# A thread is one ``Post all`` click but N sequential requests; waiting a fixed
# 15 s for the whole batch would fail a legitimate 20-post thread.
_POST_WAIT_S_PER_TWEET = 15.0
_DELETE_WAIT_S = 15.0
_POLL_INTERVAL_S = 0.05
_PROBE_INTERVAL_S = 0.5
_ENABLE_WAIT_S = 5.0
# X mounts the composer asynchronously after the route shell; the selectors'
# per-candidate budget is too short for a cold live load.
_COMPOSER_READY_TIMEOUT_S = 15.0
_COMPOSER_READY_POLL_S = 0.25
# X uploads as soon as the file is chosen and keeps Post disabled until every
# preview is ready. The budget scales with the bytes so a 5 MiB image is not
# rushed but a slow link cannot hang the run forever.
_UPLOAD_POLL_S = 0.25
_UPLOAD_TIMEOUT_BASE_S = 30.0
_UPLOAD_TIMEOUT_PER_MIB_S = 20.0
_UPLOAD_TIMEOUT_CAP_S = 180.0
_ATTACHMENT_PROGRESS = (
    '[role="dialog"][aria-modal="true"] [data-testid="attachments"] [role="progressbar"]'
)
# Media previews are counted with dedicated locators. The logical
# ``composer_media_preview`` selector is visible-first and falls back to generic
# modal images, which on the live page is the account avatar while the empty
# attachments container has no box.
_MEDIA_IMAGES_PRIMARY = '[role="dialog"][aria-modal="true"] [data-testid="attachments"] img'
_MEDIA_IMAGES_FALLBACK = '[role="dialog"][aria-modal="true"] [role="group"][aria-label="Media"] img'
_MEDIA_ATTACHMENT_CONTROLS = (
    '[role="dialog"][aria-modal="true"] button[aria-label="Edit media"], '
    '[role="dialog"][aria-modal="true"] button[aria-label="Remove media"]'
)
# If the files were set but the counter never moves while the attachment is
# visibly there (controls and enabled Post), waiting out the whole upload
# budget would hide a counting bug. This is a reporting guard, not a retry.
_MEDIA_UNCOUNTED_GRACE_S = 5.0
_CLICK_HOVER_FRACTION = 0.25
# Keyboard submit is a fallback for a covered Post button, never the default:
# it is a different behavioural signature from a click. Meta+Enter on macOS,
# Control+Enter elsewhere, exactly as the web clients accept it.
_SUBMIT_SHORTCUT_MODIFIER = "Meta" if sys.platform.startswith("darwin") else "Control"
# Invisible characters that contenteditable editors can inject (Draft.js legacy).
# ZWJ/ZWNJ are deliberately kept: they are meaningful inside emoji sequences.
_INVISIBLE_EDITOR_CHARS = str.maketrans("", "", "\u200b\ufeff\u200e\u200f")
# The one permitted re-click after an HTTP 5xx waits like a person would; the
# research recommends 30-60 s for a transient failure, so 15-45 s is the low end
# of that advice, drawn fresh each time to avoid a metronome.
_RETRY_BACKOFF_RANGE = (15.0, 45.0)
# Visible text is classified strictly, and only inside containers that actually
# hold alerts. Reading the whole body would park on a timeline post that merely
# mentions "captcha" or "authenticate".
_ALERT_SELECTORS = ", ".join(
    (
        '[role="alert"]',
        '[role="alertdialog"]',
        '[role="dialog"]',
        "[data-sonner-toast]",
        '[data-testid="toast"]',
    )
)
_TERMINAL_ACTIONS = frozenset(
    {
        guard.Action.PARK,
        guard.Action.RELOGIN,
        guard.Action.REJECT,
        guard.Action.STOP_THREAD,
    }
)


class ClickNotDispatchedError(PlaywrightError):
    """A click failed before the mouse button could reach the page.

    The distinction is a ledger safety boundary: only this failure proves that
    no request was dispatched, so only then may the caller clear the persisted
    click barrier. A failure from ``page.mouse.click`` itself is deliberately
    not wrapped because the browser may already have received the mousedown.
    """


async def human_click(
    page: Page,
    locator: Locator,
    humanizer: Humanizer,
    *,
    start: tuple[float, float] = (0.0, 0.0),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    timeout_ms: int = 5_000,
) -> tuple[float, float]:
    """Click ``locator`` along a human pointer path and return the new position.

    A pointer that teleports to every target is one of the few behavioural
    signals a page can measure without fingerprinting. The caller keeps the
    returned point so the next click continues from where this one ended.

    A coordinate click is only used after ``document.elementFromPoint`` proves
    the point lands on the target element or one of its descendants. A covered
    element is scrolled to the centre of the viewport and measured once more; if
    it is still covered, Playwright's own actionability-checked
    :meth:`Locator.click` is the fallback, so an overlay can never receive a
    blind click meant for a button underneath it.

    Every failure that happens before the pointer can reach the page raises
    :class:`ClickNotDispatchedError`; the posting code may then clear its click
    barrier instead of quarantining a thread that was never clicked.
    """
    try:
        await locator.scroll_into_view_if_needed(timeout=timeout_ms)
        point = await _uncovered_point(locator, humanizer)
    except PlaywrightError as exc:
        raise ClickNotDispatchedError(str(exc)) from exc
    if point is None:
        try:
            await locator.click(timeout=timeout_ms)
        except PlaywrightError as exc:
            raise ClickNotDispatchedError(str(exc)) from exc
        box = await locator.bounding_box()
        if box is not None:
            return (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        return start
    for x, y in humanizer.mouse_path(start, point):
        try:
            await page.mouse.move(x, y)
        except PlaywrightError as exc:
            raise ClickNotDispatchedError(str(exc)) from exc
    await sleep(humanizer.pause_s("action") * _CLICK_HOVER_FRACTION)
    # Not wrapped: a failure here may mean the browser already saw the mousedown.
    await page.mouse.click(point[0], point[1])
    return point


async def _uncovered_point(locator: Locator, humanizer: Humanizer) -> tuple[float, float] | None:
    """Return a click point on the element that nothing else covers.

    At most two measurements: the element as first scrolled into view, then
    centred in the viewport. ``None`` means the caller must fall back to
    Playwright's actionability-checked click instead of a coordinate click.
    """
    box = await locator.bounding_box()
    if box is None:
        raise UiChangedError(
            "An element that should be clickable has no on-screen box; the page "
            "layout may have changed. Inspect the artifacts and update selectors."
        )
    point = _offset_point(box, humanizer)
    if await _point_hits(locator, point):
        return point
    with contextlib.suppress(PlaywrightError):
        await locator.evaluate(
            "element => element.scrollIntoView({block: 'center', inline: 'center'})"
        )
    box = await locator.bounding_box()
    if box is None:
        return None
    point = _offset_point(box, humanizer)
    if await _point_hits(locator, point):
        return point
    return None


def _offset_point(box: FloatRect, humanizer: Humanizer) -> tuple[float, float]:
    """Pick a Gaussian point inside the element's current box."""
    offset_x, offset_y = humanizer.click_offset(box["width"], box["height"])
    return (box["x"] + offset_x, box["y"] + offset_y)


async def _point_hits(locator: Locator, point: tuple[float, float]) -> bool:
    """Whether ``elementFromPoint`` at ``point`` lands on the element itself."""
    try:
        hit = await locator.evaluate(
            """(element, [x, y]) => {
                const found = document.elementFromPoint(x, y);
                return !!found && (found === element || element.contains(found));
            }""",
            [point[0], point[1]],
        )
    except PlaywrightError:
        return False
    return bool(hit)


class _ResponseWatcher:
    """Collects ``CreateTweet``/``DeleteTweet`` responses without touching the DOM.

    The listener must be registered before the click and survive navigations, so
    it lives on the page for as long as one submit takes. Playwright event
    handlers are synchronous, so the callback only queues the interesting
    responses; the posting coroutine drains the queue, which keeps the id
    handling in one place and makes the check order deterministic.
    """

    def __init__(self, page: Page, *, deletion: bool = False) -> None:
        self._page = page
        self._deletion = deletion
        self._changed = asyncio.Event()
        self._queue: list[Response] = []
        self._stopped = False
        self._handler = self._on_response
        self.on_created: Callable[[str], None] | None = None
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.verdicts: list[guard.Verdict] = []
        self.statuses: list[int] = []

    def start(self) -> None:
        """Start listening; call before the click that triggers the request."""
        self._page.on("response", self._handler)

    def stop(self) -> None:
        """Stop listening; safe to call more than once."""
        if self._stopped:
            return
        self._stopped = True
        try:
            self._page.remove_listener("response", self._handler)
        except (ValueError, PlaywrightError):
            return

    def last_verdict(self) -> guard.Verdict | None:
        """The most specific verdict seen so far, or ``None``."""
        return self.verdicts[-1] if self.verdicts else None

    def has_server_error(self) -> bool:
        """True when at least one ``CreateTweet`` response was an HTTP 5xx."""
        return any(500 <= status < 600 for status in self.statuses)

    async def wait_for_created(
        self,
        count: int,
        timeout_s: float,
        *,
        probe: Callable[[], Awaitable[guard.Verdict | None]] | None = None,
    ) -> None:
        """Wait until ``count`` ids were captured or an error verdict arrived.

        Returning early on a verdict keeps a refused or challenged post from
        burning the whole per-tweet timeout; the caller decides what the
        verdict means. ``probe`` is the only way to notice a challenge that the
        page renders without issuing a request at all.
        """
        await self._wait(
            lambda: len(self.created) >= count or bool(self.verdicts),
            timeout_s,
            probe=probe,
        )

    async def wait_for_deleted(self, count: int, timeout_s: float) -> None:
        """Wait until ``count`` deletes were confirmed or a verdict arrived."""
        await self._wait(lambda: len(self.deleted) >= count or bool(self.verdicts), timeout_s)

    def forget_verdicts(self) -> None:
        """Drop seen verdicts before waiting again for a client-side retry."""
        self.verdicts.clear()

    async def _wait(
        self,
        done: Callable[[], bool],
        timeout_s: float,
        *,
        probe: Callable[[], Awaitable[guard.Verdict | None]] | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        last_probe = float("-inf")
        while True:
            await self._drain()
            if done():
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            now = loop.time()
            if probe is not None and now - last_probe >= _PROBE_INTERVAL_S:
                last_probe = now
                verdict = await probe()
                if verdict is not None and verdict.action in _TERMINAL_ACTIONS:
                    self.verdicts.append(verdict)
                    return
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=min(remaining, 0.5))
            except TimeoutError:
                continue

    def _on_response(self, response: Response) -> None:
        """Queue only the responses this watcher reasons about; never await here."""
        try:
            url = response.url
            if self._deletion:
                if guard.is_delete_tweet_response(url):
                    self._queue.append(response)
                    self._changed.set()
                return
            if guard.is_create_tweet_response(url):
                self._queue.append(response)
                self._changed.set()
                return
            if response.request.resource_type == "document":
                verdict = guard.classify_url(url)
                if verdict is not None:
                    self.verdicts.append(verdict)
                    self._changed.set()
        except (PlaywrightError, TypeError, ValueError):
            return

    async def _drain(self) -> None:
        while self._queue:
            await self._inspect(self._queue.pop(0))

    async def _inspect(self, response: Response) -> None:
        try:
            status = response.status
            self.statuses.append(status)
            payload = await _response_json(response)
            mapping = payload if isinstance(payload, Mapping) else None
            verdict = guard.classify_response(status, mapping)
            if verdict is not None:
                self.verdicts.append(verdict)
                return
            if self._deletion:
                self.deleted.append(response.url)
                return
            if mapping is None:
                return
            created = guard.extract_created_tweet_id(mapping)
            if created:
                self.created.append(created)
                if self.on_created is not None:
                    self.on_created(created)
        except (PlaywrightError, TypeError, ValueError):
            return


async def _response_json(response: Response) -> object | None:
    """Return the parsed JSON body, or ``None`` when it is absent or not JSON."""
    try:
        return cast(object, await response.json())
    except (PlaywrightError, ValueError):
        return None


def _initial_pointer(page: Page) -> tuple[float, float]:
    """A plausible pointer start in the central third of the viewport.

    Starting at (0, 0) makes the first click a long diagonal from the screen
    corner, which no person produces. The point is drawn once per poster, so
    later clicks continue from where the previous one ended.
    """
    size = page.viewport_size or {"width": 1280, "height": 800}
    width = float(size["width"])
    height = float(size["height"])
    return (
        random.uniform(width / 3.0, width * 2.0 / 3.0),
        random.uniform(height / 3.0, height * 2.0 / 3.0),
    )


def _current_line(text: str) -> str:
    """The text after the last newline, for the typeahead guard."""
    return text.replace("\r\n", "\n").replace("\r", "\n").rsplit("\n", 1)[-1]


def _query_free_url(url: str) -> str:
    """Strip the query string (and fragment) from a URL before persisting it."""
    return url.split("?", 1)[0].split("#", 1)[0]


def _upload_timeout_s(media: Sequence[Path]) -> float:
    """Scale the upload budget with the bytes: 30 s + 20 s per MiB, cap 180 s."""
    total = 0
    for path in media:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    mib = total / (1024 * 1024)
    return min(_UPLOAD_TIMEOUT_CAP_S, _UPLOAD_TIMEOUT_BASE_S + _UPLOAD_TIMEOUT_PER_MIB_S * mib)


async def _read_editor_text(editor: Locator) -> str:
    """Read the composer editor's logical text for the pre-Post read-back.

    A native textarea exposes its value directly. The classic composer is
    Draft.js: its ``innerText`` adds an extra newline for a blank line (an empty
    block contains only ``br[data-text]``), so the logical text is rebuilt from
    the blocks instead: each block's ``[data-text="true"]`` spans concatenated,
    blocks joined by one newline. Other contenteditables fall back to rendered
    text.
    """
    try:
        tag = await editor.evaluate("element => element.tagName.toLowerCase()")
    except PlaywrightError:
        tag = ""
    if tag == "textarea":
        try:
            return await editor.input_value()
        except PlaywrightError:
            return ""
    try:
        blocks = await editor.evaluate(
            """(editor) => {
                const blocks = Array.from(editor.querySelectorAll('[data-block="true"]'));
                if (!blocks.length) {
                    return null;
                }
                return blocks.map((block) => Array.from(
                    block.querySelectorAll('[data-text="true"]')
                ).map((node) => (
                    node.tagName.toLowerCase() === 'br' ? '' : (node.textContent || '')
                )).join(''));
            }"""
        )
    except PlaywrightError:
        blocks = None
    if isinstance(blocks, list):
        return "\n".join(str(part) for part in blocks)
    try:
        return await editor.inner_text()
    except PlaywrightError:
        try:
            return await editor.text_content() or ""
        except PlaywrightError:
            return ""


def _normalise_editor_text(text: str) -> str:
    """Normalise editor text before comparing it with the intended tweet.

    NFC makes a decomposed combining sequence compare equal to its composed
    form; CRLF and lone CR become LF; NBSP becomes a normal space; invisible
    characters injected by contenteditable editors are dropped. ZWJ/ZWNJ stay
    because they are meaningful inside emoji sequences. A single trailing
    newline is ignored because a contenteditable adds one for the caret.

    Trailing spaces are ignored at the end of every line and at the very end:
    the typeahead guard deliberately types one after a line-final ``@handle``,
    ``#hashtag`` or ``$cashtag``. Only spaces are ignored; tabs, internal runs
    of spaces, newlines and every other character still have to match exactly.
    """
    normalised = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    normalised = normalised.replace("\u00a0", " ").translate(_INVISIBLE_EDITOR_CHARS)
    if normalised.endswith("\n"):
        normalised = normalised[:-1]
    return "\n".join(line.rstrip(" ") for line in normalised.split("\n"))


def _typed_mismatch_summary(expected: str, actual: str) -> str:
    """A short diff for the artifact: lengths, first difference, intended prefix.

    The editor's text is our own tweet, but the summary deliberately omits the
    actual text: only the intended text is included, truncated, so a mismatch
    caused by page content can never leak somebody else's post.
    """
    wanted = _normalise_editor_text(expected)
    got = _normalise_editor_text(actual)
    index = min(len(wanted), len(got))
    for position, (left, right) in enumerate(zip(wanted, got, strict=False)):
        if left != right:
            index = position
            break
    return (
        "typed text mismatch\n"
        f"expected_length: {len(wanted)}\n"
        f"actual_length: {len(got)}\n"
        f"first_difference_index: {index}\n"
        f"intended_prefix: {wanted[:200]!r}\n"
    )


@dataclass(slots=True)
class _Attempt:
    """Mutable progress of one posting attempt, shared with the strategies.

    ``clicked`` mirrors the persisted click barrier: it becomes true only after
    ``Ledger.mark_clicking`` is fsynced and is reset only when a pre-dispatch
    failure proves that nothing could have been sent. Once true, the failure
    handling must never downgrade the ledger entry by itself.
    """

    thread_hash: str
    strategy: str
    handle: str
    label: str = ""
    began: bool = False
    clicked: bool = False
    captured: list[PostedTweet] = field(default_factory=list)


class XPoster:
    """Post threads, verify the session, and delete test posts.

    Every collaborator is injectable so a test can run the real Chrome against
    an offline fake of x.com with deterministic timing, while production builds
    them from one immutable :class:`XPosterConfig`.
    """

    def __init__(
        self,
        config: XPosterConfig,
        *,
        store: SessionStore | None = None,
        ledger: Ledger | None = None,
        pacer: Pacer | None = None,
        humanizer: Humanizer | None = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        playwright_factory: Callable[[], Any] = async_playwright,
        context_hook: ContextHook | None = None,
        submit_shortcut_fallback: bool = False,
        allow_alt_text: bool = False,
    ) -> None:
        self.config = config
        self.store = store if store is not None else SessionStore(config)
        self.ledger = ledger if ledger is not None else Ledger(config.ledger_path, clock=clock)
        self.pacer = pacer if pacer is not None else Pacer(config.pacing, self.ledger, clock=clock)
        self._humanizer = humanizer if humanizer is not None else Humanizer(config.human)
        self._clock = clock
        self._sleep = sleep
        self._playwright_factory = playwright_factory
        self._context_hook = context_hook
        self._submit_shortcut_fallback = submit_shortcut_fallback
        self._allow_alt_text = allow_alt_text
        self._mouse: tuple[float, float] | None = None

    def session_status(self) -> SessionSummary:
        """Cookie-free view of the saved session, safe to print or serve."""
        return self.store.summarize(self._now())

    def parked(self) -> ParkInfo | None:
        """Why automation is parked, or ``None`` when it may run."""
        return self.store.parked()

    def unpark(self) -> bool:
        """Clear the parking marker; True when it existed."""
        return self.store.unpark()

    async def login(self, *, on_status: Callable[[str], None] | None = None) -> SessionInfo:
        """Run the one-time human login and report the signed-in account."""
        return await sessions.login(
            self.config,
            self.store,
            on_status=on_status,
            playwright_factory=self._playwright_factory,
            context_hook=self._context_hook,
        )

    async def verify(self) -> SessionInfo:
        """Check the saved session against X without posting anything."""
        return await sessions.verify(
            self.config,
            self.store,
            playwright_factory=self._playwright_factory,
            context_hook=self._context_hook,
        )

    async def refresh(self, *, force: bool = False) -> RefreshResult:
        """Load X once to keep the saved session alive."""
        return await sessions.refresh(
            self.config,
            self.store,
            force=force,
            playwright_factory=self._playwright_factory,
            context_hook=self._context_hook,
            clock=self._clock,
            humanizer=self._humanizer,
        )

    def reset_ledger(self) -> Path:
        """Quarantine a corrupt ledger and return the path it was moved to.

        A corrupt ledger is the one case where the duplicate-post evidence is
        unreadable; the operator must make this choice explicitly instead of a
        posting run silently starting from an empty ledger.
        """
        return self.ledger.reset_corrupt()

    def logout(self, *, keep_backup: bool = True) -> None:
        """Forget the browser profile; keep the cookie backup unless asked not to."""
        sessions.reset_profile(self.config, self.store, keep_backup=keep_backup)

    async def post_thread(
        self,
        thread: Thread,
        *,
        force: bool = False,
        discard_recorded: bool = False,
        dry_run: bool = False,
    ) -> PostedThread:
        """Post ``thread`` once, returning the ledger record of what landed.

        The order below is the idempotency contract: validate, consult the
        ledger, check local pacing, then open the browser. A ledger answer that
        the thread is already posted ends the call without launching Chrome.
        ``discard_recorded`` is the operator's explicit statement that the
        recorded tweets of a partial attempt are gone; it is the only way to
        post that thread as a fresh thread, and it is never implied by
        ``force``.
        """
        self.store.raise_if_parked()
        self._validate_thread(thread)
        thread_hash = thread.content_hash
        decision = self.ledger.decide(thread_hash, force=force, discard_recorded=discard_recorded)
        if decision is Decision.ALREADY_POSTED:
            return self._stored_thread(thread_hash)
        if decision is Decision.BLOCKED_UNKNOWN:
            raise PostOutcomeUnknownError(
                "A previous attempt on this thread has an unknown outcome. Check "
                f"{self.config.base_url} for a live copy, then re-run with --force "
                "(replies only) or --discard-recorded after verifying the profile.",
                self._entry_posted(thread_hash),
            )
        resume: tuple[PostedTweet, ...] = ()
        if decision is Decision.RESUME:
            if self.config.strategy is not Strategy.REPLIES:
                raise PostOutcomeUnknownError(
                    "This thread was partially posted, and the composer strategy "
                    "cannot continue it without re-posting the recorded tweets. "
                    "Check the profile, delete the partial thread, then re-run with "
                    "--discard-recorded to post it as a fresh thread.",
                    self._entry_posted(thread_hash),
                )
            resume = self.ledger.resume_from(thread_hash)
            if not resume:
                raise PostOutcomeUnknownError(
                    "The ledger has an interrupted attempt without a recorded "
                    "tweet id, so there is nothing safe to resume from. Check the "
                    "profile and re-run with --force only if nothing was posted.",
                )
        if not dry_run:
            self.pacer.check(len(thread.tweets))
        label = f"{self.config.strategy.value}-{thread_hash[:8]}"
        async with sessions.authenticated_page(
            self.config,
            self.store,
            playwright_factory=self._playwright_factory,
            context_hook=self._context_hook,
            goto="/home",
        ) as session:
            page = session.page
            handle = session.handle or self.config.handle or ""
            attempt = _Attempt(
                thread_hash=thread_hash,
                strategy=self.config.strategy.value,
                handle=handle,
                label=label,
            )
            if not dry_run:
                self.ledger.begin(
                    thread_hash,
                    strategy=attempt.strategy,
                    handle=handle,
                    n_tweets=len(thread.tweets),
                    discard_recorded=discard_recorded,
                )
                attempt.began = True
            try:
                if dry_run:
                    await self._rehearse(page, thread, attempt)
                    await self._discard_composer(page)
                    return PostedThread(
                        thread_hash=thread_hash,
                        handle=handle,
                        strategy="dry-run",
                        posted_at=self._stamp(),
                        tweets=(),
                        complete=False,
                    )
                if self.config.strategy is Strategy.COMPOSER:
                    await self._post_composer(page, thread, attempt)
                else:
                    await self._post_replies(page, thread, attempt, resume)
            except LedgerCorruptError:
                raise
            except Exception as exc:
                self._record_failure(attempt, exc)
                if isinstance(exc, PlaywrightError):
                    artifacts = await self._capture_artifacts(page, label)
                    raise UiChangedError(
                        f"A browser operation failed while posting: {exc}. "
                        "Inspect the saved artifacts and retry after fixing the UI.",
                        artifacts,
                    ) from exc
                if isinstance(exc, UiChangedError) and exc.artifacts is None:
                    exc.artifacts = await self._capture_artifacts(page, label)
                raise
            except BaseException as exc:
                self._record_interruption(attempt, exc)
                raise
            posted = self._completed(attempt, thread, resume)
            self.ledger.complete(thread_hash, posted)
            self.pacer.record_thread_done(len(thread.tweets))
            return posted

    async def post_text(
        self,
        text: str,
        *,
        media: Sequence[Path] = (),
        first: str | None = None,
        force: bool = False,
        discard_recorded: bool = False,
        dry_run: bool = False,
        numbering: bool = True,
    ) -> PostedThread:
        """Split ``text`` into tweets and post the resulting thread.

        With ``first`` the caller pins the opening post verbatim (an index post
        must never be re-flowed); only the remaining text is split. The media
        attach to tweet one because that is the post readers see in the card.
        """
        limit = self.config.max_tweet_weight
        if first is None:
            segments = splitter.split_thread(text, limit=limit, numbering=numbering)
        else:
            if numbering:
                segments = splitter.split_with_first(first, text, limit=limit)
            else:
                splitter.validate_tweet(first, limit=limit)
                rest = (
                    splitter.split_thread(text, limit=limit, numbering=False)
                    if text.strip()
                    else []
                )
                segments = [first, *rest]
        tweets = [Tweet(segments[0], media=tuple(media))]
        tweets.extend(Tweet(segment) for segment in segments[1:])
        return await self.post_thread(
            Thread(tweets=tuple(tweets), key=""),
            force=force,
            discard_recorded=discard_recorded,
            dry_run=dry_run,
        )

    async def delete_thread(self, posted: PostedThread) -> list[str]:
        """Delete the thread's tweets newest-first through the web UI.

        This exists for cleaning up test posts on the operator's own account;
        the ledger keeps its POSTED row (the thread really was posted), so a
        re-run still refuses to duplicate it.
        """
        self.store.raise_if_parked()
        if not posted.tweets:
            return []
        deleted: list[str] = []
        async with sessions.authenticated_page(
            self.config,
            self.store,
            playwright_factory=self._playwright_factory,
            context_hook=self._context_hook,
            goto="/home",
        ) as session:
            page = session.page
            for tweet in sorted(posted.tweets, key=lambda item: item.index, reverse=True):
                try:
                    await self._delete_one(page, tweet)
                except UiChangedError as exc:
                    if exc.artifacts is None:
                        exc.artifacts = await self._capture_artifacts(page, f"delete-{tweet.id}")
                    raise
                except PlaywrightError as exc:
                    artifacts = await self._capture_artifacts(page, f"delete-{tweet.id}")
                    raise UiChangedError(
                        f"Could not delete tweet {tweet.id}: {exc}. "
                        "Inspect the saved artifacts and retry.",
                        artifacts,
                    ) from exc
                deleted.append(tweet.id)
        return deleted

    async def _post_composer(self, page: Page, thread: Thread, attempt: _Attempt) -> None:
        """Type every tweet into the /compose/post modal and press Post all."""
        root = await self._open_composer(page)
        editor: Locator | None = None
        for index, tweet in enumerate(thread.tweets):
            if index > 0:
                add = await selectors.locate(
                    page, "composer_add_post_button", timeout_ms=self.config.default_timeout_ms
                )
                await self._click(page, add)
            editor = await self._textarea_for(page, index)
            await self._assert_editor_in_modal(root, editor)
            await self._click(page, editor)
            await self._type(page, tweet.text)
            await self._verify_typed_text(page, editor, tweet.text, attempt)
            await self._attach_media(page, tweet.media, tweet.alt_texts)
            if index < len(thread.tweets) - 1:
                await self._pause("think")
        if editor is None:
            raise UiChangedError("The composer has no text editor; refusing to submit.")
        await self._pre_submit(page)
        thread_mode = len(thread.tweets) > 1
        await self._refuse_disabled(
            await self._composer_submit(page, thread_mode=thread_mode), thread
        )
        await self._click_and_capture(
            page,
            thread,
            attempt,
            indexes=list(range(len(thread.tweets))),
            submit=lambda: self._composer_submit(page, thread_mode=thread_mode),
            editor=editor,
        )
        self._ensure_complete(attempt, thread, ())

    async def _post_replies(
        self,
        page: Page,
        thread: Thread,
        attempt: _Attempt,
        resume: Sequence[PostedTweet],
    ) -> None:
        """Post tweet one in the composer, then reply to each previous tweet."""
        recorded = {tweet.index: tweet for tweet in resume}
        if 0 not in recorded:
            root = await self._open_composer(page)
            editor = await self._textarea_for(page, 0)
            await self._assert_editor_in_modal(root, editor)
            await self._click(page, editor)
            await self._type(page, thread.tweets[0].text)
            await self._verify_typed_text(page, editor, thread.tweets[0].text, attempt)
            await self._attach_media(page, thread.tweets[0].media, thread.tweets[0].alt_texts)
            await self._pre_submit(page)
            await self._refuse_disabled(
                await self._composer_submit(page, thread_mode=False), thread
            )
            await self._click_and_capture(
                page,
                thread,
                attempt,
                indexes=[0],
                submit=lambda: self._composer_submit(page, thread_mode=False),
                editor=editor,
            )
        previous = self._last_known(attempt, resume)
        for index in range(1, len(thread.tweets)):
            if index in recorded:
                continue
            if previous is None:
                raise PostOutcomeUnknownError(
                    "The reply chain lost its parent tweet; refusing to guess a reply target.",
                    tuple(attempt.captured),
                )
            await self._pause("between_tweet")
            await page.goto(self._status_ref(previous), wait_until="domcontentloaded")
            await self._check_page_health(page)
            await selectors.locate(page, "tweet_article", timeout_ms=self.config.default_timeout_ms)
            scope = await self._focal_scope(page, previous.id)
            reply = await selectors.locate(
                scope, "tweet_reply_button", timeout_ms=self.config.default_timeout_ms
            )
            await self._click(page, reply)
            reply_root = await selectors.locate(
                page, "composer_root", timeout_ms=self.config.default_timeout_ms
            )
            editor = await self._textarea_for(page, 0)
            await self._assert_editor_in_modal(reply_root, editor)
            await self._click(page, editor)
            await self._type(page, thread.tweets[index].text)
            await self._verify_typed_text(page, editor, thread.tweets[index].text, attempt)
            await self._attach_media(
                page, thread.tweets[index].media, thread.tweets[index].alt_texts
            )
            await self._pre_submit(page)
            await self._refuse_disabled(await self._reply_submit(page), thread)
            await self._click_and_capture(
                page,
                thread,
                attempt,
                indexes=[index],
                submit=lambda: self._reply_submit(page),
                editor=editor,
            )
            previous = self._last_known(attempt, resume)
        self._ensure_complete(attempt, thread, resume)

    async def _rehearse(self, page: Page, thread: Thread, attempt: _Attempt) -> None:
        """Type the whole thread without clicking Post, for ``--dry-run``.

        The replies strategy cannot be rehearsed end to end because its later
        tweets need the ids of earlier real posts, so a dry run always exercises
        the composer UI and the content, then discards it.
        """
        root = await self._open_composer(page)
        for index, tweet in enumerate(thread.tweets):
            if index > 0:
                add = await selectors.locate(
                    page, "composer_add_post_button", timeout_ms=self.config.default_timeout_ms
                )
                await self._click(page, add)
            editor = await self._textarea_for(page, index)
            await self._assert_editor_in_modal(root, editor)
            await self._click(page, editor)
            await self._type(page, tweet.text)
            await self._verify_typed_text(page, editor, tweet.text, attempt)
            await self._attach_media(page, tweet.media, tweet.alt_texts)
            if index < len(thread.tweets) - 1:
                await self._pause("think")
        await self._pre_submit(page)
        await self._refuse_disabled(
            await self._composer_submit(page, thread_mode=len(thread.tweets) > 1), thread
        )

    async def _click_and_capture(
        self,
        page: Page,
        thread: Thread,
        attempt: _Attempt,
        *,
        indexes: Sequence[int],
        submit: Callable[[], Awaitable[Locator]],
        editor: Locator,
    ) -> None:
        """Click submit, read the created ids passively, and retry a bare 5xx once.

        A 5xx proves the server created nothing, so one re-click is safe. Any
        other retry would either duplicate a tweet or deepen a challenge; those
        become a typed error or an unknown outcome instead.
        """
        expected_before = len(attempt.captured)
        pending = list(indexes)
        retried = False
        while True:
            watcher = _ResponseWatcher(page)

            def on_created(tweet_id: str) -> None:
                index = pending.pop(0) if pending else len(attempt.captured)
                self._record_posted(
                    attempt, self._posted_tweet(index, tweet_id, thread, attempt.handle)
                )

            watcher.on_created = on_created
            watcher.start()
            try:
                button = await submit()
                # The barrier is fsynced before the pointer can move: a crash
                # after this line leaves the thread quarantined, never NEW.
                self.ledger.mark_clicking(attempt.thread_hash)
                attempt.clicked = True
                try:
                    await self._click(page, button)
                except ClickNotDispatchedError:
                    # Both click paths failed before dispatch. With the opt-in
                    # fallback, submit from the focused editor; otherwise the
                    # barrier is safe to clear because nothing was sent.
                    if not self._submit_shortcut_fallback:
                        attempt.clicked = False
                        self.ledger.clear_clicking(attempt.thread_hash)
                        raise
                    try:
                        await self._submit_by_shortcut(page, editor)
                    except ClickNotDispatchedError:
                        attempt.clicked = False
                        self.ledger.clear_clicking(attempt.thread_hash)
                        raise
                except UiChangedError:
                    attempt.clicked = False
                    self.ledger.clear_clicking(attempt.thread_hash)
                    raise
                with contextlib.suppress(TimeoutError):
                    await watcher.wait_for_created(
                        len(indexes),
                        _POST_WAIT_S_PER_TWEET * max(1, len(indexes)),
                        probe=lambda: self._page_verdict(page),
                    )
                if len(watcher.created) >= len(indexes):
                    return
                verdict = watcher.last_verdict()
                if verdict is not None and verdict.action is guard.Action.REFRESH_CSRF:
                    # X's own client refreshes ct0 and retries; never forge the
                    # header ourselves, just wait for that retry to arrive.
                    watcher.forget_verdicts()
                    with contextlib.suppress(TimeoutError):
                        await watcher.wait_for_created(len(indexes), _POST_WAIT_S_PER_TWEET)
                    if len(watcher.created) >= len(indexes):
                        return
                    verdict = watcher.last_verdict()
                if (
                    not retried
                    and verdict is not None
                    and verdict.action is guard.Action.RETRY
                    and watcher.has_server_error()
                    and len(attempt.captured) == expected_before
                ):
                    retried = True
                    await self._sleep(random.uniform(*_RETRY_BACKOFF_RANGE))
                    continue
                if verdict is None:
                    verdict = await self._page_verdict(page)
                if verdict is not None:
                    if verdict.action is guard.Action.STOP_THREAD:
                        raise PostOutcomeUnknownError(
                            f"{verdict.reason} The thread stopped part-way; check "
                            "the profile before continuing it.",
                            tuple(attempt.captured),
                        )
                    if verdict.action in _TERMINAL_ACTIONS:
                        self._raise_for_verdict(verdict)
                raise PostOutcomeUnknownError(
                    "The Post click produced no tweet id we could verify. Check "
                    f"{self.config.base_url} for a live copy before trying again; "
                    "re-running with --force may duplicate what already landed.",
                    tuple(attempt.captured),
                )
            finally:
                watcher.stop()

    def _record_posted(self, attempt: _Attempt, posted: PostedTweet) -> None:
        """Remember one created tweet and persist it before anything else fails."""
        attempt.captured.append(posted)
        if attempt.began:
            self.ledger.record_tweet(attempt.thread_hash, posted)

    def _posted_tweet(self, index: int, tweet_id: str, thread: Thread, handle: str) -> PostedTweet:
        text = thread.tweets[index].text if 0 <= index < len(thread.tweets) else ""
        return PostedTweet(
            index=index,
            id=tweet_id,
            url=self._status_url(handle, tweet_id),
            text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def _status_url(self, handle: str, tweet_id: str) -> str:
        base = self.config.base_url.rstrip("/")
        if handle:
            return f"{base}/{handle}/status/{tweet_id}"
        return f"{base}/i/web/status/{tweet_id}"

    def _status_ref(self, tweet: PostedTweet) -> str:
        return tweet.url or self._status_url(self.config.handle or "", tweet.id)

    async def _focal_scope(self, page: Page, tweet_id: str) -> Page | Locator:
        """Return the article for ``tweet_id``, or the page when none matches.

        A status page can render unrelated articles before the focal one (an
        ad, a quoted post, a pinned reply). Replying to or deleting the wrong
        article is unrecoverable, so the target is identified by the anchor
        whose href ends with ``/status/<id>``. Falling back to the first
        article mirrors what a human sees as the main post when the anchor is
        missing; falling back to the page keeps the caller's logical selector
        working when the article markup itself changed.
        """
        articles = await selectors.locate_all(page, "tweet_article")
        if not articles:
            return page
        suffix = f"/status/{tweet_id}"
        for article in articles:
            try:
                matches = await article.locator(f'a[href$="{suffix}"]').count()
            except PlaywrightError:
                continue
            if matches:
                return article
        return articles[0]

    def _last_known(self, attempt: _Attempt, resume: Sequence[PostedTweet]) -> PostedTweet | None:
        known = {tweet.index: tweet for tweet in resume}
        for tweet in attempt.captured:
            known[tweet.index] = tweet
        if not known:
            return None
        return known[max(known)]

    @staticmethod
    def _ensure_complete(attempt: _Attempt, thread: Thread, resume: Sequence[PostedTweet]) -> None:
        covered = {tweet.index for tweet in resume} | {tweet.index for tweet in attempt.captured}
        missing = [index for index in range(len(thread.tweets)) if index not in covered]
        if missing:
            raise PostOutcomeUnknownError(
                f"Only {len(covered)} of {len(thread.tweets)} tweets could be "
                f"verified (missing indexes {missing}).",
                tuple(attempt.captured),
            )

    def _completed(
        self, attempt: _Attempt, thread: Thread, resume: Sequence[PostedTweet]
    ) -> PostedThread:
        by_index = {tweet.index: tweet for tweet in resume}
        for tweet in attempt.captured:
            by_index[tweet.index] = tweet
        tweets = tuple(by_index[index] for index in sorted(by_index))
        return PostedThread(
            thread_hash=attempt.thread_hash,
            handle=attempt.handle,
            strategy=attempt.strategy,
            posted_at=self._stamp(),
            tweets=tweets,
            complete=len(tweets) == len(thread.tweets),
        )

    def _record_failure(self, attempt: _Attempt, exc: BaseException) -> None:
        """Write the failure state that makes a retry safe (or impossible).

        A definite rejection frees the thread for a corrected retry; anything
        after a click that is not proven must quarantine the thread instead.
        """
        if not attempt.began:
            return
        if isinstance(exc, PostRejectedError):
            self.ledger.mark_failed(attempt.thread_hash, str(exc))
            return
        if attempt.clicked or isinstance(exc, PostOutcomeUnknownError):
            self.ledger.mark_unknown(attempt.thread_hash, str(exc))
            return
        self.ledger.mark_failed(attempt.thread_hash, str(exc))

    def _record_interruption(self, attempt: _Attempt, exc: BaseException) -> None:
        """Leave the ledger conservative when the process itself is stopping.

        ``KeyboardInterrupt``, ``CancelledError`` and ``SystemExit`` must not
        downgrade a raised click barrier: the click may have landed and the next
        run has to see it. Only an interruption before the barrier (provably
        nothing dispatched) records a normal failure.
        """
        if not attempt.began or attempt.clicked:
            return
        self.ledger.mark_failed(attempt.thread_hash, str(exc) or type(exc).__name__)

    def _raise_for_verdict(self, verdict: guard.Verdict) -> None:
        """Park on challenge-class verdicts, reject on content verdicts."""
        action = verdict.action
        if action in (guard.Action.PARK, guard.Action.RELOGIN):
            parking_exc = guard.to_exception(verdict) or self._fallback_error(verdict)
            sessions.park_on(parking_exc, self.store)
            raise parking_exc
        if action is guard.Action.REJECT:
            raise PostRejectedError(verdict.reason, code=verdict.code)
        converted = guard.to_exception(verdict)
        if converted is not None:
            raise converted
        raise PostRejectedError(verdict.reason or "X refused this action.", code=verdict.code)

    @staticmethod
    def _fallback_error(verdict: guard.Verdict) -> XPosterError:
        if verdict.action is guard.Action.RELOGIN:
            return NotLoggedInError(verdict.reason)
        if verdict.action is guard.Action.RETRY:
            return RateLimitedError(verdict.reason)
        if verdict.action is guard.Action.REJECT:
            return PostRejectedError(verdict.reason)
        return ChallengeError(verdict.reason)

    async def _page_verdict(self, page: Page) -> guard.Verdict | None:
        """Read a challenge URL, challenge overlay or alert text from the page.

        The challenge selector probe is essential: an Arkose challenge can be an
        iframe with no text in the top document, so body text alone would miss
        it and the run would end UNKNOWN instead of parking. Text classification
        is strict and scoped to alert/dialog/toast containers, never the whole
        body, because a timeline post may contain words like "captcha" or
        "authenticate" without any challenge being present.
        """
        try:
            url_verdict = guard.classify_url(page.url)
        except PlaywrightError:
            url_verdict = None
        if url_verdict is not None:
            return url_verdict
        if await sessions.challenge_visible(page):
            return guard.Verdict(
                action=guard.Action.PARK,
                code=None,
                reason=(
                    "X is showing a verification challenge during posting; a human "
                    "must solve it before automation resumes."
                ),
                kind="challenge",
            )
        text = await self._alert_text(page)
        if not text:
            return None
        return guard.classify_text(text, strict=True)

    async def _alert_text(self, page: Page) -> str:
        """Visible text of alert-like containers only, never the whole body."""
        alerts = page.locator(_ALERT_SELECTORS)
        try:
            total = await alerts.count()
        except PlaywrightError:
            return ""
        chunks: list[str] = []
        for index in range(min(total, 10)):
            try:
                text = await alerts.nth(index).inner_text()
            except PlaywrightError:
                continue
            if text.strip():
                chunks.append(text)
        return "\n".join(chunks)

    async def _check_page_health(self, page: Page) -> None:
        """Park on a challenge/URL verdict before doing more work on the page."""
        verdict = await self._page_verdict(page)
        if verdict is not None:
            self._raise_for_verdict(verdict)

    async def _open_composer(self, page: Page) -> Locator:
        """Open /compose/post, wait for the editor, and return the modal root."""
        await page.goto(
            f"{self.config.base_url.rstrip('/')}/compose/post",
            wait_until="domcontentloaded",
        )
        await self._check_page_health(page)
        await self._wait_for_composer_ready(page)
        return await selectors.locate(
            page, "composer_root", timeout_ms=self.config.default_timeout_ms
        )

    async def _wait_for_composer_ready(self, page: Page) -> None:
        """Wait for a visible composer editor before any selector lookup.

        X renders the route shell first and mounts the composer modal later, so
        the first selector call on a cold page can fall inside a short candidate
        budget and fail with a false "UI changed". The shared wait gives the
        mount 15 s and polls with the injected sleep, so tests stay fast.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _COMPOSER_READY_TIMEOUT_S
        while True:
            editors = await selectors.locate_all(page, "composer_textarea_0")
            for editor in editors:
                try:
                    if await editor.is_visible():
                        return
                except PlaywrightError:
                    continue
            if loop.time() >= deadline:
                raise UiChangedError(
                    "The composer editor did not become visible within "
                    f"{_COMPOSER_READY_TIMEOUT_S:.0f} s; the page may not have "
                    "finished loading or the composer markup changed."
                )
            await self._sleep(_COMPOSER_READY_POLL_S)

    async def _assert_editor_in_modal(self, root: Locator, editor: Locator) -> None:
        """Refuse to type into an editor outside the compose modal.

        The live classic page has a home-timeline inline composer *and* the
        compose modal, both exposing ``tweetTextarea_0``. Typing into the inline
        one silently composes nothing and then targets the wrong Post button;
        this containment check turns that into a UI error before a keystroke.
        """
        try:
            root_handle = await root.element_handle()
        except PlaywrightError as exc:
            raise UiChangedError(f"The composer dialog is not attached: {exc}.") from exc
        if root_handle is None:
            raise UiChangedError("The composer dialog disappeared before typing.")
        try:
            inside = await editor.evaluate(
                """(editor, root) => {
                    if (!root || !root.contains(editor)) {
                        return false;
                    }
                    const modal = root.closest('[role="dialog"][aria-modal="true"]')
                        || (root.matches && root.matches('[role="dialog"][aria-modal="true"]')
                            ? root : null);
                    if (!modal) {
                        return true;
                    }
                    const editorModal = editor.closest('[role="dialog"][aria-modal="true"]');
                    return !!editorModal && (editorModal === modal || modal.contains(editor));
                }""",
                root_handle,
            )
        except PlaywrightError as exc:
            raise UiChangedError(f"The composer editor could not be checked: {exc}.") from exc
        finally:
            with contextlib.suppress(PlaywrightError):
                await root_handle.dispose()
        if not inside:
            raise UiChangedError(
                "The composer editor is outside the compose modal (an inline timeline "
                "composer may have been picked); refusing to type into it."
            )

    async def _textarea_for(self, page: Page, index: int) -> Locator:
        if index == 0:
            return await selectors.locate(
                page, "composer_textarea_0", timeout_ms=self.config.default_timeout_ms
            )
        boxes = await selectors.locate_all(page, "composer_textarea_n")
        if index >= len(boxes):
            raise UiChangedError(
                f"The composer shows {len(boxes)} text boxes but tweet {index + 1} "
                "needs one; the 'Add another post' click may not have added a post."
            )
        return boxes[index]

    async def _composer_submit(self, page: Page, *, thread_mode: bool) -> Locator:
        name = "composer_post_all_button" if thread_mode else "composer_post_button"
        button = await selectors.locate(page, name, timeout_ms=self.config.default_timeout_ms)
        if thread_mode:
            # "Post all" has a "Schedule" fallback in the selector table; a
            # scheduled thread is not a posted thread, so refuse to click a
            # button that does not say exactly what we intend to do.
            try:
                label = (await button.inner_text()).strip()
            except PlaywrightError as exc:
                raise UiChangedError(
                    "The 'Post all' button could not be read; refusing to click a "
                    "button that might schedule the thread instead of posting it."
                ) from exc
            if label != "Post all":
                raise UiChangedError(
                    f"The thread submit button reads {label!r}, not 'Post all'; "
                    "refusing to click because a fallback could schedule the thread."
                )
        return button

    async def _reply_submit(self, page: Page) -> Locator:
        """Locate the reply composer's submit control.

        The reply composer labels its submit "Reply", not "Post"; the logical
        name set only covers the post composer, so a role lookup scoped to the
        dialog is the fallback. It must be scoped or it would match the tweet's
        own Reply action button behind the modal.
        """
        try:
            root = await selectors.locate(
                page, "composer_root", timeout_ms=self.config.default_timeout_ms
            )
        except UiChangedError:
            return await selectors.locate(
                page, "composer_post_button", timeout_ms=self.config.default_timeout_ms
            )
        reply = root.get_by_role("button", name="Reply", exact=True)
        if await reply.count() > 0:
            return reply.first
        return await selectors.locate(
            page, "composer_post_button", timeout_ms=self.config.default_timeout_ms
        )

    async def _refuse_disabled(self, button: Locator, thread: Thread) -> None:
        """Fail before clicking when the client refuses the content itself.

        Media uploads keep the button disabled until X answers, so a short
        grace period separates "not ready yet" from "this content is refused".
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _ENABLE_WAIT_S
        while True:
            try:
                disabled = await button.is_disabled()
            except PlaywrightError as exc:
                raise UiChangedError(
                    "The Post button could not be inspected (detached or covered); "
                    "the composer markup may have changed."
                ) from exc
            if not disabled:
                return
            if loop.time() >= deadline:
                break
            await asyncio.sleep(_POLL_INTERVAL_S)
        weights = [splitter.weighted_length(tweet.text) for tweet in thread.tweets]
        weight = max(weights) if weights else 0
        limit = self.config.max_tweet_weight
        if weight > limit:
            raise PostRejectedError(
                f"X disabled the Post button: a tweet weighs {weight} characters "
                f"against the {limit} limit (over by {weight - limit})."
            )
        raise UiChangedError(
            "The Post button stayed disabled after the thread was typed; the "
            "composer rejected the content or its markup changed. Inspect the artifacts."
        )

    async def _verify_typed_text(
        self, page: Page, editor: Locator, expected: str, attempt: _Attempt
    ) -> None:
        """Compare the editor with the intended text before any Post click.

        The fake and the field report both show how a composer can silently
        lose input. A mismatch is a UI/content failure, not an unknown post:
        nothing was submitted, so the ledger is marked failed and the caller
        gets a typed error with evidence.
        """
        actual = await _read_editor_text(editor)
        if _normalise_editor_text(actual) == _normalise_editor_text(expected):
            return
        summary = _typed_mismatch_summary(expected, actual)
        artifacts = await self._capture_artifacts(page, attempt.label or "typed")
        if artifacts is not None:
            with contextlib.suppress(OSError, ValueError):
                self.store.write_artifact(artifacts, "typed_mismatch.txt", summary)
        raise UiChangedError(
            "The composer did not keep the text that was typed, so Post is not "
            "being clicked. Inspect typed_mismatch.txt in the artifacts.",
            artifacts,
        )

    async def _type(self, page: Page, text: str) -> None:
        """Type one tweet with real key events at the planned cadence.

        Never ``keyboard.insert_text``: that is one synthetic input event, and
        X's React composer leaves Post ``aria-disabled="true"`` for text that
        arrives by paste or script (field report, March 2026). ``keyboard.type``
        dispatches the per-character events the composer listens for and still
        handles characters that are not on the keyboard layout (emoji, flags,
        combining marks). Line breaks are **Shift+Return**, never a bare
        Return: a bare Return submits on x.com, so a newline typo could publish
        half a thread. Shift+Return is **verified live** (2026-11-10): two
        presses produced one blank line with no early submit.
        """
        steps = self._humanizer.typing_plan(text)
        typed = ""
        for step in steps:
            chunk = step.text
            if not chunk:
                continue
            if chunk in ("\n", "\r", "\r\n") and ends_with_typeahead_token(_current_line(typed)):
                # The line is complete: close the typeahead with a space, never
                # with Return/Tab/Escape (Escape closes the whole composer and a
                # Return would select the suggestion instead of breaking the
                # line). Typing the space before the newline keeps the token
                # intact; checking before the token's last character would split
                # the handle.
                await page.keyboard.type(" ")
                typed += " "
            if chunk in ("\n", "\r", "\r\n"):
                await page.keyboard.press("Shift+Enter")
            else:
                await page.keyboard.type(chunk)
            typed += chunk
            await self._sleep(step.delay_ms / 1000.0)
        if ends_with_typeahead_token(_current_line(typed)):
            # A mention at the very end of the tweet also leaves the dropdown
            # open; the same harmless space closes it before Post is clicked.
            await page.keyboard.type(" ")

    async def _attach_media(
        self, page: Page, media: Sequence[Path], alt_texts: Sequence[str] = ()
    ) -> None:
        """Attach files, wait for X's upload to finish, then add alt text.

        X starts uploading the moment the file is chosen and keeps Post disabled
        until every preview is decoded. Waiting for the editor's attachment area
        and the button here means a slow image cannot turn into a click on a
        disabled button or an unexplained timeout later; the upload happens
        before any Post click, so a failure is a normal failed run, never an
        unknown outcome.
        """
        if not media:
            return
        field = await selectors.locate(
            page,
            "composer_media_input",
            timeout_ms=self.config.default_timeout_ms,
            visible=False,
        )
        # The baseline is taken after the target editor has focus and right
        # before the files are set, so an empty or invisible attachments
        # container can never contribute to it.
        before = await self._count_media_previews(page)
        try:
            await field.set_input_files([str(path) for path in media])
        except PlaywrightError as exc:
            raise UiChangedError(f"The media file input rejected the selection: {exc}.") from exc
        timeout_s = _upload_timeout_s(media)
        await self._wait_for_media_ready(
            page, before=before, expected=before + len(media), timeout_s=timeout_s
        )
        descriptions = tuple(alt_texts)[: len(media)]
        if any(text.strip() for text in descriptions):
            await self._apply_alt_texts(page, descriptions, skip=before)

    async def _wait_for_media_ready(
        self, page: Page, *, before: int, expected: int, timeout_s: float
    ) -> None:
        """Wait for previews, decoded images, no progressbar and an enabled Post.

        ``before`` is the media-image count taken immediately before the files
        were set. If the count never moves while a usable attachment (controls
        plus an enabled Post) is already on screen, the counter itself is
        wrong; report that explicitly instead of hiding it behind the upload
        budget.
        """
        started = self._now()
        while True:
            count = await self._count_media_previews(page)
            if count > expected:
                raise UiChangedError(
                    f"The composer shows {count} media previews where {expected} "
                    "were expected; refusing to post an unexpected attachment."
                )
            progress = 0
            if count == expected:
                progress = await self._attachment_progress_count(page)
                if (
                    progress == 0
                    and await self._previews_loaded(page)
                    and await self._submit_enabled(page)
                ):
                    return
            elapsed = (self._now() - started).total_seconds()
            if (
                elapsed >= _MEDIA_UNCOUNTED_GRACE_S
                and count == before
                and await self._has_attachment_controls(page)
                and await self._submit_enabled(page)
            ):
                diagnostics = await self._media_diagnostics(page)
                raise UiChangedError(
                    "An attachment appeared but could not be counted (the preview "
                    f"is not inside the modal's attachments area); {diagnostics}. "
                    "Post was not clicked."
                )
            if elapsed >= timeout_s:
                diagnostics = await self._media_diagnostics(page)
                raise UiChangedError(
                    f"The media upload did not finish within {timeout_s:.0f} s "
                    f"({count} preview(s), {progress} upload indicator(s)); "
                    f"{diagnostics}. Post was not clicked."
                )
            await self._sleep(_UPLOAD_POLL_S)

    async def _has_attachment_controls(self, page: Page) -> bool:
        """Whether the modal shows X's Edit/Remove media controls."""
        try:
            return await page.locator(_MEDIA_ATTACHMENT_CONTROLS).count() > 0
        except PlaywrightError:
            return False

    async def _media_images(self, page: Page) -> list[Locator]:
        """Every ``img`` inside the modal's attachments area, attached or not.

        This deliberately does not use the logical ``composer_media_preview``
        chain: it is visible-first and, while the empty attachments container
        has no box, falls through to generic modal images such as the account
        avatar. Only images that are descendants of a real attachments/Media
        container are media; an image with a non-empty ``alt`` outside those
        containers (an avatar) is never counted.
        """
        for selector in (_MEDIA_IMAGES_PRIMARY, _MEDIA_IMAGES_FALLBACK):
            try:
                total = await page.locator(selector).count()
            except PlaywrightError:
                continue
            if not total:
                continue
            images: list[Locator] = []
            for index in range(total):
                image = page.locator(selector).nth(index)
                try:
                    inside = await image.evaluate(
                        """(img) => {
                            const container = img.closest('[data-testid="attachments"]')
                                || img.closest('[role="group"][aria-label="Media"]');
                            const modal = img.closest('[role="dialog"][aria-modal="true"]');
                            return !!(container && modal);
                        }"""
                    )
                except PlaywrightError:
                    continue
                if inside is True:
                    images.append(image)
            if images:
                return images
        return []

    async def _count_media_previews(self, page: Page) -> int:
        """Count image previews inside the modal's attachments areas."""
        return len(await self._media_images(page))

    async def _attachment_progress_count(self, page: Page) -> int:
        """Best-effort count of upload indicators inside the attachments area.

        Scoped to ``[data-testid="attachments"]`` so the composer footer's
        always-present character counter is never mistaken for an upload.
        """
        try:
            return await page.locator(_ATTACHMENT_PROGRESS).count()
        except PlaywrightError:
            return 0

    async def _previews_loaded(self, page: Page) -> bool:
        """True when every attachment preview is usable.

        A decoded image (``naturalWidth``/``naturalHeight`` above zero) is the
        normal case. X can also paint a blob preview through a sibling
        background layer while the ``img`` itself reports zero natural size;
        for a blob/data source a non-empty on-screen box plus the real
        Edit/Remove media control is equivalent proof. The Post-enabled gate is
        checked separately, so a still-uploading blob cannot slip through.
        """
        images = await self._media_images(page)
        if not images:
            return False
        for image in images:
            try:
                state = await image.evaluate(
                    """(img) => {
                        const rect = img.getBoundingClientRect();
                        const container = img.closest('[role="group"][aria-label="Media"]')
                            || img.closest('[data-testid="attachments"]');
                        const control = !!(container && container.querySelector(
                            'button[aria-label="Edit media"], button[aria-label="Remove media"]'
                        ));
                        return {
                            src: img.currentSrc || img.src || '',
                            complete: img.complete,
                            naturalWidth: img.naturalWidth,
                            naturalHeight: img.naturalHeight,
                            width: rect.width,
                            height: rect.height,
                            control: control,
                        };
                    }"""
                )
            except PlaywrightError:
                return False
            width = float(state["width"])
            height = float(state["height"])
            if width <= 0 or height <= 0:
                return False
            natural_width = int(state["naturalWidth"])
            natural_height = int(state["naturalHeight"])
            if bool(state["complete"]) and natural_width > 0 and natural_height > 0:
                continue
            scheme = str(state["src"]).split(":", 1)[0].lower()
            if scheme in {"blob", "data"} and width > 0 and height > 0 and state["control"]:
                continue
            return False
        return True

    async def _media_diagnostics(self, page: Page) -> str:
        """One line per preview plus the Post state, for a timeout message.

        Only the source *scheme* is reported (never a full blob/data URL), so a
        token or an account-specific URL cannot leak into an error or the
        artifacts.
        """
        images = await self._media_images(page)
        entries: list[str] = []
        for image in images:
            try:
                state = await image.evaluate(
                    """(img) => {
                        const rect = img.getBoundingClientRect();
                        const src = img.currentSrc || img.src || '';
                        return {
                            src: (src.split(':')[0] || 'none').toLowerCase(),
                            complete: img.complete,
                            naturalWidth: img.naturalWidth,
                            naturalHeight: img.naturalHeight,
                            bbox: `${Math.round(rect.width)}x${Math.round(rect.height)}`,
                        };
                    }"""
                )
            except PlaywrightError:
                entries.append("src=detached")
                continue
            entries.append(
                f"src={state['src']} complete={state['complete']} "
                f"natural={state['naturalWidth']}x{state['naturalHeight']} "
                f"bbox={state['bbox']}"
            )
        enabled = await self._submit_enabled(page)
        joined = "; ".join(entries) if entries else "no preview images"
        return f"images=[{joined}]; post_enabled={enabled}"

    async def _submit_enabled(self, page: Page) -> bool:
        """Whether the current composer's submit control is enabled.

        A missing button is treated as enabled so the upload wait cannot block
        on a markup variant; the later ``_refuse_disabled`` gate still refuses
        a button that is actually disabled.
        """
        try:
            button = await selectors.locate(page, "composer_post_button", timeout_ms=800)
        except UiChangedError:
            return True
        try:
            return not await button.is_disabled()
        except PlaywrightError:
            return False

    async def _apply_alt_texts(
        self, page: Page, alt_texts: Sequence[str], *, skip: int = 0
    ) -> None:
        """Describe the requested images through X's alt editor before posting.

        ``alt_texts`` is aligned with the batch just attached; blank entries
        mean "no description for this image" and must never open any alt UI or
        make the precheck demand a control. ``skip`` is the number of images
        that already belonged to earlier tweets in the same composer, so a
        thread's descriptions stay with their own image.

        The live description screen has never been observed end to end (the
        text field and Save of "Edit image description" are unverified), so
        every missing element is a typed failure before the Post click, and the
        first-run education sheet is dismissed with "No, thanks" only.
        """
        requested = [(index, text.strip()) for index, text in enumerate(alt_texts) if text.strip()]
        if not requested:
            return
        buttons = await selectors.locate_all(page, "composer_alt_button")
        images = await self._media_images(page)
        batch = images[skip : skip + len(alt_texts)]
        highest = max(index for index, _ in requested)

        controls: dict[int, Locator] = {}
        for index, _ in requested:
            image = batch[index] if index < len(batch) else None
            if image is not None:
                control = await self._alt_control_for_image(page, image)
                if control is not None:
                    controls[index] = control
        if len(controls) < len(requested):
            # Fall back to the global ordered list, but only require controls
            # for the non-blank images that were actually requested.
            if len(buttons) < skip + highest + 1:
                raise UiChangedError(
                    f"The composer shows {len(buttons)} description control(s), "
                    f"but image {skip + highest + 1} needs one; the media editor "
                    "markup changed."
                )
            for index, _ in requested:
                controls.setdefault(index, buttons[skip + index])

        for index, alt_text in requested:
            await self._describe_image(page, controls[index], alt_text)

    async def _alt_control_for_image(self, page: Page, image: Locator) -> Locator | None:
        """The description control inside the tile/Media group of ``image``."""
        try:
            container = await image.evaluate_handle(
                """(img) => img.closest('[role="group"][aria-label="Media"]')
                    || img.closest('[data-testid="attachments"]')"""
            )
        except PlaywrightError:
            return None
        element = container.as_element()
        if element is None:
            await container.dispose()
            return None
        try:
            control_handle = await element.query_selector(
                '[data-testid="altTextWrapper"], button[aria-label="Add description"]'
            )
            if control_handle is None:
                control_handle = await element.query_selector(':text-is("Add description")')
            if control_handle is None:
                return None
            buttons = await selectors.locate_all(page, "composer_alt_button")
            for button in buttons:
                try:
                    handle = await button.element_handle()
                except PlaywrightError:
                    continue
                if handle is None:
                    continue
                try:
                    same = await control_handle.evaluate("(a, b) => a === b", handle)
                finally:
                    await handle.dispose()
                if same is True:
                    return button
            return None
        except PlaywrightError:
            return None
        finally:
            await element.dispose()

    async def _describe_image(self, page: Page, control: Locator, alt_text: str) -> None:
        """Open the description editor, type, verify, save and confirm closed."""
        images_before = await self._count_media_previews(page)
        await self._click(page, control)
        if await selectors.is_present(page, "alt_intro_sheet", timeout_ms=1_000):
            dismiss = await selectors.locate(
                page, "alt_intro_dismiss_button", timeout_ms=self.config.default_timeout_ms
            )
            await self._click(page, dismiss)
        await selectors.locate(page, "alt_editor_title", timeout_ms=self.config.default_timeout_ms)
        field = await self._alt_text_field(page)
        await self._click(page, field)
        await self._type(page, alt_text)
        actual = await _read_editor_text(field)
        if _normalise_editor_text(actual) != _normalise_editor_text(alt_text):
            raise UiChangedError(
                "The image-description editor did not keep the alt text that was "
                "typed; refusing to post."
            )
        save = await selectors.locate(
            page, "alt_editor_save_button", timeout_ms=self.config.default_timeout_ms
        )
        await self._click(page, save)
        await self._wait_for_alt_editor_closed(page)
        if await self._count_media_previews(page) != images_before:
            raise UiChangedError(
                "The image disappeared while saving its description; refusing to post."
            )

    async def _alt_text_field(self, page: Page) -> Locator:
        """Locate the description textarea, scoped to the editor layer.

        ``composer_alt_text_input``'s ``:has(:text-is(...))`` can match every
        ancestor of the title, including ``body``; on the new client that makes
        it resolve to the composer's own textarea. Accept the logical match only
        when it is inside the editor dialog, otherwise scope to that dialog.
        """
        try:
            field = await selectors.locate(
                page, "composer_alt_text_input", timeout_ms=self.config.default_timeout_ms
            )
        except UiChangedError:
            field = None
        if field is not None:
            try:
                inside = await field.evaluate(
                    """(el) => {
                        const dialog = el.closest('[role="dialog"]');
                        return !!dialog
                            && dialog.textContent.includes('Edit image description');
                    }"""
                )
            except PlaywrightError:
                inside = False
            if inside is True:
                return field
        editor = page.locator('[role="dialog"]:has(:text-is("Edit image description"))').first
        try:
            await editor.wait_for(state="visible", timeout=self.config.default_timeout_ms)
        except PlaywrightError as exc:
            raise UiChangedError(f"The image-description editor did not appear: {exc}.") from exc
        area = editor.locator("textarea, [role='textbox'], [contenteditable='true']").first
        try:
            await area.wait_for(state="visible", timeout=self.config.default_timeout_ms)
        except PlaywrightError as exc:
            raise UiChangedError(f"The image-description editor has no text field: {exc}.") from exc
        return area

    async def _wait_for_alt_editor_closed(self, page: Page) -> None:
        started = self._now()
        while True:
            if not await selectors.is_present(page, "alt_editor_title", timeout_ms=300):
                return
            if (self._now() - started).total_seconds() >= self.config.default_timeout_ms / 1000.0:
                raise UiChangedError(
                    "The image-description editor did not close after Save; refusing to post."
                )
            await self._sleep(_UPLOAD_POLL_S)

    async def _pre_submit(self, page: Page) -> None:
        """Scroll like a reader and dwell before committing the post."""
        for delta, pause_s in self._humanizer.scroll_plan():
            await page.mouse.wheel(0, delta)
            await self._sleep(pause_s)
        await self._pause("pre_post")

    async def _submit_by_shortcut(self, page: Page, editor: Locator) -> None:
        """Submit with Meta+Enter (macOS) or Control+Enter elsewhere.

        Used only when the opt-in fallback is on and both click paths failed
        before dispatching anything. The click barrier is already persisted by
        the caller and stays up, because a keystroke may submit.
        """
        try:
            await editor.focus(timeout=self.config.default_timeout_ms)
        except PlaywrightError as exc:
            raise ClickNotDispatchedError(
                f"The editor could not be focused for the submit shortcut: {exc}."
            ) from exc
        await page.keyboard.press(f"{_SUBMIT_SHORTCUT_MODIFIER}+Enter")

    async def _click(self, page: Page, locator: Locator) -> None:
        start = self._mouse if self._mouse is not None else _initial_pointer(page)
        self._mouse = await human_click(
            page,
            locator,
            self._humanizer,
            start=start,
            sleep=self._sleep,
        )

    async def _pause(self, kind: str) -> None:
        await self._humanizer.pause(kind, sleep=self._sleep)

    async def _discard_composer(self, page: Page) -> None:
        """Close a rehearsed draft without ever choosing "Save"."""
        try:
            close = await selectors.locate(page, "composer_close_button", timeout_ms=3_000)
        except UiChangedError:
            return
        await self._click(page, close)
        if await selectors.is_present(page, "draft_save_cancel_button", timeout_ms=3_000):
            cancel = await selectors.locate(page, "draft_save_cancel_button", timeout_ms=3_000)
            await self._click(page, cancel)
            return
        discard = page.get_by_role("button", name="Discard", exact=True)
        if await discard.count() > 0:
            await self._click(page, discard.first)

    async def _delete_one(self, page: Page, tweet: PostedTweet) -> None:
        await page.goto(self._status_ref(tweet), wait_until="domcontentloaded")
        await self._check_page_health(page)
        await selectors.locate(page, "tweet_article", timeout_ms=self.config.default_timeout_ms)
        watcher = _ResponseWatcher(page, deletion=True)
        watcher.start()
        try:
            scope = await self._focal_scope(page, tweet.id)
            more = await selectors.locate(scope, "tweet_more_button")
            await self._click(page, more)
            item = await selectors.locate(page, "menu_delete_item")
            await self._click(page, item)
            confirm = await selectors.locate(page, "delete_confirm_button")
            await self._click(page, confirm)
            with contextlib.suppress(TimeoutError):
                await watcher.wait_for_deleted(1, _DELETE_WAIT_S)
            if watcher.deleted:
                return
            verdict = watcher.last_verdict() or await self._page_verdict(page)
            if verdict is not None:
                self._raise_for_verdict(verdict)
            raise UiChangedError(
                f"The delete of tweet {tweet.id} was never confirmed by X; "
                "check the profile — it may still be there."
            ) from None
        finally:
            watcher.stop()

    def _stored_thread(self, thread_hash: str) -> PostedThread:
        entry = self.ledger.get(thread_hash)
        if entry is None:
            raise PostOutcomeUnknownError(
                "The ledger said this thread was posted but its record vanished; "
                "check the profile before posting again."
            )
        if entry.completed is not None:
            return entry.completed
        return PostedThread(
            thread_hash=thread_hash,
            handle=entry.handle,
            strategy=entry.strategy,
            posted_at=entry.updated_at,
            tweets=tuple(sorted(entry.posted, key=lambda item: item.index)),
            complete=True,
        )

    def _entry_posted(self, thread_hash: str) -> tuple[PostedTweet, ...]:
        entry = self.ledger.get(thread_hash)
        return tuple(entry.posted) if entry is not None else ()

    def _distinct_mentions(self, thread: Thread) -> list[str]:
        """Distinct handles across the thread, ignoring the posting account."""
        own = self.config.handle.lower() if self.config.handle else None
        handles: list[str] = []
        for tweet in thread.tweets:
            for handle in extract_mentions(tweet.text):
                if own is not None and handle == own:
                    continue
                if handle not in handles:
                    handles.append(handle)
        return handles

    def _validate_thread(self, thread: Thread) -> None:
        """Refuse content X cannot accept before Chrome is launched."""
        if len(thread.tweets) > _MAX_THREAD_TWEETS:
            raise ValidationError(
                f"This thread has {len(thread.tweets)} tweets; X's composer allows "
                f"at most {_MAX_THREAD_TWEETS}. Split it into two threads."
            )
        if not self._allow_alt_text and any(
            alt.strip() for tweet in thread.tweets for alt in tweet.alt_texts
        ):
            raise ValidationError(
                "alt text is not verified against the live site yet; pass "
                "allow_alt_text=True / --allow-alt to try it on a test account"
            )
        mentions = self._distinct_mentions(thread)
        cap = self.config.max_mentions_per_thread
        if len(mentions) > cap:
            raise ValidationError(
                f"{len(mentions)} distinct @mentions exceed "
                f"max_mentions_per_thread={cap}. X treats unsolicited automated "
                "mentions as spam "
                "(https://help.x.com/en/rules-and-policies/x-automation); mention "
                "only accounts that expect it, or raise the cap deliberately on a "
                "test account."
            )
        for index, tweet in enumerate(thread.tweets, start=1):
            splitter.validate_tweet(tweet.text, limit=self.config.max_tweet_weight)
            if len(tweet.media) > self.config.max_media_per_tweet:
                raise ValidationError(
                    f"Tweet {index} has {len(tweet.media)} media files; X allows at "
                    f"most {self.config.max_media_per_tweet} per post."
                )
            validate_media(tweet.media, tweet_number=index)

    async def _capture_artifacts(self, page: Page, label: str) -> Path | None:
        """Save screenshot, sanitised HTML and query-free URL for a UI failure.

        The HTML is sanitised before it is written (hidden inputs and CSRF/meta
        values are redacted), every file is 0600, and the URL is stored without
        its query string so a token in a query parameter cannot be persisted.
        Cookie values are never copied by this method.
        """
        try:
            directory = self.store.new_artifacts_dir(label)
        except OSError:
            return None
        screenshot = directory / "screenshot.png"
        with contextlib.suppress(PlaywrightError, OSError):
            await page.screenshot(path=str(screenshot), full_page=False)
            with contextlib.suppress(OSError):
                self.store.protect_file(screenshot)
        with contextlib.suppress(PlaywrightError, OSError, ValueError):
            html = await page.content()
            self.store.write_artifact(directory, "page.html", html, sanitise=True)
        with contextlib.suppress(PlaywrightError, OSError, ValueError):
            self.store.write_artifact(directory, "url.txt", _query_free_url(page.url))
        return directory

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("XPoster clock must return a timezone-aware datetime.")
        return value.astimezone(UTC)

    def _stamp(self) -> str:
        return self._now().isoformat()
