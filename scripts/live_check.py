#!/usr/bin/env python3
"""Manual live smoke check for a throwaway X test account.

Run this after the one-time login, against the test account's own home::

    .venv/bin/python scripts/live_check.py --home ~/.olsa/x-test --handle test_acct

    # also post ONE real composer thread, read it back and delete it:
    .venv/bin/python scripts/live_check.py --home ~/.olsa/x-test --handle test_acct --post

    # compare headless and visible behaviour on the real site:
    .venv/bin/python scripts/live_check.py ... --display visible

The check runs, in order: the offline session summary, a verified browser
visit, the DOM calibration report (nothing is posted), the browser-tells table
and, only with ``--post``, one real 3-tweet composer thread followed by a
read-back and a delete. The final table is the go/no-go verdict.

Safety properties:

* a handle in ``XPOSTER_LIVE_FORBIDDEN_HANDLES`` (comma-separated, default
  ``olsa``, ``openlifescienceai``, ``olsa_ai``) is refused before any browser
  starts; the built-in list cannot be weakened by the environment;
* the script never prints a cookie, a token or a storage-state value, and every
  failure detail is redacted for credential-shaped text before display;
* posting is opt-in, one short thread at most, and the tweet ids are printed
  before the delete so a failed cleanup can be finished by hand;
* posting is refused while the ledger holds an ``UNKNOWN`` entry or a
  ``PENDING`` entry with a recorded click, because an unresolved outcome must be
  inspected by a human before another thread is posted;
* posting is skipped when calibration found a required selector missing or the
  browser tells failed the honest-browser policy, so a broken or detectable
  browser cannot leave a half-posted thread behind.

Exit codes: ``0`` go, ``1`` at least one step failed, ``2`` refused or invalid
arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import xposter.diagnose as diagnose
import xposter.sessions as sessions
from xposter import (
    Display,
    HumanPolicy,
    KeepalivePolicy,
    PacingPolicy,
    Strategy,
    Thread,
    Tweet,
    XPosterConfig,
)
from xposter.browser import Tell, resolve_display
from xposter.ledger import LedgerStatus
from xposter.models import PostedThread, SessionSummary
from xposter.poster import XPoster
from xposter.selectors import locate
from xposter.splitter import validate_tweet
from xposter.store import ParkInfo, SessionStore

FORBIDDEN_HANDLES = frozenset({"olsa", "openlifescienceai", "olsa_ai"})
PRODUCTION_HOME = Path.home() / ".olsa" / "x"

LIVE_HUMAN = HumanPolicy(
    # Typing stays at the shipped cadence; only the short dwell pauses shrink.
    # The between-tweet pause is the production 20-45 s range on purpose (H4):
    # the replies strategy must never send its chain as a burst.
    typing_delay_ms=(35, 120),
    pre_post_pause_s=(1.0, 2.0),
    between_tweet_pause_s=(20.0, 45.0),
    action_pause_s=(0.15, 0.4),
    think_pause_s=(0.6, 1.4),
    scroll_before_post=False,
)

LIVE_PACING = PacingPolicy(
    min_gap_between_threads_s=60.0,
    max_threads_per_day=20,
    max_tweets_per_day=200,
    max_tweets_per_hour=60,
    quiet_hours=None,
    jitter=0.0,
)

LIVE_KEEPALIVE = KeepalivePolicy(min_interval_s=3600.0, browse_seconds=(2.0, 4.0))

CONDITIONAL_COMPOSER_SELECTORS = frozenset(
    {
        # Only present in thread mode or with media / the alt-text editor open.
        "composer_post_all_button",
        "composer_media_preview",
        "composer_media_remove_button",
        "composer_alt_button",
        "composer_alt_text_input",
        "composer_alt_save_button",
    }
)

_SECRET_RE = re.compile(r"(?i)\b(auth_token|ct0|password|secret|api[_-]?key)\b(\s*[:=]\s*)\S+")


@dataclass(slots=True)
class Step:
    """One row of the go/no-go table."""

    name: str
    status: str
    detail: str


def _redact(text: str) -> str:
    """Last line of defence: no credential-shaped value reaches the terminal."""
    return _SECRET_RE.sub(r"\1\2[redacted]", text)


def _add(steps: list[Step], name: str, status: str, detail: str) -> None:
    steps.append(Step(name, status, _redact(" ".join(detail.split()))))


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _forbidden_handles() -> set[str]:
    """The built-in production handles plus whatever the operator configured."""
    raw = _env("XPOSTER_LIVE_FORBIDDEN_HANDLES")
    configured = {item.strip().lstrip("@").lower() for item in raw.split(",") if item.strip()}
    return configured | set(FORBIDDEN_HANDLES)


def _marker(kind: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"[xposter-live-check {kind} {stamp}]"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="live_check.py",
        description=(
            "Opt-in live smoke check for a throwaway X test account. It never runs "
            "against a production handle and never prints cookie values."
        ),
        epilog=(
            "examples:\n"
            "  .venv/bin/python scripts/live_check.py --home ~/.olsa/x-test"
            " --handle test_acct\n"
            "  .venv/bin/python scripts/live_check.py --home ~/.olsa/x-test"
            " --handle test_acct --post\n"
            "  .venv/bin/python scripts/live_check.py --home ~/.olsa/x-test --handle test_acct "
            "--display visible\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--home",
        required=True,
        help="XPOSTER_HOME of the throwaway test account (for example ~/.olsa/x-test)",
    )
    parser.add_argument(
        "--handle",
        required=True,
        help="expected signed-in handle, with or without a leading @",
    )
    parser.add_argument(
        "--display",
        choices=("auto", "headless", "visible"),
        default="auto",
        help="browser mode; compare headless and visible behaviour on the real site",
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="post ONE real 3-tweet composer thread, read it back and delete it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    handle = args.handle.strip().lstrip("@")
    if not handle:
        parser.error("--handle must name the test account")
    if not args.home.strip():
        parser.error("--home must point at the test account's XPOSTER_HOME")
    forbidden = _forbidden_handles()
    if handle.lower() in forbidden:
        print(
            f"REFUSED: @{handle} is in the live-test forbidden list "
            f"({', '.join(sorted(forbidden))}); use a throwaway test account.",
            file=sys.stderr,
        )
        return 2

    home = Path(args.home).expanduser()
    if home == PRODUCTION_HOME:
        print(
            f"warning: {home} looks like the production home; prefer a separate "
            "throwaway-account home.",
            file=sys.stderr,
        )

    config = XPosterConfig.from_env().with_overrides(
        home=home,
        handle=handle,
        display=Display(args.display),
        human=LIVE_HUMAN,
        pacing=LIVE_PACING,
        keepalive=LIVE_KEEPALIVE,
    )
    try:
        return asyncio.run(_run(config, post=args.post))
    except KeyboardInterrupt:
        print(
            "\ninterrupted; if a test thread was posted, delete the ids printed above by hand",
            file=sys.stderr,
        )
        return 130


async def _run(config: XPosterConfig, *, post: bool) -> int:
    steps: list[Step] = []
    poster = XPoster(config)
    store = poster.store

    print(f"live check for @{config.handle}")
    print(f"  home:    {config.home}")
    print(f"  display: {config.display.value}")

    summary = poster.session_status()
    parked = store.parked()
    _print_session(config, summary, parked)
    session_ok, session_detail = _session_verdict(config, summary, parked)
    _add(steps, "session", "ok" if session_ok else "fail", session_detail)
    if not session_ok:
        for name in ("verify", "dom", "tells", "post", "cleanup"):
            _add(steps, name, "skip", "skipped: the saved session is not usable")
        return _finish(steps)

    print("\nverify: opening the saved Chrome profile; nothing is posted")
    try:
        info = await poster.verify()
    except Exception as exc:
        _add(steps, "verify", "fail", f"{type(exc).__name__}: {exc}")
        for name in ("dom", "tells", "post", "cleanup"):
            _add(steps, name, "skip", "skipped: session verification failed")
        return _finish(steps)
    print(f"  signed in as @{info.handle} ({info.display} mode)")
    _add(steps, "verify", "ok", f"@{info.handle} verified in {info.display} mode")

    print("\ndom: calibrating /compose/post; nothing is posted")
    dom_ok = False
    try:
        report = await diagnose.dom_report(config, store)
    except Exception as exc:
        _add(steps, "dom", "fail", f"{type(exc).__name__}: {exc}")
    else:
        print("\n".join(diagnose.format_dom_report(report)))
        missing = [
            row.name
            for row in report.rows
            if row.missing
            and diagnose.is_composer_selector(row.name)
            and row.name not in CONDITIONAL_COMPOSER_SELECTORS
        ]
        conditional = [
            row.name
            for row in report.rows
            if row.missing and row.name in CONDITIONAL_COMPOSER_SELECTORS
        ]
        dom_ok = not missing
        detail = f"{len(report.rows)} selectors; artifacts: {report.artifacts}"
        if conditional:
            detail += f"; conditional not shown: {', '.join(conditional)}"
        if missing:
            detail += f"; MISSING: {', '.join(missing)}"
        _add(steps, "dom", "ok" if dom_ok else "fail", detail)

    print("\ntells: measuring what the live browser reports (no login needed)")
    tells_ok = False
    try:
        tells = await diagnose.tells_report(config, store)
    except Exception as exc:
        _add(steps, "tells", "fail", f"{type(exc).__name__}: {exc}")
    else:
        print("\n".join(diagnose.format_tells(tells)))
        try:
            resolved = resolve_display(config)
        except Exception as exc:
            failures = [f"could not resolve the display: {type(exc).__name__}: {exc}"]
        else:
            failures = _tells_policy_failures(tells, resolved)
        warnings = [tell.name for tell in tells if tell.risk == "warn"]
        notes = [tell.name for tell in tells if tell.risk == "note"]
        tells_ok = not failures
        if failures:
            _add(steps, "tells", "fail", "policy: " + "; ".join(failures))
        else:
            detail = f"{len(tells)} tells; {len(warnings)} warning(s), {len(notes)} note(s)"
            if warnings:
                detail += f"; warnings: {', '.join(warnings)}"
            _add(steps, "tells", "warn" if warnings else "ok", detail)

    if not post:
        _add(steps, "post", "skip", "not requested; pass --post for one real composer thread")
        _add(steps, "cleanup", "skip", "nothing was posted")
    elif not dom_ok:
        _add(steps, "post", "skip", "skipped: DOM calibration found missing required selectors")
        _add(steps, "cleanup", "skip", "nothing was posted")
    elif not tells_ok:
        _add(steps, "post", "skip", "skipped: the browser tells failed the honest-browser policy")
        _add(steps, "cleanup", "skip", "nothing was posted")
    else:
        await _run_post(config, steps)

    return _finish(steps)


def _session_verdict(
    config: XPosterConfig, summary: SessionSummary, parked: ParkInfo | None
) -> tuple[bool, str]:
    if not summary.present:
        return False, f"no saved session in {config.home}; run `xposter login` there first"
    if not summary.has_auth_token:
        return False, "the saved state has no auth_token cookie; run `xposter login` again"
    if summary.expired:
        return False, "the saved session looks expired; run `xposter login` again"
    if parked is not None:
        return False, f"parked since {parked.since}: {parked.reason}; run `xposter unpark`"
    expected = (config.handle or "").lower()
    if summary.handle is not None and summary.handle.lower() != expected:
        return False, f"the saved session records @{summary.handle}, not @{config.handle}"
    expiry = (
        f", auth expires in {summary.days_left} day(s)" if summary.days_left is not None else ""
    )
    return True, f"@{summary.handle or config.handle} present{expiry}, not parked"


def _print_session(config: XPosterConfig, summary: SessionSummary, parked: ParkInfo | None) -> None:
    """Print the cookie-free summary fields; no cookie value can appear here."""
    print("\nsession (offline):")
    print(f"  home:            {config.home}")
    print(f"  present:         {summary.present}")
    print(f"  auth_token:      {summary.has_auth_token}")
    print(f"  ct0:             {summary.has_ct0}")
    print(f"  auth_expires_at: {summary.auth_expires_at} (days_left={summary.days_left})")
    print(f"  expired:         {summary.expired}")
    print(f"  recorded handle: {summary.handle}")
    print(f"  last verified:   {summary.last_verified_at}")
    if parked is None:
        print("  parked:          no")
    else:
        print(f"  parked:          YES since {parked.since}: {parked.reason}")


def _tell(tells: list[Tell], name: str) -> Tell | None:
    for tell in tells:
        if tell.name == name:
            return tell
    return None


def _tells_policy_failures(tells: list[Tell], resolved: Display) -> list[str]:
    """Return the honest-browser policy failures the pre-flight cares about.

    Headless Chrome is expected to advertise its HeadlessChrome token, so that
    one check warns instead of failing in headless mode; on visible or Xvfb it
    must be absent. ``navigator.webdriver``, non-empty languages and a
    desktop-sized screen are required in every mode.
    """
    failures: list[str] = []

    webdriver = _tell(tells, "navigator.webdriver")
    if webdriver is None:
        failures.append("the probe did not report navigator.webdriver")
    elif webdriver.value != "false":
        failures.append(f"navigator.webdriver={webdriver.value!r} (must be false)")

    user_agent = _tell(tells, "userAgent")
    if user_agent is None:
        failures.append("the probe did not report userAgent")
    elif "HeadlessChrome" in user_agent.value:
        if resolved in (Display.VISIBLE, Display.XVFB):
            failures.append(f"HeadlessChrome in the UA on the {resolved.value} display")
        else:
            print(
                "  WARNING: this headless run advertises HeadlessChrome and X can see "
                "it; re-run with --display visible for the supervised post.",
                file=sys.stderr,
            )

    languages = _tell(tells, "navigator.languages")
    if languages is None:
        failures.append("the probe did not report navigator.languages")
    elif languages.value in ("", "(empty)", "unavailable"):
        failures.append(f"navigator.languages={languages.value!r} (must be non-empty)")

    screen = _tell(tells, "screen")
    if screen is None:
        failures.append("the probe did not report screen")
    else:
        match = re.search(r"(\d+)x(\d+)", screen.value)
        if match is None:
            failures.append(f"the screen tell is not parseable: {screen.value!r}")
        else:
            width, height = int(match.group(1)), int(match.group(2))
            if width < 1024 or height < 768:
                failures.append(f"screen {width}x{height} is below the 1024x768 minimum")

    return failures


def _unresolved_ledger(poster: XPoster) -> list[str]:
    """Entries that mean a previous click may already be live on X."""
    blockers: list[str] = []
    for entry in poster.ledger.all_entries():
        clicked = bool(getattr(entry, "clicking_at", ""))
        if entry.status is LedgerStatus.UNKNOWN:
            blockers.append(f"{entry.thread_hash[:12]} is UNKNOWN")
        elif entry.status is LedgerStatus.PENDING and clicked:
            blockers.append(f"{entry.thread_hash[:12]} is PENDING with a recorded click")
    return blockers


async def _run_post(config: XPosterConfig, steps: list[Step]) -> None:
    """Post one composer thread, read it back, then always try to delete it."""
    poster = XPoster(config.with_overrides(strategy=Strategy.COMPOSER))
    blockers = _unresolved_ledger(poster)
    if blockers:
        _add(
            steps,
            "post",
            "fail",
            "REFUSED: the ledger has an unresolved outcome ("
            + "; ".join(blockers)
            + "); inspect the account, delete any partial thread by hand, then clear "
            "that ledger row with the ledger tooling",
        )
        _add(steps, "cleanup", "skip", "nothing was posted")
        return

    marker = _marker("thread")
    texts = (
        f"{marker} live check part 1/3.\n\nA blank line separates this paragraph.",
        f"{marker} live check part 2/3 replies to the first.",
        f"{marker} live check part 3/3 ends the check thread.",
    )
    thread = Thread(tweets=tuple(Tweet(text) for text in texts), key=marker)
    for text in texts:
        validate_tweet(text, limit=config.max_tweet_weight)

    target: PostedThread | None = None
    pending: BaseException | None = None
    status = "ok"
    detail = ""
    print(f"\npost: posting one real 3-tweet composer thread marked {marker}")
    try:
        target = await poster.post_thread(thread)
        ids = [tweet.id for tweet in target.tweets]
        print(f"  posted ids (delete by hand if cleanup fails): {ids}", flush=True)
        _check_posted(target, texts)
        await _assert_rendering(config, target, texts)
        detail = f"posted and read back {len(target.tweets)} tweets"
    except BaseException as exc:
        status = "fail"
        detail = f"{type(exc).__name__}: {exc}"
        if not isinstance(exc, Exception):
            pending = exc
        if target is None:
            target = _partial_thread(poster, thread)
            if target is not None:
                print(
                    "  partial ids (delete by hand if cleanup fails): "
                    f"{[tweet.id for tweet in target.tweets]}",
                    flush=True,
                )
    finally:
        cleanup_status = "skip"
        cleanup_detail = "nothing was posted"
        if target is not None:
            if await _delete_or_report(poster, target):
                if await _profile_marker_absent(config, marker):
                    cleanup_status = "ok"
                    cleanup_detail = (
                        f"deleted {len(target.tweets)} tweets; the marker is gone from the profile"
                    )
                else:
                    cleanup_status = "fail"
                    cleanup_detail = (
                        "delete reported success but the marker is still on the profile"
                    )
            else:
                cleanup_status = "fail"
                cleanup_detail = "delete failed; delete the ids printed above by hand"
        _add(steps, "post", status, detail)
        _add(steps, "cleanup", cleanup_status, cleanup_detail)
    if pending is not None:
        raise pending


def _check_posted(posted: PostedThread, texts: tuple[str, ...]) -> None:
    assert posted.complete is True, "the posted thread is marked incomplete"
    assert len(posted.tweets) == len(texts), (
        f"expected {len(texts)} tweet ids, got {len(posted.tweets)}"
    )
    assert [tweet.index for tweet in posted.tweets] == list(range(len(texts)))
    assert len({tweet.id for tweet in posted.tweets}) == len(texts)
    assert all(tweet.id.isdigit() for tweet in posted.tweets)


async def _assert_rendering(
    config: XPosterConfig, posted: PostedThread, texts: tuple[str, ...]
) -> None:
    """Load every status URL and prove the text and the reply chain survived."""
    store = SessionStore(config)
    async with sessions.authenticated_page(config, store, goto="/home") as authed:
        page = authed.page
        for index, tweet in enumerate(posted.tweets):
            print(f"  reading back tweet {index + 1}/{len(texts)} at {tweet.url}")
            await page.goto(tweet.url, wait_until="domcontentloaded")
            assert tweet.id in page.url, (
                f"tweet {index + 1} redirected to {page.url}; it may already be missing"
            )
            await locate(page, "tweet_article", timeout_ms=config.default_timeout_ms)
            needle = f"part {index + 1}/{len(texts)}"
            rendered = await _article_text(page, needle)
            assert rendered, (
                f"tweet {index + 1} ({tweet.id}) did not render; no article contained {needle!r}"
            )
            assert _normalised(texts[index]) in _normalised(rendered), (
                f"tweet {index + 1} text differs on the live page.\n"
                f"expected: {texts[index]!r}\nrendered article: {rendered!r}"
            )
            if index > 0:
                previous = posted.tweets[index - 1]
                links = await page.locator(f'a[href*="/status/{previous.id}"]').count()
                assert links > 0, (
                    f"tweet {index + 1}'s page has no link to the previous tweet "
                    f"{previous.id}; the reply chain did not render"
                )


async def _article_text(page: Any, needle: str) -> str:
    articles = page.locator("article")
    for index in range(await articles.count()):
        text = await articles.nth(index).inner_text()
        if needle in text:
            return text
    return ""


def _normalised(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


async def _profile_text(config: XPosterConfig) -> str:
    store = SessionStore(config)
    async with sessions.authenticated_page(config, store, goto=f"/{config.handle}") as authed:
        await locate(authed.page, "primary_column", timeout_ms=config.default_timeout_ms)
        return await authed.page.inner_text("body")


async def _profile_marker_absent(config: XPosterConfig, marker: str) -> bool:
    try:
        text = await _profile_text(config)
    except Exception as exc:
        print(
            f"  could not verify the profile after cleanup: "
            f"{type(exc).__name__}: {_redact(str(exc))}",
            file=sys.stderr,
        )
        return False
    return marker not in text


def _partial_thread(poster: XPoster, thread: Thread) -> PostedThread | None:
    """Recover ids recorded before a post failed, so they can be deleted too."""
    entry = poster.ledger.get(thread.content_hash)
    if entry is None or not entry.posted:
        return None
    return PostedThread(
        thread_hash=thread.content_hash,
        handle=entry.handle,
        strategy=entry.strategy,
        posted_at=entry.updated_at,
        tweets=tuple(sorted(entry.posted, key=lambda item: item.index)),
        complete=False,
    )


async def _delete_or_report(poster: XPoster, posted: PostedThread) -> bool:
    ids = [tweet.id for tweet in posted.tweets]
    print(f"  deleting {len(ids)} test tweet(s) through the web UI: {ids}", flush=True)
    if not ids:
        return True
    try:
        deleted = await poster.delete_thread(posted)
    except Exception as exc:
        print(
            f"  CLEANUP FAILED ({type(exc).__name__}: {_redact(str(exc))}); delete by hand: {ids}",
            file=sys.stderr,
        )
        return False
    print(f"  deleted: {deleted}")
    return len(deleted) == len(ids)


def _finish(steps: list[Step]) -> int:
    """Print the go/no-go table and map failures to the process exit code."""
    width = max([len("step"), *(len(step.name) for step in steps)])
    print()
    print(f"{'step':<{width}}  {'status':<6}  detail")
    print(f"{'-' * width}  {'-' * 6}  {'-' * 50}")
    for step in steps:
        detail = step.detail
        if len(detail) > 110:
            detail = f"{detail[:107]}..."
        print(f"{step.name:<{width}}  {step.status:<6}  {detail}")
    warnings = sum(1 for step in steps if step.status == "warn")
    failed = [step.name for step in steps if step.status == "fail"]
    print()
    if failed:
        print(f"NO-GO: failed step(s): {', '.join(failed)}")
        return 1
    suffix = f" ({warnings} warning(s))" if warnings else ""
    print(f"GO{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
