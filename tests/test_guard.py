"""Table tests for the pure guard: code, URL, status, text and payload mapping.

Every branch that can park the tool or stop a thread is pinned here, because a
silent regression in this module is the difference between stopping at a
challenge and retrying into a ban.
"""

from __future__ import annotations

from typing import Any

import pytest

from xposter import guard as guard_module
from xposter.errors import (
    AccountRestrictedError,
    ChallengeError,
    NotLoggedInError,
    PostRejectedError,
    RateLimitedError,
    XPosterError,
)
from xposter.guard import (
    Action,
    Verdict,
    classify_error_payload,
    classify_http_status,
    classify_response,
    classify_text,
    classify_url,
    extract_created_tweet_id,
    is_create_tweet_response,
    is_delete_tweet_response,
    to_exception,
)

PARK_CODES = [
    (27, "locked"),
    (64, "suspended"),
    (159, "suspended"),
    (185, "rate_limit"),
    (225, "automation"),
    (226, "automation"),
    (228, "automation"),
    (236, "challenge"),
    (263, "challenge"),
    (283, "automation"),
    (292, "automation"),
    (315, "challenge"),
    (326, "challenge"),
    (344, "rate_limit"),
    (501, "rate_limit"),
]

# Every row of web-client.md §3's observed code table, plus the codes named in
# its "additional strings" section (159/225/228/283/292) and §5.2 (220/445).
# (code, action, kind) — retry is allowed only for 88 and 131.
BODY_TABLE = [
    (27, Action.PARK, "locked"),
    (32, Action.RELOGIN, "auth"),
    (64, Action.PARK, "suspended"),
    (88, Action.RETRY, "rate_limit"),
    (89, Action.RELOGIN, "auth"),
    (131, Action.RETRY, "server"),
    (135, Action.RELOGIN, "auth"),
    (159, Action.PARK, "suspended"),
    (179, Action.PARK, "other"),
    (185, Action.PARK, "rate_limit"),
    (186, Action.REJECT, "other"),
    (187, Action.REJECT, "duplicate"),
    (215, Action.RELOGIN, "auth"),
    (220, Action.RELOGIN, "auth"),
    (225, Action.PARK, "automation"),
    (226, Action.PARK, "automation"),
    (228, Action.PARK, "automation"),
    (236, Action.PARK, "challenge"),
    (261, Action.PARK, "other"),
    (263, Action.PARK, "challenge"),
    (283, Action.PARK, "automation"),
    (292, Action.PARK, "automation"),
    (315, Action.PARK, "challenge"),
    (326, Action.PARK, "challenge"),
    (344, Action.PARK, "rate_limit"),
    (349, Action.PARK, "other"),
    (353, Action.REFRESH_CSRF, "auth"),
    (385, Action.STOP_THREAD, "other"),
    (386, Action.REJECT, "other"),
    (403, Action.RELOGIN, "auth"),
    (408, Action.REJECT, "other"),
    (433, Action.REJECT, "other"),
    (445, Action.PARK, "challenge"),
    (501, Action.PARK, "rate_limit"),
]

ALLOWED_KINDS = {
    "challenge",
    "locked",
    "suspended",
    "rate_limit",
    "auth",
    "duplicate",
    "automation",
    "server",
    "other",
}


def _payload(code: int, message: str = "") -> dict[str, Any]:
    return {"errors": [{"code": code, "message": message}]}


def test_action_values_are_stable() -> None:
    assert {action.value for action in Action} == {
        "retry",
        "park",
        "relogin",
        "stop_thread",
        "refresh_csrf",
        "reject",
    }


@pytest.mark.parametrize(("code", "action", "kind"), BODY_TABLE)
def test_every_research_body_code(code: int, action: Action, kind: str) -> None:
    verdict = classify_error_payload(_payload(code))

    assert verdict is not None
    assert verdict.action is action
    assert verdict.kind == kind
    assert verdict.code == code
    assert verdict.reason


def test_only_capacity_body_codes_retry() -> None:
    retrying = sorted(
        code for code, (action, _, _) in guard_module._BODY_CODE.items() if action is Action.RETRY
    )

    assert retrying == [88, 131]


@pytest.mark.parametrize(("code", "kind"), PARK_CODES)
def test_park_codes(code: int, kind: str) -> None:
    verdict = classify_error_payload(_payload(code))

    assert verdict is not None
    assert verdict.action is Action.PARK
    assert verdict.kind == kind
    assert verdict.code == code
    assert verdict.reason


@pytest.mark.parametrize(("code", "kind"), [(88, "rate_limit"), (131, "server")])
def test_retry_codes(code: int, kind: str) -> None:
    verdict = classify_error_payload(_payload(code))

    assert verdict is not None
    assert verdict.action is Action.RETRY
    assert verdict.kind == kind
    assert verdict.code == code


@pytest.mark.parametrize("code", [32, 89, 135, 215, 403])
def test_relogin_codes(code: int) -> None:
    verdict = classify_error_payload(_payload(code))

    assert verdict is not None
    assert verdict.action is Action.RELOGIN
    assert verdict.kind == "auth"
    assert verdict.code == code


def test_csrf_code_refreshes_once() -> None:
    verdict = classify_error_payload(_payload(353))

    assert verdict is not None
    assert verdict.action is Action.REFRESH_CSRF
    assert verdict.code == 353


@pytest.mark.parametrize(("code", "kind"), [(186, "other"), (187, "duplicate")])
def test_reject_codes(code: int, kind: str) -> None:
    verdict = classify_error_payload(_payload(code))

    assert verdict is not None
    assert verdict.action is Action.REJECT
    assert verdict.kind == kind
    assert verdict.code == code


def test_reply_target_gone_stops_the_thread() -> None:
    verdict = classify_error_payload(_payload(385))

    assert verdict is not None
    assert verdict.action is Action.STOP_THREAD
    assert verdict.code == 385


def test_unknown_code_parks_instead_of_retrying() -> None:
    verdict = classify_error_payload(_payload(999, "something new"))

    assert verdict is not None
    assert verdict.action is Action.PARK
    assert verdict.kind == "other"
    assert verdict.code == 999
    assert verdict.reason == "unrecognised X error code 999 — stopping rather than retrying"


def test_unknown_code_reason_and_exception_are_operator_actionable() -> None:
    verdict = classify_error_payload(_payload(12345))

    assert verdict is not None
    error = to_exception(verdict)

    assert isinstance(error, ChallengeError)
    assert "unrecognised X error code 12345" in str(error)
    assert "xposter doctor" in str(error)


def test_unknown_message_parks_instead_of_retrying() -> None:
    verdict = classify_error_payload({"errors": [{"message": "a brand new failure"}]})

    assert verdict is not None
    assert verdict.action is Action.PARK
    assert verdict.kind == "other"
    assert verdict.code is None
    assert verdict.reason == "unrecognised X error message — stopping rather than retrying"


@pytest.mark.parametrize(
    ("message", "action", "kind"),
    [
        (
            "This request looks like it might be automated. Try again later.",
            Action.PARK,
            "automation",
        ),
        ("Your account has been locked.", Action.PARK, "locked"),
        ("This account has been suspended.", Action.PARK, "suspended"),
        ("We noticed unusual activity on this account.", Action.PARK, "challenge"),
        ("Verify you are a human to continue.", Action.PARK, "challenge"),
        ("You are over the daily limit for sending posts.", Action.PARK, "rate_limit"),
        ("You have reached your limit for now.", Action.PARK, "rate_limit"),
        ("Sorry, you are rate limited. Please wait.", Action.RETRY, "rate_limit"),
        ("Something went wrong. Try again.", Action.RETRY, "server"),
    ],
)
def test_message_only_payloads(message: str, action: Action, kind: str) -> None:
    verdict = classify_error_payload({"errors": [{"message": message}]})

    assert verdict is not None
    assert verdict.action is action
    assert verdict.kind == kind
    assert verdict.code is None
    assert verdict.reason


def test_graphql_extensions_code_is_read() -> None:
    verdict = classify_error_payload({"errors": [{"message": "nope", "extensions": {"code": 226}}]})

    assert verdict is not None
    assert verdict.action is Action.PARK
    assert verdict.code == 226


@pytest.mark.parametrize(
    ("payload", "action"),
    [
        ({"error": {"code": 226}}, Action.PARK),
        ({"data": {"create_tweet": {"errors": [{"code": 385}]}}}, Action.STOP_THREAD),
        ({"response": {"errors": [{"code": 64}]}}, Action.PARK),
        (
            {"errors": [{"extensions": {"errors": [{"code": 187}]}}]},
            Action.REJECT,
        ),
    ],
)
def test_nested_graphql_shapes(payload: dict[str, Any], action: Action) -> None:
    verdict = classify_error_payload(payload)

    assert verdict is not None
    assert verdict.action is action


@pytest.mark.parametrize(
    ("codes", "action"),
    [
        ([88, 226], Action.PARK),
        ([187, 385], Action.STOP_THREAD),
        ([131, 89], Action.RELOGIN),
        ([353, 187], Action.REJECT),
        ([88, 353], Action.REFRESH_CSRF),
        ([131, 88], Action.RETRY),
    ],
)
def test_the_most_severe_error_wins(codes: list[int], action: Action) -> None:
    verdict = classify_error_payload({"errors": [{"code": code} for code in codes]})

    assert verdict is not None
    assert verdict.action is action


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"errors": []},
        {"errors": None},
        {"errors": "boom"},
        {"errors": [None, 1, "x"]},
        {"errors": [{"code": "abc", "message": 5}]},
        {"data": {}},
        {"errors": {}},
    ],
)
def test_odd_payloads_return_none(payload: dict[str, Any]) -> None:
    assert classify_error_payload(payload) is None


def test_non_mapping_payload_returns_none() -> None:
    assert classify_error_payload(None) is None  # type: ignore[arg-type]
    assert classify_error_payload([]) is None  # type: ignore[arg-type]


def test_all_verdicts_have_known_kinds_and_reasons() -> None:
    payloads = [_payload(code) for code, _ in PARK_CODES] + [
        _payload(88),
        _payload(131),
        _payload(32),
        _payload(353),
        _payload(186),
        _payload(187),
        _payload(385),
        _payload(999),
        {"errors": [{"message": "something went wrong"}]},
    ]
    for payload in payloads:
        verdict = classify_error_payload(payload)
        assert verdict is not None
        assert verdict.kind in ALLOWED_KINDS
        assert verdict.reason
        assert verdict.code is None or isinstance(verdict.code, int)


@pytest.mark.parametrize(
    ("status", "action", "kind"),
    [
        (401, Action.RELOGIN, "auth"),
        (403, Action.PARK, "challenge"),
        (429, Action.PARK, "rate_limit"),
        (500, Action.RETRY, "server"),
        (502, Action.RETRY, "server"),
        (599, Action.RETRY, "server"),
        (200, None, None),
        (204, None, None),
        (301, None, None),
        (400, None, None),
        (404, None, None),
    ],
)
def test_http_status(status: int, action: Action | None, kind: str | None) -> None:
    verdict = classify_http_status(status)

    if action is None:
        assert verdict is None
        return
    assert verdict is not None
    assert verdict.action is action
    assert verdict.kind == kind
    assert verdict.code == status


def test_bare_http_403_is_a_behavioural_block() -> None:
    verdict = classify_http_status(403)

    assert verdict is not None
    assert verdict.action is Action.PARK
    assert verdict.kind == "challenge"
    assert verdict.reason == (
        "HTTP 403 with no recognisable error body — behavioural block; stopping"
    )


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 409, 418, 422, 429])
def test_non_5xx_http_status_never_retries(status: int) -> None:
    verdict = classify_http_status(status)

    assert verdict is None or verdict.action is not Action.RETRY


@pytest.mark.parametrize(
    ("status", "payload", "action", "kind"),
    [
        (403, None, Action.PARK, "challenge"),
        (403, {"errors": []}, Action.PARK, "challenge"),
        (403, {"html": "cloudflare"}, Action.PARK, "challenge"),
        (403, {"errors": [{"code": 32}]}, Action.RELOGIN, "auth"),
        (403, {"errors": [{"code": 89}]}, Action.RELOGIN, "auth"),
        (403, {"errors": [{"code": 135}]}, Action.RELOGIN, "auth"),
        (403, {"errors": [{"code": 215}]}, Action.RELOGIN, "auth"),
        (403, {"errors": [{"code": 353}]}, Action.REFRESH_CSRF, "auth"),
        (403, {"errors": [{"code": 187}]}, Action.REJECT, "duplicate"),
        (403, {"errors": [{"code": 186}]}, Action.REJECT, "other"),
        (403, {"errors": [{"code": 226}]}, Action.PARK, "automation"),
        (403, {"errors": [{"code": 88}]}, Action.PARK, "challenge"),
        (401, None, Action.RELOGIN, "auth"),
        (401, {"errors": [{"code": 226}]}, Action.PARK, "automation"),
        (429, None, Action.PARK, "rate_limit"),
        (429, {"errors": [{"code": 89}]}, Action.PARK, "rate_limit"),
        (500, None, Action.RETRY, "server"),
        (503, {"errors": [{"code": 64}]}, Action.PARK, "suspended"),
        (200, None, None, None),
        (200, {"errors": [{"code": 226}]}, Action.PARK, "automation"),
        (204, {"errors": []}, None, None),
    ],
)
def test_classify_response_combines_status_and_body(
    status: int, payload: dict[str, Any] | None, action: Action | None, kind: str | None
) -> None:
    verdict = classify_response(status, payload)

    if action is None:
        assert verdict is None
        return
    assert verdict is not None
    assert verdict.action is action
    assert verdict.kind == kind


def test_classify_response_tolerates_unparseable_bodies() -> None:
    assert classify_response(200, None) is None
    assert classify_response(200, "html") is None  # type: ignore[arg-type]
    assert classify_response(403, "html") is not None  # type: ignore[arg-type]
    assert classify_response(403, None).action is Action.PARK  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("url", "action", "kind"),
    [
        ("https://x.com/i/flow/login", Action.RELOGIN, "auth"),
        ("https://x.com/login?next=%2Fhome", Action.RELOGIN, "auth"),
        ("https://x.com/logout", Action.RELOGIN, "auth"),
        (
            "https://x.com/i/jf/onboarding/web?mode=login&redirect_after_login=%2F",
            Action.RELOGIN,
            "auth",
        ),
        ("https://x.com/account/access", Action.PARK, "locked"),
        ("https://x.com/i/flow/consent?foo=1", Action.PARK, "locked"),
        ("https://x.com/account/login_challenge", Action.PARK, "challenge"),
        ("https://x.com/i/flow/challenge", Action.PARK, "challenge"),
        ("https://x.com/account/suspended", Action.PARK, "suspended"),
        ("https://x.com/account/suspended/appeal", Action.PARK, "suspended"),
        ("https://x.com/suspended", Action.PARK, "suspended"),
        ("https://x.com/i/flow/login/step2", Action.RELOGIN, "auth"),
        ("https://x.com/account/access/verify", Action.PARK, "locked"),
        ("https://x.com/home", None, None),
        ("https://x.com/home?next=/login", None, None),
        ("https://x.com/search?q=%2Flogin", None, None),
        ("https://x.com/settings/login_alerts", None, None),
        ("https://example.com/notices?redirect=/account/access", None, None),
        ("", None, None),
        ("not a url", None, None),
    ],
)
def test_url_classification(url: str, action: Action | None, kind: str | None) -> None:
    verdict = classify_url(url)

    if action is None:
        assert verdict is None
        return
    assert verdict is not None
    assert verdict.action is action
    assert verdict.kind == kind


@pytest.mark.parametrize(
    ("text", "action", "kind"),
    [
        ("Your account has been locked.", Action.PARK, "locked"),
        ("ACCOUNT IS LOCKED", Action.PARK, "locked"),
        ("This account has been suspended.", Action.PARK, "suspended"),
        ("Account suspended", Action.PARK, "suspended"),
        ("Verify you are a human to continue", Action.PARK, "challenge"),
        ("We saw some unusual activity", Action.PARK, "challenge"),
        ("Confirm your identity to continue", Action.PARK, "challenge"),
        ("Authenticate to continue", Action.PARK, "challenge"),
        ("This request looks like it might be automated", Action.PARK, "automation"),
        ("The account is temporarily blocked from sending posts", Action.PARK, "automation"),
        ("You are over the daily limit for sending posts.", Action.PARK, "rate_limit"),
        ("You have reached your limit", Action.PARK, "rate_limit"),
        ("Something went wrong. Try again.", Action.RETRY, "server"),
        ("Could not log you in", Action.PARK, "challenge"),
        ("", None, None),
        ("   ", None, None),
        ("Just an ordinary post about research", None, None),
    ],
)
def test_text_classification(text: str, action: Action | None, kind: str | None) -> None:
    verdict = classify_text(text)

    if action is None:
        assert verdict is None
        return
    assert verdict is not None
    assert verdict.action is action
    assert verdict.kind == kind
    assert verdict.code is None


@pytest.mark.parametrize(
    ("text", "action", "kind"),
    [
        ("Your account has been locked.", Action.PARK, "locked"),
        ("Account is locked", Action.PARK, "locked"),
        ("Account suspended", Action.PARK, "suspended"),
        ("This account has been suspended", Action.PARK, "suspended"),
        ("Verify you are a human", Action.PARK, "challenge"),
        ("Confirm your identity", Action.PARK, "challenge"),
        ("This request looks like it might be automated", Action.PARK, "automation"),
        ("You are over the daily limit for sending posts.", Action.PARK, "rate_limit"),
        ("Daily limit reached", Action.PARK, "rate_limit"),
    ],
)
def test_strict_text_uses_high_precision_phrases(text: str, action: Action, kind: str) -> None:
    verdict = classify_text(text, strict=True)

    assert verdict is not None
    assert verdict.action is action
    assert verdict.kind == kind
    assert verdict.code is None


@pytest.mark.parametrize(
    "text",
    [
        "Solve this captcha to authenticate your account",
        "Something went wrong. Try again.",
        "We saw some unusual activity",
        "Could not log you in",
        "Authenticate to continue",
        "CAPTCHA",
    ],
)
def test_strict_text_ignores_broad_phrases(text: str) -> None:
    assert classify_text(text, strict=True) is None


def test_broad_text_still_covers_the_login_probe() -> None:
    verdict = classify_text("Solve this captcha to authenticate your account")

    assert verdict is not None
    assert verdict.action is Action.PARK
    assert verdict.kind == "challenge"


@pytest.mark.parametrize(
    ("verdict", "error_type"),
    [
        (Verdict(Action.PARK, 226, "automation block", "automation"), ChallengeError),
        (Verdict(Action.PARK, 315, "captcha", "challenge"), ChallengeError),
        (Verdict(Action.PARK, 27, "locked", "locked"), AccountRestrictedError),
        (Verdict(Action.PARK, 64, "suspended", "suspended"), AccountRestrictedError),
        (Verdict(Action.PARK, 429, "rate limited", "rate_limit"), RateLimitedError),
        (Verdict(Action.PARK, None, "stop", "other"), ChallengeError),
        (Verdict(Action.RELOGIN, None, "session invalid", "auth"), NotLoggedInError),
        (Verdict(Action.REJECT, 187, "duplicate", "duplicate"), PostRejectedError),
        (Verdict(Action.STOP_THREAD, 385, "target gone", "other"), PostRejectedError),
    ],
)
def test_to_exception_mapping(verdict: Verdict, error_type: type[XPosterError]) -> None:
    error = to_exception(verdict)

    assert isinstance(error, error_type)
    assert str(error)


def test_to_exception_reject_code_and_relogin_guidance() -> None:
    rejected = to_exception(Verdict(Action.REJECT, 187, "duplicate", "duplicate"))
    assert isinstance(rejected, PostRejectedError)
    assert rejected.code == 187

    relogin = to_exception(Verdict(Action.RELOGIN, None, "session invalid", "auth"))
    assert isinstance(relogin, NotLoggedInError)
    assert "xposter login" in str(relogin)

    already = Verdict(Action.RELOGIN, None, "Run `xposter login`.", "auth")
    assert str(to_exception(already)).count("xposter login") == 1


@pytest.mark.parametrize(
    "verdict",
    [
        Verdict(Action.RETRY, 88, "retry", "rate_limit"),
        Verdict(Action.REFRESH_CSRF, 353, "refresh", "auth"),
    ],
)
def test_retry_and_refresh_are_not_exceptions(verdict: Verdict) -> None:
    assert to_exception(verdict) is None


def test_post_rejected_error_is_not_a_park_condition() -> None:
    error = PostRejectedError("X refused the post.", code=187)

    assert isinstance(error, XPosterError)
    assert error.code == 187
    assert PostRejectedError("X refused the post.").code is None


def test_reasons_and_exceptions_never_echo_cookie_values() -> None:
    secret = "abc123secret"
    verdicts: list[Verdict] = [
        Verdict(Action.PARK, 226, "automation block", "automation"),
        Verdict(Action.RELOGIN, None, "session invalid", "auth"),
        Verdict(Action.REJECT, 187, "duplicate", "duplicate"),
    ]
    payloads = [
        {"errors": [{"code": 999, "message": f"auth_token={secret} rejected"}]},
        {"errors": [{"message": f"ct0={secret} something went wrong"}]},
        {"errors": [{"message": f"Your account has been locked auth_token={secret}"}]},
        {"errors": [{"message": f"ct0={secret} a brand new failure"}]},
    ]
    for payload in payloads:
        verdict = classify_error_payload(payload)
        assert verdict is not None
        verdicts.append(verdict)
    for text in (
        f"Your account has been locked. auth_token={secret}",
        f"Something went wrong. ct0={secret}",
        f"Solve this captcha to authenticate. auth_token={secret}",
    ):
        verdict = classify_text(text)
        assert verdict is not None
        verdicts.append(verdict)
        strict_verdict = classify_text(text, strict=True)
        if strict_verdict is not None:
            verdicts.append(strict_verdict)
    url_verdict = classify_url(f"https://x.com/login?auth_token={secret}")
    assert url_verdict is not None
    verdicts.append(url_verdict)
    for status in (403, 429):
        status_verdict = classify_http_status(status)
        assert status_verdict is not None
        verdicts.append(status_verdict)

    for verdict in verdicts:
        assert secret not in verdict.reason
        assert "auth_token" not in verdict.reason
        assert "ct0" not in verdict.reason
        error = to_exception(verdict)
        if error is not None:
            assert secret not in str(error)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.x.com/graphql/8TlLvl0sHLceb7rlGZZ6bg/CreateTweet", True),
        ("https://x.com/i/api/graphql/abc123/CreateTweet?variables=%7B%7D", True),
        ("https://api.x.com/graphql/abc/createTweetNoteTweetMutation", True),
        ("https://api.x.com/graphql/abc/CreateTweet/", True),
        ("/graphql/abc/CreateTweet", True),
        ("https://api.x.com/graphql/abc/DeleteTweet", False),
        ("https://example.com/graphql/abc/CreateTweet", False),
        ("https://api.x.com.evil.com/graphql/abc/CreateTweet", False),
        ("https://x.com/home", False),
        ("", False),
    ],
)
def test_create_tweet_response_detection(url: str, expected: bool) -> None:
    assert is_create_tweet_response(url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.x.com/graphql/abc/DeleteTweet", True),
        ("https://x.com/i/api/graphql/abc/DeleteTweet", True),
        ("/graphql/abc/DeleteTweet", True),
        ("https://api.x.com/graphql/abc/CreateTweet", False),
        ("https://example.com/graphql/abc/DeleteTweet", False),
        ("https://x.com/home", False),
        ("", False),
    ],
)
def test_delete_tweet_response_detection(url: str, expected: bool) -> None:
    assert is_delete_tweet_response(url) is expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"data": {"create_tweet": {"tweet_results": {"rest_id": "123"}}}},
            "123",
        ),
        (
            {"data": {"create_tweet": {"tweet_results": {"result": {"rest_id": "456"}}}}},
            "456",
        ),
        (
            {
                "data": {
                    "create_tweet": {"tweet_results": {"result": {"tweet": {"rest_id": "789"}}}}
                }
            },
            "789",
        ),
        (
            {"data": {"notetweet_create": {"tweet_results": {"result": {"rest_id": "111"}}}}},
            "111",
        ),
        (
            {"data": {"notetweet_create": {"tweet_results": {"rest_id": "222"}}}},
            "222",
        ),
        (
            {
                "data": {
                    "notetweet_create": {"tweet_results": {"result": {"tweet": {"rest_id": "333"}}}}
                }
            },
            "333",
        ),
    ],
)
def test_extract_created_tweet_id_shapes(payload: dict[str, Any], expected: str) -> None:
    assert extract_created_tweet_id(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": {}},
        {"data": {"create_tweet": {}}},
        {"data": {"create_tweet": {"tweet_results": None}}},
        {"data": {"create_tweet": {"tweet_results": {"result": "junk"}}}},
        {"data": {"create_tweet": {"tweet_results": {"rest_id": "abc"}}}},
        {"data": {"create_tweet": {"tweet_results": {"rest_id": 123}}}},
        {"data": {"create_tweet": {"tweet_results": {"rest_id": "12a"}}}},
        {"data": {"create_tweet": {"tweet_results": {"rest_id": ""}}}},
        {"data": {"create_tweet": {"tweet_results": {"rest_id": True}}}},
        {"data": {"create_tweet": {"tweet_results": {"rest_id": "-1"}}}},
    ],
)
def test_extract_created_tweet_id_junk(payload: dict[str, Any]) -> None:
    assert extract_created_tweet_id(payload) is None


def test_extract_created_tweet_id_never_raises_on_non_mapping() -> None:
    assert extract_created_tweet_id(None) is None  # type: ignore[arg-type]
    assert extract_created_tweet_id("junk") is None  # type: ignore[arg-type]
