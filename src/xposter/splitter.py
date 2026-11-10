"""Split long text into X thread segments using X's weighted character count.

X does not count characters, it counts *weight*. The rules below come from
twitter-text's v3 configuration
(https://github.com/twitter/twitter-text/blob/master/config/v3.json, generated
into ``js/src/configs.js``):

* ``maxWeightedTweetLength`` is 280, ``scale`` is 100, ``defaultWeight`` is
  200, ``transformedURLLength`` is 23 and ``emojiParsingEnabled`` is true.
* Code points 0-4351, 8192-8205, 8208-8223 and 8242-8247 weigh 100 (one
  character after dividing by the scale); every other code point weighs 200
  (two characters). Unicode Mathematical Sans-Serif Bold letters, for example,
  live above U+1D400 and therefore cost two each.
* Every URL weighs 23 no matter how long it is.
* An emoji sequence weighs one default weight (two characters), not one
  weight per code point. twitter-text delegates this to ``twemoji-parser``;
  this module uses a small, documented approximation of the same sequence
  rules, including ZWJ sequences, flags, keycaps and skin-tone modifiers.

``parseTweet`` (``js/src/parseTweet.js``) calls
``String.prototype.normalize()`` (NFC) before counting, so this module
normalises the same way.

The module is pure: no network, clock or filesystem access. That keeps thread
building deterministic and lets tests run anywhere.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from .errors import ValidationError

DEFAULT_LIMIT = 280

_URL_WEIGHT = 23
_EMOJI_WEIGHT = 2
_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))

# Common gTLDs and ccTLDs. twitter-text ships the full IANA list; this module
# keeps a deliberately smaller list so bare domains can be recognised without
# dragging in a large generated table. Scheme URLs are always recognised, even
# when their TLD is missing here.
_TLDS = (
    "com|org|net|io|ai|co|dev|app|xyz|info|me|ly|gg|tv|edu|gov|"
    "uk|de|fr|jp|cn|ru|br|in|it|es|nl|se|no|fi|dk|pl|ch|at|be|ca|"
    "au|nz|za|kr|tw|hk|sg|us|eu|tech|science|news|blog|site|online|"
    "store|cloud|page|link|social|video|photo|email|design|art|media|"
    "press|world|life|today|digital|agency|studio|works|network|"
    "systems|solutions"
)

# A URL is either an explicit http(s) URL or a bare domain with a known TLD.
# The lookbehind stops a match in the middle of an email, hashtag or word.
# Labels are capped at DNS limits (63 characters, twenty labels) so a long
# run of dots or letters cannot make the regex backtrack pathologically.
_URL_PATTERN = (
    r"(?:"
    r"https?://[^\s<>\"']+"
    r"|"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,20}"
    rf"(?:{_TLDS})"
    r"(?![a-z0-9-])"
    r"(?::\d{1,5})?"
    r"(?:[/?#][^\s<>\"']*)?"
    r")"
)
_URL_RE = re.compile(r"(?i)(?<![\w@#])" + _URL_PATTERN)
_URL_ANY_RE = re.compile(r"(?i)" + _URL_PATTERN)

_TRAILING_URL_PUNCTUATION = ".,!?;:'\"”’…"
_CLOSING_BRACKETS = {")": "(", "]": "[", "}": "{"}


def _char_weight(code_point: int) -> int:
    """Return the v3 weight of one code point (one or two characters)."""
    for low, high in _LIGHT_RANGES:
        if low <= code_point <= high:
            return 1
    return 2


def _trim_url_end(text: str, start: int, end: int) -> int:
    """Drop sentence punctuation and unbalanced closing brackets from a URL.

    twitter-text's extractor leaves trailing punctuation outside the URL, so
    "see https://example.com)." counts the dot and the paren as ordinary text.
    The same rule is applied here; balanced brackets such as Wikipedia titles
    stay part of the URL.
    """
    while end > start and text[end - 1] in _TRAILING_URL_PUNCTUATION:
        end -= 1
    while end > start and text[end - 1] in _CLOSING_BRACKETS:
        closing = text[end - 1]
        opener = _CLOSING_BRACKETS[closing]
        if text.count(opener, start, end) < text.count(closing, start, end):
            end -= 1
        else:
            break
    while end > start and text[end - 1] in _TRAILING_URL_PUNCTUATION:
        end -= 1
    return end


def _url_spans(text: str) -> list[tuple[int, int]]:
    """Return trimmed ``(start, end)`` spans for every URL in *text*."""
    spans: list[tuple[int, int]] = []
    for match in _URL_RE.finditer(text):
        start = match.start()
        end = _trim_url_end(text, start, match.end())
        if end > start:
            spans.append((start, end))
    return spans


# Emoji bases outside the light ranges, taken from twemoji's generated regex
# (the parser twitter-text uses). The big astral range covers emoticons and
# pictographs; the BMP range covers dingbats and miscellaneous symbols. Any
# symbol there that is not an emoji already weighs two, so treating it as a
# sequence does not change a lone character's weight.
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x2194, 0x2199),
    (0x21A9, 0x21AA),
    (0x231A, 0x231B),
    (0x23E9, 0x23F3),
    (0x23F8, 0x23FA),
    (0x25FB, 0x25FE),
    (0x2934, 0x2935),
    (0x2B05, 0x2B07),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
)

# BMP code points outside the ranges above that still act as emoji bases.
_EMOJI_SINGLES = frozenset(
    {
        0x203C,
        0x2049,
        0x2139,
        0x2328,
        0x23CF,
        0x24C2,
        0x25AA,
        0x25AB,
        0x25B6,
        0x25C0,
    }
)

# Symbols that twemoji only treats as emoji when followed by VS16. Without the
# selector, U+00A9 and U+00AE are ordinary one-weight characters.
_TEXT_EMOJI_BASES = frozenset({0x00A9, 0x00AE, 0x2122, 0x265F})
_KEYCAP_BASES = frozenset("#*0123456789")


def _is_emoji_base(code_point: int) -> bool:
    """Return whether *code_point* can start an emoji sequence."""
    for low, high in _EMOJI_RANGES:
        if low <= code_point <= high:
            return True
    return code_point in _EMOJI_SINGLES


def _emoji_end(text: str, index: int) -> int | None:
    """Return the end of the emoji sequence at *index*, or ``None``.

    This follows twemoji's sequence grammar closely enough for weighting:
    keycaps, regional-indicator pairs, variation selectors, skin-tone
    modifiers, tag sequences and ZWJ joins. A text-presentation selector
    (U+FE0E) cancels emoji treatment, matching twemoji's ``(?!\\ufe0e)`` rule.
    """
    char = text[index]
    code_point = ord(char)

    if char in _KEYCAP_BASES:
        end = index + 1
        if end < len(text) and text[end] == "\ufe0f":
            end += 1
        if end < len(text) and text[end] == "\u20e3":
            return end + 1
        return None

    if 0x1F1E6 <= code_point <= 0x1F1FF:
        end = index + 1
        if end < len(text) and 0x1F1E6 <= ord(text[end]) <= 0x1F1FF:
            end += 1
        return end

    if code_point in _TEXT_EMOJI_BASES:
        if index + 1 >= len(text) or text[index + 1] != "\ufe0f":
            return None
        end = index + 2
    elif _is_emoji_base(code_point):
        if index + 1 < len(text) and text[index + 1] == "\ufe0e":
            return None
        end = index + 1
    else:
        return None

    while end < len(text):
        char = text[end]
        code_point = ord(char)
        if (
            char in "\ufe0e\ufe0f"
            or 0x1F3FB <= code_point <= 0x1F3FF
            or 0xE0020 <= code_point <= 0xE007F
        ):
            end += 1
        elif char == "\u200d" and end + 1 < len(text) and _is_emoji_base(ord(text[end + 1])):
            end += 2
        else:
            break
    return end


def weighted_length(text: str) -> int:
    """Return *text*'s length in X characters (weighted, NFC-normalised).

    ASCII letters cost one, most other code points cost two, a URL costs 23
    and one emoji sequence costs two. This mirrors twitter-text's
    ``parseTweet`` so callers can decide whether a tweet fits before posting.
    """
    normalized = unicodedata.normalize("NFC", text)
    spans = _url_spans(normalized)
    total = 0
    index = 0
    span_index = 0
    while index < len(normalized):
        if span_index < len(spans) and index == spans[span_index][0]:
            total += _URL_WEIGHT
            index = spans[span_index][1]
            span_index += 1
            continue
        emoji_end = _emoji_end(normalized, index)
        if emoji_end is not None:
            total += _EMOJI_WEIGHT
            index = emoji_end
            continue
        total += _char_weight(ord(normalized[index]))
        index += 1
    return total


_PARAGRAPH_RE = re.compile(r"\r?\n[ \t]*\r?\n[ \t]*")
_HASHTAG_RE = re.compile(r"(?<![\w#])#\w+")
_MENTION_RE = re.compile(r"(?<![\w@])@\w+")


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Return spans the splitter must never cut through.

    URLs, hashtags, mentions and emoji sequences are atomic even when a caller
    asks for a hard split; cutting one changes its meaning or breaks the link.
    """
    spans = _url_spans(text)
    spans.extend(match.span() for match in _HASHTAG_RE.finditer(text))
    spans.extend(match.span() for match in _MENTION_RE.finditer(text))
    index = 0
    while index < len(text):
        emoji_end = _emoji_end(text, index)
        if emoji_end is not None:
            spans.append((index, emoji_end))
            index = emoji_end
        else:
            index += 1
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _blocked_url_starts(text: str) -> list[int]:
    """Return positions that would become a URL if a segment started there.

    ``_URL_RE`` uses the preceding character to avoid matching inside a word.
    A hard cut can remove that context, so a fragment such as ``a.ca`` could
    suddenly weigh 23 instead of four. Such positions are therefore banned as
    cut points.
    """
    starts: list[int] = []
    for match in _URL_ANY_RE.finditer(text):
        start = match.start()
        if _URL_RE.match(text, start) is None:
            starts.append(start)
    return starts


def _protected_mask(text: str, spans: list[tuple[int, int]]) -> bytearray:
    """Mark positions where a segment boundary must not fall."""
    mask = bytearray(len(text))
    for start, end in spans:
        mask[start + 1 : end] = b"\x01" * (end - start - 1)
    for start in _blocked_url_starts(text):
        mask[start] = 1
    return mask


def _grapheme_end(text: str, index: int) -> int:
    """Return the end of the grapheme cluster starting at *index*.

    Python's stdlib has no grapheme segmenter, so this approximates UAX #29
    for the cases that matter here: CRLF, emoji sequences, combining marks,
    variation selectors and ZWJ joins. A hard split may only land between
    clusters, which keeps accents and emoji intact.
    """
    if text[index] == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
        return index + 2
    emoji_end = _emoji_end(text, index)
    if emoji_end is not None:
        return emoji_end
    end = index + 1
    while end < len(text):
        char = text[end]
        code_point = ord(char)
        if (
            unicodedata.combining(char)
            or char in "\ufe0e\ufe0f"
            or 0x1F3FB <= code_point <= 0x1F3FF
        ):
            end += 1
        elif char == "\u200d" and end + 1 < len(text):
            end += 2
        else:
            break
    return end


def _next_atom(text: str, index: int) -> tuple[int, int]:
    """Return ``(end, weight)`` for the atomic unit starting at *index*."""
    match = _URL_RE.match(text, index)
    if match is not None:
        end = _trim_url_end(text, index, match.end())
        if end > index:
            return end, _URL_WEIGHT
    emoji_end = _emoji_end(text, index)
    if emoji_end is not None:
        return emoji_end, _EMOJI_WEIGHT
    return index + 1, _char_weight(ord(text[index]))


def _max_prefix_end(text: str, start: int, limit: int) -> int:
    """Return the furthest end offset whose weight still fits *limit*."""
    total = 0
    index = start
    last_end = start
    while index < len(text):
        end, weight = _next_atom(text, index)
        if total + weight > limit:
            break
        total += weight
        index = end
        last_end = end
    return last_end


def _paragraph_cut(text: str, start: int, max_end: int, _mask: bytearray) -> int | None:
    """Return the last blank-line boundary inside the prefix, if any."""
    best: int | None = None
    for match in _PARAGRAPH_RE.finditer(text, start, max_end):
        if match.start() > start:
            best = match.start()
    return best


def _sentence_cut(text: str, start: int, max_end: int, mask: bytearray) -> int | None:
    """Return the last sentence boundary inside the prefix, if any."""
    best: int | None = None
    index = start
    while index < max_end:
        char = text[index]
        if char in "。！？":
            cut = index + 1
            if not mask[cut]:
                best = cut
            index += 1
            continue
        if char in ".!?…":
            end = index + 1
            while end < len(text) and text[end] in ".!?…":
                end += 1
            while end < len(text) and text[end] in "\"'”’)]}":
                end += 1
            if (
                end <= max_end
                and (end >= len(text) or not mask[end])
                and (end >= len(text) or text[end].isspace())
            ):
                best = end
            index = end
            continue
        index += 1
    return best


def _word_cut(text: str, start: int, max_end: int, _mask: bytearray) -> int | None:
    """Return the last whitespace boundary inside the prefix, if any."""
    for index in range(max_end - 1, start, -1):
        if text[index].isspace() and not text[index - 1].isspace():
            return index
    return None


def _grapheme_cut(text: str, start: int, max_end: int, mask: bytearray) -> int | None:
    """Return the last unprotected grapheme boundary inside the prefix."""
    best: int | None = None
    index = start
    while index < max_end:
        end = _grapheme_end(text, index)
        if end > max_end:
            break
        index = end
        if not mask[index]:
            best = index
    return best


def _previous_cut(text: str, start: int, cut: int, mask: bytearray) -> int | None:
    """Return the latest unprotected grapheme cut strictly before *cut*."""
    previous: int | None = None
    index = start
    while index < cut:
        end = _grapheme_end(text, index)
        if end >= cut:
            break
        index = end
        if not mask[index]:
            previous = index
    return previous


def _unsplittable_message(text: str, start: int, limit: int) -> str:
    """Explain which atomic token cannot fit so the operator can fix it."""
    token = text[start : start + 40].split(maxsplit=1)[0]
    return (
        f"Cannot fit {token!r} in {limit} weighted characters without breaking "
        "a URL, hashtag, mention or emoji sequence; shorten it or raise the limit."
    )


def _find_cut(text: str, start: int, limit: int, mask: bytearray) -> int:
    """Choose where to end one segment, preferring the most natural boundary.

    Paragraphs beat sentences, sentences beat words, and words beat a hard
    grapheme split. Within a level the latest boundary is chosen so the greedy
    packer emits as few segments as possible.
    """
    max_end = _max_prefix_end(text, start, limit)
    if max_end >= len(text):
        return len(text)
    if max_end <= start:
        raise ValidationError(_unsplittable_message(text, start, limit))
    for finder in (_paragraph_cut, _sentence_cut, _word_cut):
        cut = finder(text, start, max_end, mask)
        if cut is not None and cut > start:
            return cut
    cut = _grapheme_cut(text, start, max_end, mask)
    if cut is None or cut <= start:
        raise ValidationError(_unsplittable_message(text, start, limit))
    return cut


def _pack_to_limit(text: str, limit: int, mask: bytearray) -> list[str]:
    """Greedily pack *text* into segments no heavier than *limit*."""
    segments: list[str] = []
    start = 0
    while start < len(text):
        while start < len(text) and text[start].isspace():
            start += 1
        if start >= len(text):
            break
        cut = _find_cut(text, start, limit, mask)
        segment = text[start:cut].strip()
        # A cut can change how the next segment's first characters parse as a
        # URL, and trimming can expose a URL hidden behind a lookahead. Verify
        # with the public measurement and walk the cut back until it fits.
        while weighted_length(segment) > limit:
            previous = _previous_cut(text, start, cut, mask)
            if previous is None:
                raise ValidationError(_unsplittable_message(text, start, limit))
            cut = previous
            segment = text[start:cut].strip()
        if not segment:
            raise ValidationError(
                "Internal splitter error: produced an empty segment; report this."
            )
        segments.append(segment)
        start = cut
    return segments


def _suffix(index: int, total: int, style: str) -> str:
    """Build the numbering suffix for one segment."""
    if style == "n":
        return f" {index}"
    return f" {index}/{total}"


def _pack_numbered(text: str, limit: int, mask: bytearray, style: str) -> list[str]:
    """Pack with a reserved suffix, iterating until the total is stable.

    The suffix width depends on the number of segments, which is only known
    after packing. Starting from one segment and re-packing with the new width
    converges quickly because a wider suffix can never reduce the count.
    """
    guess = 1
    for _ in range(100):
        reserve = weighted_length(_suffix(guess, guess, style))
        if reserve >= limit:
            raise ValidationError(
                f"limit={limit} is too small for numbering; use at least "
                f"{reserve + 1} or turn numbering off."
            )
        segments = _pack_to_limit(text, limit - reserve, mask)
        if len(segments) == guess:
            total = len(segments)
            return [
                f"{segment}{_suffix(index, total, style)}"
                for index, segment in enumerate(segments, start=1)
            ]
        guess = len(segments)
    raise ValidationError("Numbering did not stabilise; turn numbering off.")


_STYLES = frozenset({"n/total", "n"})


def validate_tweet(text: str, *, limit: int = DEFAULT_LIMIT) -> None:
    """Raise :class:`ValidationError` when *text* cannot be one tweet.

    The message names the measured weight, the limit and the overage so the
    operator can trim the post without guessing.
    """
    if limit <= 0:
        raise ValidationError(f"limit must be a positive integer, got {limit}.")
    if not text.strip():
        raise ValidationError("Tweet text is empty; add at least one non-whitespace character.")
    weight = weighted_length(text)
    if weight > limit:
        raise ValidationError(
            f"Tweet is {weight} characters by X's weighted count; the limit is "
            f"{limit}, so it is over by {weight - limit}."
        )


def split_thread(
    text: str,
    *,
    limit: int = DEFAULT_LIMIT,
    numbering: bool = True,
    numbering_style: str = "n/total",
) -> list[str]:
    """Split *text* into thread segments that each fit X's weighted limit.

    Paragraphs, then sentences, then words are preferred, with a grapheme hard
    split only for a single enormous token. URLs, hashtags, mentions and emoji
    sequences are never cut. A text that already fits is returned as one
    unnumbered segment. When numbering is on, room for the suffix is reserved
    while packing and the total is found by iterating until stable.
    """
    if limit <= 0:
        raise ValidationError(f"limit must be a positive integer, got {limit}.")
    if numbering_style not in _STYLES:
        raise ValidationError(f"Unknown numbering_style {numbering_style!r}; use 'n/total' or 'n'.")
    normalized = unicodedata.normalize("NFC", text).strip()
    if not normalized:
        raise ValidationError("Thread text is empty; add at least one non-whitespace character.")
    if weighted_length(normalized) <= limit:
        return [normalized]
    mask = _protected_mask(normalized, _protected_spans(normalized))
    if not numbering:
        return _pack_to_limit(normalized, limit, mask)
    return _pack_numbered(normalized, limit, mask, numbering_style)


def pack_segments(parts: Sequence[str], *, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Merge pre-split *parts* greedily into tweet-sized segments.

    Callers such as the weekly edition already know their natural units (one
    paper summary each), so merging them with blank lines keeps those units
    together instead of re-flowing prose. A part that alone exceeds the limit
    is handed to :func:`split_thread` without numbering.
    """
    if limit <= 0:
        raise ValidationError(f"limit must be a positive integer, got {limit}.")
    cleaned = [part.strip() for part in parts]
    cleaned = [part for part in cleaned if part]
    if not cleaned:
        raise ValidationError("pack_segments needs at least one non-empty part.")
    segments: list[str] = []
    current = ""
    for part in cleaned:
        if weighted_length(part) > limit:
            if current:
                segments.append(current)
                current = ""
            segments.extend(split_thread(part, limit=limit, numbering=False))
        elif not current:
            current = part
        elif weighted_length(f"{current}\n\n{part}") <= limit:
            current = f"{current}\n\n{part}"
        else:
            segments.append(current)
            current = part
    if current:
        segments.append(current)
    return segments


def split_with_first(first: str, rest: str, *, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Keep *first* verbatim and split *rest* into the following segments.

    The OLSA weekly thread starts with a hand-written index post; that post must
    be posted exactly as written (and is never numbered or re-split), while the
    per-paper replies go through the normal splitter. *rest* uses the regular
    numbering so a long reply chain stays ordered.
    """
    validate_tweet(first, limit=limit)
    if not rest.strip():
        return [first]
    return [first, *split_thread(rest, limit=limit)]
