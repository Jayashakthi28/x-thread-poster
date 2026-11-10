"""An offline, in-process fake of x.com for end-to-end driver tests.

``FakeX.install`` registers a single ``context.route("**/*")`` handler on a real
Chrome context. Requests to x.com hosts are served from :mod:`tests.fake_x.pages`
or from tiny JSON stubs; every other host is aborted and recorded so a test can
prove that nothing leaked to the network. No process, socket or DNS lookup is
needed, which keeps the whole driver exercisable while offline.

The fake mirrors the observed ``x-web`` client (see
``docs/research/web-client.md``): a native ``<textarea>`` composer, sequential
``CreateTweet`` requests chained through ``in_reply_to_tweet_id``, the
``data.create_tweet.tweet_results.rest_id`` success path, and GraphQL error
codes from the observed error map. It also renders the classic ``data-testid``
DOM when ``dom="legacy"`` so the driver's fallback selectors can be exercised.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Request, Route

from . import pages
from .pages import weighted_length

__all__ = ["FakeTweet", "FakeX", "Scenario", "weighted_length"]

CREATE_QUERY_ID = "8TlLvl0sHLceb7rlGZZ6bg"
NOTE_QUERY_ID = "Nff4GppYFB2cZhIO5UZi_A"

_ALLOWED_HOSTS = frozenset({"x.com", "www.x.com", "api.x.com", "upload.x.com"})
_API_HOSTS = frozenset({"x.com", "www.x.com", "api.x.com"})
_LOGIN_PATHS = frozenset({"/i/flow/login", "/i/jf/onboarding/web"})
_OPERATION_RE = re.compile(r"/(?:i/api/)?graphql/[^/]+/([A-Za-z]+)$")
_PROFILE_RE = re.compile(r"/([^/]+)")
_STATUS_RE = re.compile(r"/([^/]+)/status/([^/]+)")

_ID_BASE = 10**18
_MEDIA_BASE = 10**15


class Scenario(StrEnum):
    """The behaviours the fake can simulate, named after the driver's outcomes."""

    OK = "ok"
    LOGGED_OUT = "logged_out"
    LOCKED = "locked"
    SUSPENDED = "suspended"
    DUPLICATE = "duplicate"
    DAILY_LIMIT = "daily_limit"
    AUTOMATED = "automated"
    CHALLENGE = "challenge"
    CHALLENGE_IFRAME = "challenge_iframe"
    FORBIDDEN_226 = "forbidden_226"
    FORBIDDEN_HTML = "forbidden_html"
    UNKNOWN_CODE = "unknown_code"
    UI_DRIFT = "ui_drift"
    SERVER_ERROR_ONCE = "server_error_once"
    FAIL_AT_N = "fail_at_n"
    SLOW = "slow"
    UPLOAD_FAILS = "upload_fails"
    UPLOAD_HANGS = "upload_hangs"
    ALT_UI_MISSING = "alt_ui_missing"
    ALT_INTRO = "alt_intro"


@dataclass
class FakeTweet:
    """One tweet the fake has accepted; ids increase so chains are checkable."""

    id: str
    text: str
    author: str
    in_reply_to: str | None
    media: tuple[str, ...]
    deleted: bool = False


class FakeX:
    """Configurable offline x.com served through a Playwright route.

    Page-level scenarios (``UI_DRIFT`` changes markup, ``CHALLENGE`` is decided
    by the composer's inline JS) are baked into the HTML when it is served, so
    set them before navigating. Response-level scenarios (duplicate, limits,
    fail-at-n, slow, server error) are read per request and can be changed with
    :meth:`set_scenario` while a page is open. ``FAIL_AT_N`` and
    ``SERVER_ERROR_ONCE`` fire once per activation so a forced retry, which the
    driver treats as a fresh batch, can succeed; ``reset`` re-arms them.
    """

    def __init__(
        self,
        *,
        handle: str = "olsa_test",
        dom: Literal["new", "legacy"] = "new",
        scenario: Scenario = Scenario.OK,
        fail_at: int = 2,
        latency_ms: int = 0,
        max_weight: int = 280,
        upload_ms: int = 300,
    ) -> None:
        if dom not in ("new", "legacy"):
            raise ValueError(f"dom must be 'new' or 'legacy', not {dom!r}")
        self.handle = handle.lstrip("@")
        self.dom: Literal["new", "legacy"] = dom
        self.scenario = Scenario(scenario)
        self.fail_at = int(fail_at)
        self.latency_ms = max(0, int(latency_ms))
        self.max_weight = int(max_weight)
        self.upload_ms = max(0, int(upload_ms))

        self.tweets: list[FakeTweet] = []
        self.create_requests: list[dict[str, Any]] = []
        self.visits: list[str] = []
        self.blocked: list[str] = []
        self.deleted: list[str] = []

        self._texts: set[str] = set()
        self._id_counter = 1
        self._media_counter = 1
        self._batch_count = 0
        self._server_error_served = False
        self._fail_at_fired = False
        self._media_names: dict[str, str] = {}
        self._installed: set[int] = set()

    # ------------------------------------------------------------------ setup

    async def install(self, context: BrowserContext) -> None:
        """Intercept every request in ``context``; call before navigating.

        Installing twice on the same context is a no-op, while a fresh context
        (for example after a profile restart) gets the route again. This keeps
        ``install`` safe for tests that exercise session persistence.
        """
        key = id(context)
        if key in self._installed:
            return
        await context.route("**/*", self._handle)
        self._installed.add(key)

    def set_scenario(self, scenario: Scenario, **kw: Any) -> None:
        """Switch behaviour; ``fail_at``/``latency_ms``/``max_weight`` override.

        Attempt counters reset so a scenario such as ``SERVER_ERROR_ONCE`` can
        be replayed without rebuilding the fake. Page-level scenarios need a
        reload (or a fresh navigation) because their markup is already served.
        """
        self.scenario = Scenario(scenario)
        for name in ("fail_at", "latency_ms", "max_weight", "upload_ms"):
            if name in kw:
                value = int(kw.pop(name))
                clamped = name in ("latency_ms", "upload_ms", "max_weight")
                setattr(self, name, max(0, value) if clamped else value)
        if "handle" in kw:
            self.handle = str(kw.pop("handle")).lstrip("@")
        if "dom" in kw:
            dom = kw.pop("dom")
            if dom not in ("new", "legacy"):
                raise ValueError(f"dom must be 'new' or 'legacy', not {dom!r}")
            self.dom = dom
        if kw:
            raise TypeError(f"unknown FakeX option(s): {', '.join(sorted(kw))}")
        self._batch_count = 0
        self._server_error_served = False
        self._fail_at_fired = False
        self._media_names.clear()

    def reset(self) -> None:
        """Forget tweets, requests, visits and counters; keep the scenario."""
        self.tweets.clear()
        self.create_requests.clear()
        self.visits.clear()
        self.blocked.clear()
        self.deleted.clear()
        self._texts.clear()
        self._id_counter = 1
        self._media_counter = 1
        self._batch_count = 0
        self._server_error_served = False
        self._fail_at_fired = False
        self._media_names.clear()

    def tweet_by_id(self, id: str) -> FakeTweet | None:
        """Return a tweet by id, including deleted ones, or ``None``."""
        return next((tweet for tweet in self.tweets if tweet.id == id), None)

    # --------------------------------------------------------------- routing

    async def _handle(self, route: Route) -> None:
        """Dispatch one intercepted request; this is the whole server."""
        request = route.request
        url = request.url
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.rstrip("/") or "/"

        if host not in _ALLOWED_HOSTS:
            self.blocked.append(url)
            await route.abort()
            return

        if request.method == "OPTIONS":
            await route.fulfill(status=204, headers=self._cors(request), body="")
            return

        if host in _API_HOSTS and _is_telemetry(path):
            await route.fulfill(status=204, headers={}, body="")
            return

        if host == "upload.x.com" or _is_media_path(path):
            await self._upload(route, request)
            return

        operation = _operation_name(path)
        if operation in ("CreateTweet", "createTweetNoteTweetMutation"):
            await self._create_tweet(route, request)
            return
        if operation == "DeleteTweet":
            await self._delete_tweet(route, request)
            return

        if request.resource_type == "document" or request.is_navigation_request():
            await self._document(route, url, path)
            return

        if path == "/favicon.ico":
            await route.fulfill(status=404, headers={}, body="")
            return
        await route.fulfill(
            status=404,
            headers={"content-type": "text/plain; charset=utf-8"},
            body="fake x: unhandled request",
        )

    async def _document(self, route: Route, url: str, path: str) -> None:
        """Serve an HTML page, recording the visit; apply parked-account redirects."""
        self.visits.append(url)
        redirect = self._redirect_target(path)
        if redirect is not None:
            await route.fulfill(
                status=200,
                headers={
                    "content-type": "text/html; charset=utf-8",
                    "cache-control": "no-store",
                },
                body=pages.redirect(self._page_config(), redirect),
            )
            return
        status, body = self._render(path)
        await route.fulfill(
            status=status,
            headers={
                "content-type": "text/html; charset=utf-8",
                "cache-control": "no-store",
            },
            body=body,
        )

    def _redirect_target(self, path: str) -> str | None:
        """Return the parked-account bounce for this path, if any."""
        if self.scenario == Scenario.LOGGED_OUT:
            if path not in _LOGIN_PATHS:
                return "/i/flow/login"
        elif self.scenario == Scenario.LOCKED:
            if path != "/account/access":
                return "/account/access"
        elif self.scenario == Scenario.SUSPENDED and path != "/account/suspended":
            return "/account/suspended"
        return None

    def _render(self, path: str) -> tuple[int, str]:
        """Map a path to ``(status, html)`` for the configured client flavour."""
        cfg = self._page_config()
        if path in ("/", "/home"):
            return 200, pages.home(cfg)
        if path == "/compose/post":
            return 200, pages.composer(cfg)
        if path in _LOGIN_PATHS:
            return 200, pages.login(cfg)
        if path == "/account/access":
            return 200, pages.locked(cfg)
        if path == "/account/suspended":
            return 200, pages.suspended(cfg)
        if path == "/logout":
            return 200, pages.logout(cfg)
        if path == "/i/arkose/challenge":
            return 200, pages.challenge_frame(cfg)
        status_match = _STATUS_RE.fullmatch(path)
        if status_match and status_match.group(1) == self.handle:
            tweet = self.tweet_by_id(status_match.group(2))
            if tweet is not None and not tweet.deleted:
                return 200, pages.status(cfg, tweet)
            return 404, pages.not_found(cfg)
        profile_match = _PROFILE_RE.fullmatch(path)
        if profile_match and profile_match.group(1) == self.handle:
            visible = [tweet for tweet in self.tweets if not tweet.deleted]
            return 200, pages.profile(cfg, visible)
        return 404, pages.not_found(cfg)

    def _page_config(self) -> dict[str, Any]:
        """The config baked into every page served now."""
        return {
            "handle": self.handle,
            "dom": self.dom,
            "scenario": self.scenario.value,
            "max_weight": self.max_weight,
            "ui_drift": self.scenario == Scenario.UI_DRIFT,
        }

    # ------------------------------------------------------------- API stubs

    async def _create_tweet(self, route: Route, request: Request) -> None:
        """Record and answer one ``CreateTweet`` POST."""
        body = _json_body(request)
        variables = body.get("variables") if isinstance(body, dict) else None
        if not isinstance(variables, dict):
            variables = {}
        self.create_requests.append(body if isinstance(body, dict) else {"variables": variables})

        if variables.get("batch_compose") == "BatchFirst":
            self._batch_count = 0
        self._batch_count += 1

        if self.scenario == Scenario.SLOW:
            await asyncio.sleep(self.latency_ms / 1000.0)

        if self.scenario == Scenario.FORBIDDEN_HTML:
            await route.fulfill(
                status=403,
                headers={
                    "content-type": "text/html; charset=utf-8",
                    **self._cors(request),
                },
                body=(
                    "<!doctype html><title>Request blocked</title><p>This request was blocked.</p>"
                ),
            )
            return

        text = str(variables.get("tweet_text", ""))
        error = self._create_error(text)
        if error is not None:
            code, message, status = error
            await self._json(
                route,
                request,
                status,
                {"errors": [{"code": code, "message": message}], "data": {}},
            )
            return

        media_ids = _media_ids(variables)
        tweet = FakeTweet(
            id=self._new_tweet_id(),
            text=text,
            author=self.handle,
            in_reply_to=_reply_target(variables),
            media=tuple(self._media_names.get(media_id, media_id) for media_id in media_ids),
        )
        self.tweets.append(tweet)
        self._texts.add(text)
        await self._json(route, request, 200, self._success_payload(tweet))

    def _create_error(self, text: str) -> tuple[int, str, int] | None:
        """Return ``(code, message, http_status)`` for the active scenario."""
        if self.scenario == Scenario.SERVER_ERROR_ONCE and not self._server_error_served:
            self._server_error_served = True
            return 131, "Something went wrong. Please try again.", 500
        if self.scenario == Scenario.DUPLICATE and text in self._texts:
            return 187, "Whoops! You already said that.", 200
        if self.scenario == Scenario.DAILY_LIMIT:
            return (
                344,
                "You have reached the daily limit for sending posts. "
                "Add a phone number to continue posting.",
                200,
            )
        if self.scenario == Scenario.AUTOMATED:
            return (
                226,
                "This request looks like it might be automated. To protect our "
                "users from spam and other malicious activity, we can't complete "
                "this action right now. Please try again later.",
                200,
            )
        if self.scenario == Scenario.FORBIDDEN_226:
            return (
                226,
                "This request looks like it might be automated. To protect our "
                "users from spam and other malicious activity, we can't complete "
                "this action right now. Please try again later.",
                403,
            )
        if self.scenario == Scenario.UNKNOWN_CODE:
            return (
                261,
                "The App is in read-only mode. Please try again later.",
                200,
            )
        if (
            self.scenario == Scenario.FAIL_AT_N
            and not self._fail_at_fired
            and self.fail_at > 0
            and self._batch_count == self.fail_at
        ):
            self._fail_at_fired = True
            return (
                385,
                "The post you are trying to reply to has been deleted or is not visible.",
                200,
            )
        return None

    def _success_payload(self, tweet: FakeTweet) -> dict[str, Any]:
        """Build the exact success shape the client reads ids from.

        ``docs/research/web-client.md`` §2.3 shows the client reading
        ``data.create_tweet.tweet_results.rest_id`` (with ``result.rest_id`` as a
        fallback), so both levels carry the id. Premium long posts use the
        separate ``notetweet_create`` field, faithful to the observed operation.
        """
        legacy = {
            "full_text": tweet.text,
            "id_str": tweet.id,
            "in_reply_to_status_id_str": tweet.in_reply_to,
        }
        result = {
            "__typename": "Tweet",
            "rest_id": tweet.id,
            "legacy": legacy,
            "note_tweet": (
                {"note_tweet_results": {"result": {"id": tweet.id, "text": tweet.text}}}
                if weighted_length(tweet.text) > 280
                else None
            ),
        }
        tweet_results = {"rest_id": tweet.id, "result": result}
        if self.max_weight > 280 and weighted_length(tweet.text) > 280:
            return {"data": {"notetweet_create": {"tweet_results": tweet_results}}}
        return {"data": {"create_tweet": {"tweet_results": tweet_results}}}

    async def _delete_tweet(self, route: Route, request: Request) -> None:
        """Mark a tweet deleted and answer ``DeleteTweet``."""
        body = _json_body(request)
        variables = body.get("variables") if isinstance(body, dict) else None
        if not isinstance(variables, dict):
            variables = {}
        raw_id = variables.get("tweet_id") or variables.get("id") or ""
        tweet = self.tweet_by_id(str(raw_id))
        if tweet is None or tweet.deleted:
            await self._json(
                route,
                request,
                200,
                {
                    "errors": [{"code": 144, "message": "No status found with that ID."}],
                    "data": {},
                },
            )
            return
        tweet.deleted = True
        self.deleted.append(tweet.id)
        await self._json(
            route,
            request,
            200,
            {"data": {"delete_tweet": {"tweet_results": {}}}},
        )

    async def _upload(self, route: Route, request: Request) -> None:
        """Answer one media upload, with the configured delay and failures."""
        if self.upload_ms:
            await asyncio.sleep(self.upload_ms / 1000.0)
        if self.scenario == Scenario.UPLOAD_HANGS:
            # Never answer: the poster's upload wait must time out pre-click.
            await asyncio.sleep(3600.0)
            return
        if self.scenario == Scenario.UPLOAD_FAILS:
            await self._json(
                route,
                request,
                400,
                {"errors": [{"code": 191, "message": "Media upload failed."}]},
            )
            return
        payload = self._media_payload()
        name = _uploaded_file_name(request)
        if name:
            self._media_names[str(payload["media_id_string"])] = name
        await self._json(route, request, 200, payload)

    def _media_payload(self) -> dict[str, Any]:
        """One upload step answered with a fresh ``media_id_string``."""
        media_id = str(_MEDIA_BASE + self._media_counter)
        self._media_counter += 1
        return {
            "media_id": int(media_id),
            "media_id_string": media_id,
            "media_key": f"7_{media_id}",
            "expires_after_secs": 86400,
            "size": 0,
        }

    # --------------------------------------------------------------- helpers

    def _new_tweet_id(self) -> str:
        """Return a monotonically increasing 19-digit id."""
        value = str(_ID_BASE + self._id_counter)
        self._id_counter += 1
        return value

    def _cors(self, request: Request) -> dict[str, str]:
        """CORS headers needed for the page's cross-origin ``api.x.com`` fetches."""
        origin = request.headers.get("origin", "https://x.com")
        return {
            "access-control-allow-origin": origin,
            "access-control-allow-methods": "POST, OPTIONS",
            "access-control-allow-headers": "content-type",
            "access-control-allow-credentials": "true",
            "vary": "origin",
        }

    async def _json(
        self,
        route: Route,
        request: Request,
        status: int,
        payload: dict[str, Any],
    ) -> None:
        """Fulfil with a JSON body and the CORS headers the browser requires."""
        headers = {
            "content-type": "application/json; charset=utf-8",
            **self._cors(request),
        }
        await route.fulfill(
            status=status,
            headers=headers,
            body=json.dumps(payload, separators=(",", ":")),
        )


def _operation_name(path: str) -> str | None:
    """Extract ``CreateTweet``/``DeleteTweet`` from both graphql URL shapes."""
    match = _OPERATION_RE.search(path)
    return match.group(1) if match else None


def _is_telemetry(path: str) -> bool:
    """Scribe/telemetry paths answer 204 and never touch a handler."""
    return (
        path.startswith("/i/api/1.1/jot/")
        or path.startswith("/1.1/jot/")
        or path.endswith("/viewer.json")
        or path.endswith("/viewer_context.json")
    )


def _is_media_path(path: str) -> bool:
    """True for the two media-upload URL families from the bundle study."""
    return path.startswith("/1.1/media/") or path.startswith("/i/media/")


def _json_body(request: Request) -> dict[str, Any]:
    """Parse a JSON request body defensively; malformed bodies become ``{}``."""
    raw = request.post_data
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _reply_target(variables: dict[str, Any]) -> str | None:
    """Read ``variables.reply.in_reply_to_tweet_id`` as a string or ``None``."""
    reply = variables.get("reply")
    if isinstance(reply, dict):
        target = reply.get("in_reply_to_tweet_id")
        if target:
            return str(target)
    return None


def _uploaded_file_name(request: Request) -> str | None:
    """Read the fake-only ``fake_file_name`` multipart field from the upload."""
    raw = request.post_data or ""
    match = re.search(r'name="fake_file_name"\r?\n\r?\n([^\r\n]+)', raw)
    return match.group(1) if match else None


def _media_ids(variables: dict[str, Any]) -> tuple[str, ...]:
    """Read the media entity ids the page attached to this tweet."""
    media = variables.get("media")
    if not isinstance(media, dict):
        return ()
    entities = media.get("media_entities")
    if not isinstance(entities, list):
        return ()
    return tuple(
        str(entity["media_id"])
        for entity in entities
        if isinstance(entity, dict) and entity.get("media_id")
    )
