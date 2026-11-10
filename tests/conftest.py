"""Shared test fixtures and live-test gating."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from xposter import HumanPolicy, XPosterConfig


@pytest.fixture
def tmp_home(tmp_path: Path) -> XPosterConfig:
    """A config rooted in a temporary home with deterministic human timing."""
    home = tmp_path / "x"
    home.mkdir()
    return XPosterConfig(home=home, human=HumanPolicy(seed=1))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip ``live`` tests unless XPOSTER_LIVE=1; they need a real X session."""
    if os.environ.get("XPOSTER_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="live test: set XPOSTER_LIVE=1 with a logged-in X session")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


pytest_plugins = ["tests.fake_x.fixtures"]
