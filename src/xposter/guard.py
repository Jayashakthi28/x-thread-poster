"""Turn raw X observations into one safety decision.

The pipeline records what the page or API showed it — an HTTP status, a URL, a
GraphQL error payload or visible text — and this module decides whether to
retry, refresh a CSRF token, stop the thread, sign in again, reject the content
or park the whole tool. Keeping the mapping pure and separate from Playwright
means every rule is table-testable and no driver can quietly "handle" a
challenge by retrying.

Policy comes from ``docs/research/web-client.md`` §3 and
``docs/research/risk-and-limits.md`` §8: anything mentioning automation,
verification, locking or suspension stops the run and parks the tool because a
retry escalates the block. Only capacity errors are retryable — HTTP 5xx, body
codes 88/131, and the explicit transient-message rules — and an unrecognised
code or message parks rather than retrying. Reasons never echo payload text, so
a stray ``auth_token`` in an error body can never reach logs.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from .errors import (
    AccountRestrictedError,
    ChallengeError,
    NotLoggedInError,
    PostRejectedError,
    RateLimitedError,
    XPosterError,
)


class Action(StrEnum):
    """What the caller should do about an observation."""

    RETRY = "retry"
    PARK = "park"
    RELOGIN = "relogin"
    STOP_THREAD = "stop_thread"
    REFRESH_CSRF = "refresh_csrf"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class Verdict:
    """A classified observation plus an operator-actionable reason."""

    action: Action
    code: int | None
    reason: str
    kind: str


# Severity used when one payload carries several errors: the worst wins.
_SEVERITY: dict[Action, int] = {
    Action.RETRY: 0,
    Action.REFRESH_CSRF: 1,
    Action.REJECT: 2,
    Action.STOP_THREAD: 3,
    Action.RELOGIN: 4,
    Action.PARK: 5,
}

# GraphQL/REST body codes, one row per code observed in web-client.md §3 plus
# the suspended/automation codes named in its "additional strings" section and
# the revoked-session codes from §5.2. Values are (action, kind, reason).
#
# Retry is deliberately limited to 88 and 131: an unrecognised or unexpected
# code parks because a retry is the behaviour that turns a block into a ban.
# Body code 403 is IneligibleFor2faAfterModification, NOT HTTP 403.
_BODY_CODE: dict[int, tuple[Action, str, str]] = {
    27: (
        Action.PARK,
        "locked",
        "X reports the account is locked (code 27); unlock it manually first.",
    ),
    32: (
        Action.RELOGIN,
        "auth",
        "X rejected the credentials (code 32); the saved session is no longer valid.",
    ),
    64: (
        Action.PARK,
        "suspended",
        "X reports the account is suspended (code 64); a human must resolve it.",
    ),
    88: (
        Action.RETRY,
        "rate_limit",
        "X rate-limited the API call (code 88); retry with backoff while the retry cap allows.",
    ),
    89: (
        Action.RELOGIN,
        "auth",
        "X invalidated the OAuth token (code 89); the saved session is no longer valid.",
    ),
    131: (
        Action.RETRY,
        "server",
        "X reported an internal error (code 131); retry once with backoff, then park.",
    ),
    135: (
        Action.RELOGIN,
        "auth",
        "The OAuth timestamp is skewed (code 135); sign in again to resync the session.",
    ),
    159: (
        Action.PARK,
        "suspended",
        "X reports the account is suspended (code 159); automation stays parked.",
    ),
    179: (
        Action.PARK,
        "other",
        "X refused to show this status (code 179); the context is read-only, so stop.",
    ),
    185: (
        Action.PARK,
        "rate_limit",
        "X reports the daily post limit (code 185); park until it resets, do not retry today.",
    ),
    186: (
        Action.REJECT,
        "other",
        "X rejected the post as too long (code 186); trim the text and post again.",
    ),
    187: (
        Action.REJECT,
        "duplicate",
        "X rejected the post as duplicate content (code 187); verify whether an earlier "
        "attempt landed instead of posting the same text again.",
    ),
    215: (
        Action.RELOGIN,
        "auth",
        "X rejected the authentication data (code 215); the saved session is no longer valid.",
    ),
    220: (
        Action.RELOGIN,
        "auth",
        "X restricted the auth token (code 220); sign in again to refresh the session.",
    ),
    225: (
        Action.PARK,
        "automation",
        "X flagged automated behaviour (code 225); park for at least 24-48 hours and post less.",
    ),
    226: (
        Action.PARK,
        "automation",
        "X flagged automated behaviour (code 226); park for at least 24-48 hours and post less.",
    ),
    228: (
        Action.PARK,
        "automation",
        "X says the account is temporarily blocked from posting (code 228); park and lower volume.",
    ),
    236: (
        Action.PARK,
        "challenge",
        "X rejected a challenge response (code 236); a human must solve it before "
        "automation resumes.",
    ),
    261: (
        Action.PARK,
        "other",
        "X is in platform read-only mode (code 261); stop and resume after the platform recovers.",
    ),
    263: (
        Action.PARK,
        "challenge",
        "X requires a human challenge (code 263); a human must solve it before automation resumes.",
    ),
    283: (
        Action.PARK,
        "automation",
        "X flagged automated behaviour (code 283); park for at least 24-48 hours and post less.",
    ),
    292: (
        Action.PARK,
        "automation",
        "X flagged automated behaviour (code 292); park for at least 24-48 hours and post less.",
    ),
    315: (
        Action.PARK,
        "challenge",
        "X requires a CAPTCHA (code 315); a human must solve it before automation resumes.",
    ),
    326: (
        Action.PARK,
        "challenge",
        "X denied access through its bouncer (code 326); complete the access flow manually.",
    ),
    344: (
        Action.PARK,
        "rate_limit",
        "X hit the daily limit for this action (code 344); park until the limit resets.",
    ),
    349: (
        Action.PARK,
        "other",
        "X refused the direct-message action (code 349); it is not a posting action, so stop.",
    ),
    353: (
        Action.REFRESH_CSRF,
        "auth",
        "X rejected the CSRF token (code 353); refresh ct0 once and repeat the request.",
    ),
    385: (
        Action.STOP_THREAD,
        "other",
        "The reply target no longer exists (code 385); stop the thread and do not continue.",
    ),
    386: (
        Action.REJECT,
        "other",
        "X rejected the attachment types (code 386); remove unsupported media before "
        "posting again.",
    ),
    403: (
        Action.RELOGIN,
        "auth",
        "X rejected the session (code 403); the saved session is no longer valid.",
    ),
    408: (
        Action.REJECT,
        "other",
        "X rejected a URL or card in the post (code 408); fix the link and post again.",
    ),
    433: (
        Action.REJECT,
        "other",
        "X restricts replies under this conversation setting (code 433); change the reply "
        "settings and post again.",
    ),
    445: (
        Action.PARK,
        "challenge",
        "X rejected the read-only auth token (code 445); complete the access flow manually.",
    ),
    501: (
        Action.PARK,
        "rate_limit",
        "X reports the daily post limit (code 501); park until it resets, do not retry today.",
    ),
}

# HTTP status rules are a separate layer from body codes: HTTP 403 is a
# behavioural block when no body explains it, while a GraphQL body code 403 is
# an auth/2FA error. Callers that have both use classify_response().
_HTTP_STATUS: dict[int, tuple[Action, str, str]] = {
    401: (
        Action.RELOGIN,
        "auth",
        "X rejected the request as unauthenticated (HTTP 401); the saved session is invalid.",
    ),
    403: (
        Action.PARK,
        "challenge",
        "HTTP 403 with no recognisable error body — behavioural block; stopping",
    ),
    429: (
        Action.PARK,
        "rate_limit",
        "X rate-limited the request (HTTP 429); retrying escalates, so automation parks.",
    ),
}

# Message heuristics for payloads that omit a usable code. Order matters: a
# message can contain several words, and the first (most restrictive) rule wins.
_MESSAGE_RULES: tuple[tuple[str, Action, str, str], ...] = (
    (
        "suspended",
        Action.PARK,
        "suspended",
        "X says the account is suspended; automation stays parked until a human resolves it.",
    ),
    (
        "locked",
        Action.PARK,
        "locked",
        "X says the account is locked; a human must unlock it before automation resumes.",
    ),
    (
        "automated",
        Action.PARK,
        "automation",
        "X says the request looks automated; park for at least 24-48 hours and lower volume.",
    ),
    (
        "temporarily blocked",
        Action.PARK,
        "automation",
        "X says posting is temporarily blocked; park and lower the posting volume.",
    ),
    (
        "unusual activity",
        Action.PARK,
        "challenge",
        "X reports unusual activity; a human must complete verification before automation resumes.",
    ),
    (
        "verify",
        Action.PARK,
        "challenge",
        "X is asking for human verification; complete it manually before automation resumes.",
    ),
    (
        "confirm your identity",
        Action.PARK,
        "challenge",
        "X wants the identity confirmed; do it manually before automation resumes.",
    ),
    (
        "captcha",
        Action.PARK,
        "challenge",
        "X is asking for a CAPTCHA; a human must solve it before automation resumes.",
    ),
    (
        "daily limit",
        Action.PARK,
        "rate_limit",
        "X reports a daily limit; park until the limit resets.",
    ),
    (
        "reached your limit",
        Action.PARK,
        "rate_limit",
        "X reports a limit was reached; park until the limit resets.",
    ),
    (
        "rate limited",
        Action.RETRY,
        "rate_limit",
        "X rate-limited the request; retry with backoff while the retry cap allows.",
    ),
    (
        "something went wrong",
        Action.RETRY,
        "server",
        "X reported a transient failure; retry once with backoff, then park.",
    ),
)

# Strict visible-text rules for alert/dialog/toast containers. High-precision
# phrases only: a timeline post that merely discusses CAPTCHAs must not park a
# healthy account, so strict mode never looks for the bare words "captcha",
# "authenticate", "something went wrong" or "unusual activity".
_STRICT_TEXT_RULES: tuple[tuple[tuple[str, ...], Action, str, str], ...] = (
    (
        ("your account has been locked", "account is locked"),
        Action.PARK,
        "locked",
        "X says the account is locked; a human must unlock it before automation resumes.",
    ),
    (
        ("account suspended", "has been suspended"),
        Action.PARK,
        "suspended",
        "X says the account is suspended; automation stays parked until a human resolves it.",
    ),
    (
        ("verify you are a human", "confirm your identity"),
        Action.PARK,
        "challenge",
        "X is asking for human verification; complete it manually before automation resumes.",
    ),
    (
        ("might be automated",),
        Action.PARK,
        "automation",
        "X flagged the session as automated; park for at least 24-48 hours and lower volume.",
    ),
    (
        ("over the daily limit", "daily limit"),
        Action.PARK,
        "rate_limit",
        "X reports a daily limit; park until the limit resets.",
    ),
)

# Broad visible-text rules for the login probe, where the whole page is the
# signal and the phrases can be less specific.
_TEXT_RULES: tuple[tuple[tuple[str, ...], Action, str, str], ...] = (
    (
        (
            "your account has been locked",
            "account is locked",
            "account has been locked",
            "account locked",
        ),
        Action.PARK,
        "locked",
        "X says the account is locked; a human must unlock it before automation resumes.",
    ),
    (
        (
            "account suspended",
            "account has been suspended",
            "has been suspended",
        ),
        Action.PARK,
        "suspended",
        "X says the account is suspended; automation stays parked until a human resolves it.",
    ),
    (
        (
            "verify you are a human",
            "verifying you are a human",
            "unusual activity",
            "confirm your identity",
            "verify your identity",
            "authenticate",
            "captcha",
        ),
        Action.PARK,
        "challenge",
        "X is asking for human verification; complete it manually before automation resumes.",
    ),
    (
        (
            "might be automated",
            "temporarily blocked from sending posts",
        ),
        Action.PARK,
        "automation",
        "X flagged the session as automated; park for at least 24-48 hours and lower volume.",
    ),
    (
        (
            "daily limit",
            "over the daily limit",
            "reached your limit",
        ),
        Action.PARK,
        "rate_limit",
        "X reports a daily limit; park until the limit resets.",
    ),
    (
        ("something went wrong",),
        Action.RETRY,
        "server",
        "X showed a transient failure; retry once with backoff, then park if it repeats.",
    ),
    (
        ("could not log you in",),
        Action.PARK,
        "challenge",
        "X could not complete the login; a human must finish it manually before "
        "automation resumes.",
    ),
)

# Route prefixes, matched against urlsplit(url).path only. Query strings such as
# ?next=/login must never turn an ordinary page into an auth verdict.
_SUSPENDED_ROUTES = ("/account/suspended", "/suspended")
_LOCKED_ROUTES = ("/account/access", "/i/flow/consent")
_CHALLENGE_ROUTES = (
    "/account/login_challenge",
    "/i/flow/login_challenge",
    "/i/flow/challenge",
    "/i/flow/arkose",
    "/i/flow/captcha",
)
_LOGIN_ROUTES = ("/i/flow/login", "/login", "/logout", "/i/jf/onboarding")

_ALLOWED_HOSTS = frozenset({"api.x.com", "x.com"})
_DIGITS = re.compile(r"[0-9]+")
_MAX_ERROR_DEPTH = 6


def _verdict(action: Action, code: int | None, reason: str, kind: str) -> Verdict:
    return Verdict(action=action, code=code, reason=reason, kind=kind)


def _more_severe(current: Verdict | None, candidate: Verdict) -> Verdict:
    """Keep the verdict whose action needs the most caution."""
    if current is None or _SEVERITY[candidate.action] > _SEVERITY[current.action]:
        return candidate
    return current


def _coerce_code(value: object) -> int | None:
    """Accept an int or a digit string; ignore bools and junk."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isascii() and text.isdigit():
            return int(text)
    return None


def _error_fields(item: Mapping[str, Any]) -> tuple[int | None, str | None]:
    """Read code and message from an error item, including GraphQL extensions."""
    code = _coerce_code(item.get("code"))
    message = item.get("message")
    extensions = item.get("extensions")
    if isinstance(extensions, Mapping):
        if code is None:
            code = _coerce_code(extensions.get("code"))
        if not isinstance(message, str):
            message = extensions.get("message")
    return code, message if isinstance(message, str) else None


def _iter_error_items(value: object, depth: int = 0) -> Iterator[Mapping[str, Any]]:
    """Yield error-shaped mappings from REST and nested GraphQL payloads.

    X has shipped both ``{"errors": [{...}]}`` and GraphQL envelopes that nest
    the same shape under ``data``, ``response``, ``error`` or ``extensions``, so
    a bounded recursive walk is safer than assuming one layout.
    """
    if depth > _MAX_ERROR_DEPTH or not isinstance(value, Mapping):
        return
    errors = value.get("errors")
    if isinstance(errors, Mapping):
        yield errors
        yield from _iter_error_items(errors, depth + 1)
    elif isinstance(errors, Sequence) and not isinstance(errors, (str, bytes, bytearray)):
        for entry in errors:
            if isinstance(entry, Mapping):
                yield entry
                yield from _iter_error_items(entry, depth + 1)
    error = value.get("error")
    if isinstance(error, Mapping):
        yield error
        yield from _iter_error_items(error, depth + 1)
    extensions = value.get("extensions")
    if isinstance(extensions, Mapping):
        yield from _iter_error_items(extensions, depth + 1)
    for key in ("data", "response"):
        child = value.get(key)
        if isinstance(child, Mapping):
            yield from _iter_error_items(child, depth + 1)
    for key, child in value.items():
        if key in {"errors", "error", "extensions", "data", "response"}:
            continue
        if isinstance(child, Mapping):
            yield from _iter_error_items(child, depth + 1)
        elif isinstance(child, Sequence) and not isinstance(child, (str, bytes, bytearray)):
            for entry in child:
                if isinstance(entry, Mapping):
                    yield from _iter_error_items(entry, depth + 1)


def _classify_error_message(message: str) -> Verdict | None:
    lowered = message.lower()
    for needle, action, kind, reason in _MESSAGE_RULES:
        if needle in lowered:
            return _verdict(action, None, reason, kind)
    return None


def _classify_error_item(item: Mapping[str, Any]) -> Verdict | None:
    """Classify one error item; an unrecognised code always parks, never retries."""
    code, message = _error_fields(item)
    message_verdict = _classify_error_message(message) if message else None
    rule = _BODY_CODE.get(code) if code is not None else None
    if rule is not None:
        action, kind, reason = rule
        best = _verdict(action, code, reason, kind)
        if message_verdict is not None:
            if message_verdict.code is None:
                message_verdict = _verdict(
                    message_verdict.action, code, message_verdict.reason, message_verdict.kind
                )
            best = _more_severe(best, message_verdict)
        return best
    if code is not None:
        if message_verdict is not None and message_verdict.action is Action.PARK:
            return _verdict(Action.PARK, code, message_verdict.reason, message_verdict.kind)
        return _verdict(
            Action.PARK,
            code,
            f"unrecognised X error code {code} — stopping rather than retrying",
            "other",
        )
    if message_verdict is not None:
        return message_verdict
    if message:
        return _verdict(
            Action.PARK,
            None,
            "unrecognised X error message — stopping rather than retrying",
            "other",
        )
    return None


def classify_error_payload(payload: Mapping[str, Any]) -> Verdict | None:
    """Classify the most severe error in a REST or GraphQL payload.

    ``None`` means the payload carried no usable errors at all. Unrecognised
    codes and messages park: silence would be worse, and a retry escalates.
    """
    if not isinstance(payload, Mapping):
        return None
    best: Verdict | None = None
    for item in _iter_error_items(payload):
        verdict = _classify_error_item(item)
        if verdict is not None:
            best = _more_severe(best, verdict)
    return best


def classify_http_status(status: int) -> Verdict | None:
    """Classify an HTTP status without looking at the body.

    A 403 body can explain the refusal (session codes, content codes or the
    CSRF code 353), so callers that have a body should use
    :func:`classify_response`. Without a body, an HTTP 403 is treated as a
    behavioural block and parks. A 429 always parks: retrying a rate limit is
    exactly the behaviour that escalates it.
    """
    rule = _HTTP_STATUS.get(status)
    if rule is not None:
        action, kind, reason = rule
        return _verdict(action, status, reason, kind)
    if 500 <= status < 600:
        return _verdict(
            Action.RETRY,
            status,
            f"X returned HTTP {status}; retry with backoff while the retry cap allows.",
            "server",
        )
    return None


def classify_response(status: int, payload: Mapping[str, Any] | None) -> Verdict | None:
    """Classify an HTTP status together with its parsed body, worst rule first.

    This is the preferred entry point when both are available. A 403 is
    special: X returns it for an expired session, for content refusals, for the
    ct0-refresh path (body code 353) and as a bare behavioural block. A
    recognised non-retry body wins; an unparseable or retry-only body falls
    back to the HTTP 403 park. For every other status the more severe of the
    status and body verdicts wins (PARK > RELOGIN > STOP_THREAD > REJECT >
    REFRESH_CSRF > RETRY).
    """
    body = classify_error_payload(payload) if isinstance(payload, Mapping) else None
    if status == 403 and body is not None and body.action is not Action.RETRY:
        return body
    status_verdict = classify_http_status(status)
    if status_verdict is None:
        return body
    if body is None:
        return status_verdict
    return _more_severe(status_verdict, body)


def _route_path(url: str) -> str:
    """Return the lowercased URL path with any trailing slash removed."""
    try:
        path = urlsplit(url.strip()).path
    except ValueError:
        return ""
    return path.rstrip("/").lower()


def _on_route(path: str, route: str) -> bool:
    """Exact route or a true sub-path; ``/login`` must not match ``/loginfoo``."""
    return path == route or path.startswith(route + "/")


def classify_url(url: str) -> Verdict | None:
    """Classify a redirect or navigation target that reveals account state.

    Only the parsed path is inspected. Matching the whole URL would let a
    query-string value such as ``?next=/login`` on an ordinary page masquerade
    as an auth redirect.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    path = _route_path(url)
    if any(_on_route(path, route) for route in _SUSPENDED_ROUTES):
        return _verdict(
            Action.PARK,
            None,
            "X redirected to a suspension notice; automation stays parked until a human "
            "resolves the account.",
            "suspended",
        )
    if any(_on_route(path, route) for route in _LOCKED_ROUTES):
        return _verdict(
            Action.PARK,
            None,
            "X redirected to its access/consent gate; complete the identity flow manually "
            "before automation resumes.",
            "locked",
        )
    if any(_on_route(path, route) for route in _CHALLENGE_ROUTES):
        return _verdict(
            Action.PARK,
            None,
            "X redirected to a human challenge; a human must solve it before automation resumes.",
            "challenge",
        )
    if any(_on_route(path, route) for route in _LOGIN_ROUTES):
        return _verdict(
            Action.RELOGIN,
            None,
            "X redirected to its login/logout flow; the saved session is no longer valid.",
            "auth",
        )
    return None


def classify_text(text: str, *, strict: bool = False) -> Verdict | None:
    """Classify visible page text such as a toast or an interstitial.

    ``strict=True`` is for alert/dialog/toast containers during posting and
    uses only high-precision phrases, so a timeline post discussing CAPTCHAs
    cannot park a healthy account. ``strict=False`` keeps the broader rules for
    the login probe, where the whole page is the signal.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    lowered = text.lower()
    rules = _STRICT_TEXT_RULES if strict else _TEXT_RULES
    for needles, action, kind, reason in rules:
        if any(needle in lowered for needle in needles):
            return _verdict(action, None, reason, kind)
    return None


def to_exception(verdict: Verdict) -> XPosterError | None:
    """Convert a verdict into the exception the caller should raise.

    ``RETRY`` and ``REFRESH_CSRF`` stay as control flow (no exception). Every
    message tells the operator what to do next and contains no payload text.
    A park with kind ``other`` (an unrecognised code, read-only mode, a status
    refusal) becomes a ChallengeError with inspection guidance, because the
    tool cannot continue safely without a human looking at it.
    """
    if verdict.action is Action.PARK:
        if verdict.kind in {"challenge", "automation"}:
            return ChallengeError(verdict.reason)
        if verdict.kind in {"locked", "suspended"}:
            return AccountRestrictedError(verdict.reason)
        if verdict.kind == "rate_limit":
            return RateLimitedError(verdict.reason)
        if verdict.kind == "other":
            return ChallengeError(
                f"{verdict.reason} Run `xposter doctor` and inspect the newest artifacts "
                "before posting again."
            )
        return AccountRestrictedError(verdict.reason)
    if verdict.action is Action.RELOGIN:
        message = verdict.reason
        if "xposter login" not in message:
            message = f"{message} Run `xposter login` to sign in again."
        return NotLoggedInError(message)
    if verdict.action in {Action.REJECT, Action.STOP_THREAD}:
        return PostRejectedError(verdict.reason, code=verdict.code)
    return None


def _url_path(url: str) -> str | None:
    """Return the lowercase path for an x.com/api.x.com URL, else ``None``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = parts.hostname
    if host is not None and host.lower() not in _ALLOWED_HOSTS:
        return None
    return parts.path.lower()


def _operation(url: str) -> str | None:
    """Return the last path segment, which is the GraphQL operation name."""
    path = _url_path(url)
    if path is None:
        return None
    return path.rstrip("/").rsplit("/", 1)[-1]


def is_create_tweet_response(url: str) -> bool:
    """Whether a response URL is X's CreateTweet (or note-tweet) mutation."""
    return _operation(url) in {"createtweet", "createtweetnotetweetmutation"}


def is_delete_tweet_response(url: str) -> bool:
    """Whether a response URL is X's DeleteTweet mutation."""
    return _operation(url) == "deletetweet"


def _follow(value: object, keys: tuple[str, ...]) -> object | None:
    """Walk nested mappings, returning ``None`` when any step is missing."""
    cursor = value
    for key in keys:
        if not isinstance(cursor, Mapping):
            return None
        cursor = cursor.get(key)
    return cursor


def _tweet_id(value: object) -> str | None:
    if isinstance(value, str) and _DIGITS.fullmatch(value):
        return value
    return None


def extract_created_tweet_id(payload: Mapping[str, Any]) -> str | None:
    """Extract the new tweet id from a CreateTweet response, or ``None``.

    X has returned the id directly under ``tweet_results`` and wrapped in
    ``result`` or ``result.tweet`` (``TweetWithVisibilityResults``), and long
    posts use the ``notetweet_create`` mutation. Any other shape is treated as
    unknown instead of raising, because the caller must not blind-retry when it
    cannot prove whether the post landed.
    """
    if not isinstance(payload, Mapping):
        return None
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    for operation in ("create_tweet", "notetweet_create"):
        results = _follow(data, (operation, "tweet_results"))
        if results is None:
            continue
        for keys in (
            ("rest_id",),
            ("result", "rest_id"),
            ("result", "tweet", "rest_id"),
        ):
            tweet_id = _tweet_id(_follow(results, keys))
            if tweet_id is not None:
                return tweet_id
    return None
