"""Health checks for the saved session, the host and X's live DOM.

``doctor`` must answer three different questions without mixing them up:

* **Offline checks** (always run): can this host start the browser we ship, is
  the login present and unexpired, is the account parked, is the private home
  actually private, and is the local clock sane? None of that needs a browser.
* **Browser tells** (``--tells``): what does the live Chrome report about
  itself? This is the honest-browser measurement, never a patch.
* **DOM calibration** (``--dom``): how does X's current composer DOM compare
  with our selectors? It posts nothing, saves nothing and always closes the
  composer without keeping a draft.

The formatting helpers are pure so the tables can be unit-tested without a
browser, a clock or a network.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import zlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from .browser import (
    Tell,
    assess_tells,
    chrome_version,
    collect_tells,
    find_chrome,
    resolve_display,
)
from .config import Display, XPosterConfig
from .errors import BrowserUnavailableError, ConfigurationError
from .selectors import SELECTORS, match_report_visible
from .sessions import authenticated_page
from .store import SessionStore, sanitise_html

CheckStatus = Literal["ok", "warn", "fail"]

MAX_DIALOG_HTML_BYTES = 200 * 1024

_COMPOSER_PREFIXES = ("composer_",)

_HEADLESS_WARNING = (
    "HeadlessChrome user agent is visible to X; use --display visible for the first "
    "runs, Xvfb on Linux"
)

_MODAL_SCOPE = '[role="dialog"][aria-modal="true"]'

# The classic client renders an inline home-timeline composer under the modal,
# sharing the same testids. Every composer wait, editor lookup and
# open/closed decision must be anchored to the visible modal dialog first;
# the legacy role=dialog fallbacks come after it.
_COMPOSER_WAIT_SELECTORS = (
    f'{_MODAL_SCOPE} [data-testid="tweetTextarea_0"]',
    f'{_MODAL_SCOPE} [role="textbox"]',
    f'{_MODAL_SCOPE} textarea[aria-multiline="true"]',
    '[role="dialog"] [data-testid="tweetTextarea_0"]',
    '[role="dialog"] [role="textbox"]',
    '[role="dialog"] textarea[aria-multiline="true"]',
)

_DIALOG_SELECTORS = (
    _MODAL_SCOPE,
    '[role="dialog"]:has([data-testid="tweetTextarea_0"])',
    '[role="dialog"]:has(textarea[aria-multiline="true"])',
    '[role="dialog"]:has([data-modal-slot-surface"])',
    "[data-modal-slot-surface]",
    '[role="dialog"]',
)

_EDITOR_SELECTORS = (
    f'{_MODAL_SCOPE} [data-testid="tweetTextarea_0"]',
    f'{_MODAL_SCOPE} [contenteditable="true"]',
    f'{_MODAL_SCOPE} [role="textbox"]',
    f'{_MODAL_SCOPE} textarea[aria-multiline="true"]',
    '[role="dialog"] [contenteditable="true"]',
    '[role="dialog"] [role="textbox"]',
    '[role="dialog"] textarea[aria-multiline="true"]',
)

# The save/discard sheet is a second dialog; match its scope first, then read
# the real button labels and testids out of it.
_SAVE_SHEET_SELECTORS = (
    '[data-testid="confirmationSheetDialog"]',
    '[data-testid="sheetDialog"]',
    '[data-testid="confirmationSheet"]',
    '[data-testid="sheet"]',
    '[role="dialog"]:has([data-testid="confirmationSheetCancel"])',
    '[role="dialog"]:has-text("Save post?")',
    '[role="alertdialog"]',
    "dialog",
)

_COMPOSER_PRESENT_SELECTORS = (
    _MODAL_SCOPE,
    "[data-fx-composer]",
    '[role="dialog"] [data-testid="tweetTextarea_0"]',
    '[role="dialog"] [role="textbox"]',
    '[role="dialog"] textarea[aria-multiline="true"]',
)

# The forensics dump after Close: every overlay/layer that is not the composer.
_OVERLAY_SELECTORS = (
    '[role="dialog"]',
    '[role="alertdialog"]',
    '[data-testid="sheetDialog"]',
    '[data-testid="confirmationSheetDialog"]',
    '[data-testid="mask"]',
    "dialog",
)

_DRAFTS_CONTROL_SELECTORS = (
    f'{_MODAL_SCOPE} [data-testid="unsentButton"]',
    f'{_MODAL_SCOPE} button:text-is("Drafts")',
    f'{_MODAL_SCOPE} button[aria-label="Drafts"]',
    '[role="dialog"] [data-testid="unsentButton"]',
    '[role="dialog"] button:text-is("Drafts")',
)

# The drafts panel must live inside the composer modal; the background
# timeline on /compose/post is not a drafts source.
_DRAFT_PANEL_SELECTORS = (
    f'{_MODAL_SCOPE} [data-testid="draftsDialog"]',
    f'{_MODAL_SCOPE} [data-testid="draftsPanel"]',
    f'{_MODAL_SCOPE} [data-testid="drafts"]',
    f'{_MODAL_SCOPE} [data-testid="draftsList"]',
    f'{_MODAL_SCOPE} [role="dialog"]:has-text("Drafts")',
    f'{_MODAL_SCOPE} [role="alertdialog"]:has-text("Drafts")',
    f'{_MODAL_SCOPE} [role="list"]:has-text("Drafts")',
    f'{_MODAL_SCOPE} [data-testid="sheetDialog"]:has-text("Drafts")',
    f'{_MODAL_SCOPE} [data-testid="confirmationSheetDialog"]:has-text("Drafts")',
)

_DRAFT_ENTRY_SELECTORS = (
    '[data-testid="draft"]',
    '[data-testid^="draft-"]',
    '[data-testid="draftsList"] [role="listitem"]',
    '[role="listitem"]',
    '[data-testid="cellInnerDiv"]',
)

_EMPTY_DRAFTS_RE = re.compile(
    r"(no drafts|no unsent|don't have any drafts|nothing saved|no saved drafts|"
    r"you haven't saved)",
    re.IGNORECASE,
)

_ADD_POST_NAMES = frozenset({"add another post", "add reply"})
_ADD_POST_TESTIDS = frozenset({"addbutton"})
_DRAFTS_NAMES = frozenset({"drafts"})
_DRAFTS_TESTIDS = frozenset({"unsentbutton"})
_SUBMIT_CLICK_RE = re.compile(r"post|reply|schedule|tweetButton|save", re.IGNORECASE)

_DEFAULT_WAIT_S = 20.0
_DEFAULT_POLL_S = 0.25
_DEFAULT_NETWORKIDLE_MS = 8_000
_DEFAULT_CLEANUP_S = 4.0
_DEFAULT_CLEANUP_POLL_S = 0.2
_DEFAULT_DRAFTS_WAIT_S = 6.0
_DEFAULT_MEDIA_WAIT_S = 30.0
_DEFAULT_MEDIA_POLL_S = 0.25

_MEDIA_CALIBRATION_NAME = "xposter-calibration.png"
_MEDIA_MATCH_NAMES = (
    "composer_media_preview",
    "composer_media_remove_button",
    "composer_alt_button",
    "composer_alt_text_input",
    "composer_alt_save_button",
)
_MEDIA_INPUT_SELECTORS = (
    f'{_MODAL_SCOPE} [data-testid="fileInput"]',
    f'{_MODAL_SCOPE} input[type="file"]',
    '[role="dialog"] [data-testid="fileInput"]',
    '[role="dialog"] input[type="file"]',
)
_MEDIA_PREVIEW_SELECTORS = (
    f'{_MODAL_SCOPE} [data-testid="attachments"] img',
    f"{_MODAL_SCOPE} [data-media-preview] img",
    f"{_MODAL_SCOPE} [data-media-preview]",
    f"{_MODAL_SCOPE} .fx-previews img",
    '[role="dialog"] [data-testid="attachments"] img',
    '[role="dialog"] [data-media-preview]',
    '[role="dialog"] .fx-previews img',
)
_MEDIA_POST_SELECTORS = (
    f'{_MODAL_SCOPE} [data-testid="tweetButton"]',
    f'{_MODAL_SCOPE} button[type="submit"]',
    f'{_MODAL_SCOPE} button:text-is("Post")',
    f'{_MODAL_SCOPE} button:text-is("Post all")',
    '[role="dialog"] button[type="submit"]',
)

# The live alt flow opens a one-time education sheet over the real editor.
# "Sure" only re-opens help; "No, thanks" dismisses the sheet and leaves the
# editor visible. Only the latter is clickable in phase D.
_ALT_INTRO_TITLE = "Add descriptions"
_ALT_INTRO_DECLINE = "No, thanks"
_ALT_INTRO_SURE_TESTID = "altTextEducationSheetPrimaryAction"
_ALT_EDITOR_TITLE = "Edit image description"
_ALT_EDITOR_FIELD_SELECTORS = (
    '[data-testid="altTextInput"]',
    'textarea[aria-label*="description" i]',
    'textarea[aria-label*="alt" i]',
    '[contenteditable="true"][aria-label*="description" i]',
)

_CLEANUP_WARNING = (
    "The composer is still open and may hold unsent text; xposter will close the browser, "
    "which discards unsaved text. Run `xposter doctor --drafts` to confirm no draft was saved."
)


class UnsafeClickError(RuntimeError):
    """A calibration click was about to hit Post/Reply/Schedule/Save."""


def assert_safe_click(
    *, text: str = "", aria_label: str = "", data_testid: str = "", phase: str = ""
) -> None:
    """Refuse any click that could submit a post or save a draft.

    The add-post, drafts, close/discard and (in phase D only) media controls are
    identified by exact names. In phase D the alt-text education sheet may only
    be left with exact ``No, thanks``; ``Sure`` is always refused. Post, Reply,
    Schedule, Save, Delete and Edit are always refused, including in phase D.
    """
    label = text.strip().lower()
    aria = aria_label.strip().lower()
    testid = data_testid.strip().lower()

    blocked_testids = {
        "tweetbutton",
        "tweetbuttoninline",
        "confirmationsheetconfirm",
        "scheduleoption",
    }
    submit_labels = {"post", "post all", "reply", "schedule", "schedule post", "save"}
    if testid in blocked_testids or label in submit_labels:
        raise UnsafeClickError(f"Refusing to click a submit/save control: {label or testid!r}.")
    if label == "sure" or testid == "alttexteducationsheetprimaryaction":
        raise UnsafeClickError(
            "Refusing to click the alt-text intro 'Sure' control; "
            "only 'No, thanks' may dismiss the education sheet."
        )
    if phase == "D" and (aria == "edit media" or label == "edit media"):
        return
    if label in {"delete", "edit"} or aria in {"delete", "edit"}:
        raise UnsafeClickError(f"Refusing to click a destructive control: {label or aria!r}.")
    if testid in _ADD_POST_TESTIDS or aria in _ADD_POST_NAMES:
        return
    if testid in _DRAFTS_TESTIDS or aria in _DRAFTS_NAMES or label in _DRAFTS_NAMES:
        return
    if testid == "confirmation-sheet-cancel" or testid == "confirmationsheetcancel":
        return
    if phase == "D":
        combined = f"{label} {aria} {testid}"
        if aria == "remove media" or label == "remove media":
            return
        if label == "add description" or aria == "add description":
            return
        if aria == "edit media" or label == "edit media":
            return
        if label == "no, thanks" or aria == "no, thanks":
            return
    if (
        label in _ADD_POST_NAMES or label in {"discard", "cancel", "close"}
    ) and not _SUBMIT_CLICK_RE.search(f"{aria} {testid}"):
        return

    combined = f"{label} {aria} {testid}"
    if not combined.strip():
        raise UnsafeClickError("Refusing to click a button with no text, aria-label or testid.")
    if _SUBMIT_CLICK_RE.search(combined):
        raise UnsafeClickError(f"Refusing to click a submit/save control: {combined.strip()!r}.")
    if not testid and not aria and label == "close":
        return
    # A close/discard/back control with a recognisable aria/testid is safe.
    if (
        label in {"discard", "cancel", "close", "back"}
        or aria in {"close", "discard", "cancel", "back"}
        or testid in {"app-bar-close", "dialog-close"}
    ):
        return
    raise UnsafeClickError(f"Refusing to click an unclassified button: {combined.strip()!r}.")


@dataclass(frozen=True, slots=True)
class Check:
    """One offline result, phrased so an operator can act on it."""

    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True, slots=True)
class ButtonInfo:
    """One visible dialog button, recorded as real text/aria/testid/state."""

    text: str
    aria_label: str
    data_testid: str
    disabled: bool

    def to_json(self) -> dict[str, object]:
        """Plain JSON-ready mapping."""
        return {
            "text": self.text,
            "aria_label": self.aria_label,
            "data_testid": self.data_testid,
            "disabled": self.disabled,
        }


@dataclass(frozen=True, slots=True)
class DomRow:
    """Calibration result for one selector name across the measured phases.

    ``phase_counts`` is ``(A, B, C)``: empty composer, typed composer, thread
    with a second editor. ``candidate`` is the best matching candidate from the
    latest phase that matched; ``missing`` is true only when no phase matched.
    """

    name: str
    candidate: str | None
    count: int
    missing: bool
    candidates: tuple[tuple[str, int], ...] = ()
    phase_counts: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class DomReport:
    """Full calibration result plus the private directory holding its evidence."""

    rows: tuple[DomRow, ...]
    artifacts: Path
    screenshot: Path | None = None
    dialog_html: Path | None = None
    report_json: Path | None = None
    rendered: bool = True
    waited_s: float = 0.0
    timeout_s: float = _DEFAULT_WAIT_S
    screenshots: tuple[Path, ...] = ()
    dialog_buttons: tuple[ButtonInfo, ...] = ()
    discard_sheet: tuple[ButtonInfo, ...] = ()
    dialog_closed: bool = False
    discard_action: str = "none"
    draft_saved: bool = False
    cleanup_ok: bool = True
    after_close: tuple[dict[str, Any], ...] = ()
    after_close_screenshot: Path | None = None
    after_close_html: Path | None = None
    close_attempts: int = 0
    close_click: dict[str, Any] | None = None
    close_retry: dict[str, Any] | None = None
    close_path: str = ""
    media: MediaReport | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class DraftsReport:
    """Read-only result of the ``doctor --drafts`` audit."""

    rendered: bool
    drafts: list[str]
    artifacts: Path
    screenshot: Path | None = None
    dialog_html: Path | None = None
    report_json: Path | None = None
    note: str = ""
    drafts_control_found: bool = True
    panel_opened: bool = False
    empty_state: bool = False


@dataclass(frozen=True, slots=True)
class MediaIntro:
    """The one-time "Add descriptions" education sheet over the alt editor."""

    opened: bool
    html: Path | None
    buttons: tuple[ButtonInfo, ...]
    dismissed: bool
    note: str = ""

    def to_json(self) -> dict[str, object]:
        """Plain JSON-ready mapping."""
        return {
            "opened": self.opened,
            "html": self.html.name if self.html is not None else None,
            "buttons": [button.to_json() for button in self.buttons],
            "dismissed": self.dismissed,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class MediaBackControl:
    """How phase D left the real "Edit image description" screen."""

    found: bool
    strategy: str = ""
    text: str = ""
    aria_label: str = ""
    data_testid: str = ""
    clicked: bool = False
    note: str = ""

    def to_json(self) -> dict[str, object]:
        """Plain JSON-ready mapping."""
        return {
            "found": self.found,
            "strategy": self.strategy,
            "text": self.text,
            "aria_label": self.aria_label,
            "data_testid": self.data_testid,
            "clicked": self.clicked,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class MediaAltEditor:
    """A media overlay capture (alt/description or edit editor)."""

    opened: bool
    html: Path | None
    buttons: tuple[ButtonInfo, ...]
    inputs: tuple[dict[str, object], ...]
    closed: bool
    note: str = ""
    heading: str = ""
    screenshot: Path | None = None
    composer_image_visible: bool = False


@dataclass(frozen=True, slots=True)
class MediaReport:
    """Everything phase D learned about the real media UI."""

    attached: bool
    ready: bool
    note: str
    requests: tuple[str, ...]
    matches: dict[str, list[tuple[str, int]]]
    dialog_buttons: tuple[ButtonInfo, ...]
    attachment_buttons: tuple[ButtonInfo, ...]
    attachment_html: Path | None
    screenshot: Path | None
    alt_editor: MediaAltEditor | None
    removed: bool
    preview_gone: bool
    media_modal_html: Path | None = None
    controls: tuple[dict[str, object], ...] = ()
    edit_editor: MediaAltEditor | None = None
    alt_intro: MediaIntro | None = None
    alt_back_control: MediaBackControl | None = None


@dataclass(frozen=True, slots=True)
class _MediaPhaseResult:
    report: MediaReport
    phase: dict[str, list[tuple[str, int]]]


@dataclass(frozen=True, slots=True)
class _AltCaptureResult:
    """Alt-screen evidence plus the intro sheet and the control that closed it."""

    editor: MediaAltEditor | None = None
    intro: MediaIntro | None = None
    back_control: MediaBackControl | None = None


@dataclass(frozen=True, slots=True)
class _CleanupResult:
    """Everything the post-close forensics and discard path learned."""

    discard_sheet: tuple[ButtonInfo, ...]
    dialog_closed: bool
    cleanup_ok: bool
    discard_action: str
    close_path: str
    close_attempts: int
    close_click: dict[str, Any] | None
    close_retry: dict[str, Any] | None
    after_close: tuple[dict[str, Any], ...]
    after_close_screenshot: Path | None
    after_close_html: Path | None


def offline_report(config: XPosterConfig) -> list[Check]:
    """Run every check that needs no browser and no network.

    Ordering is intentional: the browser first (nothing works without it), then
    the session and safety state, then host hygiene. A single failing check does
    not stop the others: an operator wants the whole picture in one run.
    """
    store = SessionStore(config)
    return [
        _chrome_check(config),
        _display_check(config),
        _xvfb_check(),
        _permissions_check(config),
        _session_check(store),
        _parked_check(store),
        _ledger_check(config),
        _clock_check(),
        _risk_check(),
    ]


def format_checks(checks: Sequence[Check]) -> list[str]:
    """Render offline checks as one stable line each."""
    return [f"[{check.status}] {check.name}: {check.detail}" for check in checks]


def format_tells(tells: Sequence[Tell]) -> list[str]:
    """Render browser tells with their risk assessment."""
    return [f"[{tell.risk}] {tell.name}: {tell.value} - {tell.explanation}" for tell in tells]


def format_dom_report(report: DomReport) -> list[str]:
    """Render the per-phase calibration table and the click-safety evidence."""
    if report.rendered:
        lines = [
            f"DOM calibration: waited {report.waited_s:.1f} s; "
            "A=empty composer, B=typed, C=thread, D=media; nothing was posted.",
        ]
    else:
        lines = [
            f"!! composer never rendered within {report.timeout_s:g} s; "
            "the counts below are NOT evidence that the selectors are missing.",
        ]
    if report.note:
        lines.append(f"note: {report.note}")
    lines.append(f"{'selector':<28} {'A':>4} {'B':>4} {'C':>4} {'D':>4}  matching candidate")

    missing_composer: list[str] = []
    for row in report.rows:
        count_a, count_b, count_c, count_d = _padded_counts(row.phase_counts or (row.count,))
        if report.rendered:
            candidate = _short(row.candidate) if row.candidate else "MISSING"
            marker = ""
            if row.missing and is_composer_selector(row.name):
                missing_composer.append(row.name)
                marker = "  <-- composer selector missing"
            lines.append(
                f"{row.name:<28} {count_a:>4} {count_b:>4} {count_c:>4} "
                f"{count_d:>4}  {candidate}{marker}"
            )
        else:
            candidate = _short(row.candidate) if row.candidate else "(not measured)"
            lines.append(
                f"{row.name:<28} {count_a:>4} {count_b:>4} {count_c:>4} {count_d:>4}  {candidate}"
            )

    lines.append("")
    if report.rendered:
        if missing_composer:
            lines.append("Composer selectors missing: " + ", ".join(missing_composer))
        else:
            lines.append("All composer selectors matched at least one element.")
    else:
        lines.append("Composer DOM was not available; selector misses are inconclusive.")

    if report.dialog_buttons:
        lines.append("Dialog buttons: " + "; ".join(map(_button_summary, report.dialog_buttons)))
    if report.discard_sheet:
        lines.append("Discard sheet: " + "; ".join(map(_button_summary, report.discard_sheet)))
        lines.append(
            f"Discard action: {report.discard_action}; draft saved: "
            f"{'YES' if report.draft_saved else 'no'}; dialog closed: "
            f"{'yes' if report.dialog_closed else 'NO'}"
        )
    else:
        lines.append("Discard sheet: none")

    if report.after_close:
        lines.append(f"After Close overlays ({len(report.after_close)}):")
        for overlay in report.after_close:
            lines.append("  " + _overlay_summary(overlay))
    else:
        lines.append("After Close overlays: none")
    if report.close_click is not None:
        lines.append("Close click: " + _click_summary(report.close_click))
    if report.close_retry is not None:
        lines.append("Close retry: " + _click_summary(report.close_retry))
    if report.close_attempts or report.after_close:
        lines.append(
            f"Cleanup: {'ok' if report.cleanup_ok else 'FAILED'}; dialog closed: "
            f"{'yes' if report.dialog_closed else 'NO'}; path: {report.close_path}"
        )
    if not report.cleanup_ok:
        lines.extend(["!" * 72, "CLEANUP FAILED", _CLEANUP_WARNING, "!" * 72])

    if report.media is not None:
        media = report.media
        lines.append(
            f"Phase D media: attached={'yes' if media.attached else 'no'}; "
            f"ready={'yes' if media.ready else 'no'}; "
            f"removed={'yes' if media.removed else 'no'}; "
            f"preview gone={'yes' if media.preview_gone else 'no'}"
        )
        if media.requests:
            lines.append("Media upload requests: " + ", ".join(media.requests))
        media_counts = "; ".join(
            f"{name}=" + str(next((count for _selector, count in matches if count > 0), 0))
            for name, matches in media.matches.items()
        )
        if media_counts:
            lines.append("Media matches: " + media_counts)
        if media.dialog_buttons:
            lines.append(
                "Media dialog buttons: " + "; ".join(map(_button_summary, media.dialog_buttons))
            )
        if media.attachment_buttons:
            lines.append(
                "Attachment buttons: " + "; ".join(map(_button_summary, media.attachment_buttons))
            )
        if media.controls:
            lines.append(
                "Media controls: "
                + "; ".join(
                    f"{item.get('name')}="
                    + (
                        f"<{item.get('tag')}> testid={item.get('data_testid')!r} "
                        f"aria={item.get('aria_label')!r} text={item.get('text')!r}"
                        if item.get("found")
                        else "NOT FOUND"
                    )
                    for item in media.controls
                )
            )
        if media.alt_intro is not None:
            intro = media.alt_intro
            lines.append(
                f"Alt intro sheet: opened={'yes' if intro.opened else 'no'}; "
                f"dismissed={'yes' if intro.dismissed else 'no'}"
            )
            if intro.buttons:
                lines.append("Alt intro buttons: " + "; ".join(map(_button_summary, intro.buttons)))
            if intro.html is not None:
                lines.append(f"Alt intro HTML: {intro.html}")
            if intro.note:
                lines.append(f"Alt intro note: {intro.note}")
        if media.alt_editor is not None:
            alt = media.alt_editor
            lines.append(
                f"Alt editor: opened={'yes' if alt.opened else 'no'}; "
                f"closed={'yes' if alt.closed else 'no'}; heading={alt.heading!r}; "
                f"image visible after={'yes' if alt.composer_image_visible else 'no'}"
            )
            if alt.buttons:
                lines.append("Alt buttons: " + "; ".join(map(_button_summary, alt.buttons)))
            if alt.html is not None:
                lines.append(f"Alt HTML: {alt.html}")
            if alt.screenshot is not None:
                lines.append(f"Alt screenshot: {alt.screenshot}")
            if alt.note:
                lines.append(f"Alt note: {alt.note}")
        if media.alt_back_control is not None:
            back = media.alt_back_control
            lines.append(
                f"Alt back control: found={'yes' if back.found else 'no'}; "
                f"strategy={back.strategy!r}; text={back.text!r}; "
                f"aria={back.aria_label!r}; testid={back.data_testid!r}; "
                f"clicked={'yes' if back.clicked else 'no'}"
            )
            if back.note:
                lines.append(f"Alt back note: {back.note}")
        if media.edit_editor is not None:
            edit = media.edit_editor
            lines.append(
                f"Edit editor: opened={'yes' if edit.opened else 'no'}; "
                f"closed={'yes' if edit.closed else 'no'}; heading={edit.heading!r}; "
                f"image visible after={'yes' if edit.composer_image_visible else 'no'}"
            )
            if edit.buttons:
                lines.append("Edit buttons: " + "; ".join(map(_button_summary, edit.buttons)))
            if edit.html is not None:
                lines.append(f"Edit HTML: {edit.html}")
            if edit.screenshot is not None:
                lines.append(f"Edit screenshot: {edit.screenshot}")
            if edit.note:
                lines.append(f"Edit note: {edit.note}")
        if media.media_modal_html is not None:
            lines.append(f"Media modal HTML: {media.media_modal_html}")
        if media.attachment_html is not None:
            lines.append(f"Attachment HTML: {media.attachment_html}")
        if media.note:
            lines.append(f"Media note: {media.note}")

    screenshots = report.screenshots or ((report.screenshot,) if report.screenshot else ())
    for path in screenshots:
        lines.append(f"Screenshot: {path}")
    if report.after_close_screenshot is not None:
        lines.append(f"After Close screenshot: {report.after_close_screenshot}")
    if report.after_close_html is not None:
        lines.append(f"After Close HTML: {report.after_close_html}")
    if report.dialog_html is not None:
        lines.append(f"Composer HTML: {report.dialog_html}")
    if report.report_json is not None:
        lines.append(f"Report: {report.report_json}")
    lines.append(f"Artifacts: {report.artifacts}")
    return lines


def _overlay_summary(overlay: Mapping[str, Any]) -> str:
    tag = str(overlay.get("tag", ""))
    role = str(overlay.get("role", ""))
    testid = str(overlay.get("data_testid", ""))
    aria = str(overlay.get("aria_label", ""))
    text = str(overlay.get("text", ""))
    buttons = overlay.get("buttons", [])
    button_text = ", ".join(
        str(button.get("text") or button.get("data_testid") or "?")
        for button in buttons
        if isinstance(button, Mapping)
    )
    return (
        f"<{tag}> role={role!r} testid={testid!r} aria={aria!r} "
        f"text={text!r} buttons=[{button_text}]"
    )


def _click_summary(info: Mapping[str, Any]) -> str:
    hit = info.get("hit")
    if isinstance(hit, Mapping):
        hit_text = (
            f"<{hit.get('tag', '')}> testid={hit.get('data_testid', '')!r} "
            f"aria={hit.get('aria_label', '')!r}"
        )
    else:
        hit_text = "unavailable"
    return (
        f"candidate={info.get('candidate', '')!r} "
        f"dispatched={bool(info.get('dispatched'))} "
        f"box={info.get('box')} hit={hit_text} error={info.get('error', '')!r}"
    )


def format_drafts_report(report: DraftsReport) -> list[str]:
    """Render the read-only drafts audit and the manual cleanup instructions."""
    lines = ["Drafts audit (read-only):"]
    if not report.rendered:
        lines.append("!! composer never rendered; no drafts were inspected.")
    elif not report.drafts_control_found:
        lines.append("Drafts control not found or not clickable; nothing was opened or changed.")
    elif not report.panel_opened and not report.drafts and not report.empty_state:
        lines.append("Drafts panel did not open; drafts=[] (no background posts were read).")
    else:
        lines.append(f"Drafts found: {len(report.drafts)}")
        for draft in report.drafts:
            marker = (
                "  !! LEFTOVER FROM CALIBRATION"
                if draft.lower().startswith("xposter calibration")
                else ""
            )
            lines.append(f"  - {draft}{marker}")
        if not report.drafts:
            reason = "empty state shown" if report.empty_state else "no visible draft entries"
            lines.append(f"  ({reason})")
    if report.note:
        lines.append(f"note: {report.note}")
    lines.extend(
        [
            "Manual cleanup (xposter never deletes drafts):",
            "  1. Open the composer and click Drafts.",
            "  2. Click Edit on the leftover draft, select its text and Delete it.",
            "  3. Close the composer, then rerun `xposter doctor --drafts` to confirm.",
        ]
    )
    if report.screenshot is not None:
        lines.append(f"Screenshot: {report.screenshot}")
    if report.dialog_html is not None:
        lines.append(f"Drafts HTML: {report.dialog_html}")
    if report.report_json is not None:
        lines.append(f"Report: {report.report_json}")
    lines.append(f"Artifacts: {report.artifacts}")
    return lines


def _button_summary(button: ButtonInfo) -> str:
    text = f"'{button.text}'" if button.text else "<no text>"
    details = []
    if button.aria_label:
        details.append(f"aria={button.aria_label!r}")
    if button.data_testid:
        details.append(f"testid={button.data_testid}")
    if button.disabled:
        details.append("disabled")
    suffix = f" ({', '.join(details)})" if details else ""
    return f"{text}{suffix}"


def is_composer_selector(name: str) -> bool:
    """True for names whose absence means the composer cannot be driven."""
    return name.startswith(_COMPOSER_PREFIXES)


def _chrome_check(config: XPosterConfig) -> Check:
    try:
        path = find_chrome(config)
    except ConfigurationError as exc:
        return Check("chrome", "fail", str(exc))
    if path is None:
        return Check(
            "chrome",
            "fail",
            "Google Chrome was not found; install Chrome from https://www.google.com/chrome/ "
            "or point XPOSTER_CHROME_PATH at its executable.",
        )
    version = chrome_version(path)
    if version is None:
        return Check(
            "chrome",
            "warn",
            f"Chrome was found at {path} but did not answer `--version`; posting may still work.",
        )
    return Check("chrome", "ok", f"Google Chrome {version} at {path}")


def _display_check(config: XPosterConfig) -> Check:
    try:
        resolved = resolve_display(config)
    except (BrowserUnavailableError, ConfigurationError) as exc:
        return Check("display", "fail", str(exc))
    detail = f"requested {config.display.value}, resolved {resolved.value} on {sys.platform}"
    if resolved is Display.HEADLESS:
        return Check("display", "warn", f"{detail}; {_HEADLESS_WARNING}.")
    return Check("display", "ok", detail)


def _xvfb_check() -> Check:
    if not sys.platform.startswith("linux"):
        return Check("xvfb", "ok", f"not needed on {sys.platform}")
    found = shutil.which("Xvfb")
    if found is not None:
        return Check("xvfb", "ok", f"Xvfb at {found}")
    return Check(
        "xvfb",
        "warn",
        "Xvfb is not installed, so auto display falls back to headless Chrome, whose "
        "user agent advertises the HeadlessChrome marker; install xvfb for a headed "
        "browser on Linux.",
    )


def _permissions_check(config: XPosterConfig) -> Check:
    home = config.home
    if not home.exists():
        return Check(
            "permissions",
            "ok",
            f"{home} does not exist yet; xposter creates it with mode 0700.",
        )

    problems = SessionStore(config).audit_permissions()
    if problems:
        shown = "; ".join(f"{path}: {problem}" for path, problem in problems)
        return Check(
            "permissions",
            "fail",
            f"private paths are too open: {shown}. Run `chmod -R go-rwx` on {home}.",
        )
    return Check(
        "permissions",
        "ok",
        "home, credential files and artifacts are private (0700/0600).",
    )


def _session_check(store: SessionStore) -> Check:
    summary = store.summarize()
    if not summary.present:
        return Check(
            "session", "warn", "No saved X session; run `xposter login` for the one-time sign-in."
        )
    if summary.expired:
        return Check(
            "session", "warn", "The saved session looks expired; run `xposter login` again."
        )
    handle = f"@{summary.handle}" if summary.handle else "handle not recorded"
    expiry = (
        f", auth cookie expires in {summary.days_left} day(s)"
        if summary.days_left is not None
        else ""
    )
    verified = f", last verified {summary.last_verified_at}" if summary.last_verified_at else ""
    return Check("session", "ok", f"Saved session for {handle}{expiry}{verified}.")


def _parked_check(store: SessionStore) -> Check:
    info = store.parked()
    if info is None:
        return Check("parked", "ok", "Not parked; automation may run.")
    since = f" since {info.since}" if info.since else ""
    return Check(
        "parked",
        "fail",
        f"Parked{since}: {info.reason}. Check the account in a normal browser, resolve "
        "the challenge or lock, then run `xposter unpark`.",
    )


def _ledger_check(config: XPosterConfig) -> Check:
    home = config.home
    try:
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(prefix=".doctor-", dir=home)
        os.close(descriptor)
        Path(raw_path).unlink()
    except OSError as exc:
        return Check(
            "ledger",
            "fail",
            f"{home} is not writable ({exc}); fix ownership or permissions before posting.",
        )

    if config.ledger_path.exists():
        try:
            data = json.loads(config.ledger_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("the ledger root is not a JSON object")
        except (OSError, ValueError) as exc:
            return Check(
                "ledger",
                "fail",
                f"{config.ledger_path.name} is corrupt ({type(exc).__name__}); restore it "
                "from a backup, or run `xposter ledger --reset-corrupt` before posting.",
            )
    return Check("ledger", "ok", f"{home} is writable and the ledger file is valid.")


def _clock_check() -> Check:
    local = datetime.now(UTC).astimezone()
    offset = local.strftime("%z")
    rendered_offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    detail = (
        f"{local.isoformat(timespec='seconds')} ({local.tzname() or 'local'}, {rendered_offset})"
    )
    if local.year < 2024:
        return Check(
            "clock", "warn", f"{detail}; the system clock looks wrong, which breaks pacing."
        )
    return Check("clock", "ok", detail)


def _risk_check() -> Check:
    return Check(
        "risk",
        "warn",
        "X's automation rules prohibit scripting the website, so this account is at risk "
        "of permanent suspension; use a test account first and prefer the official API.",
    )


async def tells_report(config: XPosterConfig, store: SessionStore, **kw: Any) -> list[Tell]:
    """Open the saved profile and measure what the live browser reports.

    Login is not required for a tells measurement, so ``check_login`` defaults
    to false; a logged-out page is still a valid sample of the browser itself.
    """
    kw.setdefault("check_login", False)
    kw.setdefault("goto", "/home")
    async with authenticated_page(config, store, **kw) as authed:
        raw = await collect_tells(authed.page)
        return assess_tells(raw, authed.display)


async def drafts_report(config: XPosterConfig, store: SessionStore, **kw: Any) -> DraftsReport:
    """List saved drafts read-only, flagging calibration leftovers.

    This command never edits or deletes a draft. It opens the composer, clicks
    only the Drafts control, records what is visible, and then dismisses the
    drafts view with Escape. Any deletion stays in the operator's hands.
    """
    wait_timeout_s = float(kw.pop("wait_timeout_s", _DEFAULT_WAIT_S))
    wait_interval_s = max(0.01, float(kw.pop("wait_interval_s", _DEFAULT_POLL_S)))
    networkidle_timeout_ms = int(kw.pop("networkidle_timeout_ms", _DEFAULT_NETWORKIDLE_MS))
    drafts_wait_s = float(kw.pop("drafts_wait_s", _DEFAULT_DRAFTS_WAIT_S))
    drafts_poll_s = max(0.01, float(kw.pop("drafts_poll_s", _DEFAULT_CLEANUP_POLL_S)))
    sleep = cast(Callable[[float], Awaitable[None]], kw.pop("sleep", asyncio.sleep))
    clock = cast(Callable[[], float], kw.pop("clock", time.monotonic))

    kw.setdefault("check_login", False)
    kw.setdefault("goto", "/compose/post")
    async with authenticated_page(config, store, **kw) as authed:
        page = authed.page
        artifacts = store.new_artifacts_dir("drafts-audit")
        if networkidle_timeout_ms > 0:
            with suppress(PlaywrightError):
                await page.wait_for_load_state("networkidle", timeout=networkidle_timeout_ms)

        rendered, waited_s = await _wait_for_composer(
            page,
            timeout_s=wait_timeout_s,
            interval_s=wait_interval_s,
            sleep=sleep,
            clock=clock,
        )
        drafts: list[str] = []
        control_found = False
        panel_opened = False
        empty_state = False
        note = ""
        if rendered:
            for candidate in _DRAFTS_CONTROL_SELECTORS:
                if await _click_candidate(page, candidate):
                    control_found = True
                    break
            if not control_found:
                note = "Drafts control not found or not clickable inside the composer modal"
            else:
                panel = await _wait_for_drafts_panel(
                    page, sleep, timeout_s=drafts_wait_s, poll_s=drafts_poll_s
                )
                if panel is None:
                    note = "Drafts panel did not open"
                else:
                    panel_opened = True
                    drafts = await _draft_entry_texts(panel)
                    empty_state = await _empty_drafts_visible(panel) if not drafts else False
                    if not drafts and not empty_state:
                        drafts = await _draft_fallback_lines(panel)
                    if not drafts and not empty_state:
                        note = (
                            "Drafts panel opened, but no entries or empty state matched; "
                            "inspect the saved HTML."
                        )
        else:
            note = f"composer never rendered within {wait_timeout_s:g} s"

        screenshot = await _screenshot(page, store, artifacts, "drafts")
        html_path = await _drafts_html(page, store, artifacts)
        report_path = store.write_artifact(
            artifacts,
            "drafts.json",
            json.dumps(
                {
                    "captured_at": datetime.now(UTC).isoformat(),
                    "handle": authed.handle,
                    "display": str(authed.display),
                    "rendered": rendered,
                    "waited_s": round(waited_s, 3),
                    "drafts": drafts,
                    "drafts_control_found": control_found,
                    "panel_opened": panel_opened,
                    "empty_state": empty_state,
                    "note": note,
                    "screenshot": screenshot.name if screenshot is not None else None,
                    "dialog_html": html_path.name if html_path is not None else None,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        if control_found:
            with suppress(PlaywrightError):
                await page.keyboard.press("Escape")
            await sleep(0.3)
        return DraftsReport(
            rendered=rendered,
            drafts=drafts,
            artifacts=artifacts,
            screenshot=screenshot,
            dialog_html=html_path,
            report_json=report_path,
            note=note,
            drafts_control_found=control_found,
            panel_opened=panel_opened,
            empty_state=empty_state,
        )


async def _drafts_panel(page: Page) -> Locator | None:
    panel = await _find_visible(page, _DRAFT_PANEL_SELECTORS)
    if panel is not None:
        return panel
    modal = await _find_visible(page, (_MODAL_SCOPE, '[role="dialog"]'))
    if modal is None:
        return None
    try:
        text = await modal.inner_text()
    except PlaywrightError:
        return None
    if "drafts" not in text.lower():
        return None
    # A full-view drafts replacement has no editor left. If the editor is
    # still visible, this is the composer plus its Drafts button, not a panel.
    if await _visible_editors(page):
        return None
    return modal


async def _wait_for_drafts_panel(
    page: Page,
    sleep: Callable[[float], Awaitable[None]],
    *,
    timeout_s: float,
    poll_s: float,
) -> Locator | None:
    attempts = max(1, int(timeout_s / poll_s) + 1)
    for _ in range(attempts):
        panel = await _drafts_panel(page)
        if panel is not None:
            return panel
        await sleep(poll_s)
    return await _drafts_panel(page)


async def _draft_entry_texts(panel: Locator) -> list[str]:
    texts: list[str] = []
    for selector in _DRAFT_ENTRY_SELECTORS:
        locator = panel.locator(selector)
        try:
            total = await locator.count()
        except PlaywrightError:
            continue
        for index in range(total):
            entry = locator.nth(index)
            try:
                if not await entry.is_visible():
                    continue
                if await entry.evaluate("el => Boolean(el.closest('article'))"):
                    continue
                text = _truncate_text(" ".join((await entry.inner_text()).split()), 60)
            except PlaywrightError:
                continue
            if text and text not in texts:
                texts.append(text)
        if texts:
            break
    return texts[:20]


async def _empty_drafts_visible(panel: Locator) -> bool:
    try:
        text = await panel.inner_text()
    except PlaywrightError:
        return False
    return bool(_EMPTY_DRAFTS_RE.search(text))


async def _draft_fallback_lines(panel: Locator) -> list[str]:
    try:
        raw = await panel.inner_text()
    except PlaywrightError:
        return []
    labels = {
        "drafts",
        "edit",
        "delete",
        "select",
        "save",
        "post",
        "post all",
        "cancel",
        "discard",
        "close",
        "back",
    }
    lines: list[str] = []
    for line in raw.splitlines():
        text = _truncate_text(" ".join(line.split()), 60)
        if len(text) < 3 or text.lower() in labels:
            continue
        if _EMPTY_DRAFTS_RE.search(text):
            continue
        if text not in lines:
            lines.append(text)
    return lines[:20]


async def _drafts_html(page: Page, store: SessionStore, artifacts: Path) -> Path | None:
    try:
        _overlays, html = await _overlay_snapshot(page)
    except PlaywrightError:
        html = ""
    if not html:
        html = await _dialog_html(page) or ""
    if not html:
        with suppress(PlaywrightError):
            html = await page.content()
    return store.write_artifact(
        artifacts,
        "drafts.html",
        _truncate_html(sanitise_html(html or "<!-- no drafts overlay captured -->")),
        sanitise=True,
    )


def _truncate_text(text: str, width: int) -> str:
    return text if len(text) <= width else f"{text[: width - 3]}..."


def make_calibration_png(width: int = 320, height: int = 180) -> bytes:
    """Build a tiny valid gradient PNG using only struct and zlib.

    Pillow is deliberately not a dependency of this package, and calibration
    must work on a bare host, so the encoder is the smallest correct PNG:
    RGB, 8 bits per channel, filter type 0 on every scanline.
    """
    if width < 1 or height < 1:
        raise ValueError("PNG dimensions must be positive.")

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for _ in range(height):
        raw.append(0)
        for x in range(width):
            red = (x * 255) // max(1, width - 1)
            green = 96
            blue = 160
            raw.extend((red, green, blue))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


async def _media_input(page: Page) -> Locator | None:
    for selector in _MEDIA_INPUT_SELECTORS:
        locator = page.locator(selector)
        try:
            if await locator.count() > 0:
                return locator.first
        except PlaywrightError:
            continue
    return None


async def _media_preview(page: Page) -> Locator | None:
    return await _find_visible(page, _MEDIA_PREVIEW_SELECTORS)


async def _node_has_media_controls(node: Locator) -> bool:
    selectors = (
        'button[aria-label="Remove media"]',
        'button[aria-label="Edit media"]',
        ':text-is("Add description")',
        ':text-is("Tag people")',
    )
    for selector in selectors:
        locator = node.locator(selector)
        try:
            total = await locator.count()
        except PlaywrightError:
            continue
        for index in range(total):
            try:
                if await locator.nth(index).is_visible():
                    return True
            except PlaywrightError:
                continue
    return False


async def _node_has_editor(node: Locator) -> bool:
    locator = node.locator(
        'textarea[aria-multiline="true"], [contenteditable="true"], [data-testid^="tweetTextarea_"]'
    )
    try:
        total = await locator.count()
    except PlaywrightError:
        return False
    for index in range(total):
        try:
            if await locator.nth(index).is_visible():
                return True
        except PlaywrightError:
            continue
    return False


async def _media_region(page: Page) -> Locator | None:
    """Find the nearest preview-image ancestor that holds its media controls.

    The live preview img sits inside a small wrapper, while Edit/Remove/Tag/Add
    description live in an ancestor. Walking up to the first ancestor with a
    media control gives a useful attachment region and stops at the editor item,
    keeping the footer's character-counter progressbar out of it.
    """
    preview = await _media_preview(page)
    if preview is None:
        return None
    first_parent: Locator | None = None
    node = preview
    for _ in range(12):
        try:
            node = node.locator("xpath=..")
            role = await node.get_attribute("role")
            aria_modal = await node.get_attribute("aria-modal")
            if role == "dialog" and aria_modal == "true":
                break
            if first_parent is None:
                first_parent = node
            testid = await node.get_attribute("data-testid")
            if testid == "attachments":
                return node
            if await _node_has_media_controls(node):
                return node
            if await _node_has_editor(node):
                return node
            is_item = await node.evaluate(
                "el => el.classList.contains('fx-item') ||"
                " Boolean(el.querySelector(':scope > [data-fx-input]'))"
            )
            if is_item:
                return node
        except PlaywrightError:
            break
    return first_parent


async def _media_loaded(preview: Locator | None) -> bool:
    if preview is None:
        return False
    try:
        return bool(
            await preview.evaluate(
                "el => el.tagName.toLowerCase() === 'img'"
                " ? el.naturalWidth > 0"
                " : Boolean(el.querySelector('img') && el.querySelector('img').naturalWidth > 0)"
            )
        )
    except PlaywrightError:
        return False


async def _media_progress_in_region(region: Locator | None) -> bool:
    if region is None:
        return False
    locator = region.locator('[role="progressbar"]')
    try:
        total = await locator.count()
    except PlaywrightError:
        return False
    for index in range(total):
        try:
            if await locator.nth(index).is_visible():
                return True
        except PlaywrightError:
            continue
    return False


def media_ready_state(
    preview_loaded: bool, progressbar_in_attachment: bool, post_enabled: bool
) -> bool:
    """Pure readiness rule: loaded preview, no attachment progress, Post enabled.

    The composer footer always contains a character-counter ring with
    ``role=progressbar``; it must never block readiness.
    """
    return preview_loaded and not progressbar_in_attachment and post_enabled


async def _media_modal(page: Page) -> Locator | None:
    """Return the innermost visible modal dialog."""
    locator = page.locator(_MODAL_SCOPE)
    try:
        total = await locator.count()
    except PlaywrightError:
        return None
    best: Locator | None = None
    best_depth = -1
    for index in range(total):
        candidate = locator.nth(index)
        try:
            if not await candidate.is_visible():
                continue
            depth = await candidate.evaluate(
                "el => { let d = 0; let node = el;"
                " while (node.parentElement) { node = node.parentElement;"
                ' if (node.matches(\'[role="dialog"][aria-modal="true"]\')) d += 1; }'
                " return d; }"
            )
        except PlaywrightError:
            continue
        if isinstance(depth, int) and depth > best_depth:
            best = candidate
            best_depth = depth
    return best


def _redact_blob_urls(html: str) -> str:
    return re.sub(r"blob:[^\s\"'<>&]+", "blob:REDACTED", html)


async def _media_modal_html(page: Page, store: SessionStore, artifacts: Path) -> Path | None:
    modal = await _media_modal(page)
    if modal is None:
        return None
    try:
        markup = await modal.evaluate("el => el.outerHTML")
    except PlaywrightError:
        return None
    if not isinstance(markup, str):
        return None
    return store.write_artifact(
        artifacts,
        "media-modal.html",
        _truncate_html(_redact_blob_urls(sanitise_html(markup)), 400 * 1024),
        sanitise=True,
    )


async def _first_visible(locator: Locator) -> Locator | None:
    try:
        total = await locator.count()
    except PlaywrightError:
        return None
    for index in range(total):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible():
                return candidate
        except PlaywrightError:
            continue
    return None


async def _visible_items(locator: Locator) -> list[Locator]:
    try:
        total = await locator.count()
    except PlaywrightError:
        return []
    visible: list[Locator] = []
    for index in range(total):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible():
                visible.append(candidate)
        except PlaywrightError:
            continue
    return visible


async def _exact_text_control(scope: Locator, text: str) -> Locator | None:
    locator = scope.get_by_text(text, exact=True)
    try:
        total = await locator.count()
    except PlaywrightError:
        return None
    for index in range(total):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible():
                return candidate
        except PlaywrightError:
            continue
    return None


async def _exact_control(scope: Locator, text: str, *, allow_plain: bool = True) -> Locator | None:
    """Find an exact-text control, climbing to its link/button ancestor.

    Live X wraps "Add description" and "Tag people" in an ``<a role="link">``;
    the fake attaches bare spans next to a real aria-labelled button. Returning
    the clickable ancestor avoids clicking a decorative span.
    """
    locator = scope.get_by_text(text, exact=True)
    try:
        total = await locator.count()
    except PlaywrightError:
        return None
    fallback: Locator | None = None
    for index in range(total):
        candidate = locator.nth(index)
        try:
            if not await candidate.is_visible():
                continue
        except PlaywrightError:
            continue
        if allow_plain and fallback is None:
            fallback = candidate
        control = candidate
        for _ in range(8):
            try:
                tag = await control.evaluate("el => el.tagName.toLowerCase()")
                role = await control.get_attribute("role")
                tabindex = await control.get_attribute("tabindex")
            except PlaywrightError:
                break
            if tag in {"a", "button"} or role in {"button", "link"} or tabindex is not None:
                return control
            try:
                control = control.locator("xpath=..")
            except PlaywrightError:
                break
    return fallback


async def _aria_control(scope: Locator, aria_label: str) -> Locator | None:
    locator = scope.locator(f'[aria-label="{aria_label}"]')
    try:
        total = await locator.count()
    except PlaywrightError:
        return None
    for index in range(total):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible():
                return candidate
        except PlaywrightError:
            continue
    return None


async def _exact_button(scope: Locator, text: str) -> Locator | None:
    """Find a visible button by its exact accessible name."""
    try:
        locator = scope.get_by_role("button", name=text, exact=True)
        total = await locator.count()
    except PlaywrightError:
        return None
    for index in range(total):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible():
                return candidate
        except PlaywrightError:
            continue
    return None


async def _attribute(locator: Locator, name: str) -> str | None:
    try:
        return await locator.get_attribute(name)
    except PlaywrightError:
        return None


async def _control_record(name: str, locator: Locator) -> dict[str, object]:
    try:
        tag = await locator.evaluate("el => el.tagName.toLowerCase()")
    except PlaywrightError:
        tag = ""
    text, aria_label, testid = await _element_meta(locator)
    data: dict[str, object] = {
        "name": name,
        "found": True,
        "tag": tag,
        "role": await _attribute(locator, "role"),
        "data_testid": testid,
        "aria_label": aria_label,
        "href": await _attribute(locator, "href"),
        "tabindex": await _attribute(locator, "tabindex"),
        "text": _truncate_text(" ".join(text.split()), 80),
        "outer_html": "",
        "parent_html": "",
    }
    try:
        outer = await locator.evaluate("el => el.outerHTML")
    except PlaywrightError:
        outer = ""
    if isinstance(outer, str):
        data["outer_html"] = _truncate_html(_redact_blob_urls(sanitise_html(outer)), 4096)
    try:
        parent = locator.locator("xpath=..")
        parent_html = await parent.evaluate("el => el.outerHTML")
    except PlaywrightError:
        parent_html = ""
    if isinstance(parent_html, str):
        data["parent_html"] = _truncate_html(_redact_blob_urls(sanitise_html(parent_html)), 4096)
    return data


async def _media_controls(page: Page, modal: Locator | None) -> tuple[dict[str, object], ...]:
    if modal is None:
        return ()
    records: list[dict[str, object]] = []
    add_description = await _exact_control(modal, "Add description")
    if add_description is None:
        add_description = await _aria_control(modal, "Add description")
    records.append(
        await _control_record("add_description", add_description)
        if add_description is not None
        else {"name": "add_description", "found": False}
    )
    tag_people = await _exact_control(modal, "Tag people")
    if tag_people is None:
        tag_people = await _aria_control(modal, "Tag people")
    records.append(
        await _control_record("tag_people", tag_people)
        if tag_people is not None
        else {"name": "tag_people", "found": False}
    )
    edit_media = await _aria_control(modal, "Edit media")
    records.append(
        await _control_record("edit_media", edit_media)
        if edit_media is not None
        else {"name": "edit_media", "found": False}
    )
    return tuple(records)


async def _media_post_enabled(page: Page) -> bool:
    for selector in _MEDIA_POST_SELECTORS:
        locator = await _find_visible(page, (selector,))
        if locator is None:
            continue
        try:
            if await locator.is_enabled():
                return True
        except PlaywrightError:
            continue
    return False


async def _wait_media_ready(
    page: Page,
    sleep: Callable[[float], Awaitable[None]],
    *,
    timeout_s: float,
    poll_s: float,
) -> bool:
    attempts = max(1, int(timeout_s / poll_s) + 1)
    for _ in range(attempts):
        preview = await _media_preview(page)
        region = await _media_region(page)
        progress = await _media_progress_in_region(region)
        loaded = await _media_loaded(preview)
        enabled = await _media_post_enabled(page)
        if media_ready_state(loaded, progress, enabled):
            return True
        await sleep(poll_s)
    preview = await _media_preview(page)
    region = await _media_region(page)
    progress = await _media_progress_in_region(region)
    loaded = await _media_loaded(preview)
    return media_ready_state(loaded, progress, await _media_post_enabled(page))


async def _find_alt_control(page: Page) -> Locator | None:
    modal = await _media_modal(page)
    if modal is None:
        return None
    control = await _exact_control(modal, "Add description", allow_plain=False)
    if control is None:
        control = await _aria_control(modal, "Add description")
    if control is None:
        control = await _exact_text_control(modal, "Add description")
    return control


async def _find_edit_control(page: Page) -> Locator | None:
    modal = await _media_modal(page)
    if modal is None:
        return None
    return await _aria_control(modal, "Edit media")


async def _inputs_in(scope: Locator) -> tuple[dict[str, object], ...]:
    elements = scope.locator('input, textarea, select, [contenteditable="true"]')
    try:
        total = await elements.count()
    except PlaywrightError:
        return ()
    inputs: list[dict[str, object]] = []
    for index in range(total):
        element = elements.nth(index)
        try:
            if not await element.is_visible():
                continue
            tag = await element.evaluate("el => el.tagName.toLowerCase()")
            data = {
                "tag": tag,
                "type": (await element.get_attribute("type")) or "",
                "aria_label": (await element.get_attribute("aria-label")) or "",
                "data_testid": (await element.get_attribute("data-testid")) or "",
                "placeholder": (await element.get_attribute("placeholder")) or "",
                "role": (await element.get_attribute("role")) or "",
                "maxlength": (await element.get_attribute("maxlength")) or "",
                "disabled": await element.is_disabled(),
            }
        except PlaywrightError:
            continue
        inputs.append(data)
    return tuple(inputs)


async def _overlay_heading(overlay: Locator) -> str:
    headings = overlay.locator("h1, h2, h3, h4, [role='heading']")
    try:
        total = await headings.count()
    except PlaywrightError:
        return ""
    for index in range(total):
        heading = headings.nth(index)
        try:
            if await heading.is_visible():
                return _truncate_text(" ".join((await heading.inner_text()).split()), 120)
        except PlaywrightError:
            continue
    return ""


async def _new_overlay(page: Page, composer: Locator | None, kind: str) -> Locator | None:
    locator = page.locator(
        '[role="dialog"], [role="alertdialog"], [data-testid="sheetDialog"], '
        '[data-testid="confirmationSheetDialog"], [data-testid*="crop" i]'
    )
    try:
        total = await locator.count()
    except PlaywrightError:
        return None
    composer_handle = await composer.element_handle() if composer is not None else None
    fallback: Locator | None = None
    for index in range(total):
        candidate = locator.nth(index)
        try:
            if not await candidate.is_visible():
                continue
            if composer_handle is not None:
                is_composer_or_ancestor = await candidate.evaluate(
                    "(el, other) => el === other || el.contains(other)", composer_handle
                )
                if is_composer_or_ancestor:
                    continue
            text = " ".join((await candidate.inner_text()).split()).lower()
            controls = await candidate.locator(
                'textarea, input, select, [contenteditable="true"]'
            ).count()
        except PlaywrightError:
            continue
        if kind == "alt":
            kind_match = "description" in text or "alt" in text
        else:
            kind_match = "edit" in text or "crop" in text or "adjust" in text
        if kind_match:
            return candidate
        if fallback is None and controls > 0:
            fallback = candidate
    return fallback


async def _wait_new_overlay(
    page: Page,
    composer: Locator | None,
    sleep: Callable[[float], Awaitable[None]],
    *,
    kind: str,
    timeout_s: float = 4.0,
    poll_s: float = 0.1,
) -> Locator | None:
    attempts = max(1, int(timeout_s / poll_s) + 1)
    for _ in range(attempts):
        overlay = await _new_overlay(page, composer, kind)
        if overlay is not None:
            return overlay
        await sleep(poll_s)
    return await _new_overlay(page, composer, kind)


async def _handle_alive(handle: Any) -> bool:
    try:
        return bool(
            await handle.evaluate(
                "el => el.isConnected && (el.offsetWidth > 0 || el.offsetHeight > 0)"
            )
        )
    except PlaywrightError:
        return False


async def _close_editor_overlay(
    page: Page,
    overlay: Locator,
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[bool, str]:
    handle = await overlay.element_handle()
    with suppress(PlaywrightError):
        await page.keyboard.press("Escape")
    await sleep(0.3)
    if handle is not None and await _handle_alive(handle):
        buttons = overlay.locator("button")
        try:
            total = await buttons.count()
        except PlaywrightError:
            total = 0
        for index in range(total):
            button = buttons.nth(index)
            try:
                if not await button.is_visible():
                    continue
                text, aria_label, testid = await _element_meta(button)
                label = text.strip().lower()
                aria = aria_label.strip().lower()
                if label not in {"back", "cancel", "close"} and aria not in {
                    "back",
                    "cancel",
                    "close",
                }:
                    continue
                assert_safe_click(text=text, aria_label=aria_label, data_testid=testid)
                await button.dispatch_event("click")
                await sleep(0.2)
                closed = handle is None or not await _handle_alive(handle)
                return (closed, f"closed with {text or aria_label}")
            except (PlaywrightError, UnsafeClickError):
                continue
        closed = handle is None or not await _handle_alive(handle)
        return (closed, "no Back/Cancel/Close control")
    return True, "closed with Escape"


async def _capture_overlay_evidence(
    page: Page,
    store: SessionStore,
    artifacts: Path,
    overlay: Locator,
    *,
    html_name: str,
    screenshot_name: str,
    heading_fallback: str = "",
) -> tuple[
    Path | None,
    tuple[ButtonInfo, ...],
    tuple[dict[str, object], ...],
    str,
    Path | None,
]:
    """Record HTML, buttons, inputs, heading and a screenshot of one overlay."""
    buttons = await _buttons_in(overlay)
    inputs = await _inputs_in(overlay)
    heading = await _overlay_heading(overlay)
    if not heading and heading_fallback:
        title = await _first_visible(overlay.get_by_text(heading_fallback, exact=True))
        if title is not None:
            heading = heading_fallback
    html_path: Path | None = None
    try:
        markup = await overlay.evaluate("el => el.outerHTML")
    except PlaywrightError:
        markup = None
    if isinstance(markup, str):
        html_path = store.write_artifact(
            artifacts,
            html_name,
            _truncate_html(_redact_blob_urls(sanitise_html(markup)), 200 * 1024),
            sanitise=True,
        )
    screenshot = await _screenshot(page, store, artifacts, screenshot_name)
    return html_path, buttons, inputs, heading, screenshot


async def _capture_editor(
    page: Page,
    store: SessionStore,
    artifacts: Path,
    *,
    trigger: Locator | None,
    kind: str,
    html_name: str,
    screenshot_name: str,
    sleep: Callable[[float], Awaitable[None]],
) -> MediaAltEditor | None:
    if trigger is None:
        return None
    composer = await _media_modal(page)
    text, aria_label, testid = await _element_meta(trigger)
    try:
        assert_safe_click(text=text, aria_label=aria_label, data_testid=testid, phase="D")
        if kind == "edit":
            # The Edit control is an image overlay; dispatch directly so an
            # overlapping sibling cannot steal the synthetic click.
            await trigger.dispatch_event("click")
        else:
            await trigger.click(timeout=3000)
    except (PlaywrightError, UnsafeClickError) as exc:
        return MediaAltEditor(
            False, None, (), (), False, f"{kind} control click failed: {exc}"[:200]
        )

    overlay = await _wait_new_overlay(page, composer, sleep, kind=kind)
    if overlay is None:
        return MediaAltEditor(False, None, (), (), False, f"{kind} editor did not open within 4 s")
    html_path, buttons, inputs, heading, screenshot = await _capture_overlay_evidence(
        page,
        store,
        artifacts,
        overlay,
        html_name=html_name,
        screenshot_name=screenshot_name,
    )
    closed, close_note = await _close_editor_overlay(page, overlay, sleep)
    image_visible = await _media_preview(page) is not None
    note = close_note
    if not closed:
        note = f"{note}; editor overlay is still open"
    if not image_visible:
        note = f"{note}; composer no longer shows the image"
    return MediaAltEditor(
        True,
        html_path,
        buttons,
        inputs,
        closed,
        note,
        heading,
        screenshot,
        image_visible,
    )


async def _find_alt_intro(page: Page) -> Locator | None:
    """Find the one-time "Add descriptions" sheet, if it is on screen.

    The live flow stacks it over the real "Edit image description" editor, so
    the sheet must be dismissed before the editor can be captured. Match the
    sheet's own testid first: the composer may be its DOM ancestor.
    """
    for sheet in await _visible_items(page.locator('[data-testid="sheetDialog"]')):
        try:
            text = " ".join((await sheet.inner_text()).split()).lower()
            sure = await sheet.locator(f'[data-testid="{_ALT_INTRO_SURE_TESTID}"]').count()
            decline = await sheet.get_by_text(_ALT_INTRO_DECLINE, exact=True).count()
        except PlaywrightError:
            continue
        if _ALT_INTRO_TITLE.lower() in text or sure > 0 or decline > 0:
            return sheet

    # If the sheet testid changed, climb from the "Sure" testid or an exact
    # "No, thanks" to the smallest sheet/dialog container.
    anchors = (
        page.locator(f'[data-testid="{_ALT_INTRO_SURE_TESTID}"]'),
        page.get_by_text(_ALT_INTRO_DECLINE, exact=True),
    )
    for anchor in anchors:
        control = await _first_visible(anchor)
        if control is None:
            continue
        node = control
        for _ in range(12):
            try:
                node = node.locator("xpath=..")
                tag = await node.evaluate("el => el.tagName.toLowerCase()")
                role = await node.get_attribute("role")
                testid = await node.get_attribute("data-testid")
            except PlaywrightError:
                break
            if testid == "sheetDialog" or role in {"dialog", "alertdialog"}:
                return node
            if tag in {"body", "html"}:
                break
    return None


async def _editor_ancestor(anchor: Locator) -> Locator | None:
    """Walk from a field/title to the smallest container with buttons and a field."""
    node = anchor
    for _ in range(14):
        try:
            node = node.locator("xpath=..")
            tag = await node.evaluate("el => el.tagName.toLowerCase()")
        except PlaywrightError:
            return None
        if tag in {"body", "html"}:
            return None
        buttons = node.locator("button, [role='button']")
        fields = node.locator(
            'textarea, input, [data-testid="altTextInput"], [contenteditable="true"]'
        )
        if await _first_visible(buttons) is not None and await _first_visible(fields) is not None:
            return node
    return None


async def _find_alt_editor_root(page: Page) -> Locator | None:
    """Locate the real editor, whose visible title is "Edit image description"."""
    title = await _first_visible(page.get_by_text(_ALT_EDITOR_TITLE, exact=True))
    if title is not None:
        root = await _editor_ancestor(title)
        if root is not None:
            return root
    field = await _find_visible(page, _ALT_EDITOR_FIELD_SELECTORS)
    if field is not None:
        return await _editor_ancestor(field)
    return None


async def _wait_alt_stage(
    page: Page,
    sleep: Callable[[float], Awaitable[None]],
    *,
    timeout_s: float = 4.0,
    poll_s: float = 0.1,
) -> tuple[Locator | None, Locator | None]:
    """Wait for the intro sheet or the alt editor, intro first."""
    attempts = max(1, int(timeout_s / poll_s) + 1)
    for _ in range(attempts):
        intro = await _find_alt_intro(page)
        if intro is not None:
            return intro, None
        editor = await _find_alt_editor_root(page)
        if editor is not None:
            # The sheet can animate in a beat after the editor underneath it.
            await sleep(0.2)
            late_intro = await _find_alt_intro(page)
            if late_intro is not None:
                return late_intro, None
            return None, editor
        await sleep(poll_s)
    return await _find_alt_intro(page), await _find_alt_editor_root(page)


async def _wait_alt_editor(
    page: Page,
    sleep: Callable[[float], Awaitable[None]],
    *,
    timeout_s: float = 4.0,
    poll_s: float = 0.1,
) -> Locator | None:
    attempts = max(1, int(timeout_s / poll_s) + 1)
    for _ in range(attempts):
        editor = await _find_alt_editor_root(page)
        if editor is not None:
            return editor
        await sleep(poll_s)
    return await _find_alt_editor_root(page)


async def _wait_until_hidden(
    locator: Locator,
    sleep: Callable[[float], Awaitable[None]],
    *,
    timeout_s: float = 2.0,
    poll_s: float = 0.1,
) -> bool:
    try:
        handle = await locator.element_handle()
    except PlaywrightError:
        return True
    if handle is None:
        return True
    attempts = max(1, int(timeout_s / poll_s) + 1)
    for _ in range(attempts):
        if not await _handle_alive(handle):
            return True
        await sleep(poll_s)
    return not await _handle_alive(handle)


async def _capture_alt_intro(
    page: Page,
    store: SessionStore,
    artifacts: Path,
    intro: Locator,
    sleep: Callable[[float], Awaitable[None]],
) -> MediaIntro:
    """Record the education sheet, then dismiss it with exact "No, thanks"."""
    buttons = await _buttons_in(intro)
    html_path: Path | None = None
    try:
        markup = await intro.evaluate("el => el.outerHTML")
    except PlaywrightError:
        markup = None
    if isinstance(markup, str):
        html_path = store.write_artifact(
            artifacts,
            "media-alt-intro.html",
            _truncate_html(_redact_blob_urls(sanitise_html(markup)), 100 * 1024),
            sanitise=True,
        )

    decline = await _exact_button(intro, _ALT_INTRO_DECLINE)
    if decline is None:
        decline = await _exact_text_control(intro, _ALT_INTRO_DECLINE)
    if decline is None:
        return MediaIntro(
            True,
            html_path,
            buttons,
            False,
            "'No, thanks' not found in the intro sheet; left it open for cleanup",
        )
    text, aria_label, testid = await _element_meta(decline)
    try:
        assert_safe_click(text=text, aria_label=aria_label, data_testid=testid, phase="D")
        await decline.click(timeout=3000)
    except (PlaywrightError, UnsafeClickError) as exc:
        return MediaIntro(
            True,
            html_path,
            buttons,
            False,
            f"'No, thanks' click failed: {exc}"[:200],
        )
    dismissed = await _wait_until_hidden(intro, sleep)
    note = "" if dismissed else "intro sheet did not disappear after 'No, thanks'"
    return MediaIntro(True, html_path, buttons, dismissed, note)


_FORBIDDEN_BACK_RE = re.compile(r"save|done|apply|post|delete|schedule|reply", re.IGNORECASE)


async def _back_like(locator: Locator) -> bool:
    text, aria_label, testid = await _element_meta(locator)
    if _FORBIDDEN_BACK_RE.search(f"{text} {aria_label} {testid}"):
        return False
    try:
        assert_safe_click(text=text, aria_label=aria_label, data_testid=testid, phase="D")
    except UnsafeClickError:
        return False
    return True


async def _find_back_control(editor: Locator) -> tuple[Locator | None, str]:
    """Find the editor header's back arrow; never return Save/Done/Apply."""
    for selector, strategy in (
        ('button[aria-label="Back" i]', "aria-label Back"),
        ('[role="button"][aria-label="Back" i]', "aria-label Back"),
        ('[data-testid*="back" i]', "data-testid back"),
    ):
        candidate = await _first_visible(editor.locator(selector))
        if candidate is not None and await _back_like(candidate):
            return candidate, strategy

    title = await _first_visible(editor.get_by_text(_ALT_EDITOR_TITLE, exact=True))
    if title is not None:
        node = title
        for _ in range(8):
            try:
                node = node.locator("xpath=..")
                tag = await node.evaluate("el => el.tagName.toLowerCase()")
            except PlaywrightError:
                break
            if tag in {"body", "html"}:
                break
            buttons = await _visible_items(node.locator("button, [role='button']"))
            if not buttons:
                continue
            first = buttons[0]
            text, aria_label, testid = await _element_meta(first)
            if _FORBIDDEN_BACK_RE.search(f"{text} {aria_label} {testid}"):
                break
            if text.strip() or aria_label.strip() or testid.strip():
                if await _back_like(first):
                    return first, "first header button"
                break
            # A bare first button in the header is X's back arrow; Save always
            # has a visible label, so position is safe here.
            return first, "first header button"

    for candidate in await _visible_items(editor.locator("button, [role='button']")):
        text, aria_label, _testid = await _element_meta(candidate)
        label = text.strip().lower()
        aria = aria_label.strip().lower()
        if (
            label in {"back", "cancel", "close"} or aria in {"back", "cancel", "close"}
        ) and await _back_like(candidate):
            return candidate, f"exact {text.strip() or aria_label.strip()}"
    return None, ""


async def _wait_alt_editor_gone(
    page: Page,
    sleep: Callable[[float], Awaitable[None]],
    *,
    timeout_s: float = 2.0,
    poll_s: float = 0.1,
) -> bool:
    attempts = max(1, int(timeout_s / poll_s) + 1)
    for _ in range(attempts):
        if await _find_alt_editor_root(page) is None:
            return True
        await sleep(poll_s)
    return await _find_alt_editor_root(page) is None


async def _leave_alt_editor(
    page: Page,
    editor: Locator,
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[bool, str, MediaBackControl]:
    """Leave the real alt editor with its back arrow; Escape would close the composer."""
    candidate, strategy = await _find_back_control(editor)
    if candidate is None:
        note = "no back arrow/Back/Cancel/Close control found; editor left open for cleanup"
        return False, note, MediaBackControl(False, note=note)
    text, aria_label, testid = await _element_meta(candidate)
    if _FORBIDDEN_BACK_RE.search(f"{text} {aria_label} {testid}"):
        note = "back candidate looked like a save/submit control; editor left open for cleanup"
        return False, note, MediaBackControl(True, strategy, text, aria_label, testid, False, note)
    safe = True
    if strategy != "first header button" or text.strip() or aria_label.strip() or testid.strip():
        try:
            assert_safe_click(text=text, aria_label=aria_label, data_testid=testid, phase="D")
        except UnsafeClickError:
            safe = False
    if not safe:
        note = "back candidate was refused by the click allow-list; editor left open for cleanup"
        return False, note, MediaBackControl(True, strategy, text, aria_label, testid, False, note)

    try:
        await candidate.click(timeout=3000)
    except PlaywrightError:
        try:
            await candidate.dispatch_event("click")
        except PlaywrightError as exc:
            note = f"back click failed: {exc}"[:200]
            return (
                False,
                note,
                MediaBackControl(True, strategy, text, aria_label, testid, False, note),
            )
    closed = await _wait_alt_editor_gone(page, sleep)
    note = (
        f"closed with {strategy}"
        if closed
        else f"{strategy} clicked but the editor is still visible"
    )
    return (
        closed,
        note,
        MediaBackControl(True, strategy, text, aria_label, testid, True, "" if closed else note),
    )


async def _capture_alt_editor(
    page: Page,
    store: SessionStore,
    artifacts: Path,
    *,
    sleep: Callable[[float], Awaitable[None]],
) -> _AltCaptureResult:
    """Click "Add description", clear the intro sheet, capture and leave the editor."""
    control = await _find_alt_control(page)
    if control is None:
        return _AltCaptureResult()
    text, aria_label, testid = await _element_meta(control)
    try:
        assert_safe_click(text=text, aria_label=aria_label, data_testid=testid, phase="D")
        await control.click(timeout=3000)
    except (PlaywrightError, UnsafeClickError) as exc:
        return _AltCaptureResult(
            editor=MediaAltEditor(
                False, None, (), (), False, f"alt control click failed: {exc}"[:200]
            )
        )

    intro_locator, editor_locator = await _wait_alt_stage(page, sleep)
    intro: MediaIntro | None = None
    if intro_locator is not None:
        intro = await _capture_alt_intro(page, store, artifacts, intro_locator, sleep)
        if not intro.dismissed:
            return _AltCaptureResult(
                editor=MediaAltEditor(True, None, (), (), False, f"alt intro: {intro.note}"),
                intro=intro,
            )
        editor_locator = await _wait_alt_editor(page, sleep)

    if editor_locator is None:
        return _AltCaptureResult(
            editor=MediaAltEditor(False, None, (), (), False, "alt editor did not open within 4 s"),
            intro=intro,
        )

    html_path, buttons, inputs, heading, screenshot = await _capture_overlay_evidence(
        page,
        store,
        artifacts,
        editor_locator,
        html_name="media-alt-editor.html",
        screenshot_name="phase-d-alt",
        heading_fallback=_ALT_EDITOR_TITLE,
    )
    closed, close_note, back = await _leave_alt_editor(page, editor_locator, sleep)
    image_visible = await _media_preview(page) is not None
    note = close_note
    if not image_visible:
        note = f"{note}; composer no longer shows the image"
    editor = MediaAltEditor(
        True,
        html_path,
        buttons,
        inputs,
        closed,
        note,
        heading,
        screenshot,
        image_visible,
    )
    return _AltCaptureResult(editor=editor, intro=intro, back_control=back)


async def _capture_edit_editor(
    page: Page,
    store: SessionStore,
    artifacts: Path,
    *,
    sleep: Callable[[float], Awaitable[None]],
) -> MediaAltEditor | None:
    control = await _find_edit_control(page)
    return await _capture_editor(
        page,
        store,
        artifacts,
        trigger=control,
        kind="edit",
        html_name="media-edit-editor.html",
        screenshot_name="phase-d-edit",
        sleep=sleep,
    )


async def _remove_media(page: Page) -> bool:
    matches = await match_report_visible(page, "composer_media_remove_button")
    for candidate, count in matches:
        if count <= 0:
            continue
        locator = await _find_visible(page, (candidate,))
        if locator is None:
            continue
        text, aria_label, testid = await _element_meta(locator)
        if aria_label.strip().lower() != "remove media" and "remove media" not in text.lower():
            continue
        try:
            assert_safe_click(text=text, aria_label=aria_label, data_testid=testid, phase="D")
            await locator.click(timeout=3000)
            return True
        except (PlaywrightError, UnsafeClickError):
            continue
    return False


async def _wait_preview_gone(
    page: Page,
    sleep: Callable[[float], Awaitable[None]],
    *,
    timeout_s: float,
    poll_s: float,
) -> bool:
    attempts = max(1, int(timeout_s / poll_s) + 1)
    for _ in range(attempts):
        if await _media_preview(page) is None:
            return True
        await sleep(poll_s)
    return await _media_preview(page) is None


async def _run_media_phase(
    page: Page,
    store: SessionStore,
    artifacts: Path,
    *,
    sleep: Callable[[float], Awaitable[None]],
    timeout_s: float,
    poll_s: float,
) -> _MediaPhaseResult:
    requests: list[str] = []

    def on_request(request: Any) -> None:
        try:
            parsed = urlsplit(str(request.url))
        except (TypeError, ValueError):
            return
        if "upload" in parsed.netloc.lower() and parsed.path not in requests:
            requests.append(parsed.path)

    page.on("request", on_request)
    note = ""
    attached = False
    ready = False
    removed = False
    preview_gone = True
    matches: dict[str, list[tuple[str, int]]] = {}
    dialog_buttons: tuple[ButtonInfo, ...] = ()
    attachment_buttons: tuple[ButtonInfo, ...] = ()
    attachment_html: Path | None = None
    media_modal_html: Path | None = None
    controls: tuple[dict[str, object], ...] = ()
    screenshot: Path | None = None
    alt_editor: MediaAltEditor | None = None
    alt_intro: MediaIntro | None = None
    alt_back_control: MediaBackControl | None = None
    edit_editor: MediaAltEditor | None = None
    alt_blocked = False
    try:
        image_path = store.write_artifact(
            artifacts, _MEDIA_CALIBRATION_NAME, make_calibration_png()
        )
        field = await _media_input(page)
        if field is None:
            note = "media file input not found inside the composer modal"
        else:
            try:
                await field.set_input_files(str(image_path))
                attached = True
            except PlaywrightError as exc:
                note = f"media file input rejected the file: {exc}"[:300]
        if attached:
            ready = await _wait_media_ready(page, sleep, timeout_s=timeout_s, poll_s=poll_s)
            if not ready:
                note = f"media preview/upload/Post-enabled did not settle within {timeout_s:g} s"
            matches = {name: await match_report_visible(page, name) for name in _MEDIA_MATCH_NAMES}
            dialog_buttons = await _dialog_buttons(page)
            modal = await _media_modal(page)
            media_modal_html = await _media_modal_html(page, store, artifacts)
            controls = await _media_controls(page, modal)
            region = await _media_region(page)
            if region is not None:
                attachment_buttons = await _buttons_in(region)
                try:
                    markup = await region.evaluate("el => el.outerHTML")
                except PlaywrightError:
                    markup = None
                if isinstance(markup, str):
                    attachment_html = store.write_artifact(
                        artifacts,
                        "media-attachments.html",
                        _truncate_html(_redact_blob_urls(sanitise_html(markup)), 100 * 1024),
                        sanitise=True,
                    )
            elif not note:
                note = "media preview did not appear inside the composer modal"
            screenshot = await _screenshot(page, store, artifacts, "phase-d")
            alt_capture = await _capture_alt_editor(page, store, artifacts, sleep=sleep)
            alt_editor = alt_capture.editor
            alt_intro = alt_capture.intro
            alt_back_control = alt_capture.back_control
            alt_blocked = alt_editor is not None and not (
                alt_editor.opened and alt_editor.closed and alt_editor.composer_image_visible
            )
            if alt_editor is None:
                note = (note + "; " if note else "") + "Add description control not found"
            elif alt_blocked:
                note = (note + "; " if note else "") + f"alt editor: {alt_editor.note}"
            edit_editor = None
            if not alt_blocked:
                edit_editor = await _capture_edit_editor(page, store, artifacts, sleep=sleep)
                if edit_editor is None:
                    note = (note + "; " if note else "") + "Edit media control not found"
                elif (
                    not edit_editor.opened
                    or not edit_editor.closed
                    or not edit_editor.composer_image_visible
                ):
                    note = (note + "; " if note else "") + f"edit editor: {edit_editor.note}"
            else:
                note = (
                    note + "; " if note else ""
                ) + "Edit media capture skipped: alt editor did not close cleanly"
        phase = await _measure_all(page)
        if screenshot is None:
            screenshot = await _screenshot(page, store, artifacts, "phase-d")
        if attached:
            if alt_blocked:
                preview_gone = await _media_preview(page) is None
                note = (note + "; " if note else "") + (
                    "media removal skipped: alt editor did not close cleanly"
                )
            else:
                removed = await _remove_media(page)
                preview_gone = await _wait_preview_gone(page, sleep, timeout_s=5.0, poll_s=poll_s)
                if not removed:
                    note = (
                        note + "; " if note else ""
                    ) + "media remove control not found or refused"
                if not preview_gone:
                    note = (note + "; " if note else "") + "attachment preview did not disappear"
        else:
            preview_gone = await _media_preview(page) is None
        return _MediaPhaseResult(
            report=MediaReport(
                attached=attached,
                ready=ready,
                note=note,
                requests=tuple(requests),
                matches=matches,
                dialog_buttons=dialog_buttons,
                attachment_buttons=attachment_buttons,
                attachment_html=attachment_html,
                screenshot=screenshot,
                alt_editor=alt_editor,
                removed=removed,
                preview_gone=preview_gone,
                media_modal_html=media_modal_html,
                controls=controls,
                edit_editor=edit_editor,
                alt_intro=alt_intro,
                alt_back_control=alt_back_control,
            ),
            phase=phase,
        )
    finally:
        with suppress(Exception):
            page.remove_listener("request", on_request)


def _editor_json(editor: MediaAltEditor | None) -> dict[str, object] | None:
    if editor is None:
        return None
    return {
        "opened": editor.opened,
        "html": editor.html.name if editor.html is not None else None,
        "buttons": [button.to_json() for button in editor.buttons],
        "inputs": [dict(item) for item in editor.inputs],
        "closed": editor.closed,
        "note": editor.note,
        "heading": editor.heading,
        "screenshot": editor.screenshot.name if editor.screenshot is not None else None,
        "composer_image_visible": editor.composer_image_visible,
    }


def _media_json(report: MediaReport) -> dict[str, object]:
    return {
        "attached": report.attached,
        "ready": report.ready,
        "note": report.note,
        "requests": list(report.requests),
        "matches": {
            name: [{"selector": selector, "count": count} for selector, count in matches]
            for name, matches in report.matches.items()
        },
        "dialog_buttons": [button.to_json() for button in report.dialog_buttons],
        "attachment_buttons": [button.to_json() for button in report.attachment_buttons],
        "attachment_html": (
            report.attachment_html.name if report.attachment_html is not None else None
        ),
        "media_modal_html": (
            report.media_modal_html.name if report.media_modal_html is not None else None
        ),
        "controls": [dict(item) for item in report.controls],
        "screenshot": report.screenshot.name if report.screenshot is not None else None,
        "alt_intro": (report.alt_intro.to_json() if report.alt_intro is not None else None),
        "alt_editor": _editor_json(report.alt_editor),
        "alt_closed": (report.alt_editor.closed if report.alt_editor is not None else False),
        "alt_back_control": (
            report.alt_back_control.to_json() if report.alt_back_control is not None else None
        ),
        "edit_editor": _editor_json(report.edit_editor),
        "edit_closed": (report.edit_editor.closed if report.edit_editor is not None else False),
        "removed": report.removed,
        "preview_gone": report.preview_gone,
    }


async def dom_report(config: XPosterConfig, store: SessionStore, **kw: Any) -> DomReport:
    """Calibrate every selector against the live composer without posting.

    The composer is opened on ``/compose/post`` (the deterministic entry point),
    then the measurement waits for React to render the editor before reading a
    single selector. Counting selectors immediately after ``domcontentloaded``
    produced a full table of false zeros on the first real run: the page was
    still showing spinners.

    Three phases follow, and none of them submits anything:

    * A: empty composer, after the render wait.
    * B: the harmless text ``xposter calibration`` typed into the first editor.
    * C: one add-post click, ``two`` typed into the second editor.

    With ``with_media=True``, phase D attaches a generated PNG, records the
    real attachment controls and removes it again before cleanup. The composer
    is then closed, always choosing Discard if a save sheet appears. Every click
    goes through :func:`assert_safe_click`, so Post, Post all, Reply, Schedule,
    Save, Delete and Edit are unreachable from calibration.
    """
    wait_timeout_s = float(kw.pop("wait_timeout_s", _DEFAULT_WAIT_S))
    wait_interval_s = max(0.01, float(kw.pop("wait_interval_s", _DEFAULT_POLL_S)))
    networkidle_timeout_ms = int(kw.pop("networkidle_timeout_ms", _DEFAULT_NETWORKIDLE_MS))
    cleanup_timeout_s = float(kw.pop("cleanup_timeout_s", _DEFAULT_CLEANUP_S))
    cleanup_poll_s = max(0.01, float(kw.pop("cleanup_poll_s", _DEFAULT_CLEANUP_POLL_S)))
    with_media = bool(kw.pop("with_media", False))
    media_timeout_s = float(kw.pop("media_timeout_s", _DEFAULT_MEDIA_WAIT_S))
    media_poll_s = max(0.01, float(kw.pop("media_poll_s", _DEFAULT_MEDIA_POLL_S)))
    sleep = cast(Callable[[float], Awaitable[None]], kw.pop("sleep", asyncio.sleep))
    clock = cast(Callable[[], float], kw.pop("clock", time.monotonic))

    kw.setdefault("check_login", False)
    kw.setdefault("goto", "/compose/post")
    async with authenticated_page(config, store, **kw) as authed:
        page = authed.page
        artifacts = store.new_artifacts_dir("dom-calibration")

        if networkidle_timeout_ms > 0:
            with suppress(PlaywrightError):
                await page.wait_for_load_state("networkidle", timeout=networkidle_timeout_ms)

        rendered, waited_s = await _wait_for_composer(
            page,
            timeout_s=wait_timeout_s,
            interval_s=wait_interval_s,
            sleep=sleep,
            clock=clock,
        )

        phases: list[Mapping[str, list[tuple[str, int]]]] = [await _measure_all(page)]
        screenshots: list[Path] = []
        phase_a = await _screenshot(page, store, artifacts, "phase-a")
        if phase_a is not None:
            screenshots.append(phase_a)

        note = ""
        if rendered:
            editors = await _visible_editors(page)
            if editors:
                await editors[0].focus()
                await page.keyboard.type("xposter calibration")
                await sleep(1.0)
                phase_b = await _measure_all(page)
                phases.append(phase_b)
                phase_b_shot = await _screenshot(page, store, artifacts, "phase-b")
                if phase_b_shot is not None:
                    screenshots.append(phase_b_shot)

                if await _add_second_editor(page, sleep=sleep):
                    editors = await _visible_editors(page)
                    if len(editors) >= 2:
                        await editors[-1].focus()
                        await page.keyboard.type("two")
                        await sleep(0.3)
                        phases.append(await _measure_all(page))
                        note = "second editor added; phase C measured the thread controls"
                    else:
                        phases.append(phase_b)
                        note = "add-post click did not create a second editor; phase C repeats B"
                else:
                    phases.append(phase_b)
                    note = "add-post button missing or refused; phase C repeats B"

                phase_c_shot = await _screenshot(page, store, artifacts, "phase-c")
                if phase_c_shot is not None:
                    screenshots.append(phase_c_shot)
            else:
                phases.extend([{}, {}])
                note = "composer visible but no editable textbox was found; phases B/C skipped"
        else:
            phases.extend([{}, {}])
            note = f"composer never rendered within {wait_timeout_s:g} s"

        if rendered:
            for missing_label in ("phase-b", "phase-c"):
                if any(path.name == f"{missing_label}.png" for path in screenshots):
                    continue
                skipped_shot = await _screenshot(page, store, artifacts, missing_label)
                if skipped_shot is not None:
                    screenshots.append(skipped_shot)

        media_report: MediaReport | None = None
        if with_media and rendered:
            media_result = await _run_media_phase(
                page,
                store,
                artifacts,
                sleep=sleep,
                timeout_s=media_timeout_s,
                poll_s=media_poll_s,
            )
            media_report = media_result.report
            phases.append(media_result.phase)
            if media_report.screenshot is not None and media_report.screenshot not in screenshots:
                screenshots.append(media_report.screenshot)
        else:
            phases.append({})
            if with_media:
                media_report = MediaReport(
                    attached=False,
                    ready=False,
                    note="composer never rendered; media phase skipped",
                    requests=(),
                    matches={},
                    dialog_buttons=(),
                    attachment_buttons=(),
                    attachment_html=None,
                    screenshot=None,
                    alt_editor=None,
                    removed=False,
                    preview_gone=True,
                )

        dialog_buttons = await _dialog_buttons(page)
        dialog_markup = await _dialog_html(page)
        html_path: Path | None = None
        if dialog_markup is not None:
            html_path = store.write_artifact(
                artifacts,
                "compose-dialog.html",
                _truncate_html(sanitise_html(dialog_markup)),
                sanitise=True,
            )

        try:
            cleanup = await _close_and_discard(
                page,
                store=store,
                artifacts=artifacts,
                sleep=sleep,
                timeout_s=cleanup_timeout_s,
                poll_s=cleanup_poll_s,
            )
        except PlaywrightError:
            # Evidence matters more than cleanup: keep the report even if the
            # page vanished while closing.
            cleanup = _CleanupResult(
                (), False, False, "failed", "failed", 0, None, None, (), None, None
            )
        discard_sheet = cleanup.discard_sheet
        dialog_closed = cleanup.dialog_closed
        discard_action = cleanup.discard_action
        rows = tuple(_phase_row(name, phases) for name in SELECTORS)
        if not rendered:
            # A composer that never rendered is not evidence that any selector
            # is wrong; keep the counts but do not label them as missing.
            rows = tuple(replace(row, missing=False) for row in rows)

        report_path = store.write_artifact(
            artifacts,
            "report.json",
            json.dumps(
                {
                    "captured_at": datetime.now(UTC).isoformat(),
                    "handle": authed.handle,
                    "display": str(authed.display),
                    "rendered": rendered,
                    "waited_s": round(waited_s, 3),
                    "timeout_s": wait_timeout_s,
                    "note": note,
                    "phases": ["A", "B", "C", "D"],
                    "rows": [
                        {
                            "name": row.name,
                            "candidate": row.candidate,
                            "count": row.count,
                            "missing": row.missing,
                            "phase_counts": {
                                label: value
                                for label, value in zip(
                                    ("A", "B", "C", "D"),
                                    _padded_counts(row.phase_counts),
                                    strict=True,
                                )
                            },
                            "candidates": [
                                {"selector": selector, "count": count}
                                for selector, count in row.candidates
                            ],
                        }
                        for row in rows
                    ],
                    "screenshots": [path.name for path in screenshots],
                    "screenshot": screenshots[0].name if screenshots else None,
                    "dialog_html": html_path.name if html_path is not None else None,
                    "dialog_buttons": [button.to_json() for button in dialog_buttons],
                    "discard_sheet": [button.to_json() for button in discard_sheet],
                    "discard_action": discard_action,
                    "dialog_closed": dialog_closed,
                    "draft_saved": False,
                    "cleanup_ok": cleanup.cleanup_ok,
                    "cleanup_path": cleanup.close_path,
                    "close_path": cleanup.close_path,
                    "close_attempts": cleanup.close_attempts,
                    "close_click": cleanup.close_click,
                    "close_retry": cleanup.close_retry,
                    "after_close": list(cleanup.after_close),
                    "after_close_screenshot": (
                        cleanup.after_close_screenshot.name
                        if cleanup.after_close_screenshot is not None
                        else None
                    ),
                    "after_close_html": (
                        cleanup.after_close_html.name
                        if cleanup.after_close_html is not None
                        else None
                    ),
                    "media": _media_json(media_report) if media_report is not None else None,
                    "media_note": media_report.note if media_report is not None else None,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

        return DomReport(
            rows=rows,
            artifacts=artifacts,
            screenshot=screenshots[0] if screenshots else None,
            dialog_html=html_path,
            report_json=report_path,
            rendered=rendered,
            waited_s=waited_s,
            timeout_s=wait_timeout_s,
            screenshots=tuple(screenshots),
            dialog_buttons=dialog_buttons,
            discard_sheet=discard_sheet,
            dialog_closed=dialog_closed,
            discard_action=discard_action,
            draft_saved=False,
            cleanup_ok=cleanup.cleanup_ok,
            after_close=cleanup.after_close,
            after_close_screenshot=cleanup.after_close_screenshot,
            after_close_html=cleanup.after_close_html,
            close_attempts=cleanup.close_attempts,
            close_click=cleanup.close_click,
            close_retry=cleanup.close_retry,
            close_path=cleanup.close_path,
            media=media_report,
            note=note,
        )


async def _measure_all(page: Page) -> dict[str, list[tuple[str, int]]]:
    """Match every logical selector once, without clicking or typing."""
    return {name: await match_report_visible(page, name) for name in SELECTORS}


async def _wait_for_composer(
    page: Page,
    *,
    timeout_s: float,
    interval_s: float,
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], float],
) -> tuple[bool, float]:
    """Poll until a composer editor is visible, or the wait budget expires.

    The editor selector is the proof: the generic dialog and the textarea can
    both be absent for a moment while React is mounting, so a wait on
    ``[role=dialog]`` alone would still race the render.
    """
    started = clock()
    deadline = started + timeout_s
    polls = max(1, int(timeout_s / interval_s) + 1)
    for _ in range(polls):
        if await _any_visible(page, _COMPOSER_WAIT_SELECTORS):
            return True, max(0.0, clock() - started)
        if clock() >= deadline:
            break
        await sleep(interval_s)
    return False, max(0.0, clock() - started)


async def _any_visible(page: Page, selectors: Sequence[str]) -> bool:
    return await _find_visible(page, selectors) is not None


async def _find_visible(page: Page, selectors: Sequence[str]) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            total = await locator.count()
        except PlaywrightError:
            continue
        for index in range(total):
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible():
                    return candidate
            except PlaywrightError:
                continue
    return None


async def _visible_editors(page: Page) -> list[Locator]:
    locator = page.locator(", ".join(_EDITOR_SELECTORS))
    try:
        total = await locator.count()
    except PlaywrightError:
        return []
    editors: list[Locator] = []
    for index in range(total):
        editor = locator.nth(index)
        try:
            if await editor.is_visible():
                editors.append(editor)
        except PlaywrightError:
            continue
    return editors


async def _element_meta(locator: Locator) -> tuple[str, str, str]:
    try:
        text = " ".join((await locator.inner_text()).split())
    except PlaywrightError:
        text = ""
    try:
        aria_label = (await locator.get_attribute("aria-label")) or ""
    except PlaywrightError:
        aria_label = ""
    try:
        testid = (await locator.get_attribute("data-testid")) or ""
    except PlaywrightError:
        testid = ""
    return text, aria_label, testid


async def _safe_click(locator: Locator) -> None:
    text, aria_label, testid = await _element_meta(locator)
    assert_safe_click(text=text, aria_label=aria_label, data_testid=testid)
    await locator.click(timeout=3000)


async def _click_candidate(page: Page, candidate: str) -> bool:
    locator = await _find_visible(page, (candidate,))
    try:
        if locator is None:
            return False
        await _safe_click(locator)
    except (PlaywrightError, UnsafeClickError):
        return False
    return True


async def _add_second_editor(page: Page, *, sleep: Callable[[float], Awaitable[None]]) -> bool:
    matches = await match_report_visible(page, "composer_add_post_button")
    for candidate, count in matches:
        if count <= 0:
            continue
        # Never fall back to an unscoped candidate: the background inline
        # composer has its own add control.
        if '[role="dialog"]' not in candidate:
            continue
        if await _click_candidate(page, candidate):
            await sleep(0.5)
            return True
    return False


async def _screenshot(page: Page, store: SessionStore, artifacts: Path, label: str) -> Path | None:
    """Take a viewport screenshot (never full-page) and make it 0600."""
    path = artifacts / f"{label}.png"
    try:
        await page.screenshot(path=str(path), full_page=False)
    except PlaywrightError:
        return None
    store.protect_file(path)
    return path


async def _dialog_buttons(page: Page) -> tuple[ButtonInfo, ...]:
    dialog = await _find_visible(page, _DIALOG_SELECTORS)
    if dialog is None:
        return ()
    return await _buttons_in(dialog)


async def _buttons_in(scope: Locator) -> tuple[ButtonInfo, ...]:
    buttons = scope.locator("button")
    try:
        total = await buttons.count()
    except PlaywrightError:
        return ()
    infos: list[ButtonInfo] = []
    for index in range(total):
        button = buttons.nth(index)
        try:
            if not await button.is_visible():
                continue
            text, aria_label, testid = await _element_meta(button)
            disabled = await button.is_disabled()
        except PlaywrightError:
            continue
        infos.append(
            ButtonInfo(
                text=text,
                aria_label=aria_label,
                data_testid=testid,
                disabled=disabled,
            )
        )
    return tuple(infos)


async def _dialog_html(page: Page) -> str | None:
    """Return the composer dialog's outerHTML, or ``None`` when it did not open."""
    dialog = await _find_visible(page, _DIALOG_SELECTORS)
    if dialog is not None:
        try:
            markup = await dialog.evaluate("el => el.outerHTML")
        except PlaywrightError:
            markup = None
        if isinstance(markup, str):
            return markup
    matches = await match_report_visible(page, "composer_root")
    for candidate, count in matches:
        if count <= 0:
            continue
        if '[role="dialog"]' not in candidate and "[data-modal-slot-surface]" not in candidate:
            continue
        try:
            locator = await _find_visible(page, (candidate,))
            if locator is None:
                continue
            markup = await locator.evaluate("el => el.outerHTML")
        except PlaywrightError:
            continue
        if isinstance(markup, str):
            return markup
    return None


def _close_candidates() -> tuple[str, ...]:
    return (
        f'{_MODAL_SCOPE} [data-testid="app-bar-close"]',
        f'{_MODAL_SCOPE} button[aria-label="Close"]',
        f'{_MODAL_SCOPE} [data-slot="dialog-close"]',
        '[role="dialog"] [data-testid="app-bar-close"]',
        '[role="dialog"] button[aria-label="Close"]',
        '[role="dialog"] [data-slot="dialog-close"]',
    )


async def _forensic_click(page: Page, candidate: str, *, force: bool = False) -> dict[str, Any]:
    """Click a candidate and record box, hit-test target and dispatch result."""
    info: dict[str, Any] = {
        "candidate": candidate,
        "dispatched": False,
        "box": None,
        "hit": None,
        "error": "",
        "force": force,
    }
    locator = await _find_visible(page, (candidate,))
    try:
        if locator is None:
            info["error"] = "candidate is not visible"
            return info
        text, aria_label, testid = await _element_meta(locator)
        assert_safe_click(text=text, aria_label=aria_label, data_testid=testid)
        box = await locator.bounding_box()
        if box is not None:
            box_values = cast(Mapping[str, float], box)
            info["box"] = {key: round(value, 1) for key, value in box_values.items()}
            info["hit"] = await _element_at(
                page,
                box_values["x"] + box_values["width"] / 2,
                box_values["y"] + box_values["height"] / 2,
            )
        await locator.click(timeout=3000, force=force)
        info["dispatched"] = True
    except (PlaywrightError, UnsafeClickError) as exc:
        info["error"] = str(exc)[:300]
    return info


async def _element_at(page: Page, x: float, y: float) -> dict[str, str]:
    raw = await page.evaluate(
        """
        ([x, y]) => {
          const el = document.elementFromPoint(x, y);
          if (!el) return {tag: "", role: "", data_testid: "", aria_label: "", text: ""};
          return {
            tag: el.tagName.toLowerCase(),
            role: el.getAttribute("role") || "",
            data_testid: el.getAttribute("data-testid") || "",
            aria_label: el.getAttribute("aria-label") || "",
            text: (el.innerText || "").trim().slice(0, 80),
          };
        }
        """,
        [x, y],
    )
    return cast(dict[str, str], raw) if isinstance(raw, dict) else {}


async def _click_close_once(
    page: Page, *, force: bool = False
) -> tuple[dict[str, Any] | None, bool]:
    last: dict[str, Any] | None = None
    for candidate in _close_candidates():
        last = await _forensic_click(page, candidate, force=force)
        if last["dispatched"]:
            return last, True
    return last, False


async def _overlay_snapshot(page: Page) -> tuple[list[dict[str, Any]], str]:
    """Return every visible non-composer overlay and their sanitised-dump HTML."""
    raw = await page.evaluate(
        """
        (selectors) => {
          const composer = document.querySelector('[data-fx-composer]')
            || document.querySelector('[role="dialog"]:has([data-modal-slot-surface])')
            || document.querySelector('[role="dialog"]:has([data-testid="tweetTextarea_0"])')
            || document.querySelector('[role="dialog"]:has(textarea[aria-multiline="true"])');
          const nodes = new Set();
          for (const selector of selectors) {
            document.querySelectorAll(selector).forEach((el) => nodes.add(el));
          }
          document.querySelectorAll('[data-testid="mask"]').forEach((mask) => {
            const parent = mask.parentElement;
            if (!parent) return;
            Array.from(parent.children).forEach((child) => {
              if (child !== mask) nodes.add(child);
            });
          });
          for (const el of document.querySelectorAll('body *')) {
            if (el.closest('main')) continue;
            const text = (el.innerText || '').trim();
            if (text && text.length <= 200 && /Save post|Discard|Save/.test(text)) {
              nodes.add(el);
            }
          }
          const result = [];
          for (const el of nodes) {
            if (!el.isConnected) continue;
            if (composer && (el === composer || el.contains(composer))) continue;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            const style = getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            const buttons = Array.from(el.querySelectorAll('button'))
              .filter((button) => {
                const box = button.getBoundingClientRect();
                return box.width > 0 && box.height > 0;
              })
              .map((button) => ({
                text: (button.innerText || '').trim(),
                aria_label: button.getAttribute('aria-label') || '',
                data_testid: button.getAttribute('data-testid') || '',
                disabled: button.disabled || button.getAttribute('aria-disabled') === 'true',
              }));
            result.push({
              tag: el.tagName.toLowerCase(),
              role: el.getAttribute('role') || '',
              data_testid: el.getAttribute('data-testid') || '',
              aria_label: el.getAttribute('aria-label') || '',
              text: (el.innerText || '').trim().slice(0, 80),
              buttons,
              html: el.outerHTML,
            });
          }
          return result;
        }
        """,
        list(_OVERLAY_SELECTORS),
    )
    if not isinstance(raw, list):
        return [], ""
    overlays: list[dict[str, Any]] = []
    html_parts: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        data = dict(item)
        markup = data.pop("html", "")
        if isinstance(markup, str):
            html_parts.append(markup)
        overlays.append(data)
    return overlays, "\n\n<!-- overlay -->\n\n".join(html_parts)


async def _capture_after_close(
    page: Page, store: SessionStore, artifacts: Path
) -> tuple[tuple[dict[str, Any], ...], Path | None, Path | None]:
    screenshot = await _screenshot(page, store, artifacts, "after-close")
    try:
        overlays, html = await _overlay_snapshot(page)
    except PlaywrightError:
        overlays, html = [], ""
    html_path = store.write_artifact(
        artifacts,
        "after-close.html",
        _truncate_html(sanitise_html(html or "<!-- no non-composer overlays -->")),
        sanitise=True,
    )
    return tuple(overlays), screenshot, html_path


async def _wait_cleanup_state(
    page: Page,
    sleep: Callable[[float], Awaitable[None]],
    *,
    timeout_s: float,
    poll_s: float,
) -> tuple[str, Locator | None]:
    """Wait for the composer to disappear or a sheet with buttons to appear."""
    attempts = max(1, int(timeout_s / poll_s) + 1)
    for _ in range(attempts):
        sheet = await _find_visible(page, _SAVE_SHEET_SELECTORS)
        if sheet is not None and (
            await _buttons_in(sheet) or await _find_discard_button(page) is not None
        ):
            return "sheet", sheet
        if not await _any_visible(page, _COMPOSER_PRESENT_SELECTORS):
            if sheet is not None:
                return "sheet", sheet
            return "closed", None
        await sleep(poll_s)
    sheet = await _find_visible(page, _SAVE_SHEET_SELECTORS)
    if sheet is not None:
        return "sheet", sheet
    if not await _any_visible(page, _COMPOSER_PRESENT_SELECTORS):
        return "closed", None
    return "open", None


async def _finish_with_sheet(
    page: Page,
    sheet: Locator,
    sleep: Callable[[float], Awaitable[None]],
    *,
    poll_s: float,
) -> tuple[tuple[ButtonInfo, ...], bool, str]:
    buttons = await _buttons_in(sheet)
    discarded = await _click_discard(sheet)
    await sleep(0.3)
    closed = await _wait_for_gone(
        page,
        _COMPOSER_PRESENT_SELECTORS,
        sleep,
        attempts=max(1, int(2.0 / poll_s) + 1),
        step_s=poll_s,
    )
    remaining = await _find_visible(page, _SAVE_SHEET_SELECTORS)
    return buttons, closed and remaining is None, "discard" if discarded else "failed"


async def _close_and_discard(
    page: Page,
    *,
    store: SessionStore,
    artifacts: Path,
    sleep: Callable[[float], Awaitable[None]],
    timeout_s: float,
    poll_s: float,
) -> _CleanupResult:
    """Close and discard, with forensics and a bounded retry.

    The live classic client did not respond to the first Close click in one run,
    so this path now records exactly what was clicked and what overlays followed,
    then tries Escape and at most one second Close click before declaring
    ``cleanup_ok=False``. It never clicks Save or any submit control.
    """
    if await _find_visible(page, _COMPOSER_PRESENT_SELECTORS) is None:
        return _CleanupResult(
            (),
            True,
            True,
            "none",
            "already-closed",
            0,
            None,
            None,
            (),
            None,
            None,
        )

    close_click, dispatched = await _click_close_once(page)
    close_attempts = 1 if dispatched else 0
    after_close, after_shot, after_html = await _capture_after_close(page, store, artifacts)

    state, sheet = await _wait_cleanup_state(page, sleep, timeout_s=timeout_s, poll_s=poll_s)
    if state == "closed":
        return _CleanupResult(
            (),
            True,
            True,
            "none",
            "close",
            close_attempts,
            close_click,
            None,
            after_close,
            after_shot,
            after_html,
        )
    if state == "sheet" and sheet is not None:
        buttons, closed, action = await _finish_with_sheet(page, sheet, sleep, poll_s=poll_s)
        return _CleanupResult(
            buttons,
            closed,
            closed,
            action,
            "close",
            close_attempts,
            close_click,
            None,
            after_close,
            after_shot,
            after_html,
        )

    with suppress(PlaywrightError):
        await page.keyboard.press("Escape")
    state, sheet = await _wait_cleanup_state(page, sleep, timeout_s=timeout_s, poll_s=poll_s)
    if state == "closed":
        return _CleanupResult(
            (),
            True,
            True,
            "none",
            "escape",
            close_attempts,
            close_click,
            None,
            after_close,
            after_shot,
            after_html,
        )
    if state == "sheet" and sheet is not None:
        buttons, closed, action = await _finish_with_sheet(page, sheet, sleep, poll_s=poll_s)
        return _CleanupResult(
            buttons,
            closed,
            closed,
            action,
            "escape",
            close_attempts,
            close_click,
            None,
            after_close,
            after_shot,
            after_html,
        )

    close_retry, retry_dispatched = await _click_close_once(page, force=True)
    if retry_dispatched:
        close_attempts += 1
    state, sheet = await _wait_cleanup_state(page, sleep, timeout_s=timeout_s, poll_s=poll_s)
    if state == "closed":
        return _CleanupResult(
            (),
            True,
            True,
            "none",
            "close-retry",
            close_attempts,
            close_click,
            close_retry,
            after_close,
            after_shot,
            after_html,
        )
    if state == "sheet" and sheet is not None:
        buttons, closed, action = await _finish_with_sheet(page, sheet, sleep, poll_s=poll_s)
        return _CleanupResult(
            buttons,
            closed,
            closed,
            action,
            "close-retry",
            close_attempts,
            close_click,
            close_retry,
            after_close,
            after_shot,
            after_html,
        )
    return _CleanupResult(
        (),
        False,
        False,
        "failed",
        "failed",
        close_attempts,
        close_click,
        close_retry,
        after_close,
        after_shot,
        after_html,
    )


async def _wait_for_visible(
    page: Page,
    selectors: Sequence[str],
    sleep: Callable[[float], Awaitable[None]],
    *,
    attempts: int = 20,
    step_s: float = 0.1,
) -> Locator | None:
    for _ in range(attempts):
        found = await _find_visible(page, selectors)
        if found is not None:
            return found
        await sleep(step_s)
    return await _find_visible(page, selectors)


async def _wait_for_gone(
    page: Page,
    selectors: Sequence[str],
    sleep: Callable[[float], Awaitable[None]],
    *,
    attempts: int = 20,
    step_s: float = 0.1,
) -> bool:
    for _ in range(attempts):
        if not await _any_visible(page, selectors):
            return True
        await sleep(step_s)
    return not await _any_visible(page, selectors)


async def _click_discard(sheet: Locator) -> bool:
    buttons = sheet.locator("button")
    try:
        total = await buttons.count()
    except PlaywrightError:
        total = 0
    for index in range(total):
        button = buttons.nth(index)
        try:
            if not await button.is_visible():
                continue
            text, aria_label, testid = await _element_meta(button)
            if testid.strip().lower() == "confirmationsheetcancel" or text.strip().lower() in {
                "discard",
                "cancel",
            }:
                assert_safe_click(text=text, aria_label=aria_label, data_testid=testid)
                await button.click(timeout=3000)
                return True
        except (PlaywrightError, UnsafeClickError):
            continue
    fallback = await _find_discard_button(sheet.page)
    if fallback is not None:
        try:
            text, aria_label, testid = await _element_meta(fallback)
            assert_safe_click(text=text, aria_label=aria_label, data_testid=testid)
            await fallback.click(timeout=3000)
            return True
        except (PlaywrightError, UnsafeClickError):
            return False
    return False


async def _find_discard_button(page: Page) -> Locator | None:
    """Find a Discard/Cancel button even if it is outside the matched sheet."""
    buttons = page.locator("button")
    try:
        total = await buttons.count()
    except PlaywrightError:
        return None
    for index in range(total):
        button = buttons.nth(index)
        try:
            if not await button.is_visible():
                continue
            text, _aria, testid = await _element_meta(button)
        except PlaywrightError:
            continue
        if testid.strip().lower() == "confirmationsheetcancel" or text.strip().lower() in {
            "discard",
            "cancel",
        }:
            return button
    return None


def _padded_counts(values: Sequence[int]) -> tuple[int, int, int, int]:
    items = [*list(values)[:4], 0, 0, 0, 0]
    return (items[0], items[1], items[2], items[3])


def _phase_row(name: str, phases: Sequence[Mapping[str, list[tuple[str, int]]]]) -> DomRow:
    counts: list[int] = []
    candidate: str | None = None
    candidates: tuple[tuple[str, int], ...] = ()
    for phase in phases:
        matches = phase.get(name, [])
        candidates = tuple(matches)
        hit = next(((selector, count) for selector, count in matches if count > 0), None)
        if hit is None:
            counts.append(0)
            continue
        candidate = hit[0]
        counts.append(hit[1])
    return DomRow(
        name=name,
        candidate=candidate,
        count=max(counts) if counts else 0,
        missing=all(count == 0 for count in counts),
        candidates=candidates,
        phase_counts=tuple(counts),
    )


def _row_from_matches(name: str, matches: Sequence[tuple[str, int]]) -> DomRow:
    return _phase_row(name, [{name: list(matches)}])


def _truncate_html(html: str, limit: int = MAX_DIALOG_HTML_BYTES) -> str:
    """Cap the dumped HTML at a byte budget without splitting a UTF-8 sequence."""
    raw = html.encode("utf-8")
    if len(raw) <= limit:
        return html
    marker = f"\n<!-- truncated at {limit // 1024} KB -->\n"
    budget = max(0, limit - len(marker.encode("utf-8")))
    cut = raw[:budget].decode("utf-8", errors="ignore")
    return f"{cut}{marker}"


def _short(value: str, width: int = 56) -> str:
    return value if len(value) <= width else f"{value[: width - 1]}..."
