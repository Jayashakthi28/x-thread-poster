"""End-to-end checks for the offline fake x.com, in a real Chrome.

Each test launches the installed Google Chrome headless through
``xposter.browser.open_context`` and installs the fake's route before any
navigation. The only selectors used here are the documented ones from
``docs/research/selectors.json`` — deliberately plain Playwright locators, not
the driver's selector module, so this suite validates the fake's DOM against the
research rather than against the driver's own guesses.
"""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path

import pytest
from playwright.async_api import Locator, Page, expect

from tests.fake_x import FakeX, Scenario, weighted_length
from tests.fake_x.fixtures import open_fake_context
from xposter.browser import find_chrome
from xposter.config import XPosterConfig

_CHROME = find_chrome(XPosterConfig())

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        _CHROME is None,
        reason="Google Chrome is not installed; fake x.com tests need real Chrome",
    ),
]

_NEW_DIALOG = '[role="dialog"]:has([data-modal-slot-surface])'

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _textarea(page: Page, dom: str, index: int) -> Locator:
    """The nth composer text entry for the requested client flavour."""
    if dom == "new":
        return page.locator('[role="dialog"] textarea[aria-multiline="true"]').nth(index)
    return page.locator(f'[role="dialog"][aria-modal="true"] [data-testid="tweetTextarea_{index}"]')


async def _click_add_post(page: Page, dom: str) -> None:
    """Click the thread builder for the requested client flavour."""
    if dom == "new":
        await page.locator('[role="dialog"] button[aria-label="Add another post"]').click()
    else:
        await page.locator('[data-testid="addButton"]').click()


def _submit(page: Page, dom: str, *, count: int, reply: bool = False) -> Locator:
    """The Post/Post all/Reply submit button for this composer state."""
    if dom == "new":
        label = "Reply" if reply else ("Post all" if count > 1 else "Post")
        return page.locator(f'[role="dialog"] button:text-is("{label}")')
    testid = "tweetButtonInline" if reply else "tweetButton"
    return page.locator(f'[data-testid="{testid}"]')


async def _compose(page: Page, dom: str, texts: list[str]) -> None:
    """Open the composer, fill one textarea per tweet, and submit the thread."""
    await page.goto("https://x.com/compose/post")
    dialog = _NEW_DIALOG if dom == "new" else '[role="dialog"][aria-modal="true"]'
    await expect(page.locator(dialog)).to_be_visible()
    for index, text in enumerate(texts):
        if index:
            await _click_add_post(page, dom)
        await _textarea(page, dom, index).fill(text)
    await _submit(page, dom, count=len(texts)).click()


async def _wait_for_toast(page: Page, fragment: str) -> None:
    """Wait until the newest toast contains ``fragment``."""
    await expect(page.locator("[data-sonner-toast]").last).to_contain_text(fragment)


def _assert_chain(fake: FakeX, texts: list[str]) -> None:
    """Assert the recorded requests form the documented sequential thread."""
    bodies = fake.create_requests
    assert [body["variables"]["tweet_text"] for body in bodies] == texts
    ids = [tweet.id for tweet in fake.tweets]
    assert len(ids) == len(texts)
    assert bodies[0]["variables"]["reply"]["in_reply_to_tweet_id"] is None
    for index in range(1, len(texts)):
        assert bodies[index]["variables"]["reply"]["in_reply_to_tweet_id"] == ids[index - 1]
    assert [body["variables"]["batch_compose"] for body in bodies] == [
        "BatchFirst",
        *["BatchSubsequent"] * (len(texts) - 1),
    ]
    assert all(len(tweet_id) == 19 for tweet_id in ids)


async def test_new_dom_thread_posts_three_tweets_in_order(
    tmp_home: XPosterConfig,
    fake_x: FakeX,
) -> None:
    async with open_fake_context(tmp_home, fake_x) as (_context, page):
        await _compose(page, "new", ["first post", "second post", "third post"])
        await _wait_for_toast(page, "Your posts were sent.")

        _assert_chain(fake_x, ["first post", "second post", "third post"])
        assert fake_x.blocked == []
        await expect(page.locator('[data-sonner-toast] button:has-text("View")')).to_be_visible()

        await page.goto(f"https://x.com/{fake_x.handle}")
        articles = page.locator('article[itemprop="SocialMediaPosting"]')
        await expect(articles).to_have_count(3)
        assert "first post" in await articles.nth(0).inner_text()
        assert "third post" in await articles.nth(2).inner_text()


async def test_legacy_dom_posts_a_two_tweet_thread(
    tmp_home: XPosterConfig,
    fake_x_factory: type[FakeX],
) -> None:
    fake = fake_x_factory(dom="legacy")
    async with open_fake_context(tmp_home, fake) as (_context, page):
        await _compose(page, "legacy", ["legacy one", "legacy two"])
        await _wait_for_toast(page, "Your posts were sent.")

        _assert_chain(fake, ["legacy one", "legacy two"])
        assert fake.blocked == []

        await page.goto(f"https://x.com/{fake.handle}")
        await expect(page.locator('[data-testid="tweet"]')).to_have_count(2)


async def test_logged_out_redirects_every_page_to_login(
    tmp_home: XPosterConfig,
    fake_x_factory: type[FakeX],
) -> None:
    fake = fake_x_factory(scenario=Scenario.LOGGED_OUT)
    async with open_fake_context(tmp_home, fake) as (_context, page):
        await page.goto("https://x.com/home")
        await expect(page).to_have_url(re.compile(r"/i/flow/login$"))
        await expect(page.locator('a:has-text("Log in with username or email")')).to_be_visible()
        await expect(page.locator('input[autocomplete="username"]')).to_be_visible()

        await page.goto("https://x.com/compose/post")
        await expect(page).to_have_url(re.compile(r"/i/flow/login$"))
        assert any(url.endswith("/compose/post") for url in fake.visits)
        assert any(url.endswith("/i/flow/login") for url in fake.visits)


@pytest.mark.parametrize(
    ("scenario", "suffix", "text"),
    [
        (Scenario.LOCKED, "/account/access", "Your account has been locked"),
        (Scenario.SUSPENDED, "/account/suspended", "suspended"),
    ],
)
async def test_parked_accounts_redirect_to_bounce_pages(
    tmp_home: XPosterConfig,
    fake_x_factory: type[FakeX],
    scenario: Scenario,
    suffix: str,
    text: str,
) -> None:
    fake = fake_x_factory(scenario=scenario)
    async with open_fake_context(tmp_home, fake) as (_context, page):
        await page.goto("https://x.com/home")
        await expect(page).to_have_url(re.compile(re.escape(suffix) + "$"))
        await expect(page.locator("body")).to_contain_text(text)


async def test_duplicate_text_is_rejected(
    tmp_home: XPosterConfig,
    fake_x_factory: type[FakeX],
) -> None:
    fake = fake_x_factory(scenario=Scenario.DUPLICATE)
    async with open_fake_context(tmp_home, fake) as (_context, page):
        await _compose(page, "new", ["the same words"])
        await _wait_for_toast(page, "Your post was sent.")
        assert len(fake.tweets) == 1

        await _compose(page, "new", ["the same words"])
        await _wait_for_toast(page, "Whoops! You already said that.")
        assert len(fake.tweets) == 1
        assert len(fake.create_requests) == 2


@pytest.mark.parametrize(
    ("scenario", "fragment"),
    [
        (Scenario.DAILY_LIMIT, "daily limit"),
        (Scenario.AUTOMATED, "automated"),
    ],
)
async def test_error_scenarios_show_a_toast_and_create_nothing(
    tmp_home: XPosterConfig,
    fake_x_factory: type[FakeX],
    scenario: Scenario,
    fragment: str,
) -> None:
    fake = fake_x_factory(scenario=scenario)
    async with open_fake_context(tmp_home, fake) as (_context, page):
        await _compose(page, "new", ["blocked post"])
        await _wait_for_toast(page, fragment)
        assert fake.tweets == []
        assert len(fake.create_requests) == 1


async def test_challenge_dialog_blocks_the_send(
    tmp_home: XPosterConfig,
    fake_x_factory: type[FakeX],
) -> None:
    fake = fake_x_factory(scenario=Scenario.CHALLENGE)
    async with open_fake_context(tmp_home, fake) as (_context, page):
        await _compose(page, "new", ["will not be sent"])
        challenge = page.locator('[role="dialog"]:has-text("Verify you are a human")')
        await expect(challenge).to_be_visible()
        await expect(page.locator('iframe[src*="arkose"]')).to_be_visible()
        assert fake.create_requests == []
        assert fake.tweets == []


async def test_ui_drift_removes_the_add_button(
    tmp_home: XPosterConfig,
    fake_x_factory: type[FakeX],
) -> None:
    fake = fake_x_factory(scenario=Scenario.UI_DRIFT)
    async with open_fake_context(tmp_home, fake) as (_context, page):
        await page.goto("https://x.com/compose/post")
        await expect(
            page.locator('[role="dialog"] button[aria-label="Add another post"]')
        ).to_have_count(0)
        await _compose(page, "new", ["single post despite drift"])
        await _wait_for_toast(page, "Your post was sent.")
        assert len(fake.tweets) == 1


async def test_server_error_once_recovers_on_retry(
    tmp_home: XPosterConfig,
    fake_x_factory: type[FakeX],
) -> None:
    fake = fake_x_factory(scenario=Scenario.SERVER_ERROR_ONCE)
    async with open_fake_context(tmp_home, fake) as (_context, page):
        await page.goto("https://x.com/compose/post")
        await _textarea(page, "new", 0).fill("retry me")
        await _submit(page, "new", count=1).click()
        await _wait_for_toast(page, "Something went wrong")
        assert fake.tweets == []

        await _submit(page, "new", count=1).click()
        await _wait_for_toast(page, "Your post was sent.")
        assert len(fake.create_requests) == 2
        assert [tweet.text for tweet in fake.tweets] == ["retry me"]


async def test_fail_at_n_stops_the_batch(
    tmp_home: XPosterConfig,
    fake_x_factory: type[FakeX],
) -> None:
    fake = fake_x_factory(scenario=Scenario.FAIL_AT_N, fail_at=2)
    async with open_fake_context(tmp_home, fake) as (_context, page):
        await _compose(page, "new", ["one", "two", "three"])
        await _wait_for_toast(page, "deleted or is not visible")
        assert len(fake.create_requests) == 2
        assert [tweet.text for tweet in fake.tweets] == ["one"]
        assert fake.create_requests[0]["variables"]["batch_compose"] == "BatchFirst"
        assert fake.create_requests[1]["variables"]["batch_compose"] == "BatchSubsequent"


async def test_slow_adds_latency(
    tmp_home: XPosterConfig,
    fake_x_factory: type[FakeX],
) -> None:
    fake = fake_x_factory(scenario=Scenario.SLOW, latency_ms=300)
    async with open_fake_context(tmp_home, fake) as (_context, page):
        await page.goto("https://x.com/compose/post")
        await _textarea(page, "new", 0).fill("slow post")
        started = time.monotonic()
        await _submit(page, "new", count=1).click()
        await _wait_for_toast(page, "Your post was sent.")
        elapsed = time.monotonic() - started
        assert elapsed >= 0.25
        assert len(fake.tweets) == 1


async def test_delete_removes_the_tweet_everywhere(
    tmp_home: XPosterConfig,
    fake_x: FakeX,
) -> None:
    async with open_fake_context(tmp_home, fake_x) as (_context, page):
        await _compose(page, "new", ["delete me"])
        await _wait_for_toast(page, "Your post was sent.")
        tweet = fake_x.tweets[0]
        permalink = f"https://x.com/{fake_x.handle}/status/{tweet.id}"

        await page.goto(permalink)
        article = page.locator('article[itemprop="SocialMediaPosting"]')
        await article.locator('button[aria-label="More"]').click()
        await page.locator('[role="menuitem"]:has-text("Delete")').click()
        dialog = page.locator('[role="dialog"]:has-text("Delete post?")')
        await dialog.locator('button:text-is("Delete")').click()
        await _wait_for_toast(page, "Your post was deleted.")

        assert fake_x.deleted == [tweet.id]
        assert tweet.deleted is True

        await page.goto(permalink)
        await expect(page.locator("body")).to_contain_text("Hmm...this page doesn't exist")
        await page.goto(f"https://x.com/{fake_x.handle}")
        await expect(page.locator('article[itemprop="SocialMediaPosting"]')).to_have_count(0)


async def test_post_button_tracks_weighted_length(
    tmp_home: XPosterConfig,
    fake_x: FakeX,
) -> None:
    async with open_fake_context(tmp_home, fake_x) as (_context, page):
        await page.goto("https://x.com/compose/post")
        button = _submit(page, "new", count=1)
        await expect(button).to_be_disabled()

        at_limit = "a" * 280
        await _textarea(page, "new", 0).fill(at_limit)
        await expect(button).to_be_enabled()
        assert weighted_length(at_limit) == 280

        await _textarea(page, "new", 0).fill(at_limit + "b")
        await expect(button).to_be_disabled()
        assert weighted_length(at_limit + "b") == 281


async def test_reply_mode_targets_the_parent_tweet(
    tmp_home: XPosterConfig,
    fake_x: FakeX,
) -> None:
    async with open_fake_context(tmp_home, fake_x) as (_context, page):
        await _compose(page, "new", ["root post"])
        await _wait_for_toast(page, "Your post was sent.")
        root = fake_x.tweets[0]

        await page.goto(f"https://x.com/{fake_x.handle}/status/{root.id}")
        await page.locator('article button[aria-label="Reply"]').click()
        dialog = page.locator(_NEW_DIALOG)
        await dialog.locator('textarea[aria-multiline="true"]').fill("a direct reply")
        await dialog.locator('button:text-is("Reply")').click()
        await _wait_for_toast(page, "Your post was sent.")

        reply = fake_x.tweets[1]
        assert reply.in_reply_to == root.id
        assert fake_x.create_requests[1]["variables"]["reply"]["in_reply_to_tweet_id"] == root.id


async def test_media_upload_reaches_create_tweet(
    tmp_home: XPosterConfig,
    fake_x: FakeX,
    tmp_path: Path,
) -> None:
    image = tmp_path / "pixel.png"
    image.write_bytes(_PNG)
    async with open_fake_context(tmp_home, fake_x) as (_context, page):
        await page.goto("https://x.com/compose/post")
        await _textarea(page, "new", 0).fill("with an image")
        await page.locator('[role="dialog"] input[type="file"]').set_input_files(str(image))
        await expect(
            page.locator('[role="dialog"] button[aria-label="Remove media"]')
        ).to_be_visible()
        await page.wait_for_function(
            "() => !document.querySelector('[data-fx-composer]').hasAttribute('data-fx-uploading')"
        )
        await _submit(page, "new", count=1).click()
        await _wait_for_toast(page, "Your post was sent.")

        entities = fake_x.create_requests[0]["variables"]["media"]["media_entities"]
        assert len(entities) == 1
        assert entities[0]["media_id"].isdigit()
        assert fake_x.tweets[0].media == ("pixel.png",)


async def test_long_post_uses_the_note_tweet_response(
    tmp_home: XPosterConfig,
    fake_x_factory: type[FakeX],
) -> None:
    fake = fake_x_factory(max_weight=25_000)
    long_text = "word " * 60
    assert weighted_length(long_text) > 280
    async with open_fake_context(tmp_home, fake) as (_context, page):
        await page.goto("https://x.com/compose/post")
        await _textarea(page, "new", 0).fill(long_text)
        submit = _submit(page, "new", count=1)
        await expect(submit).to_be_enabled()
        async with page.expect_response(
            lambda response: response.url.endswith("/CreateTweet")
        ) as response_info:
            await submit.click()
        response = await response_info.value
        payload = await response.json()
        assert "notetweet_create" in payload["data"]
        assert payload["data"]["notetweet_create"]["tweet_results"]["rest_id"]
        await _wait_for_toast(page, "Your post was sent.")
        assert fake.tweets[0].text == long_text


def test_weighted_length_matches_the_documented_rule() -> None:
    """Keep the Python and inline-JS weighting rule pinned for test authors."""
    assert weighted_length("") == 0
    assert weighted_length("a") == 1
    assert weighted_length("é") == 1
    assert weighted_length("😀") == 2
    assert weighted_length("https://example.com/path") == 23
