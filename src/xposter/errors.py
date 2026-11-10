"""Exception hierarchy for xposter.

Every message is written for the operator: what happened, what was tried and
what to do next. Cookie values never appear in messages so logs can be shared
and pasted into tickets safely.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import PostedTweet


class XPosterError(Exception):
    """Base class for every xposter failure."""


class ConfigurationError(XPosterError):
    """A setting is missing or invalid; fix the value and re-run."""


class BrowserUnavailableError(XPosterError):
    """Chrome is missing or failed to start; install Chrome or fix chrome_path."""


class ValidationError(XPosterError):
    """The content itself is invalid (empty, too long, or too much media)."""


class SessionBusyError(XPosterError):
    """The Chrome profile lock is held by another process; wait for it to finish."""


class SessionError(XPosterError):
    """Base class for saved-session problems that ``xposter login`` can fix."""


class NotLoggedInError(SessionError):
    """No saved session, or it expired: run ``xposter login``."""


class AccountMismatchError(SessionError):
    """The logged-in handle is not the configured one; log in as the expected account."""


class ParkedError(XPosterError):
    """Automation is parked after a challenge; a human must run ``xposter unpark``."""

    def __init__(self, reason: str, since: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.since = since


class ChallengeError(XPosterError):
    """X showed a captcha or verification prompt; stop, park and never retry automatically."""


class AccountRestrictedError(XPosterError):
    """The account is locked, suspended or limited; stop, park and resolve it manually."""


class RateLimitedError(XPosterError):
    """X rate-limited the account; stop, park and never retry automatically."""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class PostRejectedError(XPosterError):
    """X refused this post in a way that retrying will not fix.

    Duplicate content (code 187), a reply target that no longer exists (385) and
    text X refuses outright all land here. Nothing is wrong with the session, so
    this is not a park condition: fix the content, or verify what already landed,
    and post again as a fresh attempt.
    """

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class PacingError(XPosterError):
    """Local pacing says the next post is too soon; wait and retry (this is not a park)."""

    def __init__(self, message: str, retry_after_s: float) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class UiChangedError(XPosterError):
    """A selector is missing because X changed its UI; inspect artifacts and update selectors."""

    def __init__(self, message: str, artifacts: Path | None = None) -> None:
        super().__init__(message)
        self.artifacts = artifacts


class PostOutcomeUnknownError(XPosterError):
    """The click may have landed; do not blind-retry or the thread may be duplicated."""

    def __init__(self, message: str, posted: tuple[PostedTweet, ...] = ()) -> None:
        super().__init__(message)
        self.posted = posted


class LedgerCorruptError(XPosterError):
    """The ledger exists but is unreadable; posting blindly could duplicate a thread.

    The ledger is the only local proof of what has already been posted. Refusing
    to start is the safe default: the operator inspects or restores the file, or
    explicitly accepts losing its history with ``xposter ledger --reset-corrupt``.
    """

    def __init__(self, message: str, *, path: Path) -> None:
        super().__init__(message)
        self.path = path
