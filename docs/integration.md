# Integrating with OLSA

The OLSA integration exists on the unmerged OLSA branch
`feat/x-thread-publish`; its decision record is
`docs/adr/0085-x-session-driver-xposter.md` (which supersedes the in-tree
browser driver of OLSA ADR-0084). It is **implemented in the repository but has
not been run live through the pipeline**. The live evidence is still the
package's own test-account runs; no pipeline-driven post has been made.

This document describes the seam that exists, not a future sketch. Read
[`design.md`](design.md) first for the package's own verified/unverified
matrix, and [`runbook.md`](runbook.md) for host operations.

## 1. What exists in OLSA

- `src/olsa/adapters/x_session.py` is a thin adapter over `xposter`. It owns
  OLSA's session status, maps `XThread`/`ThreadPost` onto xposter's models and
  translates every package exception into `olsa.domain.errors`. Browser
  selectors, typing, read-back and response capture stay in this package.
- The adapter implements `ThreadPublisher`
  (`src/olsa/ports/thread_publisher.py`), not the older one-shot
  `SocialPublisher`. The port exists because a thread is several dependent
  writes and the caller must receive each accepted id immediately.
- `XSessionPublisher` is the publisher class (`name = "x-session"`); its
  `destination` is the expected handle, or `"x"` when no handle is configured.
- `xposter` is imported **lazily** inside the adapter methods, through a
  module-level seam typed as `ModuleType`, so OLSA imports everywhere without
  the optional extra installed. A missing package becomes
  `AdapterUnavailableError` with the install command.
- The OLSA extra is `x-session = ["x-thread-poster>=0.2.0"]` in
  `pyproject.toml`, beside the existing `x = ["xdk==..."]` API extra.
  `make check` and `make dev-install` succeed without it; the adapter tests use
  a fake package.
- The offline readers `session_status`, `state_path`, `XSession`, `XLogin` and
  `XRefresh` use only the standard library. `session_status` reads the presence
  of the Chrome profile, the `storage_state.json` auth-cookie expiry,
  `session.json`'s handle and `parked.json`'s non-secret reason. It never
  imports xposter, opens Chrome or returns a cookie value.
- The operator surface is `olsa x login | logout | status | verify | post |
  draft | show | unpark | refresh`, plus the portal Connections card and the
  existing automatic daily/weekly chain. The portal shows only the cookie-free
  session summary.

## 2. Port contract and model mapping

The port (`ThreadPublisher`) declares:

```python
name: str
destination: str
capabilities: ThreadCapabilities


def validate(self, thread: XThread) -> None: ...
async def verify(self) -> str: ...
async def publish(
    self,
    thread: XThread,
    *,
    already_posted: Sequence[PostedThreadPost] = (),
    on_posted: Callable[[PostedThreadPost], None] | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[PostedThreadPost, ...]: ...
```

`XThread(kind, title, posts)` maps to xposter's `Thread` with
`key=f"{thread.kind}:{thread.title}"` and one `Tweet` per `ThreadPost`:

```python
tweets = tuple(module.Tweet(post.text, media=tuple(post.media)) for post in thread.posts)
source = module.Thread(tweets=tweets, key=f"{thread.kind}:{thread.title}")
```

- `ThreadPost.text` is the final text (numbering and Unicode styling included).
- `ThreadPost.media` becomes `Tweet.media`; paths are validated by xposter.
- `ThreadPost.note` is operator-only and is never sent.
- **`ThreadPost.alt_text` is deliberately dropped.** The adapter passes no alt
  text: xposter's live image-description editor is not verified end to end, and
  enabling an unverified dialog would fail at the last step instead of publishing
  the image. The API transport still sets alt text. Integrating without alt text
  loses accessibility text; see the open checklist.
- The accepted prefix maps back from `PostedThreadPost` to xposter's
  `PostedTweet` (`index`, `id`, `url`, `text_sha256` recomputed from the post
  text). Every newly captured id is converted to `PostedThreadPost` and reported
  through the caller's `on_posted` before the next id is processed.
- The port's `max_chars` is a **per-post weighted limit** (280 for a standard
  account), not a limit on the whole thread; `max_media` is the per-post image
  limit (4 by default).
- `validate()` refuses an empty post, an over-long post, too many images or an
  invalid image **before any browser starts**; it uses xposter's weighted length
  and media validator. A post over the configured limit is split by the thread
  generator, not by the publisher.

### Images

- xposter accepts at most four PNG/JPEG/GIF/WebP images per tweet, 5 MiB per
  image (15 MiB for a GIF), and no GIF mixed with another type.
- **Images over 5 MiB are refused; xposter never resizes.** The caller must
  downscale or re-encode a PDF page screenshot before publishing. This is still
  an open integration item for the ADR-0085 region bundle.
- Alt text is dropped on this transport (see above).

### Thread length and numbering

- X's composer caps one thread at **25 posts**; xposter checks this before
  Chrome. The weekly edition must fit the index plus per-paper posts within 25,
  or be split into two threads.
- xposter's splitter protects URLs, hashtags, mentions and emoji sequences from
  being cut and uses X's weighted 280-character rules (URLs weigh 23, bold
  Mathematical Sans-Serif letters weigh 2 each).
- The daily and weekly generators already carry the `k/N` numbering and the
  Unicode styling in `ThreadPost.text`; the adapter does not re-flow or
  re-number it.

## 3. Publishing: fresh thread, resume and authority

`XSessionPublisher.publish()` calls `XPoster.post_chain()` with the thread and
the pipeline's accepted prefix. `post_chain` chooses the mechanism itself;
`XPosterConfig.strategy` is `composer` and is not the switch.

### Fresh thread (empty prefix)

- xposter types every post into the one `/compose/post` modal and sends the
  batch with **one `Post all` click**. This is the path verified live on the
  test account for text threads, images on several posts and own-handle
  mentions.
- Each id is captured from X's own `CreateTweet` response. The ledger records
  the tweet **before** `on_posted` runs, and the callback runs synchronously in
  capture order. Because the web client posts the rest of the batch itself, the
  ids arrive back-to-back after the click.
- If `on_posted` raises a non-xposter exception, xposter cannot stop a batch
  that is already in flight: it remembers the error, skips later callbacks,
  still records every captured id, lets the batch settle and then raises
  `PostOutcomeUnknownError` with the cause chained. An `XPosterError` from the
  callback is re-raised unchanged after the batch settles.
- The click barrier is fsynced before the click, so a crash after the click is
  recoverable as an unknown outcome rather than a silent retry.

### Resume (non-empty prefix)

- A non-empty `already_posted` prefix makes xposter navigate to the last
  accepted tweet's status URL and reply **one post at a time**, waiting for
  `on_posted` to return before typing the next. The result's `strategy` is
  `"replies"`.
- **This reply-by-reply resume path is `UNVERIFIED-LIVE`.** It exists to finish
  a thread that stopped part-way; it has only been exercised against the offline
  fake. It is slower than the composer batch (about 20–45 s between posts).
- If the prefix already covers the whole thread, `post_chain` returns a complete
  `PostedThread` **without starting Chrome** and without touching the ledger or
  pacer.

### The caller's record is the authority

- `already_posted` must be exactly the accepted prefix `0..k-1`, in order, with
  non-empty ASCII-digit ids. A gap, reordering, non-digit id or a prefix longer
  than the thread is a `ValidationError` before Chrome.
- `post_chain` never consults `Ledger.decide` for the prefix. A previous
  `PENDING`, `UNKNOWN` or `POSTED` entry for the same content **never blocks a
  chain call**; this includes a raised click barrier from an interrupted run.
  The caller decides what to resume from.
- The ledger is still updated: `ledger.begin(...)` keeps history and
  `previous_completed`, then the caller's prefix is recorded as the starting
  forensic record. If the ledger holds tweets the caller does not know about
  (for example after a crash between the click and the callback), those entries
  are kept in history but cleared from the active record.
- Pacing counts only the **remaining** tweets, so a resumed thread is not
  rejected because the whole thread would exceed a daily cap.
- Parking, the session lock, login and handle checks, the click barrier, the
  pre-Post read-back and the local 25-post/mention checks still apply.
- OLSA keeps its durable `x_thread.json` record as the authority for resume,
  cleanup and unknown outcomes. xposter's `ledger.json` is forensic evidence
  and the pacing memory only. The two can disagree after a manual deletion on
  X; the pipeline record decides what is posted, and the package ledger is not
  edited by hand.
- A mid-batch failure raises `PostOutcomeUnknownError` with the ids captured so
  far; those ids were already reported through `on_posted`. OLSA records them
  and its unknown-outcome rule blocks a blind retry until a human has looked at
  the account.

## 4. Error translation

`x_session.py` translates every package exception at the boundary; no vendor
type crosses the port. The implemented table:

| `xposter` exception | OLSA exception | Behaviour |
| --- | --- | --- |
| `NotLoggedInError`, `AccountMismatchError`, `ParkedError`, `ChallengeError`, `AccountRestrictedError` | `PublishAuthError` | A human must log in as the expected account, check the account, or unpark. |
| `RateLimitedError`, `PacingError` | `PublishQuotaError` | Includes the package's retry delay when it provides one. |
| `PostOutcomeUnknownError` | `PublishOutcomeUnknownError` | A click may have landed; the reported ids are the accepted prefix and a blind retry is forbidden. |
| `UiChangedError` | `PublishError` | Names the artifacts directory; X's page no longer matches the selectors and the failing step posted nothing. |
| `PostRejectedError`, `ValidationError` | `PublishError` | Content or media was refused; retrying the same input will not help. |
| `BrowserUnavailableError` | `AdapterUnavailableError` | Chrome is missing or failed to start; the message names `x.session_chrome_path`. |
| `SessionBusyError` | `AdapterUnavailableError` | Another run holds the Chrome profile; wait and retry. |
| `LedgerCorruptError` | `AdapterUnavailableError` | Restore the local ledger or reset it after checking the account. |
| `ConfigurationError` | `AdapterUnavailableError` | Bad package configuration, such as a bad Chrome path or display mode. |
| any other `XPosterError`, or an unexpected exception | `PublishError` | Unknown failure; report it, do not classify it as quota/auth. |
| `require_xposter()` ImportError | `AdapterUnavailableError` | The `x-session` extra is not installed. |
| login: `BrowserUnavailableError` | `AdapterUnavailableError` | Install Chrome or fix `x.session_chrome_path`. |
| login: `ParkedError` or any other `XPosterError` | `ConfigurationError` | Setup/sign-in did not finish; re-run `olsa x login`. |

`olsa x post` and the automatic chain use the same rules. A local `PacingError`
does not mean X rate-limited the account, but both map to `PublishQuotaError`
because the next action is to wait.

## 5. Settings and commands

`src/olsa/config.py` has the session settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `x.transport` | `session` | `session` drives the saved Chrome profile; `api` uses the paid official API. |
| `x.session_display` | `auto` | `auto`, `headless`, `xvfb` or `visible`. Use `visible` for the first runs and `xvfb` on a Linux server; `headless` exposes a `HeadlessChrome` user agent. |
| `x.session_chrome_path` | `""` | Explicit Google Chrome executable; blank finds the installed Chrome. |
| `x.expected_handle` | `""` | The account the login must belong to; publishing refuses another account. |
| `x.enabled` | `false` | Whether the automatic chain posts X threads. |
| `x.daily` / `x.weekly` | `true` | Narrow the automatic chain to one kind of thread. |
| `x_session_dir` (top level) | `~/.olsa/x` | The saved Chrome profile and `storage_state.json` backup. Both are full-account credentials. |

- The adapter builds `XPosterConfig` explicitly from these settings; it does
  **not** read ambient `XPOSTER_*` variables. `XPOSTER_*` remains for the
  standalone CLI and host timers.
- Never put the Chrome profile, `storage_state.json` or cookies in
  `~/.olsa/secrets.json`. The secret store holds named values; the browser
  profile is a directory of state and cannot be injected by the portal.
- The login, keepalive and posting must run as the same OS user, on the same
  machine and IP, with the same home. Use [`runbook.md`](runbook.md), section 6,
  for server login. A copied profile defeats the device/session consistency the
  design relies on.
- The portal job runner serialises jobs per variant, but a systemd keepalive
  timer is outside that lock. Both use the same home, so two processes can
  collide; xposter's `SessionBusyError` is the safe result. Do not remove the
  lock.

### Commands

| Command | What it does |
| --- | --- |
| `olsa x login` | Opens a plain Chrome window; the human signs in and quits Chrome. `--display` / `--headed` choose the display. |
| `olsa x logout` | Wipes the Chrome profile through xposter, keeping `storage_state.json.pre-reset` by default. |
| `olsa x status` | Offline summary: profile/backup presence, handle, expiry, parked state. |
| `olsa x verify` | Opens Chrome and proves the saved login; posts nothing. |
| `olsa x post` | Composes if needed, confirms the cost (session estimate is $0.00), then publishes. `--force` posts a finished thread again as a new thread; `--retry-unknown` continues after an unknown outcome only after a human check. |
| `olsa x draft` | Generates/writes the thread record and X draft without publishing. |
| `olsa x show` | Prints the saved thread record. |
| `olsa x unpark` | Clears xposter's park marker after a human has checked the account. |
| `olsa x refresh` | Loads and scrolls `/home` when xposter's 12-hour keepalive interval says it is due; a timer calls it without `--force`. |

## 6. Packaging and tests

- `x-session = ["x-thread-poster>=0.2.0"]` is an **optional** extra, not a base
  dependency. `xposter` is imported lazily; a host without it still imports
  OLSA and simply cannot use the `session` transport.
- Until the package is published, install it from the sibling checkout:
  `pip install -e /path/to/x-thread-poster`, or
  `pip install -e ".[x-session]"` with that checkout on the path. Pin a tag or
  commit when a real index is used.
- Adapter tests use a fake package and a fake poster: no network, no Chrome,
  no live session. The real browser suite remains opt-in in this package.

## 7. Prompt side: daily/weekly threads

- The daily prompt (`prompts/social/x_thread_daily.txt`) still forbids
  model-written `@mentions` anywhere; the session transport on this branch does
  not add tags itself. OLSA ADR-0086 (main) adds high-confidence author tags
  through `handles.json` and `SocialPost.mentions`, but that path is not wired
  into `XSessionPublisher`; keep the prompt rule until it is.
- The weekly index is rendered in Unicode Mathematical Sans-Serif Bold. Those
  code points weigh 2 each under twitter-text, so the generator validates the
  index with OLSA's own weighted-length helper (the same rules as xposter's) and
  caps the thread at 25 posts. Do not measure it with `len()`.
- Naming and numbering are already in `ThreadPost.text`; the adapter neither
  renumbers nor re-splits a post.

## 8. Open integration items

These are deliberately **not done** on the unmerged branch:

- [ ] **Mention policy versus OLSA ADR-0086.** OLSA ADR-0084 (main) resolves
  author and organisation handles, and OLSA ADR-0086 (accepted on main) tags
  only `high`-confidence picks, with per-platform caps and a `renders_mentions`
  flag on a publisher. The session transport on this branch does **not** yet
  implement that mention path: `XSessionPublisher` maps text and media only.
  The policy question is separate and still open: an automated account tagging
  people who did not opt in is an **unsolicited automated mention**; xposter's
  default cap is 5 distinct non-self handles per thread and refuses an
  every-author-every-tweet design. Decide whether this transport renders tags,
  at what confidence and volume, which tweet(s), and whether the cap is raised.
  A wrong tag is public and permanent; a missing one is invisible. Keep the
  daily prompt's no-`@mention` rule for model text until this is recorded.
- [ ] **Alt text.** `ThreadPost.alt_text` is dropped on this transport. The
  package's live "Edit image description" field/Save are UNVERIFIED; enabling
  it blindly would fail at the last step. Integrating without alt text loses
  accessibility text; verify the live editor and wire it in deliberately.
- [ ] **Region crops resize.** OLSA ADR-0085 (paper regions, main) has page-1
  and region crops that can exceed xposter's 5 MiB image limit. ADR-0085 only
  warns; xposter refuses and never resizes. Add a downscale/recompress step
  before `ThreadPost.media`, map `regions.json` paths per tweet, and keep each
  tweet at four images or fewer.
- [ ] **First live pipeline run.** No pipeline-driven post has been made. Run
  the test account first, then the OLSA account; check `x_thread.json`, the
  package ledger, the live permalink and the thread order.

## 9. What not to do

- Do not import `xposter` from `olsa.domain` or at OLSA package import time; it
  is an optional extra, and the browser import must stay inside the adapter.
- Do not put `xposter` exceptions, `Path` objects from its ledger, or cookie
  values across the port. Translate at the boundary.
- Do not put the profile, `storage_state.json` or cookies in
  `~/.olsa/secrets.json`; do not copy them into CI, a container image, a project
  folder or another host. The session is created on the posting machine.
- Do not run the keepalive timer and a post job against the same home without
  accepting `SessionBusyError`; xposter's lock protects the profile.
- Do not treat a returned `PostedThread` as success without checking that it has
  post ids; an empty result is an unknown outcome.
- Do not claim a live pipeline run, multi-account support, proxy support or
  "undetectable" behaviour. None exists.
- Do not make X a default target until the adapter, the session and the
  calibration are proven on the host; keep it opt-in.
