"""Tests for X-weighted length, tweet validation and thread splitting."""

from __future__ import annotations

import random
import re

import pytest

from xposter.errors import ValidationError
from xposter.splitter import (
    pack_segments,
    split_thread,
    split_with_first,
    validate_tweet,
    weighted_length,
)

NUMBERING_RE = re.compile(r" \d+/\d+$")
STYLE_N_RE = re.compile(r" \d+$")


def _without_numbering(segment: str) -> str:
    return NUMBERING_RE.sub("", segment)


def test_ascii_weight_matches_code_points() -> None:
    assert weighted_length("") == 0
    assert weighted_length("hello") == 5
    assert weighted_length("a" * 280) == 280
    assert weighted_length("a" * 281) == 281


def test_url_weighs_23_regardless_of_length() -> None:
    assert weighted_length("https://example.com") == 23
    assert weighted_length("https://example.com/" + "a" * 500) == 23
    assert weighted_length("example.com") == 23
    assert weighted_length("www.example.com/path?q=1") == 23
    assert weighted_length("See https://example.com now") == 31


def test_url_trailing_punctuation_is_counted_separately() -> None:
    assert weighted_length("https://example.com.") == 24
    assert weighted_length("(https://example.com)") == 25
    assert weighted_length("https://example.com/foo(bar)") == 23


@pytest.mark.parametrize(
    "emoji",
    [
        "😀",
        "👍🏽",
        "👨‍👩‍👧‍👦",
        "🇺🇸",
        "1️⃣",
        "❤️",
        "🏳️‍🌈",
    ],
)
def test_emoji_sequence_weighs_two(emoji: str) -> None:
    assert weighted_length(emoji) == 2
    assert weighted_length(f"a{emoji}b") == 4


def test_cjk_and_math_bold_weigh_two_per_character() -> None:
    assert weighted_length("漢") == 2
    assert weighted_length("日本語") == 6
    assert weighted_length("𝗔") == 2
    assert weighted_length("𝗢𝗟𝗦𝗔") == 8
    assert weighted_length("𝟬") == 2
    assert weighted_length("a𝗔") == 3


def test_nfc_normalisation() -> None:
    assert weighted_length("e\u0301") == 1
    assert weighted_length("\u00e9") == 1
    assert weighted_length("e\u0301" * 280) == 280


def test_validate_tweet_accepts_exactly_at_limit() -> None:
    validate_tweet("a" * 280)
    validate_tweet("a" * 250, limit=250)


@pytest.mark.parametrize("text", ["", " ", "\n\t  "])
def test_validate_tweet_rejects_empty(text: str) -> None:
    with pytest.raises(ValidationError, match="empty"):
        validate_tweet(text)


def test_validate_tweet_reports_weight_limit_and_overage() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_tweet("a" * 281)
    message = str(excinfo.value)
    assert "281" in message
    assert "280" in message
    assert "over by 1" in message


def test_validate_tweet_rejects_non_positive_limit() -> None:
    with pytest.raises(ValidationError, match="limit"):
        validate_tweet("hello", limit=0)


def test_split_ascii_at_boundaries() -> None:
    assert split_thread("a" * 280) == ["a" * 280]
    segments = split_thread("a" * 281)
    assert len(segments) == 2
    assert [weighted_length(segment) for segment in segments] == [280, 9]
    assert segments[0].endswith(" 1/2")
    assert segments[1].endswith(" 2/2")


def test_numbering_reserves_room_and_totals_are_consistent() -> None:
    text = " ".join(["word"] * 150)
    segments = split_thread(text, limit=280)
    assert len(segments) == 3
    assert all(weighted_length(segment) <= 280 for segment in segments)
    assert [segment.rsplit(" ", 1)[1] for segment in segments] == [
        "1/3",
        "2/3",
        "3/3",
    ]


def test_numbering_style_n_is_supported() -> None:
    text = " ".join(["word"] * 150)
    segments = split_thread(text, limit=280, numbering_style="n")
    assert all(STYLE_N_RE.search(segment) for segment in segments)
    assert [STYLE_N_RE.search(segment).group(0).strip() for segment in segments] == [
        "1",
        "2",
        "3",
    ]


def test_unknown_numbering_style_is_rejected() -> None:
    with pytest.raises(ValidationError, match="numbering_style"):
        split_thread("hello " * 100, numbering_style="roman")


def test_fitting_text_is_returned_unnumbered() -> None:
    assert split_thread("hello", numbering=True) == ["hello"]
    assert split_thread("a" * 280, numbering=True) == ["a" * 280]
    assert split_thread("  hello  ") == ["hello"]


def test_split_thread_rejects_empty_text() -> None:
    with pytest.raises(ValidationError, match="empty"):
        split_thread("  \n\t ")


def test_split_thread_rejects_non_positive_limit() -> None:
    with pytest.raises(ValidationError, match="limit"):
        split_thread("hello", limit=-1)


def test_premium_limit_returns_one_segment() -> None:
    text = "a" * 10_000
    assert split_thread(text, limit=25_000) == [text]


def test_paragraph_boundary_is_preferred() -> None:
    first = "A" * 150
    second = "B" * 150
    segments = split_thread(f"{first}\n\n{second}", limit=200, numbering=False)
    assert segments == [first, second]


def test_paragraph_blank_line_is_preserved_inside_a_segment() -> None:
    assert split_thread("first\n\nsecond") == ["first\n\nsecond"]


def test_sentence_boundary_is_preferred_over_word_boundary() -> None:
    first = "A" * 150 + "."
    second = "B" * 150
    segments = split_thread(f"{first} {second}", limit=200, numbering=False)
    assert segments == [first, second]


def test_single_long_token_hard_splits_on_graphemes() -> None:
    token = "x" * 400
    segments = split_thread(token, limit=280, numbering=False)
    assert [len(segment) for segment in segments] == [280, 120]
    assert "".join(segments) == token


def test_hard_split_never_breaks_an_emoji_sequence() -> None:
    text = "😀" * 200
    segments = split_thread(text, limit=10, numbering=False)
    assert len(segments) == 40
    assert all(segment == "😀" * 5 for segment in segments)


def test_hard_split_keeps_zwj_families_whole() -> None:
    family = "👨‍👩‍👧‍👦"
    segments = split_thread(family * 100, limit=10, numbering=False)
    assert len(segments) == 20
    assert all(segment == family * 5 for segment in segments)


def test_hard_split_does_not_create_url_weight_at_segment_start() -> None:
    text = "_" * 8 + "a.ca"
    segments = split_thread(text, limit=8, numbering=False)
    assert all(weighted_length(segment) <= 8 for segment in segments)


def test_fragments_never_gain_url_weight_after_a_hard_split() -> None:
    rng = random.Random(7)
    chunks = ["a.ca", "x.com", "word", "😀", "漢", "#tag", "@name", " ", "\n"]
    for _ in range(300):
        text = "".join(rng.choice(chunks) for _ in range(rng.randint(1, 40)))
        if not text.strip():
            continue
        for limit in (8, 10, 20, 60):
            try:
                segments = split_thread(text, limit=limit)
            except ValidationError:
                continue
            assert all(weighted_length(segment) <= limit for segment in segments)


def test_url_is_never_split() -> None:
    url = "https://example.com/a/very/long/path?with=query&more=1"
    text = ("word " * 60).strip() + " " + url + " " + ("tail " * 60).strip()
    for numbering in (False, True):
        segments = split_thread(text, limit=100, numbering=numbering)
        assert all(weighted_length(segment) <= 100 for segment in segments)
        assert sum(url in segment for segment in segments) == 1


def test_hashtag_and_mention_are_never_split() -> None:
    hashtag = "#" + "a" * 40
    mention = "@" + "b" * 40
    filler = ("filler " * 30).strip()
    tail = ("tail " * 30).strip()
    text = f"{filler} {hashtag} {mention} {tail}"
    for numbering in (False, True):
        segments = split_thread(text, limit=80, numbering=numbering)
        assert all(weighted_length(segment) <= 80 for segment in segments)
        assert sum(hashtag in segment for segment in segments) == 1
        assert sum(mention in segment for segment in segments) == 1


def test_lorem_text_keeps_every_word_in_order() -> None:
    words = [f"word{index}" for index in range(900)]
    text = " ".join(words)
    assert len(text) > 5000
    segments = split_thread(text, limit=280)
    assert all(weighted_length(segment) <= 280 for segment in segments)
    restored = " ".join(_without_numbering(segment) for segment in segments).split()
    assert restored == words


def test_pack_segments_merges_with_blank_lines() -> None:
    assert pack_segments(["one", "two", "three"]) == ["one\n\ntwo\n\nthree"]
    assert pack_segments(["one", "two", "three"], limit=25_000) == ["one\n\ntwo\n\nthree"]
    assert pack_segments(["a" * 100, "b" * 100, "c" * 100], limit=150) == [
        "a" * 100,
        "b" * 100,
        "c" * 100,
    ]


def test_pack_segments_splits_an_oversized_part_without_numbering() -> None:
    big = "a" * 400
    segments = pack_segments(["short", big, "tail"], limit=280)
    assert segments == ["short", "a" * 280, "a" * 120, "tail"]
    assert all(weighted_length(segment) <= 280 for segment in segments)
    assert all(NUMBERING_RE.search(segment) is None for segment in segments)


def test_pack_segments_rejects_no_usable_parts() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        pack_segments([])
    with pytest.raises(ValidationError, match="non-empty"):
        pack_segments(["  ", "\n\t"])


def test_pack_segments_rejects_non_positive_limit() -> None:
    with pytest.raises(ValidationError, match="limit"):
        pack_segments(["one"], limit=0)


def test_split_with_first_keeps_first_verbatim_and_splits_rest() -> None:
    first = "Weekly index: 7 papers"
    rest = "paper summary. " * 40
    segments = split_with_first(first, rest, limit=280)
    assert segments[0] == first
    assert segments[1:] == split_thread(rest, limit=280)
    assert all(weighted_length(segment) <= 280 for segment in segments)
    assert NUMBERING_RE.search(segments[0]) is None


def test_split_with_first_validates_first_and_handles_empty_rest() -> None:
    assert split_with_first("index", "   ") == ["index"]
    assert split_with_first("index", "") == ["index"]
    with pytest.raises(ValidationError, match="empty"):
        split_with_first("", "rest")
    with pytest.raises(ValidationError, match="over by 1"):
        split_with_first("a" * 281, "rest")


def test_random_strings_never_exceed_limit_and_never_lose_words() -> None:
    rng = random.Random(0)
    vocabulary = [
        "alpha",
        "beta",
        "gamma",
        "delta",
        "paper",
        "thread",
        "OLSA",
        "https://example.com/paper/123",
        "#olsa",
        "@olsa",
        "😀",
        "👨‍👩‍👧‍👦",
        "🇺🇸",
        "1️⃣",
        "漢字",
        "𝗔𝗕",
        "word,",
        "end.",
    ]
    separators = [" ", " ", " ", "\n", "\n\n"]
    for _ in range(300):
        parts = [rng.choice(vocabulary) for _ in range(rng.randint(1, 120))]
        text = rng.choice(separators).join(parts)
        limit = rng.randint(60, 280)
        for numbering in (False, True):
            segments = split_thread(text, limit=limit, numbering=numbering)
            assert segments
            assert all(weighted_length(segment) <= limit for segment in segments)
            stripped = [_without_numbering(segment) for segment in segments]
            assert " ".join(stripped).split() == text.split()
            if numbering and len(segments) > 1:
                suffixes = [
                    match.group(0).strip()
                    for segment in segments
                    if (match := NUMBERING_RE.search(segment)) is not None
                ]
                assert len(suffixes) == len(segments)
                assert {suffix.split("/")[1] for suffix in suffixes} == {str(len(segments))}
                assert [suffix.split("/")[0] for suffix in suffixes] == [
                    str(index) for index in range(1, len(segments) + 1)
                ]
