"""Unit tests for mention extraction and the typeahead suffix detector."""

from __future__ import annotations

import pytest

from xposter.mentions import (
    count_thread_mentions,
    ends_with_typeahead_token,
    extract_mentions,
)


def test_extract_mentions_is_distinct_lowercase_and_ordered() -> None:
    assert extract_mentions("Hi @Bob, @alice and @bob again") == ["bob", "alice"]


def test_extract_mentions_ignores_email_addresses() -> None:
    assert extract_mentions("Contact foo@example.com or @real") == ["real"]


def test_extract_mentions_ignores_url_paths() -> None:
    assert extract_mentions("See https://x.com/@name and @real") == ["real"]


def test_extract_mentions_rejects_stacked_handles() -> None:
    assert extract_mentions("@a@b") == []


def test_extract_mentions_accepts_trailing_punctuation() -> None:
    assert extract_mentions("Thanks @bob, @alice! @carol.") == ["bob", "alice", "carol"]


def test_extract_mentions_does_not_truncate_a_long_handle() -> None:
    assert extract_mentions("@abcdefghijklmnop") == []
    assert extract_mentions("@abcdefghijklmno") == ["abcdefghijklmno"]


def test_extract_mentions_ignores_hashtags_and_cashtags() -> None:
    assert extract_mentions("#AI and $XYZ but @real") == ["real"]


def test_count_thread_mentions_counts_distinct_handles_across_tweets() -> None:
    texts = ["@one and @two", "again @one and @three", "nothing here"]
    assert count_thread_mentions(texts) == 3


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@handle", True),
        ("hello @handle", True),
        ("#hashtag", True),
        ("$CASHTAG", True),
        ("@handle,", False),
        ("@handle ", False),
        ("plain text", False),
        ("mail foo@bar", True),
        ("@abcdefghijklmnop", False),
        ("@abcdefghijklmno", True),
        ("a#", False),
        ("$123", False),
    ],
)
def test_ends_with_typeahead_token(text: str, expected: bool) -> None:
    assert ends_with_typeahead_token(text) is expected
