"""Pytest fixtures for the offline fake x.com.

``tests/conftest.py`` registers this module as a plugin, so any test in the
suite can request ``fake_x`` or ``fake_x_factory`` without importing it. The
helper :func:`open_fake_context` is not a fixture: tests use it as an async
context manager so Chrome is always closed, even when an assertion fails.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest
from playwright.async_api import BrowserContext, Page, async_playwright

from xposter.browser import find_chrome, new_page, open_context
from xposter.config import Display, XPosterConfig

from . import FakeX


@pytest.fixture
def fake_x() -> FakeX:
    """A fake x.com with the defaults every driver test can share."""
    return FakeX()


@pytest.fixture
def fake_x_factory() -> Callable[..., FakeX]:
    """Return the ``FakeX`` constructor so a test can choose a scenario/dom."""
    return FakeX


@asynccontextmanager
async def open_fake_context(
    tmp_home: XPosterConfig,
    fake_x: FakeX,
) -> AsyncIterator[tuple[BrowserContext, Page]]:
    """Launch real headless Chrome on ``tmp_home`` with ``fake_x`` installed.

    The route is installed before the first page is touched, so even Chrome's
    initial ``about:blank``-to-x.com navigation cannot escape. The config is
    derived from ``tmp_home`` so the caller's temporary profile is used.
    """
    chrome = find_chrome(tmp_home)
    if chrome is None:
        raise RuntimeError("Google Chrome is not installed; refusing to start the fake")
    config = tmp_home.with_overrides(
        display=Display.HEADLESS,
        chrome_path=chrome,
        default_timeout_ms=15_000,
    )
    async with async_playwright() as playwright, open_context(playwright, config) as context:
        await fake_x.install(context)
        page = await new_page(context)
        yield context, page
