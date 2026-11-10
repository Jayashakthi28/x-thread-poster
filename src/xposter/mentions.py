"""Mention extraction and the typeahead guard used while typing.

X's Automation Rules treat unsolicited automated mentions as spam, so the
poster caps the number of distinct accounts a thread may mention. This module
is pure: it only reads text, which keeps the cap testable without a browser.

The same module owns the *typeahead* detector. X opens a suggestion dropdown as
soon as a typed `@handle`, `#hashtag` or `$cashtag` looks complete; a following
Return/Shift+Return selects the highlighted suggestion instead of inserting a
line break. The poster avoids that by typing one plain space after a line-final
token (a space cannot belong to the token, so the dropdown closes harmlessly).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

__all__ = [
    "count_thread_mentions",
    "ends_with_typeahead_token",
    "extract_mentions",
]

# A mention is an "@" at a word boundary followed by a legal handle. The
# lookbehind rejects e-mail addresses (``foo@example.com``), URL paths
# (``https://x.com/@name``) and a second "@" (``@a@b``); the lookahead rejects
# a handle that continues past the 15-character limit or into another "@".
_MENTION_RE = re.compile(r"(?<![\w@/.])@([A-Za-z0-9_]{1,15})(?![\w@])")

# What X's typeahead reacts to at the end of the text typed so far. This is
# deliberately looser than the mention extractor (a token mid-word still opens
# the dropdown) and has no lookbehind.
_TYPEAHEAD_SUFFIX_RE = re.compile(r"(?:@[A-Za-z0-9_]{1,15}|#[A-Za-z0-9_]+|\$[A-Za-z]+)$")


def extract_mentions(text: str) -> list[str]:
    """Return distinct mentioned handles in first-appearance order, lower-cased.

    E-mail addresses and URL paths are ignored by the lookbehind; a 16th
    character after a handle makes the whole candidate invalid rather than
    returning a truncated 15-character handle.
    """
    handles: list[str] = []
    for match in _MENTION_RE.finditer(text):
        handle = match.group(1).lower()
        if handle not in handles:
            handles.append(handle)
    return handles


def count_thread_mentions(tweets: Sequence[str]) -> int:
    """Number of distinct handles across every tweet in a thread."""
    seen: list[str] = []
    for text in tweets:
        for handle in extract_mentions(text):
            if handle not in seen:
                seen.append(handle)
    return len(seen)


def ends_with_typeahead_token(text: str) -> bool:
    """True when ``text`` ends with a token that can open X's typeahead.

    Callers pass the text of the current line, not the whole tweet: a token
    followed by a newline in the full text is still line-final for the browser.
    """
    if not text:
        return False
    return _TYPEAHEAD_SUFFIX_RE.search(text) is not None
