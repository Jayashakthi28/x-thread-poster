# x-thread-poster

Post a thread to X (Twitter) by driving a real Google Chrome window with a saved,
one-time login. The package is standalone: it does not import the OLSA pipeline
and can be tested, installed and run on its own. See
[`docs/integration.md`](docs/integration.md) for how the unmerged OLSA branch
`feat/x-thread-publish` integrates it (OLSA ADR-0085); no pipeline-driven post
has been run live yet.

## Read this first: policy and account risk

**X's Automation Rules prohibit this way of posting.** The rule, updated April
2026, says:

> "Use non-API-based forms of automation, such as scripting the X website. The
> use of these techniques may result in the permanent suspension of your
> account."
> — <https://help.x.com/en/rules-and-policies/x-automation>

This tool does exactly that: a script drives `x.com`'s web UI to publish posts.
It is **not** a grey area about speed or fingerprints. The mechanism itself is
named, and the quoted consequence is permanent suspension. X's Authenticity
policy also lists "Anti-spam challenges" (locks), "Temporary loss of access" and
"Suspension", and says severe violations can be suspended "at first detection".

What this means in practice:

- This tool **lowers** some detectable signals (real Chrome, a persistent
  profile, human pacing, no CAPTCHA solving or fingerprint spoofing) but it
  **cannot remove the policy risk**. No pacing profile, browser choice, session
  reuse or IP strategy converts a prohibited mechanism into a permitted one.
- **The operator accepted that risk when choosing this design** (decision dated
  2026-11-09; see [`docs/design.md`](docs/design.md), decision 12). That decision
  is not a claim that the account is safe.
- **Use a test account first.** Run the whole flow once on an account you can
  afford to lose before pointing it at the OLSA account.
- **The official X API removes the mechanism risk** — it is the alternative the
  research memo recommends. At the researched rates (2026-11-09; rates can
  change), a `POST /2/tweets` costs $0.015, or $0.200 when the post contains a
  URL. A 25-tweet thread with the link in only the first tweet therefore costs
  about **$0.56**, or about **$2.42/month at one thread per week**. If every
  tweet carries a link it is **$5.00/thread**, about **$21.67/month**. The API
  still has content rules, but it removes the login, session, browser-fingerprint
  and "scripting the website" risks entirely. The numbers and sources are in
  [`docs/research/risk-and-limits.md`](docs/research/risk-and-limits.md).

### Automated mentions have their own rule and cap

X's Automation Rules, section **“Automated mentions and replies”** (April 2026),
say:

> "Automating these actions to reach many users on an unsolicited basis is an
> abuse of the feature, and is not permitted."
> — <https://help.x.com/en/rules-and-policies/x-automation>

Tagging paper authors or organisations from an automated account is therefore an
**unsolicited automated mention** unless those users opted in. It is not made
safe by pacing or by the account being yours.

xposter caps a thread at **5 distinct `@handles`** by default
(`max_mentions_per_thread`). The posting account's own handle is not counted;
repeats count once. The check runs **before any browser starts**. The cap is
deliberately low because the Automation Rules treat unsolicited automated
mentions as abuse. For a test account only, you can raise it deliberately with
the `--max-mentions N` global option or
`XPOSTER_MAX_MENTIONS_PER_THREAD=N`. A wrong tag is public and permanent; a
missing tag is invisible. See [`docs/design.md`](docs/design.md), decision 13.

Typing a mention is safe mechanically but has one live hazard: X opens a
suggestion dropdown for a line-final `@handle`, `#hashtag` or `$cashtag`, and a
following Shift+Enter **selects the suggestion** instead of making the line
break. xposter closes such a token with one plain space before the newline
(never Enter/Tab/Escape), so a line may legitimately end with a trailing space.
The read-back tolerates trailing spaces only. A real post containing the
account's **own** handle was verified on 2026-11-10; mentions of **other**
accounts, notification delivery, `#hashtag`/`$cashtag` typeahead and handles
that do not exist remain **UNVERIFIED live**.

Deliberately **not** implemented, because these make the risk worse rather than
better: CAPTCHA solving, user-agent or fingerprint spoofing, JavaScript
patching, proxy rotation, automatic password login, and multi-account
automation.

## What it is

`xposter` launches the real Google Chrome installed on the machine (or the
executable you set with `XPOSTER_CHROME_PATH`), never Playwright's bundled
Chromium, using a dedicated persistent profile. You sign
in **once, by hand, in a plain Chrome window**; later runs reopen that profile.
The package then:

- splits text with X's *weighted* 280-character rules (URLs count 23, one emoji
  sequence counts 2, Unicode Mathematical Sans-Serif Bold letters count 2 each),
- types the thread into the `/compose/post` composer, or posts it as a chain of
  replies (the `replies` strategy),
- reads the new tweet ids passively from X's own `CreateTweet` responses, never
  by scraping the DOM or the success toast,
- records every step in a local JSON ledger **before and after each click**, so
  a rerun cannot silently double-post,
- also exposes `XPoster.post_chain()` for callers that own the durable record:
  a fresh thread uses the verified composer batch; a non-empty accepted prefix
  resumes reply by reply,
- enforces local pacing and, when configured, quiet hours even when X would
  accept the post, and
- **parks** on a challenge, lock, suspension or rate limit instead of retrying.

Status: functionally complete. The ledger, splitter, guard, pacing, session
store, selectors and poster are covered by offline tests and tests against a
real local Chrome plus an in-process fake of `x.com`. The authenticated live
DOM was the largest unknown; the 2026-11-09/10 test account (`@JayashakthiVis2`,
`~/.olsa/x-test`, Chrome 156–157) served the **classic** composer, which is now
the primary selector target. See [`docs/design.md`](docs/design.md) for the
full verified/unverified matrix.

Live progress (2026-11-09/10, `VERIFIED-LIVE` on the test account): plain-Chrome
login + `verify`; classic DOM; two-composer scoping; real per-key typing;
Shift+Enter blank line; Draft.js read-back; a watched 2-tweet dry run; a real
2-tweet composer thread with ids from X's `CreateTweet` responses, `POSTED`
ledger row, correct reply chain and 30-minute pacing block; `xposter delete`
removed both test tweets; headless `verify` and `refresh --force` worked once
each. Still **UNVERIFIED**: the replies strategy live, alt text live, Premium
note-tweets, `doctor --drafts` live (check the Drafts panel manually, including
for a possible earlier calibration leftover), the live calibration discard
sheet, Xvfb/Linux, 20+ tweet threads, the submit-shortcut fallback, and all
long-term account safety/challenge/rate-limit/12-hour keepalive behaviour. The
policy warning above is unchanged. See the live verification log in
[`docs/design.md`](docs/design.md).

Images (2026-11-10, `VERIFIED-LIVE` on the test account): per-tweet images
attached through the modal's hidden file input, uploaded immediately by X, and
landed in the focused editor; a real 2-tweet thread with one image on each tweet
posted with the reply chain intact (status pages showed 1200×675 and 800×450
images) and was then deleted. Still **UNVERIFIED live**: alt text end to end
(off by default), more than two images or four per tweet, GIF/WebP/JPEG uploads,
near-5 MiB/slow uploads, and images on the replies strategy. Video is
unsupported; xposter never resizes, so the caller must keep images under 5 MiB.

Mentions (2026-11-10, `VERIFIED-LIVE` on the test account, own handle only, so
nobody else was notified): a real 2-tweet composer thread posted every
`@JayashakthiVis2` as a real link (`<a href="/JayashakthiVis2">`: two in tweet 1,
one in tweet 2, including a line-final mention before a blank line and a
tweet-final mention); the blank lines and the guard space survived, and both
tweets were deleted through `xposter delete` (pages then 404). Mentions of
**other** accounts, notification delivery, hashtag/cashtag typeahead and handles
that do not exist remain **UNVERIFIED live**. The default cap is 5 distinct
non-self mentions per thread, enforced before any browser starts; see the policy
section below and decision 13.

## Quickstart (about five minutes)

Prerequisites:

- Python 3.11 or newer.
- Google Chrome installed normally. You do **not** need
  `playwright install chromium`: the package launches your installed Chrome (or
  the executable in `XPOSTER_CHROME_PATH`), never Playwright's bundled Chromium.
- On Linux, a display for the one-time login. On a headless server see
  [`docs/runbook.md`](docs/runbook.md), section 6 (X11 forwarding or a temporary
  VNC desktop).

```bash
cd x-thread-poster
python3 -m venv .venv
.venv/bin/pip install -e .

# 1. One-time login. A normal Chrome window opens; sign in and QUIT Chrome
#    completely (Cmd+Q on macOS, or close every window) so cookies reach disk.
.venv/bin/xposter login

# 2. Measure X's live composer against the bundled selector table.
#    Nothing is submitted; the command dismisses the scratch composer.
.venv/bin/xposter doctor --dom

# 3. Rehearse a thread: validates, types it, discards it, writes no ledger row.
.venv/bin/xposter post --text "First post of the thread. The rest follows." --dry-run

# 4. Post for real (it asks for confirmation; add --yes to skip the prompt).
.venv/bin/xposter post --text "First post of the thread. The rest follows."
```

After activation (`source .venv/bin/activate`) the same commands are just
`xposter ...`.

On macOS, `--display auto` resolves to headless, and `xposter doctor` reports
that as a warning because the `HeadlessChrome` token is visible to X. For the
first supervised runs add `--display visible` before the subcommand (for example
`xposter --display visible doctor --dom`). On a Linux server, install Xvfb so
`auto` resolves to a headed Chrome on a virtual display instead.

`doctor --dom` is only useful with a logged-in profile. If it is run logged out it
still exits successfully but reports every composer selector as missing, which
is the correct answer for a logged-out page.

## Images

Each tweet can carry up to four images. The CLI accepts repeatable per-tweet
specifications:

- `--media PATH` attaches to tweet 1; `--media N:PATH` attaches to 1-based tweet
  `N` (for example `--media 2:reply.png`). Repeat for several images.
- `--alt TEXT` fills tweet 1's images in order; `--alt N.M:TEXT` addresses tweet
  `N`, image `M` (for example `--alt 2.1:"A second figure"`). Repeat as needed.
- `--thread-json` accepts `{"text": ..., "media": [...], "alt": [...]}` objects,
  which is the clearest way to give each tweet its own image.

Alt text is **off by default** because X's live "Edit image description" screen
has not been verified end to end. Passing non-blank alt without `--allow-alt`
(or `XPoster(allow_alt_text=True)` in Python) raises `ValidationError` before any
browser starts. Try it on a test account first.

Two-tweet example, one image on each tweet (`thread.json`):

```json
[
  {
    "text": "First tweet with a cover image.",
    "media": ["cover.png"],
    "alt": ["A descriptive cover image."]
  },
  {
    "text": "Reply tweet with its own figure.",
    "media": ["figure.png"],
    "alt": ["The figure described for the reply."]
  }
]
```

```bash
# Attach and upload both images for real, then discard without posting.
xposter post --thread-json thread.json --allow-alt --dry-run

# Post for real.
xposter post --thread-json thread.json --allow-alt
```

Images attach through the composer's hidden `[data-testid="fileInput"]` with
`set_input_files`, to the **focused** editor; xposter never clicks the media
picker. `--dry-run` really uploads the images to X and then discards them, so it
is also a media-upload rehearsal; it writes no ledger row and posts nothing.

Local validation, before Chrome starts: PNG, JPEG, GIF or WebP only, with the
extension and magic bytes agreeing; at most 5 MiB per image (15 MiB for a GIF);
a GIF may not be mixed with other images; at most four images per tweet; no
duplicate paths; no zero-byte files; video is rejected. **xposter never resizes
an image.** Files over 5 MiB must be resized by the caller before posting.

## Python API

```python
import asyncio
from pathlib import Path

from xposter import Thread, Tweet, XPoster, XPosterConfig, split_with_first


async def main() -> None:
    # XPOSTER_HOME / XPOSTER_HANDLE / XPOSTER_DISPLAY / XPOSTER_STRATEGY /
    # XPOSTER_CHROME_PATH are read by XPosterConfig.from_env(); explicit values
    # win.
    config = XPosterConfig.from_env()
    poster = XPoster(config)  # browser layer loads on access, not on `import xposter`

    index = "The pinned index post; it must be published exactly as written."
    body = "The per-paper replies, which may be longer than one tweet. " * 20
    segments = split_with_first(index, body)

    tweets = [Tweet(segments[0], media=(Path("cover.png"),))]
    tweets.extend(Tweet(text) for text in segments[1:])
    thread = Thread(tweets=tuple(tweets), key="weekly-2026-W45")

    posted = await poster.post_thread(thread)
    print(posted.first_url)
    for tweet in posted.tweets:
        print(tweet.index, tweet.url)


asyncio.run(main())
```

`post_thread` is the ledger-authoritative call: xposter's own ledger decides
whether the same visible thread was already posted.

### Resumable chains with `post_chain`

For callers that keep their own durable record, `post_chain` trusts the caller's
accepted prefix instead of deciding from xposter's ledger:

```python
from xposter import PostedTweet

# Exactly the accepted prefix, indexes 0..k-1, loaded from the caller's record.
accepted: tuple[PostedTweet, ...] = load_accepted_prefix()


def on_posted(tweet: PostedTweet) -> None:
    save_to_durable_record(tweet)  # the caller's record is the authority


posted = await poster.post_chain(
    thread,
    already_posted=accepted,
    on_posted=on_posted,
)
print(posted.strategy, posted.complete)
```

`XPoster.post_chain(thread, *, already_posted=(), on_posted=None) -> PostedThread`
behaves as follows:

- **Fresh thread (empty prefix).** Posts the whole thread through the verified
  composer `Post all` path — one batch click, per-tweet images, the Draft.js
  read-back before the click, and ids read from X's `CreateTweet` responses.
  `on_posted(PostedTweet)` is called synchronously for each captured id, in
  capture order; the ledger records the tweet before the callback runs.
- **Non-empty prefix.** Resumes by navigating to the last accepted tweet and
  replying **one post at a time**, waiting for `on_posted` to return before
  typing the next. This reply-by-reply resume path is **not verified against the
  live site** and its result uses `strategy="replies"`.
- **The caller owns idempotency.** `already_posted` must be exactly indexes
  `0..k-1`, in order, with non-empty ASCII-digit ids; a malformed prefix is
  refused before Chrome. `post_chain` never consults `Ledger.decide`, so a
  previous `PENDING`, `UNKNOWN` or `POSTED` entry (including a raised click
  barrier) never blocks a chain call. The ledger is still seeded with the
  caller's prefix as forensic evidence. A prefix tweet with an empty `url` gets
  one rebuilt from the handle and id, so the returned `PostedThread` has URLs
  when the account is known.
- **Pacing, lock, login and safety still apply.** The check is on the remaining
  tweets only; the session lock, login/handle check, park-on-challenge, click
  barrier, read-back, 25-tweet limit and mention cap all still run. The session
  and page are still opened for a non-empty remainder.
- **Complete prefix.** If `already_posted` covers the whole thread, a complete
  `PostedThread` is returned without starting Chrome; the ledger and pacer are
  left untouched.
- **Callback failure.** In a composer batch the click has already dispatched
  every request: a non-`XPosterError` from `on_posted` is remembered, later
  callbacks are skipped, every captured id is still recorded, and the batch is
  allowed to settle before `PostOutcomeUnknownError` is raised with the cause
  chained. An `XPosterError` from the callback is re-raised unchanged after the
  batch settles. In the reply-by-reply resume path a callback failure stops the
  chain immediately: a non-`XPosterError` becomes `PostOutcomeUnknownError` with
  the accepted tweets, and an `XPosterError` is re-raised.
- **Strategy is chosen by the method.** `XPosterConfig.strategy` does not matter
  here: `post_chain` uses `composer` for a fresh thread and `replies` only to
  resume. A 26-tweet thread is rejected with `ValidationError` before Chrome.

Notes:

- `Thread.key` is part of the content hash. Use a stable identity for the
  intended post (for example OLSA's post idempotency key) so the same content
  hashes the same on a rerun.
- `Thread.content_hash` hashes the key, every tweet text and every media
  **file name**. Re-downloading a file under the same name keeps the hash;
  renaming it changes the hash and can therefore allow a duplicate.
- Media belongs to tweet 1 when callers use `XPoster.post_text`. `Tweet.alt_texts`
  is applied through X's image-description editor: provide one string per media
  file in the same order. If the editor cannot be driven, the run fails with
  artifacts **before** clicking Post instead of silently publishing undescribed
  media.
- `xposter.XPoster`, `SessionInfo` and `RefreshResult` are lazy imports;
  `import xposter` itself does not import Playwright (pinned by
  `tests/test_public_api.py`).

## CLI reference

Global options go **before** the subcommand:

| Option | Meaning |
| --- | --- |
| `--home PATH` | State directory; overrides `XPOSTER_HOME` / `~/.olsa/x`. |
| `--handle NAME` | Expected X handle, with or without a leading `@`. |
| `--display MODE` | `auto`, `headless`, `xvfb` or `visible`. |
| `--strategy MODE` | `composer` (default) or `replies`. |
| `--premium` | Raise the per-tweet weight limit to 25,000. |
| `--max-mentions N` | Override the distinct-`@mention` cap for this run; meant for test accounts. |
| `-v`, `--verbose` | Print a traceback for unexpected failures. |

| Command | What it does | Exit codes |
| --- | --- | --- |
| `xposter login` | Opens a plain, non-automated Chrome window for the one-time sign-in, then verifies the result. | 0, 2, 3, 4, 7, 8, 1 |
| `xposter logout [--yes] [--no-backup]` | Deletes the saved Chrome profile; keeps a `storage_state.json.pre-reset` copy by default for manual same-machine recovery. The next step is `xposter login`. | 0, 2, 8, 1 |
| `xposter status [--json]` | Offline view of the saved session, parked state, next allowed post and ledger counts. | 0, 2, 6, 1 |
| `xposter verify` | Opens the saved profile and proves the login still works. | 0, 2, 3, 4, 7, 8, 1 |
| `xposter refresh [--force]` | Loads `/home` and scrolls for 20–60 s when the keepalive interval has elapsed; `--force` is refused when the last refresh is under ten minutes old. | 0, 2, 3, 4, 7, 8, 1 |
| `xposter unpark [--yes/-y]` | Clears `parked.json` after a human has checked the account. | 0, 2, 1 |
| `xposter post ...` | Previews, confirms and posts one thread. | 0, 2, 3, 4, 5, 6, 7, 8, 1 |
| `xposter delete TARGET... [--yes]` | Deletes posts by status URL or numeric id; intended only for cleaning up test posts. | 0, 2, 3, 4, 7, 8, 1 |
| `xposter ledger [--limit N] [--json] [--reset-corrupt]` | Shows recent ledger entries offline; `--reset-corrupt` quarantines an unreadable ledger after confirmation. | 0, 2, 6, 1 |
| `xposter doctor [--dom] [--dom-reply STATUS_URL] [--tells] [--drafts] [--with-media]` | Offline host/session checks; `--tells` measures the live browser; `--dom` calibrates the composer selectors without submitting; `--dom-reply` calibrates the **reply** composer read-only under one of the configured account's own statuses; `--with-media` adds phase D (generated PNG upload, attachment/alt markup capture, remove and discard); `--drafts` lists saved drafts read-only. | 0, 1 (failing checks), 2, 4, 8 |

`doctor --dom-reply STATUS_URL` is the read-only reply-composer calibration. It
requires `--handle` (or `XPOSTER_HANDLE`), accepts an x.com status URL or a
numeric id for **your own** account, and refuses another account before Chrome
starts. It opens the status page, clicks only the reply icon inside the focal
tweet, types `xposter reply calibration`, records the modal/inline composer and
submit state, then discards (modal) or clears and navigates away (inline). It
never submits. The reply composer is **UNVERIFIED-LIVE**: only the offline fake
covers it, so run it on a test account first.

`post` accepts exactly one source: `--text`, `--file PATH` (or `-` for stdin), or
`--thread-json PATH`. Add `--first TEXT` / `--first-file PATH` to pin a verbatim
opening post, repeat `--media PATH` / `--media N:PATH` to attach images to tweet
1 or 1-based tweet N, repeat `--alt TEXT` / `--alt N.M:TEXT` for the matching
descriptions (requires `--allow-alt`), use `--key TEXT` to make a deliberate
repeat with identical text a different thread, `--no-numbering` to leave split
text unnumbered, `--dry-run` to rehearse (including real image upload) without
posting, `--discard-recorded` only after deleting a partially posted thread by
hand, `--force` for recovery after checking the account, `--yes` to skip the
prompt, and `--json` for a machine-readable `PostedThread` on stdout (preview and
notices go to stderr).

`--force` is **recovery, not a repeat tool**. It re-opens a `POSTED` or
`UNKNOWN` record, but a partially posted thread continues after the recorded
tweets rather than re-posting them; the composer strategy still refuses because
it cannot continue safely. A completed `POSTED` thread is posted again as a new
attempt, with the previous record kept in the ledger history.

`refresh --force` is still rate-limited: if the last successful refresh is under
ten minutes old, it refuses and prints the "forced refresh too soon" reason.
Scheduled timers must call plain `xposter refresh` (no `--force`); the command's
own due check honors `keepalive.min_interval_s`.

`--discard-recorded` starts over an `UNKNOWN` or click-barrier partial thread
after you have deleted it by hand; it is the explicit assertion that the recorded
ids are gone. A `FAILED` partial row still takes the normal resume path, so it
cannot be forced into a clean start this way; use the replies strategy or repair
the content. To post the same text again on purpose, give it a new `--key`.

`xposter doctor --drafts` lists saved drafts read-only and never deletes or edits
one. Its scope is still **UNVERIFIED live**: the first live run read timeline
posts as drafts, and the scoped fix is covered only by offline tests. Check the
account's Drafts panel manually once (especially after a calibration run) before
trusting the output.

### Exit codes

| Code | Meaning | What to do |
| ---: | --- | --- |
| 0 | Success. Also: cancelled at a prompt, refresh "not due", unpark with no marker. | — |
| 1 | Unexpected or unclassified failure. `doctor` also uses 1 when an offline check fails. | Read the message; re-run with `-v`; run `xposter doctor`. |
| 2 | Configuration or input validation error. | Fix the value or the content and re-run. |
| 3 | Session problem: not logged in, wrong account, or an unrecognised login state. | Run `xposter login` (or log in as the expected account). |
| 4 | Parked, challenge, locked/suspended account, or rate limited. | Resolve it in a normal browser, then `xposter unpark`. Never retry in a loop. |
| 5 | Local pacing blocked the post. | Wait for the printed local time. |
| 6 | Post outcome unknown, or the ledger is unreadable/corrupt. | Check the profile. Use `--force` only after checking (and only for replies/empty records); use `--discard-recorded` only after deleting a partial thread. For a corrupt ledger, restore it or run `xposter ledger --reset-corrupt`. |
| 7 | X's UI changed or a selector did not match. | Inspect the artifacts, run `xposter doctor --dom`. |
| 8 | Chrome unavailable or the profile/session lock is held. | Install Chrome, or close the other Chrome/xposter run. |

## Configuration

Configuration is a frozen dataclass; there is **no config file**. `XPosterConfig`
has safe defaults, `XPosterConfig.from_env()` reads the five `XPOSTER_*`
variables below, and the CLI flags override the environment. Nested policies
(`human`, `pacing`, `keepalive`) are set from Python with
`config.with_overrides(...)`; they have no environment variables today.

### XPosterConfig

| Field | Default | Meaning | Environment variable |
| --- | --- | --- | --- |
| `home` | `~/.olsa/x` | State directory. | `XPOSTER_HOME` (blank = unset) |
| `handle` | `None` | Expected handle; a leading `@` is dropped. | `XPOSTER_HANDLE` |
| `display` | `auto` | `auto`, `headless`, `xvfb`, `visible`. | `XPOSTER_DISPLAY` |
| `strategy` | `composer` | `composer` or `replies`. | `XPOSTER_STRATEGY` |
| `chrome_path` | `None` | Explicit Chrome executable; a bad value is a configuration error. | `XPOSTER_CHROME_PATH` |
| `human` | `HumanPolicy()` | Typing, pause and scroll timing. | — |
| `pacing` | `PacingPolicy()` | Local posting limits. | — |
| `keepalive` | `KeepalivePolicy()` | Refresh interval and browse duration. | — |
| `max_tweet_weight` | `280` | Weighted characters per tweet; `--premium` sets 25,000. | — |
| `max_media_per_tweet` | `4` | X's composer limit. | — |
| `max_mentions_per_thread` | `5` | Distinct non-self `@handles` allowed per thread, checked before Chrome. | `XPOSTER_MAX_MENTIONS_PER_THREAD` |
| `default_timeout_ms` | `30000` | Playwright default timeout. | — |
| `keep_artifacts` | `10` | Newest artifact directories kept. | — |
| `artifact_max_age_days` | `14` | Artifact directories older than this are pruned on the next artifact write; the newest is always kept. | — |
| `base_url` | `https://x.com` | Web client base; tests point it elsewhere. | — |

### HumanPolicy

| Field | Default | Meaning |
| --- | --- | --- |
| `typing_delay_ms` | `(35, 120)` | Inclusive per-keystroke delay range. |
| `pre_post_pause_s` | `(1.5, 4.0)` | Dwell before clicking Post. |
| `between_tweet_pause_s` | `(20.0, 45.0)` | Gap between replies in the `replies` strategy. |
| `action_pause_s` | `(0.4, 1.4)` | Generic click/pause range. |
| `think_pause_s` | `(2.0, 6.0)` | Pause after a composer item is typed. |
| `scroll_before_post` | `True` | Scroll like a reader before submitting. |
| `seed` | `None` | `None` in production; tests set it for repeatability. |

### PacingPolicy

| Field | Default | Meaning |
| --- | --- | --- |
| `min_gap_between_threads_s` | `1800` | Minimum gap; a jittered `not_before` is persisted. |
| `max_threads_per_day` | `6` | Local daily thread cap. |
| `max_tweets_per_day` | `40` | Local daily tweet cap. A thread larger than this is a validation error, never a wait. |
| `max_tweets_per_hour` | `12` | Rolling-hour cap. A thread larger than this is allowed only when the hour window is empty. |
| `quiet_hours` | `None` | Local `[start, end)` window, may wrap midnight, e.g. `(23, 7)`; `None` disables. |
| `jitter` | `0.25` | Fractional spread added to the gap (0.25 = up to +25%). |

### KeepalivePolicy

| Field | Default | Meaning |
| --- | --- | --- |
| `min_interval_s` | `43200` | Minimum seconds between normal `xposter refresh` runs (12 hours). `refresh --force` is still refused under 10 minutes. |
| `browse_seconds` | `(20.0, 60.0)` | Randomised `/home` scroll duration. |

### Files under `home`

| Path | Contents |
| --- | --- |
| `profile/` | Persistent Chrome profile (the primary session; full-account credential). |
| `storage_state.json` | Plaintext backup cookie/local-storage state, refreshed after a clean healthy run. A **full-account credential**; mode `0600`. |
| `storage_state.json.bak` | Last known-good backup. |
| `storage_state.json.pre-reset` | Copy of `storage_state.json` made by `xposter logout` (unless `--no-backup`); restoring it is a manual recovery step. |
| `session.json` | Handle, verification and refresh timestamps; no cookie values. |
| `parked.json` | Park reason, kind, `since` and details. |
| `ledger.json` | Idempotency and pacing history. A corrupt file blocks posting with exit 6 until it is restored or quarantined as `ledger.json.corrupt-<timestamp>`. |
| `session.lock` | Single-process lock for the Chrome profile. |
| `artifacts/` | Newest `keep_artifacts` screenshot/HTML evidence directories. |

The home, profile and artifact directories are mode `0700`; `session.json`,
`ledger.json`, `parked.json`, `storage_state.json`, its backup and every
artifact file are written mode `0600`. The directory protects anything the
browser library creates before it is tightened.

The profile and `storage_state.json` are credentials: never commit them and
never put them in the repository. The Chrome profile is **not portable across
operating systems** (Chrome encrypts cookies per OS), and the ledger belongs to
the machine that posts. Log in on the machine that will post; copying
`storage_state.json` is only a documented last resort (see
[`docs/runbook.md`](docs/runbook.md), section 6).

## Guarantees and non-guarantees

**What the tool guarantees, within its own control:**

- **Idempotency.** The ledger is written `PENDING` once the session is verified
  and before the thread is touched, a `clicking_at` barrier is fsynced *before*
  the Post button is clicked, every captured tweet id is persisted immediately,
  and an already-`POSTED` thread returns its stored result without launching
  Chrome. A later run treats a `PENDING` row with a click barrier like `UNKNOWN`,
  never as a clean thread. The content hash covers the thread key, every text
  and every media file name.
- **No silent ledger loss.** The ledger is written atomically (file fsync,
  `os.replace`, directory fsync, mode `0600`). A non-empty unreadable file
  raises `LedgerCorruptError` (exit 6) instead of being discarded, because its
  rows are the only local proof of what was posted. `xposter ledger
  --reset-corrupt` is the explicit, confirmed override.
- **No blind retry.** Only an HTTP 5xx on the first click, where the server
  proved it created nothing, is re-clicked once. A CSRF rejection waits for X's
  own client to refresh `ct0`; an unrecognised error code/message, a bare HTTP
  403, a challenge, lock or rate limit stops and parks.
- **Park, never evade.** A challenge, lock, suspension, automation flag or rate
  limit writes `parked.json` and raises a typed error. The next run raises
  `ParkedError` before starting Chrome. Only a human clears it with
  `xposter unpark`.
- **Local pacing and quiet hours** are enforced before Chrome starts, including
  the gap between threads, daily thread/tweet caps and the rolling-hour cap.
- **Secrets stay out of output.** Errors and ledger notes are redacted; the
  session summary contains booleans and timestamps, never cookie values.
- **Deterministic text splitting** based on twitter-text's v3 weighting, with
  URLs, hashtags, mentions and emoji sequences never cut.

**What it does not guarantee:**

- **It cannot promise the account will never be flagged, locked or suspended.**
  The policy risk is inherent to the mechanism (see the warning above).
- **The classic authenticated composer DOM is `VERIFIED-LIVE` on the test
  account (2026-11-09/10)**, including a real 2-tweet thread, a 2-tweet image
  thread and delete. The replies strategy, alt text end to end, untested image
  variants, Premium note-tweets, new-client DOM and 20+ tweet threads remain
  unverified. Selectors still require `doctor --dom` after login and after a UI
  change; a mismatch stops with exit 7 rather than guessing.
- **The response shape can change.** If a `CreateTweet` response no longer
  yields an id, the outcome becomes `UNKNOWN`; a human must check the profile.
- **X can refuse content** (for example duplicate code 187 or too long code
  186). That is a rejection, not a park, and retrying the same text will not
  help.
- **Media upload is X's own client upload.** Images are validated locally and
  uploaded through the composer; large files, codec support and slow uploads are
  not specially handled beyond a size-scaled timeout. Alt text is off by default
  until its live editor is verified, video is unsupported, and xposter never
  resizes an image.
- **No scheduler, no automatic retry across restarts, no multi-account.** A
  crash leaves ledger/UNKNOWN state for a human to resolve.
- **Tests never hit the network.** The real login, live DOM, media upload and
  delete flow are only exercised by the opt-in `live` tests
  (`XPOSTER_LIVE=1`) and by the operator's own first live test.

## Deliberately not implemented

- CAPTCHA/Arkose solving or any challenge bypass.
- User-agent, Canvas/WebGL or `navigator` spoofing; JavaScript patching.
- Proxy rotation or IP/region switching.
- Automatic login with a password, 2FA automation or cookie import.
- Multi-account switching or parallel accounts.
- Direct calls to X's internal GraphQL API (query ids and headers rotate; the
  browser drives the real client instead).
- An official-API backend; this package is the browser path only.
- Likes, follows, reposts or other engagement; the keepalive only loads and
  scrolls `/home`.
- Deleting real content; `xposter delete` exists to clean up test posts.

## Documentation

- [`docs/design.md`](docs/design.md) — decision log, with VERIFIED vs UNVERIFIED
  evidence for each choice.
- [`docs/runbook.md`](docs/runbook.md) — first login, calibration, live test,
  refresh timers, logging in from a server / moving to Linux, parked state, UNKNOWN entries, artifacts,
  backup and troubleshooting.
- [`docs/integration.md`](docs/integration.md) — the OLSA integration that now
  exists on the unmerged `feat/x-thread-publish` branch (OLSA ADR-0085):
  `ThreadPublisher` adapter, error mapping, settings, commands and the
  still-open mention/alt-text/region items.
- [`docs/research/risk-and-limits.md`](docs/research/risk-and-limits.md) — the
  policy/limits memo behind the warning and the API cost estimate.
- [`docs/research/field-reports.md`](docs/research/field-reports.md) — independent
  field reports, with source and date, tagged REPORTED and not treated as proof.
- [`docs/research/web-client.md`](docs/research/web-client.md) — the public
  bundle study behind the selectors and the response parsing.
- [`docs/research/selectors.json`](docs/research/selectors.json) — the selector
  study with confidence tags.

## Development

```bash
make dev-install   # editable install with dev dependencies
make check-fast    # ruff + format-check + mypy + non-browser/non-live tests
make test-file FILE=tests/test_splitter.py   # one file, serial, no xdist
make test-browser  # only the real-Chrome tests, under xdist
make check         # ruff + format-check + mypy + the whole suite, parallel
```

`make check-fast` is the inner loop and must stay under roughly 30 seconds.
Use `make test-file FILE=...` for the files you changed. Only **`make check`**
is the definition of done: it runs the whole suite under xdist and defaults to
six workers with `--dist load`; override with
`XPOSTER_TEST_WORKERS=4 make check` on a busy or smaller machine. See
`tests/README_TESTING.md` for the xdist rules (temporary homes, loopback port 0,
no fixed paths).

Live tests are skipped unless `XPOSTER_LIVE=1` and a real logged-in session are
present. Browser tests launch a local Chrome but never touch the network or
`x.com`.
