"""Selector-table and real-Chrome DOM tests for the logical selector layer.

The pure tests keep the embedded table honest against the research JSON. The
browser tests use a local ``page.set_content`` page in the real installed Chrome
and never touch x.com: they prove that the new-client candidates and the
classic ``data-testid`` fallbacks both resolve the composer, and that a missing
element fails loudly with every candidate listed for ``xposter doctor --dom``.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import Page, async_playwright

from xposter.browser import find_chrome, new_page, open_context
from xposter.config import Display, XPosterConfig
from xposter.errors import UiChangedError
from xposter.selectors import (
    SELECTORS,
    SelectorSpec,
    count,
    handle_link_selector,
    is_present,
    locate,
    locate_all,
    match_report,
)

_CHROME = find_chrome(XPosterConfig())

browser = pytest.mark.browser
requires_chrome = pytest.mark.skipif(
    _CHROME is None,
    reason="Google Chrome is not installed; browser tests need a real Chrome",
)

_DOC = Path(__file__).resolve().parents[1] / "docs" / "research" / "selectors.json"

_COMPOSER_NAMES = (
    "composer_root",
    "composer_textarea_0",
    "composer_textarea_n",
    "composer_post_button",
    "composer_post_all_button",
    "composer_add_post_button",
    "composer_media_button",
    "composer_media_input",
    "composer_media_preview",
    "composer_media_remove_button",
    "composer_alt_button",
    "composer_alt_text_input",
    "composer_alt_save_button",
    "composer_close_button",
)

_HIDDEN_COMPOSER_NAMES = {"composer_media_input"}

_CLASSIC_FIRST = [
    (
        "composer_root",
        '[role="dialog"][aria-modal="true"]:has([data-testid="tweetTextarea_0"])',
    ),
    (
        "composer_textarea_0",
        '[role="dialog"][aria-modal="true"] [data-testid="tweetTextarea_0"]',
    ),
    (
        "composer_textarea_n",
        '[role="dialog"][aria-modal="true"] '
        '[data-testid^="tweetTextarea_"][contenteditable="true"]',
    ),
    ("composer_post_button", '[role="dialog"][aria-modal="true"] [data-testid="tweetButton"]'),
    (
        "composer_post_all_button",
        '[role="dialog"][aria-modal="true"] [data-testid="tweetButton"]',
    ),
    ("composer_add_post_button", '[role="dialog"][aria-modal="true"] [data-testid="addButton"]'),
    (
        "composer_media_button",
        '[role="dialog"][aria-modal="true"] [data-testid="toolBar"] '
        'button[aria-label="Add photos or video"]',
    ),
    ("composer_media_input", '[role="dialog"][aria-modal="true"] [data-testid="fileInput"]'),
    (
        "composer_media_preview",
        '[role="dialog"][aria-modal="true"] [data-testid="attachments"] img',
    ),
    ("composer_close_button", '[role="dialog"][aria-modal="true"] [data-testid="app-bar-close"]'),
    ("composer_alt_text_input", ':has(:text-is("Edit image description")) textarea'),
    ("draft_save_confirm_button", '[data-testid="confirmationSheetConfirm"]'),
    ("draft_save_cancel_button", '[data-testid="confirmationSheetCancel"]'),
    ("toast", '[data-testid="toast"]'),
    ("nav_profile_link", '[data-testid="AppTabBar_Profile_Link"]'),
    ("tweet_article", 'article[data-testid="tweet"]'),
    ("tweet_reply_button", 'article [data-testid="reply"]'),
    ("tweet_more_button", '[data-testid="caret"]'),
    ("delete_confirm_button", '[data-testid="confirmationSheetConfirm"]'),
]

_PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

_NEW_COMPOSER = f"""
<!doctype html>
<meta charset="utf-8">
<title>new x-web composer</title>
<div role="dialog" aria-modal="true" data-modal-slot-surface aria-label="Create post">
  <textarea aria-multiline="true"></textarea>
  <button type="submit">Post</button>
  <button>Post all</button>
  <button aria-label="Add another post">add</button>
  <button aria-label="Add media">media</button>
  <input type="file" multiple accept="image/jpeg" aria-hidden="true" style="display:none">
  <div data-testid="attachments">
    <img alt="preview" width="24" height="24" src="{_PIXEL}">
    <button aria-label="Remove media">x</button>
  </div>
  <button aria-label="Add 1 description">alt</button>
  <textarea aria-label="Image description"></textarea>
  <button>Save</button>
  <button aria-label="Close">close</button>
</div>
"""

_CLASSIC_COMPOSER = f"""
<!doctype html>
<meta charset="utf-8">
<title>classic responsive-web composer</title>
<div role="dialog" aria-modal="true">
  <div data-testid="tweetTextarea_0_label">
    <div data-testid="tweetTextarea_0RichTextInputContainer">
      <div role="textbox" contenteditable="true" aria-label="Post text"
           data-testid="tweetTextarea_0"></div>
    </div>
  </div>
  <div role="textbox" contenteditable="true" data-testid="tweetTextarea_1"></div>
  <button data-testid="tweetButton" disabled>Post</button>
  <button data-testid="addButton">add</button>
  <div data-testid="toolBar">
    <button aria-label="Add photos or video">media</button>
  </div>
  <input type="file" data-testid="fileInput" multiple style="display:none">
  <div data-testid="attachments" style="width:24px;height:24px">
    <img alt="preview" width="24" height="24" src="{_PIXEL}">
    <button aria-label="Remove media">x</button>
  </div>
  <button aria-label="Add description">alt</button>
  <textarea data-testid="altTextInput"></textarea>
  <button>Done</button>
  <button data-testid="app-bar-close" aria-label="Close"
          style="width:24px;height:24px"></button>
</div>
"""

_TWO_REPLIES = """
<!doctype html>
<meta charset="utf-8">
<article itemprop="SocialMediaPosting">
  <button aria-label="Reply">reply one</button>
</article>
<article itemprop="SocialMediaPosting">
  <button aria-label="Reply">reply two</button>
</article>
"""


@pytest.fixture
def browser_config(tmp_path: Path) -> XPosterConfig:
    """A temporary Chrome profile that never sees the operator's real session."""
    assert _CHROME is not None
    home = tmp_path / "home"
    home.mkdir()
    return XPosterConfig(
        home=home,
        display=Display.HEADLESS,
        chrome_path=_CHROME,
        default_timeout_ms=15_000,
    )


@asynccontextmanager
async def _page(config: XPosterConfig) -> AsyncIterator[Page]:
    async with (
        async_playwright() as playwright,
        open_context(playwright, config) as context,
    ):
        yield await new_page(context)


# ---------------------------------------------------------------------------
# Pure table checks
# ---------------------------------------------------------------------------


def test_every_documented_selector_name_is_embedded() -> None:
    if not _DOC.is_file():
        pytest.skip("docs/research/selectors.json is not present")
    documented = json.loads(_DOC.read_text(encoding="utf-8"))["selectors"]

    missing = sorted(set(documented) - set(SELECTORS))

    assert not missing, f"selectors documented but missing from SELECTORS: {missing}"


def test_research_additions_are_embedded() -> None:
    assert {"tweet_more_button", "menu_delete_item", "delete_confirm_button"} <= set(SELECTORS)


def test_specs_are_well_formed_and_names_are_unique() -> None:
    assert len(SELECTORS) == len({spec.name for spec in SELECTORS.values()})

    for name, spec in SELECTORS.items():
        assert isinstance(spec, SelectorSpec)
        assert spec.name == name
        assert spec.primary.strip(), name
        assert spec.candidates[0] == spec.primary
        for candidate in spec.candidates:
            assert candidate.strip(), name
            assert not candidate.lower().startswith(("xpath=", "//", "..")), candidate


def test_candidates_property_is_primary_then_fallbacks() -> None:
    spec = SelectorSpec("demo", "main", ('[role="main"]',), "demo element")

    assert spec.candidates == ("main", '[role="main"]')
    assert SelectorSpec("empty", "main", ()).candidates == ("main",)


def test_post_all_never_falls_back_to_schedule() -> None:
    spec = SELECTORS["composer_post_all_button"]

    assert not any("schedule" in candidate.lower() for candidate in spec.candidates)
    assert '[data-testid="tweetButton"]' in spec.candidates


@pytest.mark.parametrize(
    ("handle", "expected"),
    [
        ("olsa", 'a[href="/olsa"]'),
        ("olsa_test", 'a[href="/olsa_test"]'),
        ("A1_", 'a[href="/A1_"]'),
        ("a" * 15, 'a[href="/' + "a" * 15 + '"]'),
    ],
)
def test_handle_link_selector_builds_the_exact_profile_link(handle: str, expected: str) -> None:
    assert handle_link_selector(handle) == expected


@pytest.mark.parametrize(
    "handle",
    [
        "",
        "a" * 16,
        "@olsa",
        "ols-a",
        "olsa/x",
        'olsa"]',
        " ",
        "olsa tweet",
        "ümlaut",
    ],
)
def test_handle_link_selector_rejects_invalid_handles(handle: str) -> None:
    with pytest.raises(ValueError):
        handle_link_selector(handle)


def test_handle_link_selector_rejects_non_strings() -> None:
    with pytest.raises(ValueError):
        handle_link_selector(None)  # type: ignore[arg-type]


def test_nav_profile_link_doc_points_at_the_helper() -> None:
    assert "nav_profile_link" in SELECTORS
    assert "handle_link_selector" in SELECTORS["nav_profile_link"].doc


@pytest.mark.parametrize(("name", "primary"), _CLASSIC_FIRST)
def test_classic_candidates_come_first(name: str, primary: str) -> None:
    assert SELECTORS[name].candidates[0] == primary


@pytest.mark.parametrize(
    "name",
    [
        name
        for name in SELECTORS
        if name.startswith("composer_") and name != "composer_alt_text_input"
    ],
)
def test_every_composer_selector_is_scoped_to_the_modal(name: str) -> None:
    assert SELECTORS[name].candidates[0].startswith('[role="dialog"][aria-modal="true"]')


def test_alt_text_input_is_scoped_to_the_editor_layer() -> None:
    assert SELECTORS["composer_alt_text_input"].candidates[0] == (
        ':has(:text-is("Edit image description")) textarea'
    )


@pytest.mark.parametrize(
    "name",
    [
        "composer_media_preview",
        "composer_media_remove_button",
        "composer_alt_button",
    ],
)
def test_media_selectors_never_use_generic_images(name: str) -> None:
    for candidate in SELECTORS[name].candidates:
        if " img" in candidate or candidate.endswith("img"):
            assert (
                '[data-testid="attachments"]' in candidate
                or '[role="group"][aria-label="Media"]' in candidate
            ), candidate


def test_alt_selector_names_and_doc_note() -> None:
    for name in (
        "alt_intro_sheet",
        "alt_intro_dismiss_button",
        "alt_editor_title",
        "alt_editor_back_button",
        "alt_editor_save_button",
    ):
        assert name in SELECTORS
        assert "Text field and Save not yet observed live" in SELECTORS[name].doc


def test_post_all_reuses_the_classic_button_and_guards_the_label() -> None:
    spec = SELECTORS["composer_post_all_button"]

    assert spec.candidates[0] == ('[role="dialog"][aria-modal="true"] [data-testid="tweetButton"]')
    assert not any("schedule" in candidate.lower() for candidate in spec.candidates)


# ---------------------------------------------------------------------------
# Real Chrome against local HTML
# ---------------------------------------------------------------------------


@browser
@requires_chrome
async def test_primary_hit_and_fallback_hit(browser_config: XPosterConfig) -> None:
    async with _page(browser_config) as page:
        await page.set_content(
            '<div role="dialog" aria-modal="true">'
            '<button data-testid="app-bar-close" aria-label="Close" '
            'style="width:24px;height:24px"></button>'
            "</div>"
        )
        close = await locate(page, "composer_close_button")
        assert await close.get_attribute("data-testid") == "app-bar-close"

        await page.set_content(_NEW_COMPOSER)
        close = await locate(page, "composer_close_button")
        assert await close.get_attribute("data-testid") is None
        assert await close.get_attribute("aria-label") == "Close"

        await page.set_content('<div data-testid="caret" style="width:24px;height:24px"></div>')
        caret = await locate(page, "tweet_more_button")
        assert await caret.get_attribute("data-testid") == "caret"


@browser
@requires_chrome
async def test_missing_selector_lists_every_candidate(
    browser_config: XPosterConfig,
) -> None:
    async with _page(browser_config) as page:
        await page.set_content("<p>nothing to see here</p>")

        with pytest.raises(UiChangedError) as caught:
            await locate(page, "composer_alt_save_button", timeout_ms=300)

        message = str(caught.value)
        assert "composer_alt_save_button" in message
        for candidate in SELECTORS["composer_alt_save_button"].candidates:
            assert candidate in message
        assert caught.value.artifacts is None


@browser
@requires_chrome
async def test_unknown_selector_name_fails_loudly(browser_config: XPosterConfig) -> None:
    async with _page(browser_config) as page:
        await page.set_content("<p>nothing</p>")

        calls = (
            lambda: locate(page, "no_such_selector"),
            lambda: locate_all(page, "no_such_selector"),
            lambda: is_present(page, "no_such_selector"),
            lambda: count(page, "no_such_selector"),
            lambda: match_report(page, "no_such_selector"),
        )
        for call in calls:
            with pytest.raises(UiChangedError, match="unknown selector name"):
                await call()


@browser
@requires_chrome
async def test_locate_all_count_and_hidden_elements(
    browser_config: XPosterConfig,
) -> None:
    async with _page(browser_config) as page:
        await page.set_content(_TWO_REPLIES)

        replies = await locate_all(page, "tweet_reply_button")
        assert len(replies) == 2
        assert [await reply.inner_text() for reply in replies] == ["reply one", "reply two"]
        assert await count(page, "tweet_reply_button") == 2
        assert await locate_all(page, "toast") == []
        assert await count(page, "toast") == 0

        await page.set_content('<div role="dialog"><input type="file" style="display:none"></div>')
        hidden = await locate(page, "composer_media_input", visible=False, timeout_ms=1000)
        assert await hidden.get_attribute("type") == "file"
        assert await hidden.is_visible() is False
        with pytest.raises(UiChangedError):
            await locate(page, "composer_media_input", timeout_ms=300)


@browser
@requires_chrome
async def test_is_present_returns_true_fast_and_false_after_the_budget(
    browser_config: XPosterConfig,
) -> None:
    async with _page(browser_config) as page:
        await page.set_content("<div data-sonner-toast>sent</div>")
        started = time.monotonic()
        assert await is_present(page, "toast", timeout_ms=1500) is True
        assert time.monotonic() - started < 2.0

        await page.set_content("<p>nothing</p>")
        started = time.monotonic()
        assert await is_present(page, "tweet_more_button", timeout_ms=500) is False
        elapsed = time.monotonic() - started
        assert elapsed >= 0.3
        assert elapsed < 10.0


@browser
@requires_chrome
async def test_match_report_counts_every_candidate(
    browser_config: XPosterConfig,
) -> None:
    async with _page(browser_config) as page:
        await page.set_content(_CLASSIC_COMPOSER)

        report = await match_report(page, "composer_post_button")

        assert [candidate for candidate, _ in report] == list(
            SELECTORS["composer_post_button"].candidates
        )
        assert all(isinstance(total, int) for _, total in report)
        counts = dict(report)
        assert counts[SELECTORS["composer_post_button"].candidates[0]] == 1
        assert counts['[data-testid="tweetButtonInline"]'] == 0


@browser
@requires_chrome
async def test_textarea_0_is_a_contenteditable_textbox(
    browser_config: XPosterConfig,
) -> None:
    async with _page(browser_config) as page:
        await page.set_content(_CLASSIC_COMPOSER)

        editor = await locate(page, "composer_textarea_0")

        assert await editor.get_attribute("data-testid") == "tweetTextarea_0"
        assert await editor.get_attribute("contenteditable") == "true"
        assert await editor.get_attribute("role") == "textbox"


@browser
@requires_chrome
async def test_textarea_n_counts_only_editable_boxes(
    browser_config: XPosterConfig,
) -> None:
    async with _page(browser_config) as page:
        await page.set_content(_CLASSIC_COMPOSER)

        boxes = await locate_all(page, "composer_textarea_n")

        assert len(boxes) == 2
        for box in boxes:
            assert await box.get_attribute("contenteditable") == "true"
            assert await box.get_attribute("role") == "textbox"
            assert not (await box.get_attribute("data-testid") or "").endswith(
                ("_label", "RichTextInputContainer")
            )


@browser
@requires_chrome
async def test_schedule_button_is_not_post_all(browser_config: XPosterConfig) -> None:
    async with _page(browser_config) as page:
        await page.set_content('<div role="dialog"><button>Schedule</button></div>')

        assert await is_present(page, "composer_post_all_button", timeout_ms=300) is False
        with pytest.raises(UiChangedError):
            await locate(page, "composer_post_all_button", timeout_ms=300)


@browser
@requires_chrome
async def test_handle_link_selector_matches_only_that_handle(
    browser_config: XPosterConfig,
) -> None:
    async with _page(browser_config) as page:
        await page.set_content('<a href="/other">other</a><a href="/olsa">me</a>')

        links = page.locator(handle_link_selector("olsa"))

        assert await links.count() == 1
        assert await links.inner_text() == "me"


@browser
@requires_chrome
async def test_new_client_composer_names_resolve(browser_config: XPosterConfig) -> None:
    async with _page(browser_config) as page:
        await page.set_content(_NEW_COMPOSER)

        for name in _COMPOSER_NAMES:
            hidden = name in _HIDDEN_COMPOSER_NAMES
            element = await locate(page, name, timeout_ms=1500, visible=not hidden)
            assert await element.is_visible() is not hidden, name


@browser
@requires_chrome
async def test_classic_client_composer_names_resolve(
    browser_config: XPosterConfig,
) -> None:
    async with _page(browser_config) as page:
        await page.set_content(_CLASSIC_COMPOSER)

        for name in _COMPOSER_NAMES:
            hidden = name in _HIDDEN_COMPOSER_NAMES
            element = await locate(page, name, timeout_ms=1500, visible=not hidden)
            assert await element.is_visible() is not hidden, name
