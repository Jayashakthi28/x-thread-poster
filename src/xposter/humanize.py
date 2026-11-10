"""Human-like timing plans: pauses, typing cadence, scrolling and mouse paths.

There is no Playwright import here on purpose. The browser layer executes these
plans, while this module stays pure and unit-testable: every random choice comes
from an injected :class:`random.Random`, so one seed reproduces a whole session.

Typing is planned per grapheme cluster, never per code point. Splitting a
family emoji, a flag or a combining sequence across keystrokes would type
something different from what the operator wrote, and X's composer would show
it.
"""

from __future__ import annotations

import asyncio
import math
import random
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .config import HumanPolicy

_ZWJ = "\u200d"
_VARIATION_SELECTORS = frozenset({"\ufe0e", "\ufe0f"})
_KEYCAP = "\u20e3"
_NEWLINES = frozenset({"\n", "\r", "\r\n"})
_PUNCTUATION = ".,;:!?)]}\"'"

_min_steps = 8
_max_steps = 60


@dataclass(frozen=True, slots=True)
class TypeStep:
    """Type ``text`` (one or more grapheme clusters), then wait ``delay_ms``."""

    text: str
    delay_ms: int


def _is_regional_indicator(char: str) -> bool:
    return "\U0001f1e6" <= char <= "\U0001f1ff"


def _is_skin_tone(char: str) -> bool:
    return "\U0001f3fb" <= char <= "\U0001f3ff"


def _is_tag(char: str) -> bool:
    return "\U000e0020" <= char <= "\U000e007f"


def _is_extender(char: str) -> bool:
    """True for marks and modifiers that attach to the preceding base."""
    return (
        unicodedata.combining(char) != 0
        or unicodedata.category(char) in {"Mn", "Mc", "Me"}
        or char in _VARIATION_SELECTORS
        or char == _KEYCAP
        or _is_skin_tone(char)
        or _is_tag(char)
    )


def _graphemes(text: str) -> list[str]:
    """Split ``text`` into user-perceived characters.

    This is a deliberately small segmenter: base character plus combining marks,
    variation selectors, skin tones, tag characters, keycap marks, regional
    indicator pairs and ZWJ sequences. It covers everything X users paste; full
    UAX #29 is more than a posting tool needs.
    """
    clusters: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        start = index
        char = text[index]
        index += 1
        if char == "\r" and index < length and text[index] == "\n":
            index += 1
            clusters.append(text[start:index])
            continue
        if char in "\r\n":
            clusters.append(char)
            continue
        if _is_regional_indicator(char) and index < length and _is_regional_indicator(text[index]):
            index += 1
        while index < length:
            following = text[index]
            if following == _ZWJ:
                index += 1
                if index < length:
                    index += 1
                continue
            if _is_extender(following):
                index += 1
                continue
            break
        clusters.append(text[start:index])
    return clusters


def _smoothstep(value: float) -> float:
    return value * value * (3.0 - 2.0 * value)


def _bezier(start: float, control1: float, control2: float, end: float, t: float) -> float:
    inverse = 1.0 - t
    return (
        inverse**3 * start
        + 3.0 * inverse**2 * t * control1
        + 3.0 * inverse * t**2 * control2
        + t**3 * end
    )


def _total_ms(steps: list[TypeStep]) -> int:
    return sum(step.delay_ms for step in steps)


def _fit_budget(steps: list[TypeStep], budget_ms: float) -> list[TypeStep]:
    """Scale delays down proportionally until the plan fits ``budget_ms``."""
    total = _total_ms(steps)
    if total <= budget_ms:
        return steps
    if budget_ms <= 0:
        return [TypeStep(step.text, 0) for step in steps]
    factor = budget_ms / total
    return [TypeStep(step.text, int(step.delay_ms * factor)) for step in steps]


class Humanizer:
    """Turns a policy into concrete delays, typing plans and pointer paths."""

    def __init__(self, policy: HumanPolicy, *, rng: random.Random | None = None) -> None:
        self._policy = policy
        self._rng = rng if rng is not None else random.Random(policy.seed)

    def pause_s(self, kind: str) -> float:
        """Draw one pause for ``kind`` from its inclusive policy range."""
        low, high = self._range(kind)
        return self._rng.uniform(low, high)

    async def pause(
        self,
        kind: str,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> float:
        """Await one pause and return the seconds actually requested."""
        seconds = self.pause_s(kind)
        await sleep(seconds)
        return seconds

    def typing_plan(self, text: str, *, max_total_s: float = 75.0) -> list[TypeStep]:
        """Plan keystrokes so the concatenation equals ``text`` exactly.

        Long Premium posts would take minutes at human cadence, so graphemes
        are merged into progressively larger chunks until the plan fits the
        budget. Newlines always stay separate steps, because presses of Return
        are a visible rhythm of their own.
        """
        if max_total_s < 0:
            raise ValueError("max_total_s must be >= 0.")
        clusters = _graphemes(text)
        if not clusters:
            return []
        budget_ms = max_total_s * 1000.0
        chunk = 1
        steps = self._build_steps(clusters, chunk)
        while _total_ms(steps) > budget_ms and chunk < len(clusters):
            chunk = min(len(clusters), chunk * 2)
            steps = self._build_steps(clusters, chunk)
        return _fit_budget(steps, budget_ms)

    def scroll_plan(self) -> list[tuple[int, float]]:
        """A few wheel steps down and a partial scroll back, like a reader.

        Returns an empty list when the policy disables the pre-post scroll.
        """
        if not self._policy.scroll_before_post:
            return []
        plan: list[tuple[int, float]] = []
        total_down = 0
        for _ in range(self._rng.randint(2, 4)):
            step = self._rng.randint(180, 480)
            total_down += step
            plan.append((step, self.pause_s("action")))
        back = -max(40, int(total_down * self._rng.uniform(0.3, 0.6)))
        plan.append((back, self.pause_s("action")))
        return plan

    def mouse_path(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        steps: int | None = None,
    ) -> list[tuple[float, float]]:
        """Cubic-Bezier pointer path with ease-in-out spacing and light jitter.

        The path starts exactly on ``start`` and ends exactly on ``end``. Step
        count grows with distance so short hops are not over-animated and long
        ones are not a single teleport.
        """
        x0, y0 = start
        x1, y1 = end
        dx = x1 - x0
        dy = y1 - y0
        distance = math.hypot(dx, dy)
        if steps is None:
            count = max(_min_steps, min(_max_steps, int(distance / 30.0) + 1))
        else:
            if steps < 2:
                raise ValueError("mouse_path needs at least 2 steps.")
            count = steps
        if distance == 0.0:
            return [(x0, y0)] * (count - 1) + [(x1, y1)]
        unit_x = dx / distance
        unit_y = dy / distance
        normal_x = -unit_y
        normal_y = unit_x
        spread = min(90.0, distance * 0.18)
        offset_one = self._rng.uniform(-spread, spread)
        offset_two = self._rng.uniform(-spread, spread)
        control_one = (
            x0 + dx * 0.25 + normal_x * offset_one,
            y0 + dy * 0.25 + normal_y * offset_one,
        )
        control_two = (
            x0 + dx * 0.75 + normal_x * offset_two,
            y0 + dy * 0.75 + normal_y * offset_two,
        )
        jitter = min(0.8, max(0.15, distance * 0.002))
        points: list[tuple[float, float]] = []
        for index in range(count):
            progress = _smoothstep(index / (count - 1))
            point_x = _bezier(x0, control_one[0], control_two[0], x1, progress)
            point_y = _bezier(y0, control_one[1], control_two[1], y1, progress)
            if 0 < index < count - 1:
                wobble = self._rng.uniform(-jitter, jitter)
                point_x += normal_x * wobble
                point_y += normal_y * wobble
            points.append((point_x, point_y))
        points[0] = (x0, y0)
        points[-1] = (x1, y1)
        return points

    def click_offset(self, width: float, height: float) -> tuple[float, float]:
        """A Gaussian point inside an element box, clamped to 15%..85% per axis."""
        return self._axis_offset(width), self._axis_offset(height)

    def _axis_offset(self, size: float) -> float:
        fraction = min(0.85, max(0.15, self._rng.gauss(0.5, 0.13)))
        return fraction * size

    def _range(self, kind: str) -> tuple[float, float]:
        match kind:
            case "action":
                return self._policy.action_pause_s
            case "think":
                return self._policy.think_pause_s
            case "pre_post":
                return self._policy.pre_post_pause_s
            case "between_tweet":
                return self._policy.between_tweet_pause_s
            case _:
                raise ValueError(f"Unknown pause kind {kind!r}.")

    def _build_steps(self, clusters: list[str], chunk: int) -> list[TypeStep]:
        steps: list[TypeStep] = []
        buffer: list[str] = []
        for cluster in clusters:
            if cluster in _NEWLINES:
                if buffer:
                    steps.append(self._step("".join(buffer)))
                    buffer.clear()
                steps.append(self._step(cluster))
                continue
            buffer.append(cluster)
            if len(buffer) >= chunk:
                steps.append(self._step("".join(buffer)))
                buffer.clear()
        if buffer:
            steps.append(self._step("".join(buffer)))
        return steps

    def _step(self, content: str) -> TypeStep:
        return TypeStep(text=content, delay_ms=self._delay_ms(content))

    def _delay_ms(self, content: str) -> int:
        """Log-normal-ish keystroke delay, longer after spaces and punctuation.

        The occasional 300-900 ms "thinking" beat is added on a trailing space,
        i.e. between words, where a human actually hesitates.
        """
        low, high = self._policy.typing_delay_ms
        low_ms = max(0, low)
        high_ms = max(low_ms, high)
        factor = min(1.0, math.exp(self._rng.gauss(0.0, 0.5)) / 3.0)
        delay = low_ms + (high_ms - low_ms) * factor
        if content and (content[-1] in " \t" or content[-1] in _PUNCTUATION):
            delay *= 1.6
        delay = min(float(high_ms), max(float(low_ms), delay))
        if content.endswith((" ", "\t")) and self._rng.random() < 1.0 / 40.0:
            delay += self._rng.uniform(300.0, 900.0)
        return int(round(delay))
