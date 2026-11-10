"""Typed configuration: safe defaults, environment overrides and derived paths.

Configuration is a frozen dataclass so a run cannot mutate settings halfway
through. ``XPOSTER_*`` variables are read only by :meth:`XPosterConfig.from_env`;
every other entry point receives a config explicitly, which keeps tests free of
ambient state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from .errors import ConfigurationError


class Display(StrEnum):
    """How the Chrome window is exposed while a run happens."""

    AUTO = "auto"
    HEADLESS = "headless"
    XVFB = "xvfb"
    VISIBLE = "visible"


class Strategy(StrEnum):
    """How a thread is assembled on X."""

    COMPOSER = "composer"
    REPLIES = "replies"


@dataclass(frozen=True, slots=True)
class HumanPolicy:
    """Human-like timing so automated posting does not look robotic.

    Pauses are inclusive ``(low, high)`` ranges. ``seed`` is ``None`` in
    production (nondeterministic) and set by tests to make runs repeatable.
    """

    typing_delay_ms: tuple[int, int] = (35, 120)
    pre_post_pause_s: tuple[float, float] = (1.5, 4.0)
    between_tweet_pause_s: tuple[float, float] = (20.0, 45.0)
    action_pause_s: tuple[float, float] = (0.4, 1.4)
    think_pause_s: tuple[float, float] = (2.0, 6.0)
    scroll_before_post: bool = True
    seed: int | None = None


@dataclass(frozen=True, slots=True)
class PacingPolicy:
    """Hard local limits that prevent bursts even when X would allow them.

    ``quiet_hours`` is a local-hour window ``[start, end)``; it may wrap
    midnight (for example ``(23, 7)``). ``jitter`` is the fractional spread
    applied to computed gaps.
    """

    min_gap_between_threads_s: float = 1800.0
    max_threads_per_day: int = 6
    max_tweets_per_day: int = 40
    max_tweets_per_hour: int = 12
    quiet_hours: tuple[int, int] | None = None
    jitter: float = 0.25


@dataclass(frozen=True, slots=True)
class KeepalivePolicy:
    """How often an idle saved session is refreshed before X expires it."""

    min_interval_s: float = 43200.0
    browse_seconds: tuple[float, float] = (20.0, 60.0)


def default_home() -> Path:
    """Return ``XPOSTER_HOME`` when set and non-blank, else ``~/.olsa/x``."""
    raw = os.environ.get("XPOSTER_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".olsa" / "x"


_EnumT = TypeVar("_EnumT", bound=StrEnum)


def _parse_enum(raw: str, enum_cls: type[_EnumT], env_name: str) -> _EnumT:
    try:
        return enum_cls(raw.strip().lower())
    except ValueError:
        choices = ", ".join(member.value for member in enum_cls)
        raise ConfigurationError(
            f"{env_name}={raw!r} is not a valid value; use one of: {choices}."
        ) from None


@dataclass(frozen=True, slots=True)
class XPosterConfig:
    """Everything a run needs to drive Chrome and post a thread."""

    home: Path = field(default_factory=default_home)
    handle: str | None = None
    display: Display = Display.AUTO
    strategy: Strategy = Strategy.COMPOSER
    chrome_path: str | None = None
    human: HumanPolicy = field(default_factory=HumanPolicy)
    pacing: PacingPolicy = field(default_factory=PacingPolicy)
    keepalive: KeepalivePolicy = field(default_factory=KeepalivePolicy)
    max_tweet_weight: int = 280
    max_media_per_tweet: int = 4
    max_mentions_per_thread: int = 5
    default_timeout_ms: int = 30000
    keep_artifacts: int = 10
    artifact_max_age_days: int = 14
    base_url: str = "https://x.com"

    @property
    def profile_dir(self) -> Path:
        """Persistent Chrome profile directory that holds the saved login."""
        return self.home / "profile"

    @property
    def storage_state_path(self) -> Path:
        """Playwright ``storage_state`` JSON with cookies and local storage."""
        return self.home / "storage_state.json"

    @property
    def meta_path(self) -> Path:
        """Session metadata: handle, expiry and last verification time."""
        return self.home / "session.json"

    @property
    def parked_path(self) -> Path:
        """Marker written when automation is parked after a challenge."""
        return self.home / "parked.json"

    @property
    def ledger_path(self) -> Path:
        """Posting ledger used to avoid duplicate threads."""
        return self.home / "ledger.json"

    @property
    def lock_path(self) -> Path:
        """Single-process lock that guards the Chrome profile."""
        return self.home / "session.lock"

    @property
    def artifacts_dir(self) -> Path:
        """Screenshots and traces kept for diagnosing failures."""
        return self.home / "artifacts"

    def with_overrides(self, **kw: Any) -> XPosterConfig:
        """Return a copy with selected fields replaced; unknown names raise ``TypeError``."""
        return replace(self, **kw)

    @classmethod
    def from_env(cls) -> XPosterConfig:
        """Build a config from ``XPOSTER_*`` variables; blank values count as unset.

        ``XPOSTER_HOME`` is already honoured by :func:`default_home`, so it is
        not applied twice. A leading ``@`` on the handle is dropped because the
        rest of the package compares bare handles.
        """
        overrides: dict[str, object] = {}

        handle = os.environ.get("XPOSTER_HANDLE", "").strip().lstrip("@")
        if handle:
            overrides["handle"] = handle

        display = os.environ.get("XPOSTER_DISPLAY", "").strip()
        if display:
            overrides["display"] = _parse_enum(display, Display, "XPOSTER_DISPLAY")

        strategy = os.environ.get("XPOSTER_STRATEGY", "").strip()
        if strategy:
            overrides["strategy"] = _parse_enum(strategy, Strategy, "XPOSTER_STRATEGY")

        chrome_path = os.environ.get("XPOSTER_CHROME_PATH", "").strip()
        if chrome_path:
            overrides["chrome_path"] = chrome_path

        mentions = os.environ.get("XPOSTER_MAX_MENTIONS_PER_THREAD", "").strip()
        if mentions:
            try:
                overrides["max_mentions_per_thread"] = int(mentions)
            except ValueError:
                raise ConfigurationError(
                    f"XPOSTER_MAX_MENTIONS_PER_THREAD must be an integer, got {mentions!r}."
                ) from None

        return cls().with_overrides(**overrides)
