"""Offline fake of x.com used by the driver's end-to-end tests.

Import the public objects from here::

    from tests.fake_x import FakeTweet, FakeX, Scenario, weighted_length

The pytest fixtures live in :mod:`tests.fake_x.fixtures` and are registered by
``tests/conftest.py``; ``open_fake_context`` launches a real Chrome and installs
the fake before the first navigation.
"""

from __future__ import annotations

from .pages import ARTIFACT_CANARY, weighted_length
from .site import FakeTweet, FakeX, Scenario

__all__ = ["ARTIFACT_CANARY", "FakeTweet", "FakeX", "Scenario", "weighted_length"]
