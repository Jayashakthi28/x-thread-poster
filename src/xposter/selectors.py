"""Logical x.com selectors, classic-DOM first with new-client fallbacks.

``docs/research/selectors.json`` records the logged-out bundle study, and the
table is embedded here so the installed package is self-contained and never
depends on the source tree. The authenticated site was VERIFIED LIVE on
2026-11-09 to serve the classic responsive-web DOM (evidence:
``tests/fixtures/live_compose_dialog_2026-11-09.html``), so every logical element
lists its classic ``data-testid`` candidates first. The new-client candidates
(role, ARIA label, exact visible text) from the ``x-web`` study remain as
fallbacks for the case X ever serves that client to logged-in users. Composer
candidates are scoped to the innermost ``[role="dialog"][aria-modal="true"]``:
``/compose/post`` also keeps the home inline composer behind the mask, and an
unscoped selector can resolve to that background editor.

Only Playwright's CSS engine (with the ``:has()``, ``:has-text()`` and
``:text-is()`` extensions) plus the ``role=`` and ``text=`` engines are used.
Positional XPath paths break as soon as X inserts a wrapper element and cannot
be diffed against the live DOM by ``xposter doctor --dom``.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from .errors import UiChangedError

_MIN_CANDIDATE_TIMEOUT_MS = 400
_POLL_INTERVAL_S = 0.05

Scope = Page | Locator
"""Anything Playwright can run a CSS selector against."""


@dataclass(frozen=True, slots=True)
class SelectorSpec:
    """One logical UI element and the ordered selectors that may match it."""

    name: str
    primary: str
    fallbacks: tuple[str, ...]
    doc: str = ""

    @property
    def candidates(self) -> tuple[str, ...]:
        """Every selector to try, primary first."""
        return (self.primary, *self.fallbacks)


_SPECS: tuple[SelectorSpec, ...] = (
    SelectorSpec(
        "composer_root",
        '[role="dialog"][aria-modal="true"]:has([data-testid="tweetTextarea_0"])',
        (
            '[role="dialog"]:has([data-testid="tweetTextarea_0"])',
            '[role="dialog"]:has([contenteditable="true"][role="textbox"])',
            '[role="dialog"]:has([data-modal-slot-surface])',
            "[data-modal-slot-surface]",
            '[role="dialog"]',
        ),
        "The post composer modal. The innermost aria-modal dialog is first because "
        "/compose/post keeps a second inline composer behind the mask; unscoped "
        "dialog candidates can resolve to the invisible outer wrapper.",
    ),
    SelectorSpec(
        "composer_textarea_0",
        '[role="dialog"][aria-modal="true"] [data-testid="tweetTextarea_0"]',
        (
            '[role="dialog"][aria-modal="true"] [contenteditable="true"][role="textbox"]',
            '[data-testid="tweetTextarea_0"]',
            '[role="dialog"] [contenteditable="true"][role="textbox"]',
            '[role="dialog"] textarea[aria-multiline="true"]',
            'textarea[aria-multiline="true"]',
            '[role="dialog"] textarea',
        ),
        "Main text box of the first composer item, scoped to the modal so the "
        "background inline composer can never be typed into. The classic client "
        "renders a contenteditable role=textbox, not a textarea.",
    ),
    SelectorSpec(
        "composer_textarea_n",
        (
            '[role="dialog"][aria-modal="true"] '
            '[data-testid^="tweetTextarea_"][contenteditable="true"]'
        ),
        (
            '[role="dialog"][aria-modal="true"] [contenteditable="true"][role="textbox"]',
            '[role="dialog"] [data-testid^="tweetTextarea_"][contenteditable="true"]',
            '[data-testid^="tweetTextarea_"]',
            '[role="dialog"] textarea[aria-multiline="true"]',
        ),
        "All thread text boxes in DOM order; call .nth(n) for item n. The modal "
        "scope excludes the inline composer and the prefix is narrowed to "
        "contenteditable so the label and rich-text wrappers do not count.",
    ),
    SelectorSpec(
        "composer_post_button",
        '[role="dialog"][aria-modal="true"] [data-testid="tweetButton"]',
        (
            '[data-testid="tweetButton"]',
            '[data-testid="tweetButtonInline"]',
            '[role="dialog"] button:text-is("Post")',
            '[role="dialog"] button[type="submit"]',
        ),
        "Submits a single post (or a reply) from the composer modal.",
    ),
    SelectorSpec(
        "composer_post_all_button",
        '[role="dialog"][aria-modal="true"] [data-testid="tweetButton"]',
        (
            '[role="dialog"][aria-modal="true"] button:text-is("Post all")',
            '[data-testid="tweetButton"]',
            '[role="dialog"] button:text-is("Post all")',
        ),
        "Submits every item of a multi-post thread at once. The classic client reuses "
        "tweetButton for a thread and changes its label to 'Post all', so the poster "
        "verifies the exact label before clicking. Schedule is never a fallback.",
    ),
    SelectorSpec(
        "composer_add_post_button",
        '[role="dialog"][aria-modal="true"] [data-testid="addButton"]',
        (
            '[role="dialog"][aria-modal="true"] button[aria-label="Add post"]',
            '[role="dialog"][aria-modal="true"] button[aria-label="Add another post"]',
            '[data-testid="addButton"]',
            'button[aria-label="Add post"]',
            '[role="dialog"] button[aria-label="Add another post"]',
            '[role="dialog"] button[aria-label="Add Reply"]',
        ),
        "Appends the next composer item. The classic add button only appears after "
        "the first text is typed, so an empty composer legitimately has none.",
    ),
    SelectorSpec(
        "composer_media_button",
        (
            '[role="dialog"][aria-modal="true"] [data-testid="toolBar"] '
            'button[aria-label="Add photos or video"]'
        ),
        (
            '[role="dialog"][aria-modal="true"] button[aria-label="Add photos or video"]',
            '[role="dialog"][aria-modal="true"] button[aria-label="Add media"]',
            '[role="dialog"][aria-modal="true"] button:has(svg[data-icon="icon-photo-plus"])',
            '[data-testid="toolBar"] button[aria-label="Add photos or video"]',
            '[role="dialog"] button[aria-label="Add media"]',
            '[role="dialog"] button:has(svg[data-icon="icon-photo-plus"])',
        ),
        "Visible media picker button in the modal toolbar; the file input next to it is hidden.",
    ),
    SelectorSpec(
        "composer_media_input",
        '[role="dialog"][aria-modal="true"] [data-testid="fileInput"]',
        (
            '[role="dialog"][aria-modal="true"] input[type="file"]',
            '[data-testid="fileInput"]',
            '[role="dialog"] input[type="file"]',
            'input[type="file"][multiple]',
            'input[type="file"][accept*="image/jpeg"]',
        ),
        "Hidden file input in the modal. The classic client hides it with opacity "
        "and a 0.1px size, so always locate it with visible=False and set_input_files.",
    ),
    SelectorSpec(
        "composer_media_preview",
        '[role="dialog"][aria-modal="true"] [data-testid="attachments"] img',
        (
            '[role="dialog"][aria-modal="true"] [data-testid="attachments"]',
            '[role="dialog"][aria-modal="true"] [role="group"][aria-label="Media"] img',
        ),
        "An attached media image or its container. Every candidate is anchored to "
        "the attachments region; a generic `img` candidate must never be used "
        "because the modal also contains the account avatar.",
    ),
    SelectorSpec(
        "composer_media_remove_button",
        (
            '[role="dialog"][aria-modal="true"] [data-testid="attachments"] '
            'button[aria-label="Remove media"]'
        ),
        (
            '[role="dialog"][aria-modal="true"] '
            '[role="group"][aria-label="Media"] button[aria-label="Remove media"]',
            '[role="dialog"][aria-modal="true"] button[aria-label="Remove media"]',
        ),
        "Removes one attached media tile; anchored to the attachments region or "
        "scoped to the composer modal.",
    ),
    SelectorSpec(
        "composer_alt_button",
        '[role="dialog"][aria-modal="true"] :text-is("Add description")',
        (
            '[role="dialog"][aria-modal="true"] button[aria-label="Add description"]',
            '[role="dialog"][aria-modal="true"] [data-testid="altTextWrapper"]',
            '[role="dialog"][aria-modal="true"] [data-testid="attachments"] '
            ':text-is("Add description")',
            '[role="dialog"][aria-modal="true"] '
            'button[aria-label^="Add"][aria-label*="description"]',
            '[role="dialog"][aria-modal="true"] button:has-text("ALT")',
            '[role="dialog"][aria-modal="true"] [data-testid="altTextInput"]',
        ),
        "Opens the image-description editor. The live control is a text span "
        "('Add description') under the image, not a labelled button, so exact "
        "text is the primary candidate; all candidates stay inside the modal.",
    ),
    SelectorSpec(
        "composer_alt_text_input",
        ':has(:text-is("Edit image description")) textarea',
        (
            ':has(:text-is("Edit image description")) [role="textbox"]',
            ':has(:text-is("Edit image description")) [data-testid="altTextInput"]',
            '[role="dialog"][aria-modal="true"] [data-testid="altTextInput"]',
            '[role="dialog"][aria-modal="true"] textarea[aria-label*="description"]',
            '[role="dialog"][aria-modal="true"] textarea',
            '[role="dialog"] textarea[aria-label*="description"]',
            '[role="dialog"] textarea',
        ),
        "Alt-text box inside the image-description editor layer. The editor is a "
        "separate layer from the composer, so the title anchors the scope. "
        "Text field and Save not yet observed live.",
    ),
    SelectorSpec(
        "composer_alt_save_button",
        '[role="dialog"][aria-modal="true"] button:text-is("Save")',
        (
            '[role="dialog"][aria-modal="true"] button:text-is("Done")',
            '[role="dialog"] button:text-is("Save")',
            '[role="dialog"] button:text-is("Done")',
        ),
        "Confirms the image description and returns to the composer modal.",
    ),
    SelectorSpec(
        "alt_intro_sheet",
        '[data-testid="sheetDialog"]:has-text("Add descriptions")',
        (
            '[data-testid="sheetDialog"]',
            '[role="dialog"]:has-text("Add descriptions")',
        ),
        "First-run alt-text education sheet shown before the description editor. "
        "Text field and Save not yet observed live.",
    ),
    SelectorSpec(
        "alt_intro_dismiss_button",
        '[data-testid="sheetDialog"] button:text-is("No, thanks")',
        (
            '[data-testid="sheetDialog"] :text-is("No, thanks")',
            '[data-testid="sheetDialog"] button:has-text("No, thanks")',
            '[role="dialog"] button:has-text("No, thanks")',
        ),
        "Dismisses the alt-text education sheet without opening the description "
        "editor. Text field and Save not yet observed live.",
    ),
    SelectorSpec(
        "alt_editor_title",
        ':text-is("Edit image description")',
        (
            '[role="dialog"]:has-text("Edit image description")',
            '[data-testid="sheetDialog"]:has-text("Edit image description")',
        ),
        "Title of the alt-text editor screen, a separate layer from the composer. "
        "Text field and Save not yet observed live.",
    ),
    SelectorSpec(
        "alt_editor_back_button",
        'button[aria-label="Back"]',
        (
            '[data-testid*="back" i]',
            ':has(:text-is("Edit image description")) button[aria-label*="Back"]',
        ),
        "Leaves the alt-text editor without saving. Text field and Save not yet observed live.",
    ),
    SelectorSpec(
        "alt_editor_save_button",
        ':has(:text-is("Edit image description")) button:text-is("Save")',
        (
            ':has(:text-is("Edit image description")) :text-is("Save")',
            ':has(:text-is("Edit image description")) button:has-text("Save")',
            '[data-testid="sheetDialog"] button:text-is("Save")',
        ),
        "Saves the image description in the alt-text editor layer. "
        "Text field and Save not yet observed live.",
    ),
    SelectorSpec(
        "composer_close_button",
        '[role="dialog"][aria-modal="true"] [data-testid="app-bar-close"]',
        (
            '[role="dialog"][aria-modal="true"] button[aria-label="Close"]',
            '[role="dialog"][aria-modal="true"] [data-slot="dialog-close"]',
            '[data-testid="app-bar-close"]',
            '[role="dialog"] button[aria-label="Close"]',
            '[role="dialog"] [data-slot="dialog-close"]',
        ),
        "Closes the composer modal; a dirty composer asks whether to save or discard.",
    ),
    SelectorSpec(
        "draft_save_dialog",
        '[role="dialog"]:has([data-testid="confirmationSheetConfirm"])',
        (
            '[role="dialog"]:has-text("Save post?")',
            '[role="dialog"]:has-text("Save post?") button:text-is("Save")',
            '[data-testid="confirmationSheetConfirm"]',
        ),
        "Draft prompt shown when a dirty composer is closed.",
    ),
    SelectorSpec(
        "draft_save_confirm_button",
        '[data-testid="confirmationSheetConfirm"]',
        ('[role="dialog"]:has-text("Save post?") button:text-is("Save")',),
        "Saves the unfinished post to drafts.",
    ),
    SelectorSpec(
        "draft_save_cancel_button",
        '[data-testid="confirmationSheetCancel"]',
        (
            '[role="dialog"]:has-text("Save post?") button:text-is("Discard")',
            '[role="dialog"]:has-text("Save post?") button:text-is("Cancel")',
        ),
        "Discards the draft prompt's changes without saving.",
    ),
    SelectorSpec(
        "discard_post_dialog",
        '[role="alertdialog"]:has-text("Discard post?")',
        ('[role="dialog"]:has-text("Discard post?")',),
        "Second confirmation shown when discarding a post outright.",
    ),
    SelectorSpec(
        "toast",
        '[data-testid="toast"]',
        (
            "[data-sonner-toast]",
            "[data-sonner-toaster] [data-sonner-toast]",
        ),
        "Transient notification; success text is 'Your post was sent.'.",
    ),
    SelectorSpec(
        "toast_view_link",
        '[data-testid="toast"] a:has-text("View")',
        (
            '[data-testid="toast"] button:has-text("View")',
            '[data-sonner-toast] button:has-text("View")',
            '[data-sonner-toast] a:has-text("View")',
        ),
        "Action inside the success toast that opens the new permalink.",
    ),
    SelectorSpec(
        "nav_post_button",
        'a[href="/compose/post"]',
        (
            '[data-testid="SideNav_NewTweet_Button"]',
            'a[href="/compose/tweet"]',
        ),
        "Left-nav Post entry; the deterministic way to open the composer route.",
    ),
    SelectorSpec(
        "nav_profile_link",
        '[data-testid="AppTabBar_Profile_Link"]',
        (
            'nav a[href^="/"]',
            '[role="navigation"] a[href^="/"]',
        ),
        "Profile link. The live authenticated client matched the classic testid; "
        "call handle_link_selector(handle) when the exact handle link is needed.",
    ),
    SelectorSpec(
        "account_switcher_button",
        'button[aria-label="Account menu"]',
        (
            '[data-testid="SideNav_AccountSwitcher_Button"]',
            'button:has-text("Account menu")',
        ),
        "Opens the account menu that shows the signed-in handle.",
    ),
    SelectorSpec(
        "tweet_article",
        'article[data-testid="tweet"]',
        (
            'article[itemprop="SocialMediaPosting"]',
            'article[itemType="https://schema.org/SocialMediaPosting"]',
            "article.group\\/post",
        ),
        "One post in a timeline; the unit for reply and delete actions.",
    ),
    SelectorSpec(
        "tweet_reply_button",
        'article [data-testid="reply"]',
        (
            'article button[aria-label="Reply"]',
            'button[aria-label="Reply"]',
        ),
        "Reply action under a post.",
    ),
    SelectorSpec(
        "primary_column",
        "main",
        ('[data-testid="primaryColumn"]', '[role="main"]'),
        "The main content column; used to scope timeline reads.",
    ),
    SelectorSpec(
        "logged_in_indicator",
        'a[href="/compose/post"]',
        ('button[aria-label="Account menu"]',),
        "A logged-in shell renders the composer nav and account menu. Cookie "
        "presence (twid) is checked by sessions.py, not by a DOM selector.",
    ),
    SelectorSpec(
        "logged_out_indicator",
        'a[href*="/i/jf/onboarding/web"][href*="mode=login"]',
        ('a:has-text("Log in")', 'a:has-text("Sign up")'),
        "The logged-out shell; its login link is the redirect target of /i/flow/login.",
    ),
    SelectorSpec(
        "login_entry_button",
        'a:has-text("Log in with username or email")',
        (
            'button:has-text("Log in with username or email")',
            'a[href*="mode=login"]',
        ),
        "Landing-page button that opens the credential form.",
    ),
    SelectorSpec(
        "login_username_input",
        'input[autocomplete="username"]',
        ('input[name="text"]', 'input[type="text"]'),
        "Username/email field in the login flow (form chunk not isolated in the study).",
    ),
    SelectorSpec(
        "login_password_input",
        'input[type="password"]',
        ('input[name="password"]',),
        "Password field in the login flow.",
    ),
    SelectorSpec(
        "login_next_button",
        '[role="dialog"] button:text-is("Next")',
        ('button:has-text("Next")',),
        "Advances the multi-step login flow.",
    ),
    SelectorSpec(
        "login_submit_button",
        'button:text-is("Log in")',
        ('button[type="submit"]:has-text("Log in")',),
        "Submits the credential form.",
    ),
    SelectorSpec(
        "challenge_dialog",
        '[role="dialog"]:has-text("Verify you are a human")',
        ('iframe[src*="arkose"]', '[data-testid="arkose"]'),
        "Human-verification prompt; its presence always parks automation.",
    ),
    SelectorSpec(
        "challenge_iframe",
        'iframe[src*="arkose"]',
        ('iframe[title*="challenge"]', 'iframe[src*="funcaptcha"]'),
        "Arkose/FunCaptcha frame embedded in the challenge dialog.",
    ),
    SelectorSpec(
        "tweet_more_button",
        '[data-testid="caret"]',
        ('button[aria-label="More"]',),
        "Overflow menu on a post; the entry point for deleting it.",
    ),
    SelectorSpec(
        "menu_delete_item",
        '[role="menuitem"]:has-text("Delete")',
        ('[role="menu"] [role="menuitem"]:has-text("Delete")', 'text="Delete"'),
        "Delete entry in the post overflow menu.",
    ),
    SelectorSpec(
        "delete_confirm_button",
        '[data-testid="confirmationSheetConfirm"]',
        (
            '[role="alertdialog"] button:text-is("Delete")',
            '[role="dialog"] button:text-is("Delete")',
        ),
        "Final Delete confirmation for an existing post.",
    ),
)

SELECTORS: Mapping[str, SelectorSpec] = MappingProxyType({spec.name: spec for spec in _SPECS})
"""Every logical element the poster may touch, keyed by its stable logical name."""

_HANDLE_PATTERN = re.compile(r"[A-Za-z0-9_]{1,15}")


def handle_link_selector(handle: str) -> str:
    """Build the exact profile-link selector for one handle.

    ``nav_profile_link`` is only a template: ``locate()`` takes a logical name
    and cannot substitute a handle, so callers that know the handle build the
    selector here. The strict pattern also stops caller input from injecting
    extra CSS into the selector.
    """
    if not isinstance(handle, str) or _HANDLE_PATTERN.fullmatch(handle) is None:
        raise ValueError(
            f"invalid X handle {handle!r}; expected 1-15 letters, digits or underscores"
        )
    return f'a[href="/{handle}"]'


def _require(name: str) -> SelectorSpec:
    """Look up a logical name, failing loudly for typos instead of silently missing."""
    spec = SELECTORS.get(name)
    if spec is None:
        known = ", ".join(sorted(SELECTORS))
        raise UiChangedError(
            f"unknown selector name {name!r}; known names: {known}. "
            "Add it to xposter.selectors.SELECTORS."
        )
    return spec


async def locate(
    scope: Scope,
    name: str,
    *,
    timeout_ms: int = 5000,
    visible: bool = True,
) -> Locator:
    """Return the first *visible* match for ``name``, trying candidates in order.

    ``.first`` is not good enough on a live page: a selector can match the
    hidden outer dialog wrapper, or a background composer under the modal mask,
    before the element the operator can actually see. Each candidate is polled
    until a match reports ``is_visible()``; ``visible=False`` is for inputs that
    are intentionally hidden (the file picker) and waits for ``attached`` only.

    The timeout is split across candidates (with a floor of ~400 ms each) so a
    selector that was renamed on one client still resolves on the other within
    one call. Playwright polls the DOM, so a match that appears mid-budget is
    returned immediately instead of waiting out the remaining candidates.
    """
    spec = _require(name)
    budget_ms = max(_MIN_CANDIDATE_TIMEOUT_MS, timeout_ms // len(spec.candidates))
    budget_s = budget_ms / 1000.0
    for candidate in spec.candidates:
        locator = scope.locator(candidate)
        if not visible:
            target = locator.first
            try:
                await target.wait_for(state="attached", timeout=budget_ms)
            except PlaywrightError:
                continue
            return target
        deadline = time.monotonic() + budget_s
        while True:
            try:
                total = await locator.count()
            except PlaywrightError:
                total = 0
            for index in range(total):
                item = locator.nth(index)
                try:
                    if await item.is_visible():
                        return item
                except PlaywrightError:
                    continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_POLL_INTERVAL_S, remaining))
    candidates = "\n".join(f"  - {candidate}" for candidate in spec.candidates)
    qualifier = "visible " if visible else ""
    raise UiChangedError(
        f"X UI changed: no {qualifier}match for {name!r} after "
        f"{len(spec.candidates)} candidate(s):\n{candidates}\n"
        "Run `xposter doctor --dom` to recalibrate the selectors."
    )


async def locate_all(scope: Scope, name: str, *, visible: bool = True) -> list[Locator]:
    """Return the visible elements of the first candidate that yields any.

    Thread composers repeat one logical element per item, so callers need the
    full list in DOM order. Hidden duplicates are filtered out: on ``/compose/post``
    the inline home composer and the modal both carry ``tweetTextarea_0``, and
    only the modal's editors may be typed into. ``visible=False`` keeps the
    attached-only behaviour for intentionally hidden inputs.
    """
    spec = _require(name)
    for candidate in spec.candidates:
        locator = scope.locator(candidate)
        try:
            total = await locator.count()
        except PlaywrightError:
            continue
        if not total:
            continue
        items = [locator.nth(index) for index in range(total)]
        if not visible:
            return items
        shown: list[Locator] = []
        for item in items:
            try:
                if await item.is_visible():
                    shown.append(item)
            except PlaywrightError:
                continue
        if shown:
            return shown
    return []


async def is_present(scope: Scope, name: str, *, timeout_ms: int = 1500) -> bool:
    """Whether ``name`` resolves to a visible element within ``timeout_ms``."""
    _require(name)
    try:
        await locate(scope, name, timeout_ms=timeout_ms)
    except UiChangedError:
        return False
    return True


async def count(scope: Scope, name: str) -> int:
    """Number of elements ``name`` resolves to, using the locate_all rule."""
    spec = _require(name)
    for candidate in spec.candidates:
        try:
            total = await scope.locator(candidate).count()
        except PlaywrightError:
            continue
        if total:
            return total
    return 0


async def match_report(scope: Scope, name: str) -> list[tuple[str, int]]:
    """Raw match count for every candidate, in order, for live-DOM calibration.

    ``xposter doctor --dom`` prints this so an operator can see which client X
    served and which candidates still work after a UI change. Counts include
    hidden matches (the background composer), so they are a trap detector as
    well as a calibration table; use :func:`match_report_visible` to count only
    what the operator can actually see.
    """
    spec = _require(name)
    report: list[tuple[str, int]] = []
    for candidate in spec.candidates:
        try:
            total = await scope.locator(candidate).count()
        except PlaywrightError:
            total = 0
        report.append((candidate, total))
    return report


async def match_report_visible(scope: Scope, name: str) -> list[tuple[str, int]]:
    """Visible match count for every candidate, in order.

    This is the companion to :func:`match_report`: the raw table proves a trap
    exists (two ``tweetTextarea_0`` nodes), while this table proves the modal
    candidate resolves to the one element the operator can see.
    """
    spec = _require(name)
    report: list[tuple[str, int]] = []
    for candidate in spec.candidates:
        locator = scope.locator(candidate)
        visible_total = 0
        try:
            total = await locator.count()
        except PlaywrightError:
            total = 0
        for index in range(total):
            try:
                if await locator.nth(index).is_visible():
                    visible_total += 1
            except PlaywrightError:
                continue
        report.append((candidate, visible_total))
    return report
