"""Real-Chrome checks for the browser layer.

These tests launch the locally installed Google Chrome headless against a
temporary profile. They never touch the network or x.com: the only HTTP server
is a loopback ``http.server`` used to prove cookies survive a context restart.
Marked ``browser`` and skipped when no real Chrome is installed, because the
point is to measure the browser we actually ship, not Playwright's Chromium.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from xposter.browser import (
    assess_tells,
    collect_tells,
    find_chrome,
    new_page,
    open_context,
)
from xposter.config import Display, XPosterConfig
from xposter.errors import BrowserUnavailableError

_CHROME = find_chrome(XPosterConfig())

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        _CHROME is None,
        reason="Google Chrome is not installed; browser tests need a real Chrome",
    ),
]

_COOKIE_NAME = "xposter_probe"
_COOKIE_VALUE = "persisted"


class _CookieHandler(BaseHTTPRequestHandler):
    """Serve one page and set a cookie that must outlive the browser."""

    def do_GET(self) -> None:
        body = b"<!doctype html><title>xposter probe</title>ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Set-Cookie",
            f"{_COOKIE_NAME}={_COOKIE_VALUE}; Path=/; Max-Age=3600; SameSite=Lax",
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the test output clean; requests are irrelevant beyond the cookie."""


@pytest.fixture
def browser_config(tmp_path: Path) -> XPosterConfig:
    """A temporary home with the real Chrome path and a short default timeout."""
    assert _CHROME is not None
    home = tmp_path / "home"
    home.mkdir()
    return XPosterConfig(
        home=home,
        display=Display.HEADLESS,
        chrome_path=_CHROME,
        default_timeout_ms=15_000,
    )


async def test_webdriver_is_false_in_a_real_chrome(browser_config: XPosterConfig) -> None:
    async with (
        async_playwright() as playwright,
        open_context(playwright, browser_config) as context,
    ):
        page = await new_page(context)

        assert await page.evaluate("navigator.webdriver") is False


async def test_headless_uses_a_normal_desktop_viewport(
    browser_config: XPosterConfig,
) -> None:
    async with (
        async_playwright() as playwright,
        open_context(playwright, browser_config) as context,
    ):
        page = await new_page(context)
        user_agent = await page.evaluate("navigator.userAgent")

        assert "HeadlessChrome" in user_agent
        assert page.viewport_size == {"width": 1440, "height": 900}


async def test_cookies_survive_closing_and_reopening_the_profile(
    browser_config: XPosterConfig,
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CookieHandler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as playwright:
            async with open_context(playwright, browser_config) as context:
                page = await new_page(context)
                await page.goto(url)
                cookies = await context.cookies(url)
                assert [
                    cookie["value"] for cookie in cookies if cookie["name"] == _COOKIE_NAME
                ] == [_COOKIE_VALUE]

            async with open_context(playwright, browser_config) as context:
                cookies = await context.cookies(url)
                assert [
                    cookie["value"] for cookie in cookies if cookie["name"] == _COOKIE_NAME
                ] == [_COOKIE_VALUE]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


async def test_a_second_context_on_the_same_profile_fails_fast(
    browser_config: XPosterConfig,
) -> None:
    async with async_playwright() as playwright:
        async with open_context(playwright, browser_config):
            with pytest.raises(
                BrowserUnavailableError, match="another Chrome is using the profile"
            ):
                async with asyncio.timeout(20.0):
                    async with open_context(playwright, browser_config):
                        pytest.fail("a second context must not open the same profile")

        async with open_context(playwright, browser_config) as context:
            assert context.pages, "the profile must be usable again after a clean close"


async def test_tells_run_end_to_end(browser_config: XPosterConfig) -> None:
    async with (
        async_playwright() as playwright,
        open_context(playwright, browser_config) as context,
    ):
        page = await new_page(context)
        raw = await collect_tells(page)
        tells = assess_tells(raw, Display.HEADLESS)

        assert raw["webdriver"] is False
        assert "HeadlessChrome" in raw["userAgent"]
        assert tells
        assert {tell.risk for tell in tells} <= {"ok", "note", "warn"}
        webdriver_tell = next(tell for tell in tells if tell.name == "navigator.webdriver")
        assert webdriver_tell.risk == "ok"
        user_agent_tell = next(tell for tell in tells if tell.name == "userAgent")
        assert user_agent_tell.risk == "warn"
