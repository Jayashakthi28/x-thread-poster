"""Public surface of xposter: configuration, models, errors and posting helpers.

Importing :mod:`xposter` is deliberately cheap: the configuration, pure models,
ledger enums, session store and the text splitter use only the standard library.
The browser-backed names (:class:`XPoster`, :class:`SessionInfo` and
:class:`RefreshResult`) are loaded lazily on first access, because importing
``xposter.poster`` or ``xposter.sessions`` imports Playwright. A caller that only
needs :func:`split_thread` or the dataclasses never pays for a browser import.

The lazy names remain ordinary attributes: ``from xposter import XPoster`` works,
and ``xposter.__all__`` lists every public name.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from .config import (
    Display,
    HumanPolicy,
    KeepalivePolicy,
    PacingPolicy,
    Strategy,
    XPosterConfig,
    default_home,
)
from .errors import (
    AccountMismatchError,
    AccountRestrictedError,
    BrowserUnavailableError,
    ChallengeError,
    ConfigurationError,
    LedgerCorruptError,
    NotLoggedInError,
    PacingError,
    ParkedError,
    PostOutcomeUnknownError,
    PostRejectedError,
    RateLimitedError,
    SessionBusyError,
    SessionError,
    UiChangedError,
    ValidationError,
    XPosterError,
)
from .ledger import Decision, LedgerStatus
from .models import PostedThread, PostedTweet, SessionSummary, Thread, Tweet
from .splitter import (
    pack_segments,
    split_thread,
    split_with_first,
    validate_tweet,
    weighted_length,
)
from .store import ParkInfo

if TYPE_CHECKING:
    # The browser-backed classes are re-exported lazily at runtime. Importing
    # them here under TYPE_CHECKING keeps static tools and IDEs aware of them
    # without importing Playwright when the package is imported.
    from .poster import XPoster as XPoster
    from .sessions import RefreshResult as RefreshResult
    from .sessions import SessionInfo as SessionInfo

__version__ = "0.2.0"

#: Names imported on first access instead of at package import time.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "XPoster": (".poster", "XPoster"),
    "SessionInfo": (".sessions", "SessionInfo"),
    "RefreshResult": (".sessions", "RefreshResult"),
}

__all__ = [
    "__version__",
    "AccountMismatchError",
    "AccountRestrictedError",
    "BrowserUnavailableError",
    "ChallengeError",
    "ConfigurationError",
    "Decision",
    "Display",
    "HumanPolicy",
    "KeepalivePolicy",
    "LedgerCorruptError",
    "LedgerStatus",
    "NotLoggedInError",
    "PacingError",
    "PacingPolicy",
    "ParkInfo",
    "ParkedError",
    "PostOutcomeUnknownError",
    "PostRejectedError",
    "PostedThread",
    "PostedTweet",
    "RateLimitedError",
    "RefreshResult",
    "SessionBusyError",
    "SessionError",
    "SessionInfo",
    "SessionSummary",
    "Strategy",
    "Thread",
    "Tweet",
    "UiChangedError",
    "ValidationError",
    "XPoster",
    "XPosterConfig",
    "XPosterError",
    "default_home",
    "pack_segments",
    "split_thread",
    "split_with_first",
    "validate_tweet",
    "weighted_length",
]


def __getattr__(name: str) -> Any:
    """Import a browser-backed public name on first access.

    The loaded object is cached in the module globals, so the import (and the
    Playwright import behind it) happens at most once per process. Unknown names
    raise :class:`AttributeError` exactly like a normal module attribute would.
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(importlib.import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """List the public names, including the lazily imported ones."""
    return sorted(set(globals()) | set(__all__))
