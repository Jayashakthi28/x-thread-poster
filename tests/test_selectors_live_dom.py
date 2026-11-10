"""Regression tests against the first authenticated classic-DOM capture.

The logged-out bundle described a new ``x-web`` dialog, but the first real
login on 2026-11-09 returned the classic responsive-web composer: a
``contenteditable`` ``tweetTextarea_0``, a ``tweetButton`` and an
``app-bar-close``. The capture is sanitised and committed as a fixture so the
classic candidates stay first and the logged-out selectors stay as fallbacks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page, async_playwright

from xposter.browser import find_chrome, new_page, open_context
from xposter.config import Display, XPosterConfig
from xposter.errors import UiChangedError
from xposter.selectors import (
    SELECTORS,
    locate,
    locate_all,
    match_report,
    match_report_visible,
)

_CHROME = find_chrome(XPosterConfig())
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "live_compose_dialog_2026-11-09.html"
_PAGE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "live_compose_page_2026-11-10.html"
_MEDIA_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "live_media_composer_2026-11-10.html"
)
_ALT_INTRO_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "live_alt_intro_sheet_2026-11-10.html"
)

_ALT_EDITOR_PAGE = """
<!doctype html>
<html>
<body>
  <div role="dialog" aria-modal="true">
    <button aria-label="Back">Back</button>
    <h1>Edit image description</h1>
    <textarea aria-label="Image description"></textarea>
    <button>Save</button>
  </div>
</body>
</html>
"""

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        _CHROME is None,
        reason="Google Chrome is not installed; browser tests need a real Chrome",
    ),
]

# name -> whether the element is intentionally hidden in the classic DOM
_RESOLVE_WITH_FIRST = {
    "composer_root": False,
    "composer_textarea_0": False,
    "composer_post_button": False,
    "composer_media_input": True,
    "composer_close_button": False,
}


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


async def _load_fixture(page: Page) -> None:
    await page.set_content(_FIXTURE.read_text(encoding="utf-8"))


async def _load_page_fixture(page: Page) -> None:
    await page.set_content(_PAGE_FIXTURE.read_text(encoding="utf-8"))


async def _load_media_fixture(page: Page) -> None:
    await page.set_content(_MEDIA_FIXTURE.read_text(encoding="utf-8"))


async def _load_alt_intro_fixture(page: Page) -> None:
    await page.set_content(_ALT_INTRO_FIXTURE.read_text(encoding="utf-8"))


async def _visible_elements_for(page: Page, candidate: str) -> list[Locator]:
    locator = page.locator(candidate)
    elements: list[Locator] = []
    for index in range(await locator.count()):
        element = locator.nth(index)
        if await element.is_visible():
            elements.append(element)
    return elements


_OLD_COMPOSER_ROOT_CANDIDATES = (
    '[role="dialog"]:has([data-testid="tweetTextarea_0"])',
    '[role="dialog"]:has([contenteditable="true"][role="textbox"])',
    '[role="dialog"]:has([data-modal-slot-surface])',
    "[data-modal-slot-surface]",
    '[role="dialog"]',
)


async def _old_locate_first(
    scope: Page, candidates: tuple[str, ...], *, timeout_ms: int = 400
) -> Locator | None:
    """Reimplement the pre-fix locate: first candidate, ``.first``, visible wait."""
    budget_ms = max(400, timeout_ms // len(candidates))
    for candidate in candidates:
        target = scope.locator(candidate).first
        try:
            await target.wait_for(state="visible", timeout=budget_ms)
        except PlaywrightError:
            continue
        return target
    return None


def test_live_fixture_is_sanitised() -> None:
    text = _FIXTURE.read_text(encoding="utf-8")

    for forbidden in ("Jayashakthi", "twimg", "http"):
        assert forbidden not in text


class TestLiveClassicComposer:
    """The five controls the poster needs first thing must match the classic DOM."""

    async def test_named_controls_resolve_with_the_classic_first_candidate(
        self, browser_config: XPosterConfig
    ) -> None:
        async with _page(browser_config) as page:
            await _load_fixture(page)

            for name, hidden in _RESOLVE_WITH_FIRST.items():
                element = await locate(page, name, timeout_ms=2000, visible=not hidden)
                report = await match_report(page, name)
                candidate, total = report[0]

                assert total >= 1, f"{name} did not match its first candidate: {report}"
                assert await element.evaluate(
                    "(el, selector) => el.matches(selector)", candidate
                ), f"{name} resolved via a fallback despite a first-candidate match"

    async def test_composer_post_button_is_disabled_when_empty(
        self, browser_config: XPosterConfig
    ) -> None:
        async with _page(browser_config) as page:
            await _load_fixture(page)

            button = await locate(page, "composer_post_button")

            assert await button.is_disabled() is True
            assert (await button.inner_text()).strip() == "Post"

    async def test_add_post_does_not_exist_before_text_is_typed(
        self, browser_config: XPosterConfig
    ) -> None:
        async with _page(browser_config) as page:
            await _load_fixture(page)

            with pytest.raises(UiChangedError):
                await locate(page, "composer_add_post_button", timeout_ms=500)

    async def test_post_all_label_is_absent_and_never_matches_schedule(
        self, browser_config: XPosterConfig
    ) -> None:
        async with _page(browser_config) as page:
            await _load_fixture(page)

            # The 'Post all' label does not exist in an empty composer. The
            # classic client reuses tweetButton, so locate returns that button
            # (the poster's exact-text guard refuses it); it must never be the
            # toolbar's Schedule control.
            counts = dict(await match_report(page, "composer_post_all_button"))
            assert counts['[role="dialog"] button:text-is("Post all")'] == 0

            button = await locate(page, "composer_post_all_button", timeout_ms=500)
            assert await button.get_attribute("data-testid") == "tweetButton"
            assert (await button.inner_text()).strip() != "Post all"


class TestScheduleIsNeverPostAll:
    """The toolbar has a Schedule control; ``locate`` must never pick it."""

    async def test_locate_returns_the_post_button_not_schedule(
        self, browser_config: XPosterConfig
    ) -> None:
        async with _page(browser_config) as page:
            await _load_fixture(page)

            schedule = page.locator('[data-testid="scheduleOption"]')
            assert await schedule.count() == 1
            assert await schedule.is_visible() is True

            button = await locate(page, "composer_post_all_button")

            assert await button.get_attribute("data-testid") == "tweetButton"
            assert await button.get_attribute("aria-label") != "Schedule post"
            assert "Schedule" not in await button.inner_text()
            assert not any(
                "schedule" in candidate.lower()
                for candidate in SELECTORS["composer_post_all_button"].candidates
            )

    async def test_a_schedule_only_page_raises_instead_of_clicking_it(
        self, browser_config: XPosterConfig
    ) -> None:
        async with _page(browser_config) as page:
            await page.set_content(
                '<div role="dialog">'
                '<button data-testid="scheduleOption" aria-label="Schedule post">'
                "Schedule</button>"
                "</div>"
            )

            with pytest.raises(UiChangedError):
                await locate(page, "composer_post_all_button", timeout_ms=300)


_IN_MODAL = 'e => !!e.closest(\'[role="dialog"][aria-modal="true"]\')'
_IN_MAIN = "e => !!e.closest('main')"


class TestLiveComposePage:
    """The real /compose/post page has an inline composer plus the modal."""

    async def test_unscoped_selector_is_a_trap_and_the_modal_one_is_not(
        self, browser_config: XPosterConfig
    ) -> None:
        async with _page(browser_config) as page:
            await _load_page_fixture(page)

            assert await page.locator('[data-testid="tweetTextarea_0"]').count() == 2

            raw = dict(await match_report(page, "composer_textarea_0"))
            assert raw['[role="dialog"][aria-modal="true"] [data-testid="tweetTextarea_0"]'] == 1
            assert raw['[data-testid="tweetTextarea_0"]'] == 2

            visible = dict(await match_report_visible(page, "composer_textarea_0"))
            assert (
                visible['[role="dialog"][aria-modal="true"] [data-testid="tweetTextarea_0"]'] == 1
            )
            assert visible['[data-testid="tweetTextarea_0"]'] == 2

    async def test_named_controls_resolve_inside_the_modal_not_the_inline_composer(
        self, browser_config: XPosterConfig
    ) -> None:
        async with _page(browser_config) as page:
            await _load_page_fixture(page)

            for name in (
                "composer_root",
                "composer_textarea_0",
                "composer_media_input",
                "composer_post_button",
                "composer_close_button",
            ):
                hidden = name == "composer_media_input"
                element = await locate(page, name, timeout_ms=3000, visible=not hidden)
                report = await match_report(page, name)

                assert report[0][1] >= 1, f"{name}: {report}"
                assert await element.evaluate(
                    "(el, selector) => el.matches(selector)", report[0][0]
                ), name
                assert await element.evaluate(_IN_MODAL) is True, name
                assert await element.evaluate(_IN_MAIN) is False, name

            editors = await locate_all(page, "composer_textarea_n")
            assert len(editors) == 1
            assert await editors[0].evaluate(_IN_MODAL) is True
            assert await editors[0].evaluate(_IN_MAIN) is False

    async def test_old_first_candidate_behaviour_still_fails_on_this_page(
        self, browser_config: XPosterConfig
    ) -> None:
        async with _page(browser_config) as page:
            await _load_page_fixture(page)

            old_root = await _old_locate_first(page, _OLD_COMPOSER_ROOT_CANDIDATES, timeout_ms=400)
            assert old_root is None

            # The capture's modal portal precedes <main> in DOM order, so the
            # old unscoped textarea `.first` happened to be the modal editor,
            # not the inline composer. Only the composer_root half of the bug
            # reproduces on this fixture; the test records that honestly.
            old_editor = await _old_locate_first(
                page, ('[data-testid="tweetTextarea_0"]',), timeout_ms=400
            )
            assert old_editor is not None
            assert await old_editor.evaluate(_IN_MODAL) is True
            assert await old_editor.evaluate(_IN_MAIN) is False


_IN_ATTACHMENTS = "e => !!e.closest('[data-testid=\"attachments\"]')"
_IN_AVATAR = "e => !!e.closest('[data-testid^=\"UserAvatar-Container\"]')"
_TAG_NAME = "e => e.tagName"


class TestEmptyComposerMediaSelectors:
    """An empty modal contains avatar images; media names must match none of them."""

    async def test_media_preview_remove_and_alt_match_nothing(
        self, browser_config: XPosterConfig
    ) -> None:
        async with _page(browser_config) as page:
            await _load_page_fixture(page)

            for name in (
                "composer_media_preview",
                "composer_media_remove_button",
                "composer_alt_button",
            ):
                assert await locate_all(page, name) == [], name
                report = await match_report(page, name)
                assert all(count == 0 for _, count in report), (name, report)


class TestLiveMediaSelectors:
    """With one attached image, each name resolves only to the media controls."""

    async def test_preview_is_the_attachment_not_the_avatar(
        self, browser_config: XPosterConfig
    ) -> None:
        async with _page(browser_config) as page:
            await _load_media_fixture(page)

            assert await page.locator('[data-testid^="UserAvatar-Container"] img').count() >= 1

            previews = await locate_all(page, "composer_media_preview")
            assert len(previews) == 1
            preview = previews[0]
            assert await preview.evaluate(_TAG_NAME) == "IMG"
            assert await preview.evaluate(_IN_ATTACHMENTS) is True
            assert await preview.evaluate(_IN_AVATAR) is False
            assert (await preview.get_attribute("alt") or "") == ""

            removes = await locate_all(page, "composer_media_remove_button")
            assert len(removes) == 1
            assert await removes[0].get_attribute("aria-label") == "Remove media"
            assert await removes[0].evaluate(_IN_ATTACHMENTS) is True
            assert await removes[0].evaluate(_IN_AVATAR) is False

            alts = await locate_all(page, "composer_alt_button")
            assert len(alts) == 1
            assert (await alts[0].inner_text()).strip() == "Add description"
            assert await alts[0].evaluate(_TAG_NAME) == "SPAN"
            assert (
                await alts[0].evaluate("e => !!e.closest('a[aria-label=\"Add description\"]')")
                is True
            )
            assert await alts[0].evaluate(_IN_AVATAR) is False

    async def test_no_media_candidate_can_match_an_avatar(
        self, browser_config: XPosterConfig
    ) -> None:
        async with _page(browser_config) as page:
            await _load_media_fixture(page)

            for name in (
                "composer_media_preview",
                "composer_media_remove_button",
                "composer_alt_button",
            ):
                for candidate in SELECTORS[name].candidates:
                    for element in await _visible_elements_for(page, candidate):
                        assert await element.evaluate(_IN_AVATAR) is False, (name, candidate)


class TestAltSheetAndEditorLayers:
    """The alt flow is two separate layers, not part of the composer modal."""

    async def test_intro_sheet_names_resolve_on_the_captured_sheet(
        self, browser_config: XPosterConfig
    ) -> None:
        async with _page(browser_config) as page:
            await _load_alt_intro_fixture(page)

            sheet = await locate(page, "alt_intro_sheet")
            assert await sheet.get_attribute("data-testid") == "sheetDialog"
            assert "Add descriptions" in await sheet.inner_text()

            dismiss = await locate(page, "alt_intro_dismiss_button")
            assert (await dismiss.inner_text()).strip() == "No, thanks"

    async def test_editor_alt_names_resolve_on_a_synthetic_layer(
        self, browser_config: XPosterConfig
    ) -> None:
        async with _page(browser_config) as page:
            await page.set_content(_ALT_EDITOR_PAGE)

            title = await locate(page, "alt_editor_title")
            assert (await title.inner_text()).strip() == "Edit image description"

            back = await locate(page, "alt_editor_back_button")
            assert await back.get_attribute("aria-label") == "Back"

            save = await locate(page, "alt_editor_save_button")
            assert (await save.inner_text()).strip() == "Save"

            field = await locate(page, "composer_alt_text_input")
            assert await field.evaluate(_TAG_NAME) == "TEXTAREA"
