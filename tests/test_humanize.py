"""Tests for human-like timing: pauses, typing plans, scrolling and mouse paths."""

from __future__ import annotations

import math
from itertools import pairwise

import pytest

from xposter.config import HumanPolicy
from xposter.humanize import Humanizer


def humanizer(seed: int = 7) -> Humanizer:
    return Humanizer(HumanPolicy(seed=seed))


def plan_texts(text: str) -> list[str]:
    return [step.text for step in humanizer().typing_plan(text)]


def test_seeded_humanizers_are_deterministic() -> None:
    policy = HumanPolicy(seed=123)
    first = Humanizer(policy)
    second = Humanizer(policy)
    text = "Hello 👋🏽 world, again!\nSee you."

    assert first.typing_plan(text) == second.typing_plan(text)
    assert first.scroll_plan() == second.scroll_plan()
    assert first.mouse_path((0.0, 0.0), (400.0, 300.0)) == second.mouse_path(
        (0.0, 0.0), (400.0, 300.0)
    )
    assert first.click_offset(100.0, 50.0) == second.click_offset(100.0, 50.0)


@pytest.mark.parametrize(
    ("kind", "low", "high"),
    [
        ("action", 0.4, 1.4),
        ("think", 2.0, 6.0),
        ("pre_post", 1.5, 4.0),
        ("between_tweet", 20.0, 45.0),
    ],
)
def test_pause_ranges(kind: str, low: float, high: float) -> None:
    human = humanizer()
    for _ in range(25):
        value = human.pause_s(kind)
        assert low <= value <= high


def test_unknown_pause_kind_raises() -> None:
    with pytest.raises(ValueError, match="Unknown pause kind"):
        humanizer().pause_s("yawn")


async def test_pause_awaits_the_injected_sleep() -> None:
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    seconds = await humanizer().pause("think", sleep=fake_sleep)

    assert recorded == [seconds]
    assert 2.0 <= seconds <= 6.0


@pytest.mark.parametrize(
    "text",
    [
        "Hello, world!\nSecond line.",
        "👨‍👩‍👧‍👦 family",
        "🇫🇷 flag 🇩🇪",
        "e\u0301 cafe\u0301",
        "你好，世界",
        "𝐇𝐞𝐥𝐥𝐨 𝐦𝐚𝐭𝐡",
        "1️⃣ keycap ❤️ heart 👍🏽 tone",
        "mixed 👩🏽‍🚀 ZWJ\r\nand CRLF",
        "",
    ],
)
def test_typing_plan_concatenation_matches_the_input(text: str) -> None:
    plan = humanizer().typing_plan(text)

    assert "".join(step.text for step in plan) == text
    assert all(step.text for step in plan)
    assert all(step.delay_ms >= 0 for step in plan)


@pytest.mark.parametrize(
    ("text", "clusters"),
    [
        ("e\u0301", 1),
        ("👨‍👩‍👧‍👦", 1),
        ("🇫🇷", 1),
        ("🇫🇷🇩🇪", 2),
        ("👍🏽", 1),
        ("1️⃣", 1),
        ("❤️", 1),
        ("a\nb", 3),
        ("a\r\nb", 3),
        ("🇫🇷X", 2),
        ("👨‍👩‍👧‍👦!", 2),
    ],
)
def test_grapheme_clusters_are_never_split(text: str, clusters: int) -> None:
    assert len(plan_texts(text)) == clusters


def test_newlines_are_their_own_step() -> None:
    assert plan_texts("a\nb") == ["a", "\n", "b"]
    assert plan_texts("a\r\nb") == ["a", "\r\n", "b"]


def test_typing_delays_stay_near_the_policy_range() -> None:
    human = humanizer(seed=11)
    plan = human.typing_plan("word " * 300)

    assert all(step.delay_ms >= 35 for step in plan)
    assert all(step.delay_ms <= 120 + 900 for step in plan)
    assert any(step.delay_ms > 120 for step in plan)


def test_long_text_respects_the_total_budget() -> None:
    text = "x" * 10_000
    plan = humanizer(seed=3).typing_plan(text, max_total_s=75.0)

    assert "".join(step.text for step in plan) == text
    assert sum(step.delay_ms for step in plan) <= 75_000
    assert len(plan) < 10_000


def test_zero_budget_flattens_delays() -> None:
    plan = humanizer().typing_plan("abc", max_total_s=0.0)

    assert "".join(step.text for step in plan) == "abc"
    assert sum(step.delay_ms for step in plan) == 0


def test_negative_budget_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_total_s"):
        humanizer().typing_plan("abc", max_total_s=-1.0)


def test_mouse_path_starts_and_ends_exactly() -> None:
    start = (10.0, 20.0)
    end = (610.0, 420.0)
    path = humanizer().mouse_path(start, end)

    assert path[0] == start
    assert path[-1] == end
    assert 8 <= len(path) <= 60


def test_mouse_path_progress_is_monotonic() -> None:
    start = (10.0, 20.0)
    end = (610.0, 420.0)
    path = humanizer().mouse_path(start, end)
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    projections = [((x - start[0]) * dx + (y - start[1]) * dy) / length for x, y in path]

    assert projections[0] == pytest.approx(0.0)
    assert projections[-1] == pytest.approx(length)
    assert all(later >= earlier for earlier, later in pairwise(projections))


def test_mouse_path_step_count_scales_with_distance() -> None:
    human = humanizer()

    assert len(human.mouse_path((0.0, 0.0), (5000.0, 0.0))) == 60
    assert len(human.mouse_path((0.0, 0.0), (10.0, 0.0))) == 8
    assert len(human.mouse_path((0.0, 0.0), (300.0, 400.0), steps=12)) == 12


def test_mouse_path_rejects_a_single_step() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        humanizer().mouse_path((0.0, 0.0), (100.0, 100.0), steps=1)


def test_mouse_path_handles_zero_distance() -> None:
    path = humanizer().mouse_path((42.0, 42.0), (42.0, 42.0))

    assert path[0] == (42.0, 42.0)
    assert path[-1] == (42.0, 42.0)
    assert len(path) == 8


def test_click_offset_stays_inside_the_biased_box() -> None:
    human = humanizer()
    samples = [human.click_offset(200.0, 100.0) for _ in range(200)]

    for x, y in samples:
        assert 30.0 <= x <= 170.0
        assert 15.0 <= y <= 85.0
    assert len(set(samples)) > 1


def test_scroll_plan_is_empty_when_disabled() -> None:
    policy = HumanPolicy(seed=7, scroll_before_post=False)
    assert Humanizer(policy).scroll_plan() == []


def test_scroll_plan_moves_down_then_partly_up() -> None:
    plan = humanizer().scroll_plan()

    assert len(plan) >= 3
    assert plan[0][0] > 0
    assert plan[-1][0] < 0
    assert any(dy < 0 for dy, _ in plan)
    assert sum(dy for dy, _ in plan) > 0
    assert all(0.4 <= pause <= 1.4 for _, pause in plan)
