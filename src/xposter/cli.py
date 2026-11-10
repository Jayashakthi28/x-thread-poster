"""Typer command line for xposter.

The CLI is deliberately thin: it parses input, renders a preview, asks for
confirmation and translates one :class:`XPosterError` into one documented exit
code. Every operation lives in :class:`xposter.poster.XPoster` or
:mod:`xposter.diagnose`, so the rules can be tested without a terminal and the
terminal can be changed without touching the rules.

Secrets never pass through this module: the session is described by
:class:`SessionSummary`, which contains booleans and timestamps but no cookie
values. Error text is redacted once more before printing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sys
import traceback
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar

import typer

from . import diagnose
from .browser import resolve_display
from .config import Display, Strategy, XPosterConfig
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
    RateLimitedError,
    SessionBusyError,
    SessionError,
    UiChangedError,
    ValidationError,
    XPosterError,
)
from .ledger import Ledger, LedgerEntry, LedgerStatus
from .media import MAX_GIF_BYTES, MAX_IMAGE_BYTES, validate_media
from .mentions import extract_mentions
from .models import PostedThread, PostedTweet, SessionSummary, Thread, Tweet
from .poster import XPoster
from .splitter import split_thread, split_with_first, weighted_length
from .store import ParkInfo, SessionStore

POLICY_REMINDER = (
    "X's automation rules prohibit scripting the website; this account is at risk "
    "\u2014 use a test account first"
)

_T = TypeVar("_T")

_STATUS_ID_RE = re.compile(r"/status(?:es)?/(?P<id>\d+)")
_MEDIA_SPEC_RE = re.compile(r"^(?P<tweet>[1-9]\d*):(?P<path>.+)$")
_ALT_SPEC_RE = re.compile(r"^(?P<tweet>[1-9]\d*)\.(?P<image>[1-9]\d*):(?P<text>.*)$")
_SUPPORTED_MEDIA_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_ALT_TEXT_REFUSAL = (
    "alt text is not verified against the live site yet; pass "
    "allow_alt_text=True / --allow-alt to try it on a test account"
)

_SECRET_RE = re.compile(
    r"(?i)\b(?P<key>auth_token|ct0|csrf(?:[-_]?token)?|xsrf(?:[-_]?token)?|"
    r"password|secret|api[_-]?key|sessionid)\b\s*[:=]\s*\S+"
    r"|\b(?P<scheme>bearer)\b\s+\S+"
)

app = typer.Typer(
    name="xposter",
    help=(
        "Post threads to X through a real Chrome browser with a saved one-time login. "
        "X's automation rules prohibit scripting the website; use a test account first."
    ),
    no_args_is_help=True,
    add_completion=False,
)


@dataclass(frozen=True, slots=True)
class _State:
    """Resolved config plus flags needed only for error rendering and warnings."""

    config: XPosterConfig
    verbose: bool
    explicit_display: Display | None = None


@app.callback()
def _main(
    ctx: typer.Context,
    home: Annotated[
        Path | None,
        typer.Option("--home", metavar="PATH", help="State directory (default: $XPOSTER_HOME)."),
    ] = None,
    handle: Annotated[
        str | None,
        typer.Option("--handle", metavar="NAME", help="Expected X handle, without the @."),
    ] = None,
    display: Annotated[
        Display | None,
        typer.Option(
            "--display",
            metavar="MODE",
            case_sensitive=False,
            help="How Chrome is exposed: auto, headless, xvfb or visible.",
        ),
    ] = None,
    strategy: Annotated[
        Strategy | None,
        typer.Option(
            "--strategy",
            metavar="MODE",
            case_sensitive=False,
            help="How the thread is assembled: composer or replies.",
        ),
    ] = None,
    premium: Annotated[
        bool,
        typer.Option("--premium", help="Allow long posts (25,000 weighted characters)."),
    ] = False,
    max_mentions: Annotated[
        int | None,
        typer.Option(
            "--max-mentions",
            metavar="N",
            help=("Override the distinct-@mention cap for this run; meant for test accounts."),
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("-v", "--verbose", help="Show a traceback for unexpected failures."),
    ] = False,
) -> None:
    """Global options shared by every command."""
    try:
        config = XPosterConfig.from_env()
        overrides: dict[str, Any] = {}
        if home is not None:
            overrides["home"] = home.expanduser()
        if handle is not None:
            overrides["handle"] = handle.strip().lstrip("@") or None
        if display is not None:
            overrides["display"] = display
        if strategy is not None:
            overrides["strategy"] = strategy
        if premium:
            overrides["max_tweet_weight"] = 25000
        if max_mentions is not None:
            overrides["max_mentions_per_thread"] = max_mentions
        config = config.with_overrides(**overrides)
    except XPosterError as exc:
        _fail(exc)
    ctx.obj = _State(config=config, verbose=verbose, explicit_display=display)


@app.command()
def login(ctx: typer.Context) -> None:
    """Sign in once in a normal Chrome window; later runs reuse the profile."""
    state = _state(ctx)
    typer.echo(
        "One-time login: a normal Chrome window opens; sign in to the account, then QUIT "
        "Chrome with Cmd+Q / close all windows so the login is flushed to disk."
    )
    typer.echo("Do not just close the tab: quitting Chrome is what saves the session.")

    def on_status(message: str) -> None:
        typer.echo(f"  {message}")

    poster = _poster(state.config)
    info = _call(poster.login(on_status=on_status), state)

    typer.echo(f"Logged in as @{info.handle}.")
    typer.echo(f"Chrome: {info.chrome_version or 'unknown'}")
    typer.echo(f"Auth cookie expires: {info.auth_expires_at or 'unknown'}")
    typer.echo(f"Session verified at: {info.verified_at}")
    typer.echo(f"Display: {info.display}")
    typer.echo("Next: run `xposter doctor --dom` to calibrate the selectors against X's live DOM.")
    typer.echo(POLICY_REMINDER)


@app.command()
def logout(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    no_backup: Annotated[
        bool,
        typer.Option("--no-backup", help="Delete the storage_state.json backup too."),
    ] = False,
) -> None:
    """Delete the saved Chrome profile; the next step is `xposter login`."""
    state = _state(ctx)
    typer.echo(
        "Logout deletes the saved Chrome profile (the saved X login). "
        "The next step is `xposter login` to sign in again."
    )
    if no_backup:
        typer.echo("The storage_state.json backup is deleted too.")
    else:
        typer.echo(
            "The storage_state.json backup is kept, so a fresh profile can restore the login."
        )
    if not yes and not typer.confirm("Delete the saved Chrome profile?", default=False):
        typer.echo("Cancelled; nothing was deleted.")
        return

    poster = _poster(state.config)
    _guard(lambda: poster.logout(keep_backup=not no_backup), state)
    typer.echo("Saved session deleted. Run `xposter login` to sign in again.")


@app.command()
def status(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print a single JSON object instead of text.")
    ] = False,
) -> None:
    """Show the saved session, parking state, next allowed post and ledger counts."""
    state = _state(ctx)
    poster = _poster(state.config)

    try:
        summary = poster.session_status()
        parked = poster.parked()
        ledger = poster.ledger
        pacer = poster.pacer
        next_allowed = pacer.next_allowed(1)
        entries = ledger.all_entries()
    except XPosterError as exc:
        _fail(exc, state=state)

    counts = _ledger_counts(entries)
    now = datetime.now(UTC)
    wait_s = max(0, math.ceil((next_allowed - now).total_seconds()))

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "session": summary.to_json(),
                    "parked": _park_json(parked),
                    "pacing": {
                        "next_allowed_at": next_allowed.isoformat(),
                        "next_allowed_local": _local_stamp(next_allowed),
                        "wait_s": wait_s,
                    },
                    "ledger": counts,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    _echo_session(summary)
    if parked is None:
        typer.echo("Parked: no")
    else:
        since = f" since {parked.since}" if parked.since else ""
        typer.echo(f"Parked: yes{since}: {parked.reason}")
    suffix = f" (in {wait_s} s)" if wait_s else " (now)"
    typer.echo(f"Next post allowed: {_local_stamp(next_allowed)} local time{suffix}")
    typer.echo(
        f"Ledger: {counts['entries']} entries (posted {counts['posted']}, "
        f"pending {counts['pending']}, unknown {counts['unknown']}, "
        f"failed {counts['failed']})."
    )


@app.command()
def verify(ctx: typer.Context) -> None:
    """Open the saved profile and prove the login still works."""
    state = _state(ctx)
    info = _call(_poster(state.config).verify(), state)
    typer.echo(f"Session valid as @{info.handle}.")
    typer.echo(f"Verified at {info.verified_at}.")
    typer.echo(f"Chrome: {info.chrome_version or 'unknown'}")
    typer.echo(f"Auth cookie expires: {info.auth_expires_at or 'unknown'}")


@app.command()
def refresh(
    ctx: typer.Context,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Refresh even when the keepalive interval has not elapsed; timers must "
                "call plain `refresh` so the cadence is respected."
            ),
        ),
    ] = False,
) -> None:
    """Reload the profile so X sees a live session before the cookie expires."""
    state = _state(ctx)
    result = _call(_poster(state.config).refresh(force=force), state)
    handle = f"@{result.handle}" if result.handle else "the saved session"
    if result.refreshed:
        typer.echo(f"Session refreshed for {handle}.")
    else:
        typer.echo(f"Session refresh not needed for {handle}: {result.reason or 'not due yet'}.")
    if result.next_due:
        typer.echo(f"Next refresh due {result.next_due}.")


@app.command()
def unpark(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Clear the parked marker after a human has checked the account."""
    state = _state(ctx)
    poster = _poster(state.config)
    parked = _guard(poster.parked, state)
    if parked is None:
        typer.echo("Not parked; nothing to clear.")
        return

    since = f" since {parked.since}" if parked.since else ""
    typer.echo(f"Parked{since}: {parked.reason}")
    typer.echo(
        "Before clearing this, open x.com in a normal browser, sign in as the expected "
        "account and confirm the challenge, lock or rate limit is resolved."
    )
    if not yes and not typer.confirm("Clear the parked state?", default=False):
        typer.echo("Left parked; nothing changed.")
        return

    cleared = _guard(poster.unpark, state)
    if cleared:
        typer.echo("Parked state cleared. You can post again.")
    else:
        typer.echo("No parked marker was present; nothing changed.")


@app.command()
def post(
    ctx: typer.Context,
    text: Annotated[
        str | None, typer.Option("--text", help="Thread text; it is split into tweets.")
    ] = None,
    file: Annotated[
        Path | None,
        typer.Option("--file", metavar="PATH", help="UTF-8 text file; '-' reads stdin."),
    ] = None,
    thread_json: Annotated[
        Path | None,
        typer.Option(
            "--thread-json",
            metavar="PATH",
            help=('JSON list of strings or {"text": ..., "media": [...], "alt": [...]} objects.'),
        ),
    ] = None,
    first: Annotated[
        str | None,
        typer.Option("--first", help="Verbatim first post; the rest of the input is split."),
    ] = None,
    first_file: Annotated[
        Path | None,
        typer.Option("--first-file", metavar="PATH", help="File holding the verbatim first post."),
    ] = None,
    media: Annotated[
        list[str] | None,
        typer.Option(
            "--media",
            metavar="PATH|N:PATH",
            help=(
                "Image for a tweet; repeatable. Plain PATH attaches to tweet 1, "
                "N:PATH to 1-based tweet N."
            ),
        ),
    ] = None,
    alt: Annotated[
        list[str] | None,
        typer.Option(
            "--alt",
            metavar="TEXT|N.M:TEXT",
            help=(
                "Alt text; repeatable. Plain TEXT applies in order to tweet 1's "
                "images, N.M:TEXT to tweet N, image M."
            ),
        ),
    ] = None,
    allow_alt: Annotated[
        bool,
        typer.Option(
            "--allow-alt",
            help=(
                'Enable alt-text entry. The live "Edit image description" screen has '
                "not been fully observed yet: try it on a test account first."
            ),
        ),
    ] = False,
    key: Annotated[
        str | None,
        typer.Option(
            "--key",
            metavar="TEXT",
            help=(
                "Repeat key (for example week-2026-W45); identical text with a different "
                "key is a different thread, so a deliberate repeat needs no --force."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Validate, preview, and really attach media in the composer, then discard.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Recovery only, not a repeat tool. Use it only after checking the account "
                "in a normal browser that nothing was posted; on a partially posted "
                "thread it CONTINUES after the recorded tweets, it never re-posts them."
            ),
        ),
    ] = False,
    discard_recorded: Annotated[
        bool,
        typer.Option(
            "--discard-recorded",
            help="I deleted the partially posted thread by hand; start over.",
        ),
    ] = False,
    no_numbering: Annotated[
        bool, typer.Option("--no-numbering", help="Do not add 1/n markers to split tweets.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the PostedThread JSON on stdout.")
    ] = False,
) -> None:
    """Preview, confirm and post one thread."""
    state = _state(ctx)
    pinned = first is not None or first_file is not None
    if thread_json is not None and pinned:
        _usage("--first/--first-file cannot be combined with --thread-json.")
    provided = sum([text is not None, file is not None, thread_json is not None])
    if provided != 1:
        _usage("Provide exactly one of --text, --file or --thread-json.")
    if pinned and (first is None) == (first_file is None):
        _usage("Provide exactly one of --first or --first-file.")

    limit = state.config.max_tweet_weight
    numbering = not no_numbering
    try:
        media_groups = _parse_media_specs(media or ())
        alt_groups = _parse_alt_specs(alt or ())
        if thread_json is not None:
            tweets = list(_tweets_from_json(thread_json))
        else:
            source = _source_text(text=text, file=file)
            if pinned:
                first_text = (
                    first.strip()
                    if first is not None
                    else _read_utf8(_require(first_file, "--first-file")).strip()
                )
                if not first_text:
                    raise ValidationError("The first post is empty.")
                if not source.strip():
                    parts = [first_text]
                elif numbering:
                    parts = split_with_first(first_text, source, limit=limit)
                else:
                    parts = [first_text, *split_thread(source, limit=limit, numbering=False)]
            else:
                parts = split_thread(source, limit=limit, numbering=numbering)
            tweets = [Tweet(text=part) for part in parts]
        tweets = _merge_media_and_alt(tweets, media_groups, alt_groups)
        thread = Thread(tweets=tuple(tweets), key=(key or "").strip())
    except XPosterError as exc:
        _fail(exc, state=state)

    mention_summary = _mention_summary(thread.tweets, state.config)

    def _preview(existing: LedgerEntry | None = None) -> None:
        _echo_preview(
            thread,
            limit,
            numbered=numbering,
            err=json_output,
            existing=existing,
            allow_alt=allow_alt,
            mentions=mention_summary,
        )

    if not allow_alt and _thread_has_alt_text(thread):
        # Refuse before the ledger or any browser work. The preview still shows
        # the alt markers and says why the run is being stopped.
        _preview()
        _fail(ValidationError(_ALT_TEXT_REFUSAL), state=state)

    if mention_summary.exceeds:
        # The preview prints the loud Automation Rules line; stop before
        # XPoster is constructed so no ledger or browser work can happen.
        _preview()
        _fail(
            ValidationError(_mentions_refusal(mention_summary.count, mention_summary.cap)),
            state=state,
        )

    poster = _poster(state.config, allow_alt_text=allow_alt)
    try:
        existing = poster.ledger.get(thread.content_hash)
    except XPosterError as exc:
        _fail(exc, state=state)

    _preview(existing)
    try:
        _check_media_limits(thread.tweets, state.config)
    except XPosterError as exc:
        _fail(exc, state=state)
    _warn_if_headless(state, yes=yes)

    if discard_recorded and not yes:
        recorded = existing.posted if existing is not None else ()
        ids = ", ".join(tweet.id for tweet in recorded) or "none"
        if not typer.confirm(
            f"--discard-recorded discards {len(recorded)} recorded tweet id(s) "
            f"({ids}) for this thread and starts over. Continue?",
            default=False,
            err=json_output,
        ):
            typer.echo("Cancelled; nothing was posted.", err=json_output)
            return

    if dry_run:
        result = _call(
            poster.post_thread(
                thread,
                force=force,
                dry_run=True,
                discard_recorded=discard_recorded,
            ),
            state,
        )
        if json_output:
            typer.echo("Dry run: nothing was posted.", err=True)
            typer.echo(json.dumps(result.to_json(), indent=2, sort_keys=True))
        else:
            typer.echo("Dry run: nothing was posted.")
        return

    if not yes:
        account = state.config.handle or "the configured account"
        if not typer.confirm(
            f"Post {len(thread.tweets)} tweet(s) as {account}?", default=False, err=json_output
        ):
            typer.echo("Cancelled; nothing was posted.", err=json_output)
            return

    posted = _call(
        poster.post_thread(thread, force=force, discard_recorded=discard_recorded), state
    )
    if json_output:
        typer.echo(json.dumps(posted.to_json(), indent=2, sort_keys=True))
        return

    typer.echo(
        f"Posted {len(posted.tweets)} tweet(s) as "
        f"@{posted.handle or state.config.handle or 'unknown'}."
    )
    for tweet in posted.tweets:
        typer.echo(tweet.url)
    if not posted.complete:
        typer.echo(
            "Warning: the thread is incomplete; check the profile before retrying.",
            err=True,
        )
    typer.echo(posted.first_url)


@app.command()
def delete(
    ctx: typer.Context,
    targets: Annotated[
        list[str],
        typer.Argument(help="Status URLs or numeric ids to delete; only for cleaning test posts."),
    ],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Delete test posts by URL or id (never use this to curate real content)."""
    state = _state(ctx)
    try:
        tweets = tuple(
            _posted_tweet(value, index, state.config) for index, value in enumerate(targets)
        )
        posted = PostedThread(
            thread_hash=hashlib.sha256(
                "\x1e".join(tweet.id for tweet in tweets).encode("ascii")
            ).hexdigest(),
            handle=state.config.handle or "",
            strategy=state.config.strategy.value,
            posted_at=datetime.now(UTC).isoformat(),
            tweets=tweets,
            complete=True,
        )
    except XPosterError as exc:
        _fail(exc, state=state)

    typer.echo("Delete is for cleaning up test posts; it cannot be undone.")
    for tweet in posted.tweets:
        typer.echo(f"  {tweet.id}  {tweet.url}")
    if not yes and not typer.confirm(f"Delete {len(posted.tweets)} post(s)?", default=False):
        typer.echo("Cancelled; nothing was deleted.")
        return

    deleted = _call(_poster(state.config).delete_thread(posted), state)
    if deleted:
        for status_id in deleted:
            typer.echo(f"Deleted {status_id}.")
    else:
        typer.echo("No posts were deleted.")


@app.command()
def ledger(
    ctx: typer.Context,
    limit: Annotated[
        int, typer.Option("--limit", min=1, help="How many recent entries to show.")
    ] = 20,
    json_output: Annotated[bool, typer.Option("--json", help="Print the entries as JSON.")] = False,
    reset_corrupt: Annotated[
        bool,
        typer.Option(
            "--reset-corrupt",
            help="Quarantine a corrupt ledger file and continue with an empty ledger.",
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Show recent ledger entries without touching the browser."""
    state = _state(ctx)
    if reset_corrupt:
        typer.echo(
            "Resetting moves the corrupt ledger aside. Its duplicate-post evidence will "
            "no longer be consulted, so check the account in a normal browser first.",
            err=json_output,
        )
        if not yes and not typer.confirm(
            "Quarantine the corrupt ledger and continue with an empty ledger?",
            default=False,
            err=json_output,
        ):
            typer.echo("Cancelled; the ledger was not changed.", err=json_output)
            return
        poster = _poster(state.config)
        try:
            moved = poster.reset_ledger()
        except FileNotFoundError:
            typer.echo("No corrupt ledger file was found; nothing to reset.")
            return
        except XPosterError as exc:
            _fail(exc, state=state)
        if json_output:
            typer.echo(json.dumps({"quarantined": str(moved)}, indent=2, sort_keys=True))
        else:
            typer.echo(f"Quarantined ledger: {moved}")
        return

    try:
        entries = Ledger(state.config.ledger_path).all_entries()
    except XPosterError as exc:
        _fail(exc, state=state)
    recent = list(reversed(entries[-limit:]))

    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "thread_hash": entry.thread_hash,
                        "status": entry.status.value,
                        "n_tweets": entry.n_tweets,
                        "first_url": _first_url(entry),
                        "started_at": entry.started_at,
                        "updated_at": entry.updated_at,
                        "handle": entry.handle,
                        "strategy": entry.strategy,
                        "error": _redact(entry.error, config=state.config),
                    }
                    for entry in recent
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return

    if not recent:
        typer.echo("Ledger is empty.")
        return
    typer.echo(f"Recent ledger entries ({len(recent)}):")
    for entry in recent:
        typer.echo(
            f"{entry.started_at[:19]:<19}  {entry.status.value:<7}  "
            f"{entry.thread_hash[:8]}  {entry.n_tweets:>2} tweet(s)  {_first_url(entry) or '-'}"
        )


@app.command()
def doctor(
    ctx: typer.Context,
    dom: Annotated[
        bool,
        typer.Option("--dom", help="Open the live composer and calibrate every selector."),
    ] = False,
    tells: Annotated[
        bool,
        typer.Option("--tells", help="Open the saved profile and report browser tells."),
    ] = False,
    drafts: Annotated[
        bool,
        typer.Option(
            "--drafts",
            help="List saved drafts read-only and flag calibration leftovers.",
        ),
    ] = False,
    with_media: Annotated[
        bool,
        typer.Option(
            "--with-media",
            help="Also run phase D: attach a generated PNG, inspect media controls, remove it.",
        ),
    ] = False,
    dom_reply: Annotated[
        str | None,
        typer.Option(
            "--dom-reply",
            metavar="STATUS_URL",
            help=(
                "Calibrate the reply composer on your own status URL or numeric id. "
                "Read-only: nothing is posted. Requires --handle."
            ),
        ),
    ] = None,
) -> None:
    """Diagnose the host, the session and (on request) X's live DOM/drafts/media."""
    state = _state(ctx)
    checks = diagnose.offline_report(state.config)
    typer.echo("Offline checks:")
    for line in diagnose.format_checks(checks):
        typer.echo(line)
    typer.echo(f"Mentions: max_mentions_per_thread={state.config.max_mentions_per_thread}")

    store = SessionStore(state.config)
    if tells:
        typer.echo("")
        typer.echo("Browser tells (live):")
        browser_tells = _call(diagnose.tells_report(state.config, store), state)
        for line in diagnose.format_tells(browser_tells):
            typer.echo(line)

    if dom:
        typer.echo("")
        typer.echo("DOM calibration (live; nothing is posted):")
        report = _call(diagnose.dom_report(state.config, store, with_media=with_media), state)
        for line in diagnose.format_dom_report(report):
            typer.echo(line)

    if dom_reply is not None:
        typer.echo("")
        typer.echo("Reply calibration (live; read-only; nothing is posted):")
        reply = _call(diagnose.reply_report(state.config, store, dom_reply), state)
        for line in diagnose.format_reply_report(reply):
            typer.echo(line)

    if drafts:
        typer.echo("")
        typer.echo("Drafts (live; read-only):")
        drafts_report = _call(diagnose.drafts_report(state.config, store), state)
        for line in diagnose.format_drafts_report(drafts_report):
            typer.echo(line)

    failed = [check for check in checks if check.status == "fail"]
    if failed:
        typer.echo("")
        typer.echo(
            "Failing checks above block posting; fix them and re-run `xposter doctor`.",
            err=True,
        )
        raise typer.Exit(1)


def _state(ctx: typer.Context) -> _State:
    value = ctx.obj
    if not isinstance(value, _State):
        raise RuntimeError("The xposter group callback did not initialise the CLI state.")
    return value


def _poster(config: XPosterConfig, *, allow_alt_text: bool = False) -> XPoster:
    try:
        return XPoster(config, allow_alt_text=allow_alt_text)
    except XPosterError as exc:
        _fail(exc)


def _call(coro: Coroutine[Any, Any, _T], state: _State) -> _T:
    """Run one async operation, translating failures into documented exit codes."""
    try:
        return asyncio.run(coro)
    except XPosterError as exc:
        _fail(exc, state=state)
    except Exception as exc:
        if state.verbose:
            _print_traceback(exc, state)
        _fail(exc, state=state)


def _guard(action: Callable[[], _T], state: _State) -> _T:
    try:
        return action()
    except XPosterError as exc:
        _fail(exc, state=state)


def _usage(message: str) -> NoReturn:
    """Report a bad option combination through the one error printer."""
    _fail(ValidationError(message))


def _fail(exc: BaseException, *, state: _State | None = None) -> NoReturn:
    """Print one operator-actionable message and exit with the documented code."""
    code = _exit_code(exc)
    for line in _error_lines(exc, code, state):
        typer.echo(line, err=True)
    raise typer.Exit(code)


def _exit_code(exc: BaseException) -> int:
    if isinstance(exc, LedgerCorruptError):
        return 6
    if isinstance(exc, (ConfigurationError, ValidationError)):
        return 2
    if isinstance(exc, SessionError):
        return 3
    if isinstance(exc, (ParkedError, ChallengeError, AccountRestrictedError, RateLimitedError)):
        return 4
    if isinstance(exc, PacingError):
        return 5
    if isinstance(exc, PostOutcomeUnknownError):
        return 6
    if isinstance(exc, UiChangedError):
        return 7
    if isinstance(exc, (BrowserUnavailableError, SessionBusyError)):
        return 8
    return 1


def _error_lines(exc: BaseException, code: int, state: _State | None) -> list[str]:
    message = _redact(str(exc), config=state.config if state is not None else None).strip()
    label = _error_label(exc)
    lines = [f"{label}: {message}" if message else label]

    if code == 2:
        lines.append("Fix the input or configuration value and re-run.")
    elif code == 3:
        if isinstance(exc, AccountMismatchError):
            lines.append("Log in as the expected account, or pass --handle with the right name.")
        else:
            lines.append("Run `xposter login` to sign in again.")
    elif code == 4:
        if isinstance(exc, ParkedError):
            since = f" since {exc.since}" if exc.since else ""
            lines.append(f"Automation is parked{since}; it will not retry by itself.")
        lines.append(
            "Open x.com in a normal browser and check the account: resolve the challenge, "
            "lock or rate limit, then run `xposter unpark` before posting again."
        )
        if isinstance(exc, RateLimitedError) and exc.retry_after_s:
            lines.append(f"X suggested waiting about {math.ceil(exc.retry_after_s)} s first.")
    elif code == 5:
        if isinstance(exc, PacingError):
            wait_s = max(0, math.ceil(exc.retry_after_s))
            local = (
                datetime.now(UTC) + timedelta(seconds=max(exc.retry_after_s, 0.0))
            ).astimezone()
            lines.append(
                f"Next allowed at {_local_stamp(local)} local time (wait about {wait_s} s)."
            )
    elif code == 6:
        if isinstance(exc, LedgerCorruptError):
            lines.append(f"Ledger file: {exc.path}")
            lines.append(
                "Two options: restore that file from a backup, or quarantine it with "
                "`xposter ledger --reset-corrupt` after checking the account in a normal "
                "browser; posting stops until one of them is done."
            )
        else:
            posted = exc.posted if isinstance(exc, PostOutcomeUnknownError) else ()
            if posted:
                lines.append("Captured tweet ids: " + ", ".join(tweet.id for tweet in posted))
            else:
                lines.append("No tweet ids were captured.")
            lines.append(
                "Check the profile in a normal browser before retrying; the click may have "
                "landed, so do not use --force until you have confirmed the thread is absent."
            )
    elif code == 7:
        artifacts = exc.artifacts if isinstance(exc, UiChangedError) else None
        if artifacts is None and state is not None:
            artifacts = state.config.artifacts_dir
        if artifacts is not None:
            lines.append(f"Artifacts: {artifacts}")
        lines.append(
            "Run `xposter doctor --dom` to compare X's live DOM with our selectors, then "
            "update selectors.py from the report."
        )
    elif code == 8:
        if isinstance(exc, SessionBusyError):
            lines.append("Wait for the other xposter run to finish, then retry.")
        else:
            lines.append(
                "Install Google Chrome, or close the Chrome window using this profile, then retry."
            )
    elif code == 1:
        if isinstance(exc, XPosterError):
            lines.append("Stop and inspect the message above; run `xposter doctor` if it persists.")
        else:
            lines.append(
                "Unexpected failure; re-run with -v for a traceback, or run `xposter doctor`."
            )
    return lines


def _error_label(exc: BaseException) -> str:
    labels: tuple[tuple[type[BaseException], str], ...] = (
        (NotLoggedInError, "Not logged in"),
        (AccountMismatchError, "Account mismatch"),
        (ParkedError, "Parked"),
        (ChallengeError, "Challenge required"),
        (AccountRestrictedError, "Account restricted"),
        (RateLimitedError, "Rate limited"),
        (PacingError, "Pacing"),
        (PostOutcomeUnknownError, "Post outcome unknown"),
        (LedgerCorruptError, "Ledger corrupt"),
        (UiChangedError, "UI changed"),
        (BrowserUnavailableError, "Browser unavailable"),
        (SessionBusyError, "Session busy"),
        (ConfigurationError, "Configuration error"),
        (ValidationError, "Validation error"),
        (SessionError, "Session error"),
        (XPosterError, "xposter error"),
    )
    for error_type, label in labels:
        if isinstance(exc, error_type):
            return label
    return f"Unexpected {type(exc).__name__}"


def _redact(text: str, *, config: XPosterConfig | None = None) -> str:
    """Hide credential-shaped text and every value from the saved cookie jar.

    Regex rules cover the common ``auth_token=...`` shape, but an exception may
    quote a raw cookie value with no key next to it. Loading the stored values
    (never printing them) lets the traceback redactor remove that exact value
    too, including from a chained exception.
    """

    for value in _stored_secret_values(config):
        text = text.replace(value, "[redacted]")

    def hide(match: re.Match[str]) -> str:
        key = match.group("key")
        if key is not None:
            return f"{key}=[redacted]"
        return f"{match.group('scheme')} [redacted]"

    return _SECRET_RE.sub(hide, text)


def _stored_secret_values(config: XPosterConfig | None) -> tuple[str, ...]:
    if config is None:
        return ()
    try:
        state = SessionStore(config).load_state()
    except (OSError, ValueError):
        return ()
    if not isinstance(state, Mapping):
        return ()
    cookies = state.get("cookies")
    if not isinstance(cookies, list):
        return ()
    values: list[str] = []
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            continue
        value = cookie.get("value")
        if isinstance(value, str) and len(value) >= 6:
            values.append(value)
    return tuple(dict.fromkeys(values))


def _print_traceback(exc: BaseException, state: _State) -> None:
    """Print a redacted traceback for ``-v``; chained text is redacted too."""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    typer.echo(_redact(text.rstrip("\n"), config=state.config), err=True)


def _echo_session(summary: SessionSummary) -> None:
    if not summary.present:
        typer.echo("Session: none; run `xposter login`.")
        return
    handle = f"@{summary.handle}" if summary.handle else "handle not recorded"
    status_word = "expired" if summary.expired else "ok"
    expiry = summary.auth_expires_at or "unknown"
    verified = summary.last_verified_at or "never"
    typer.echo(
        f"Session: {status_word} ({handle}); auth expires {expiry}; last verified {verified}."
    )


def _ledger_counts(entries: Sequence[LedgerEntry]) -> dict[str, int]:
    counts = {
        "entries": len(entries),
        LedgerStatus.POSTED.value: 0,
        LedgerStatus.PENDING.value: 0,
        LedgerStatus.UNKNOWN.value: 0,
        LedgerStatus.FAILED.value: 0,
    }
    for entry in entries:
        counts[entry.status.value] = counts.get(entry.status.value, 0) + 1
    return counts


def _first_url(entry: LedgerEntry) -> str:
    if entry.completed is not None:
        return entry.completed.first_url
    if entry.posted:
        return entry.posted[0].url
    return ""


def _park_json(parked: ParkInfo | None) -> dict[str, str] | None:
    if parked is None:
        return None
    return {
        "reason": parked.reason,
        "kind": parked.kind,
        "since": parked.since,
        "detail": parked.detail,
    }


def _source_text(*, text: str | None, file: Path | None) -> str:
    if file is not None:
        return _read_utf8(file).strip()
    value = (text or "").strip()
    if not value:
        raise ValidationError("The thread text is empty.")
    return value


def _read_utf8(path: Path) -> str:
    if str(path) == "-":
        return sys.stdin.read()
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValidationError(f"{path} is not valid UTF-8; save it as UTF-8 and retry.") from None
    except OSError as exc:
        raise ValidationError(f"Could not read {path}: {exc}") from None


def _tweets_from_json(path: Path) -> tuple[Tweet, ...]:
    raw = _read_utf8(path)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"{path} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})."
        ) from None
    if not isinstance(data, list) or not data:
        raise ValidationError(
            f"{path} must contain a non-empty JSON list of strings or "
            '{"text": ..., "media": [...], "alt": [...]} objects.'
        )

    base = Path.cwd() if str(path) == "-" else path.parent
    tweets: list[Tweet] = []
    for index, item in enumerate(data, start=1):
        if isinstance(item, str):
            value = item.strip()
            media_paths: tuple[Path, ...] = ()
            alt_texts: tuple[str, ...] = ()
        elif isinstance(item, dict):
            raw_text = item.get("text")
            if not isinstance(raw_text, str) or not raw_text.strip():
                raise ValidationError(f'Tweet {index} in {path} needs a non-empty "text" string.')
            value = raw_text.strip()
            raw_media = item.get("media", [])
            if not isinstance(raw_media, list) or not all(
                isinstance(entry, str) for entry in raw_media
            ):
                raise ValidationError(
                    f'Tweet {index} in {path} has a "media" value that is not a list of paths.'
                )
            media_paths = _resolve_media(
                [Path(entry) for entry in raw_media], f"tweet {index}", base=base
            )
            raw_alt = item.get("alt", [])
            if not isinstance(raw_alt, list) or not all(
                isinstance(entry, str) for entry in raw_alt
            ):
                raise ValidationError(
                    f'Tweet {index} in {path} has an "alt" value that is not a list of strings.'
                )
            if len(raw_alt) > len(media_paths):
                raise ValidationError(
                    f"Tweet {index} in {path} has {len(raw_alt)} alt text(s) for "
                    f"{len(media_paths)} media file(s); align them one-to-one."
                )
            alt_texts = tuple(raw_alt)
        else:
            raise ValidationError(f"Tweet {index} in {path} is neither a string nor a JSON object.")
        tweets.append(Tweet(text=value, media=media_paths, alt_texts=alt_texts))
    return tuple(tweets)


def _parse_media_specs(values: Sequence[str]) -> dict[int, list[Path]]:
    """Parse repeatable ``--media PATH`` / ``--media N:PATH`` values."""
    groups: dict[int, list[Path]] = {}
    for raw in values:
        match = _MEDIA_SPEC_RE.match(raw)
        if match is not None:
            tweet_number = int(match.group("tweet"))
            path = Path(match.group("path")).expanduser()
        else:
            prefix, separator, rest = raw.partition(":")
            if separator and prefix.isdigit() and not rest:
                raise ValidationError(f"--media {raw!r} needs a path after the tweet number.")
            tweet_number = 1
            path = Path(raw).expanduser()
        groups.setdefault(tweet_number, []).append(path)
    return groups


def _parse_alt_specs(values: Sequence[str]) -> dict[int, list[tuple[int, str]]]:
    """Parse repeatable ``--alt TEXT`` / ``--alt N.M:TEXT`` values.

    Plain values fill tweet 1's images in order; qualified values address one
    image directly. ``N`` and ``M`` are one-based.
    """
    groups: dict[int, list[tuple[int, str]]] = {}
    next_plain_image = 1
    for raw in values:
        match = _ALT_SPEC_RE.match(raw)
        if match is not None:
            tweet_number = int(match.group("tweet"))
            image_number = int(match.group("image"))
            groups.setdefault(tweet_number, []).append((image_number, match.group("text")))
        else:
            groups.setdefault(1, []).append((next_plain_image, raw))
            next_plain_image += 1
    return groups


def _merge_media_and_alt(
    tweets: Sequence[Tweet],
    media_groups: Mapping[int, Sequence[Path]],
    alt_groups: Mapping[int, Sequence[tuple[int, str]]],
) -> list[Tweet]:
    """Apply CLI media/alt groups to the parsed tweets, aligned per image."""
    referenced = set(media_groups) | set(alt_groups)
    for tweet_number in sorted(referenced):
        if tweet_number > len(tweets):
            raise ValidationError(
                f"--media/--alt references tweet {tweet_number}, but this thread has "
                f"{len(tweets)} tweet(s)."
            )

    merged: list[Tweet] = []
    for index, tweet in enumerate(tweets, start=1):
        extra_media = _resolve_media(media_groups.get(index, ()), f"tweet {index}")
        media = (*tweet.media, *extra_media)
        alt_texts = list(tweet.alt_texts)
        alt_texts.extend("" for _ in range(len(media) - len(alt_texts)))
        for image_number, text in alt_groups.get(index, ()):
            if image_number > len(media):
                raise ValidationError(
                    f"--alt references tweet {index}, image {image_number}, but that "
                    f"tweet has {len(media)} image(s)."
                )
            alt_texts[image_number - 1] = text
        merged.append(Tweet(text=tweet.text, media=media, alt_texts=tuple(alt_texts)))
    return merged


def _resolve_media(
    values: Sequence[Path], where: str, *, base: Path | None = None
) -> tuple[Path, ...]:
    resolved: list[Path] = []
    for value in values:
        path = value.expanduser()
        if not path.is_absolute() and base is not None:
            path = base / path
        if not path.is_file():
            raise ValidationError(f"Media file for {where} was not found: {path}")
        resolved.append(path)
    return tuple(resolved)


def _check_media_limits(tweets: Sequence[Tweet], config: XPosterConfig) -> None:
    for index, tweet in enumerate(tweets, start=1):
        if len(tweet.media) > config.max_media_per_tweet:
            raise ValidationError(
                f"Tweet {index} has {len(tweet.media)} media files; X allows at most "
                f"{config.max_media_per_tweet}."
            )
        validate_media(tweet.media, tweet_number=index)


def _thread_has_alt_text(thread: Thread) -> bool:
    """True when any image carries non-blank alt text.

    Blank entries are placeholders that keep alt lists aligned with media and
    must never trigger the unverified-alt refusal.
    """
    return any(alt.strip() for tweet in thread.tweets for alt in tweet.alt_texts)


@dataclass(frozen=True, slots=True)
class _MentionSummary:
    """Per-tweet mentions plus the de-duplicated thread total and its cap."""

    per_tweet: tuple[tuple[str, ...], ...]
    distinct: tuple[str, ...]
    cap: int

    @property
    def count(self) -> int:
        return len(self.distinct)

    @property
    def exceeds(self) -> bool:
        return self.count > self.cap


def _mention_summary(tweets: Sequence[Tweet], config: XPosterConfig) -> _MentionSummary:
    """Mirror the poster's mention counting, including the self-handle exemption.

    The cap exists because X's Automation Rules treat unsolicited automated
    mentions as spam; mentioning your own account is not an unsolicited
    notification and must not consume the budget.
    """
    own = config.handle.lower() if config.handle else None
    per_tweet: list[tuple[str, ...]] = []
    distinct: list[str] = []
    for tweet in tweets:
        handles = tuple(handle for handle in extract_mentions(tweet.text) if handle != own)
        per_tweet.append(handles)
        for handle in handles:
            if handle not in distinct:
                distinct.append(handle)
    return _MentionSummary(tuple(per_tweet), tuple(distinct), config.max_mentions_per_thread)


def _mentions_refusal(count: int, cap: int) -> str:
    """The same content the poster's pre-browser ValidationError raises."""
    return (
        f"{count} distinct @mentions exceed max_mentions_per_thread={cap}. "
        "X treats unsolicited automated mentions as spam "
        "(https://help.x.com/en/rules-and-policies/x-automation); mention "
        "only accounts that expect it, or raise the cap deliberately on a "
        "test account."
    )


def _media_details(tweet: Tweet) -> list[str]:
    details: list[str] = []
    for image_index, path in enumerate(tweet.media):
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        suffix = path.suffix.lower()
        media_limit = MAX_GIF_BYTES if suffix == ".gif" else MAX_IMAGE_BYTES
        alt = image_index < len(tweet.alt_texts) and bool(tweet.alt_texts[image_index].strip())
        details.append(
            f"{path.name} ({size / 1024:.1f} KB, {'alt' if alt else 'no alt'}, "
            f"{media_limit // (1024 * 1024)} MiB limit)"
        )
    return details


def _echo_preview(
    thread: Thread,
    limit: int,
    *,
    numbered: bool,
    err: bool,
    existing: LedgerEntry | None = None,
    allow_alt: bool = True,
    mentions: _MentionSummary | None = None,
) -> None:
    typer.echo(
        f"Preview: {len(thread.tweets)} tweet(s), limit {limit} weighted characters each"
        + ("" if numbered else ", numbering off"),
        err=err,
    )
    if not allow_alt and _thread_has_alt_text(thread):
        typer.echo(
            "!! alt text is present but --allow-alt is not set; this post will be "
            "refused. Re-run with --allow-alt on a test account.",
            err=err,
        )
    total = len(thread.tweets)
    for index, tweet in enumerate(thread.tweets, start=1):
        weight = weighted_length(tweet.text)
        over = " OVER" if weight > limit else ""
        typer.echo(
            f'  {index}/{total}  {weight}/{limit}{over}  "{_snippet(tweet.text)}"',
            err=err,
        )
        details = _media_details(tweet)
        if details:
            typer.echo(f"    media: {'; '.join(details)}", err=err)
        if mentions is not None and mentions.per_tweet[index - 1]:
            typer.echo(
                "    mentions: "
                + ", ".join(f"@{handle}" for handle in mentions.per_tweet[index - 1]),
                err=err,
            )
        for path in tweet.media:
            suffix = path.suffix.lower()
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            media_limit = MAX_GIF_BYTES if suffix == ".gif" else MAX_IMAGE_BYTES
            if size > media_limit:
                typer.echo(
                    f"!! {path.name} is {size / 1024:.1f} KB, over X's "
                    f"{media_limit // (1024 * 1024)} MiB image limit.",
                    err=err,
                )
            if suffix not in _SUPPORTED_MEDIA_SUFFIXES:
                typer.echo(
                    f"!! {path.name} has unsupported media type {suffix or '(none)'}; "
                    "use PNG, JPEG, GIF or WebP.",
                    err=err,
                )
    if mentions is not None:
        typer.echo(f"mentions: {mentions.count} distinct (cap {mentions.cap})", err=err)
        if mentions.exceeds:
            typer.echo(
                f"!! {mentions.count} distinct @mentions exceed "
                f"max_mentions_per_thread={mentions.cap}; X treats unsolicited "
                "automated mentions as spam",
                err=err,
            )
        elif mentions.count:
            typer.echo(
                "mentions notify those accounts; only tag accounts that expect it "
                "(X Automation Rules)",
                err=err,
            )
    if existing is not None and existing.posted:
        _echo_recorded(existing, err=err)


def _echo_recorded(entry: LedgerEntry, *, err: bool) -> None:
    """Show recorded ids/URLs before a recovery flag can touch the ledger."""
    partial = entry.completed is None or not entry.completed.complete
    kind = "a partial attempt" if partial else "an already posted thread"
    typer.echo(f"Ledger records {len(entry.posted)} tweet(s) from {kind}:", err=err)
    for tweet in entry.posted:
        typer.echo(f"  index {tweet.index}  id {tweet.id}  {tweet.url}", err=err)
    if partial:
        typer.echo(
            "  --force CONTINUES after these recorded tweets; it never re-posts them.",
            err=err,
        )
    typer.echo(
        "  --discard-recorded starts over and is only for a thread you deleted by hand.",
        err=err,
    )


_HEADLESS_POST_WARNING = (
    "HeadlessChrome user agent is visible to X; use --display visible for the first "
    "runs, Xvfb on Linux."
)


def _warn_if_headless(state: _State, *, yes: bool) -> None:
    """Warn on the unsafe default unless the operator opted into it explicitly."""
    if yes and state.explicit_display is Display.HEADLESS:
        return
    try:
        resolved = resolve_display(state.config)
    except XPosterError:
        return
    if resolved is Display.HEADLESS:
        typer.echo(_HEADLESS_POST_WARNING, err=True)


def _snippet(text: str, width: int = 60) -> str:
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    return f"{flat[:width]}..."


def _posted_tweet(value: str, index: int, config: XPosterConfig) -> PostedTweet:
    raw = value.strip()
    match = _STATUS_ID_RE.search(raw)
    if match is not None:
        status_id = match.group("id")
    elif raw.isdigit():
        status_id = raw
    else:
        raise ValidationError(f"{value!r} is not a status URL or a numeric status id.")
    if raw.startswith(("http://", "https://")):
        url = raw.split("?", 1)[0]
    else:
        url = f"{config.base_url.rstrip('/')}/i/status/{status_id}"
    return PostedTweet(index=index, id=status_id, url=url, text_sha256="")


def _local_stamp(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()


def _require(value: Path | None, flag: str) -> Path:
    if value is None:
        raise ValidationError(f"{flag} needs a path.")
    return value
