# Design decisions

This is the package's decision log. It uses the same
Status / Context / Decision / Consequences / Alternatives shape as the OLSA
ADRs, but stays in one file because every decision belongs to this standalone
package rather than to the pipeline. The decisions are dated 2026-11-09 unless
stated otherwise.

**Evidence labels used below.** `VERIFIED` means a test in this repository, or a
measurement against the locally installed Chrome, proves the statement. It does
**not** mean X's live authenticated site behaves that way. `VERIFIED-LIVE` means
the statement was observed in the **2026-11-09 test-account run** (home
`~/.olsa/x-test`, Chrome 156, visible mode), exactly as scoped in the
[Live verification log](#live-verification-log--2026-11-09-test-account); it is
not a test result and not a general guarantee. `UNVERIFIED` means the statement
is inferred from the public bundle study
([`research/web-client.md`](research/web-client.md)), the research memo
([`research/risk-and-limits.md`](research/risk-and-limits.md)) or the selectors
study, and has not been observed in a real logged-in session. The final
[verification matrix](#verification-matrix) summarises the coverage.

---

## 1. A standalone package, not a step inside OLSA

- **Status:** Accepted (2026-11-09)
- **Verification:** VERIFIED (the source imports no `olsa` module, tested in
  `tests/test_public_api.py`, and its test suite runs on its own) / UNVERIFIED
  (the OLSA integration has not been done)

### Context

The OLSA pipeline needs an X destination eventually, but the browser path is
policy-risky, slow and needs its own session lifecycle. Developing it inside
`olsa-post-pipeline` would mix a new external service, a new browser profile and
a new failure model into a pipeline that is already complete and running. The
work was explicitly scoped to leave the OLSA repository untouched.

### Decision

Build a standalone distributable package, `x-thread-poster`, imported as
`xposter`. It depends only on `playwright` and `typer` (plus the standard
library) and never imports `olsa`. Its public surface is pinned in
`xposter.__all__`; the browser-backed names are lazy so the pure helpers and
models are cheap to import.

### Consequences

- The package can be tested, installed and released without the pipeline. The
  offline fake of X and the real-Chrome integration tests are self-contained.
- Integration is a later, explicit change in the OLSA repository: a
  `SocialPublisher` adapter and an ADR. The sketch is in
  [`integration.md`](integration.md).
- Two ledgers exist: `xposter`'s own `ledger.json` and OLSA's `posts.json`.
  Integration must treat one as the source of truth for idempotency and keep
  them consistent, or a retry can be skipped by one and attempted by the other.
- A separate package needs its own versioning, release and dependency pinning.
  The pipeline's optional `x` extra is the seam.

### Alternatives considered

- **Implement directly in `olsa/adapters/`.** Rejected for this task: it would
  touch the pipeline, couple a risky experiment to a working system, and make
  the offline fake and Chrome tests part of the pipeline suite.
- **A separate long-running service.** Rejected: the posting volume is a few
  threads a week, and a HTTP service would add deployment and auth surface for
  no benefit. A package with a CLI and a Python API is enough.
- **Copy the code into OLSA later.** Rejected: a copy would drift. The package
  is the unit of change; integration imports it.

---

## 2. Real Chrome with a persistent profile, `storage_state` only as backup

- **Status:** Accepted
- **Verification:** VERIFIED for launching real Chrome, `navigator.webdriver`
  being false, cookies surviving a context restart, and the backup/restore code
  path (local Chrome tests) / UNVERIFIED for X accepting the persistent profile
  over days and for the real `auth_token` lifetime

### Context

The public bundle study says the browser environment is one of the few signals
the client itself collects, and that X's classifier maps `HeadlessChrome` and
`Playwright` in the user agent to bot tags. Playwright defaults to its bundled
Chromium, whose `Sec-CH-UA` brand list lacks `"Google Chrome"`. Playwright's
`storage_state` saves cookies and local storage but not session storage, and
`launch_persistent_context` keeps the whole profile. The research memo also
warns not to point a persistent context at the operator's daily-driver profile.

### Decision

Launch the **installed Google Chrome** through `executable_path` and
`launch_persistent_context` on a dedicated profile under
`<home>/profile`. After every clean, verified run capture
`context.storage_state()` into `storage_state.json`, keeping the previous good
copy as `.bak`. Restore the backup **only** when the profile is empty, because
restoring cookies into a live profile could overwrite newer session state.

### Consequences

- The profile is a full-account credential. The home and profile are `0700`, the
  state files `0600`. The profile is never copied: Chrome encrypts cookies with
  an OS-specific scheme, so it does not move between macOS and Linux at all.
  `storage_state.json` is plaintext and mode `0600`; copying it is only the
  runbook's explicitly not-recommended last resort.
- The profile is the primary session; `storage_state.json` is a same-machine
  recovery path, not a portable credential. This matches the bundle study's
  observation that X's edge tracks session/device/audit data.
- A crash can leave Chrome's `SingletonLock`; the browser layer detects a live
  owner and reports a busy profile instead of corrupting the database.
- A successful run that fails to capture the state logs the failure type and
  keeps the old backup; it does not turn a successful post into an error.
- `storage_state` does not preserve session storage. If X moved something
  important there, only the profile carries it; this is `UNVERIFIED`.

### Alternatives considered

- **Playwright's bundled Chromium.** Rejected: the brand mismatch and the
  `HeadlessChrome`/`Playwright` classifier are exactly the cheap signals the
  research memo says to avoid. It is also not the browser the operator uses.
- **`storage_state` only (no persistent profile).** Rejected: it is less
  complete, looks more like a transplanted cookie jar, and would require a new
  context every run. It remains the backup, not the primary.
- **The operator's daily Chrome profile.** Rejected: Playwright explicitly warns
  against it, the automation would share the person's real tabs and extensions,
  and a crash could damage their browser. A dedicated profile is both safer and
  more honest.

---

## 3. One-time login in plain Chrome, never through Playwright

- **Status:** Accepted
- **Verification:** VERIFIED for the command construction and process handling
  (unit tests with a fake `Popen`) / UNVERIFIED against the real login flow,
  2FA and X's unusual-activity block

### Context

Login is the most scrutinised moment. The research memo records that X can
answer an automated-looking login with the "we weren't sure it was really you"
block, and that repeated logins are the documented ban correlate in community
reports. A CDP-controlled browser at that moment is the opposite of what the
"log in by hand once" pattern is trying to achieve.

### Decision

`xposter login` starts an **ordinary Chrome process** with the same
`--user-data-dir`, the login URL and no Playwright/CDP:
`plain Chrome → human signs in → human quits Chrome completely → Playwright
reopens the same profile only to verify it`. The login window has a 15-minute
budget, then `terminate` and finally `kill`; SIGKILL is avoided first because it
would discard the cookie jar. The CLI tells the user to quit Chrome with
`Cmd+Q`/close every window, not just the tab.

### Consequences

- Login needs a real desktop the human can see. On a headless Linux server this
  means a VNC/X11-forwarded session; `xvfb-run` alone has no window to interact
  with. The runbook states this instead of pretending the login is unattended.
- If the human closes the window before sign-in finished, `verify` reports
  `NotLoggedInError` with the exact recovery step. The store lock is held for
  the whole plain-window lifetime, so a simultaneous login or posting run cannot
  open the same profile before Chrome writes its own `SingletonLock`.
- No password ever passes through this package, so there is no password to log,
  leak or rotate.
- A crashed login can leave a profile lock; the next run reports the busy
  profile rather than launching a second Chrome.

### Alternatives considered

- **Playwright-driven login.** Rejected: it would put the automation marker at
  the highest-scrutiny moment. The package deliberately uses no CDP there.
- **Automatic password/2FA login.** Rejected as out of scope and strictly worse:
  it repeats the riskiest event on a schedule and requires storing a password.
- **Cookie import from another browser.** Rejected: the research memo associates
  cookie transplants across tools/machines with suspensions, and the bundle
  study shows server-side session/audit context. The profile must be created on
  the machine and IP that will post. Transferring `storage_state.json` is
  documented only as a last resort in the runbook, never as the normal path.

---

## 4. Display modes: headless on macOS, Xvfb on Linux

- **Status:** Accepted
- **Verification:** VERIFIED locally (headless UA token, viewport, sizes,
  `webdriver` false, cookie persistence) / UNVERIFIED on a Linux server and for
  X's reaction to any particular display mode

### Context

The package has to run both on a developer's macOS machine and on an unattended
Linux host. Headless Chrome is convenient but advertises itself in the user
agent. Linux has no native headless substitute for a real headed browser, but
Xvfb can render a real headed Chrome into a virtual display. A normal desktop
window also keeps the window/screen numbers internally consistent.

### Decision

`Display.AUTO` resolves to:

- **Linux:** Xvfb when `Xvfb` is on `PATH`, otherwise headless.
- **macOS and everything else:** headless.

`headless`, `xvfb` and `visible` can be forced; `xvfb` fails with an install
hint if Xvfb is missing, and `visible` on Linux fails if neither `DISPLAY` nor
`WAYLAND_DISPLAY` is set. Headless uses a 1440×900 viewport. Headed runs use
`no_viewport` plus `--window-size=1440,900`, so the real window chrome is part of
the measurements. Xvfb uses screen `1920x1080x24`, a free display in `:99`–`:198`
and `-nolisten tcp`.

**Measured facts (development machine, macOS, Chrome 156.0.8078.80,
2026-11-09):**

- Headless Chrome's user agent contains the `HeadlessChrome/156.0.0.0` token;
  headed Chrome's contains `Chrome/156.0.0.0`.
- `navigator.webdriver` is `false` in both modes with this package's launch
  options, because `--enable-automation` is removed.
- Without an explicit viewport, headless Chrome's screen defaults to
  **800×600**. This package passes a 1440×900 viewport, so `doctor --tells`
  measured a 1440×900 screen with the options actually shipped. The 800×600
  default remains the reason `assess_tells` flags a screen below 1024×768 as
  unusual.
- A headed window is subject to the operating system. macOS clamps a window to
  the usable screen area, so `outerWidth`/`outerHeight` can be smaller than the
  requested 1440×900 on a small display. The code therefore treats
  `--window-size` as a request, not a guarantee; `doctor --tells` reports what
  the browser actually measured.

### Consequences

- On Linux, installing Xvfb is the recommended setup: it avoids the
  `HeadlessChrome` token entirely. `xposter doctor` warns when Linux auto falls
  back to headless because Xvfb is absent.
- On macOS, headless is the unattended default and `doctor --tells` labels the
  headless token as a `note` because it is truthful for that mode. The separate
  `display` check treats auto-headless as a `warn` and recommends
  `--display visible` for the first supervised runs, because the token is still
  visible to X.
- `visible` is useful for the first login and for `doctor --tells`, not for
  unattended posting.
- Windows has Chrome discovery paths but is not a supported target: the session
  store uses POSIX `fcntl` locking, so the package does not import on Windows.

### Alternatives considered

- **Always headless.** Rejected: it is a known marker and unnecessary on Linux
  where Xvfb is one package away.
- **Always visible.** Rejected: an unattended server has no desktop, and a
  visible window on a shared machine is intrusive.
- **Spoof the user agent or patch `navigator`.** Rejected; see decision 5.

---

## 5. Remove `--enable-automation`; patch nothing else

- **Status:** Accepted
- **Verification:** VERIFIED locally (`navigator.webdriver === false` in a real
  Chrome; `collect_tells` reports the honest values) / UNVERIFIED for X's
  server-side classifier and for CDP detection in aggregate

### Context

Playwright passes `--enable-automation` by default, and that switch makes
`navigator.webdriver` true. The research memo calls it the strongest single
"this is Playwright" signal because any site can check it in one line. The
public bundle study found no client-side `webdriver` check in X's current
client, but it did find a user-agent classifier that tags `Playwright` and
`HeadlessChrome`. Stealth plugins that override `navigator`, Canvas, WebGL or
the UA are themselves fingerprints, and they are explicitly out of scope.

### Decision

- Pass `ignore_default_args=["--enable-automation"]` to Playwright.
- Keep one launch-side Blink switch,
  `--disable-blink-features=AutomationControlled`, which removes the browser's
  automation-controlled feature rather than patching JavaScript.
- Change nothing else: no UA override, no `navigator` property overrides, no
  injected scripts, no Canvas/WebGL/audio spoofing, no proxy.
- Keep Playwright's own launch defaults in place; the plain login window
  explicitly uses `--use-mock-keychain` and `--password-store=basic` so it does
  not touch the operator's real keychain.
- `doctor --tells` only **measures** what the browser reports; it never patches
  it. A warning's fix is a different display mode, not a JavaScript override.
- `page.evaluate` is limited to read-only diagnostics and hit-testing. No init
  script, script tag, extra header, user-agent/locale/timezone override, proxy,
  request interception or runtime viewport change is used;
  `tests/test_no_injection.py` fails if one appears in `src/`.

### Consequences

- The local Chrome tests assert `navigator.webdriver` is false and that the
  headless token is the only automation-shaped value.
- CDP remains detectable in principle. The package does not claim otherwise; it
  removes the cheap deterministic flag and keeps the browser honest.
- No injected JavaScript can break the composer's own React handlers, because
  nothing is injected; the only `page.evaluate` calls are read-only reads and
  hit-tests.
- Changing Playwright's defaults later could silently reintroduce the flag; the
  integration test is the guard.

### Alternatives considered

- **A stealth plugin or custom `navigator` shim.** Rejected: it is a fingerprint
  of its own, can desynchronise from the real browser, and the research memo
  explicitly excludes fingerprint spoofing.
- **A custom user-agent string.** Rejected: a fake UA that disagrees with
  client hints and the actual rendering is an inconsistency; the real headed
  Chrome UA is the truthful one.
- **Leave `--enable-automation` in place.** Rejected: it is the one-line check a
  site runs first, and removing it costs nothing.

---

## 6. Composer and replies strategies; read ids passively from responses

- **Status:** Accepted
- **Verification:** VERIFIED against the offline fake with a real Chrome
  (composer thread, replies resume, media, duplicate, 5xx retry, challenge,
  UNKNOWN) / UNVERIFIED for the live `CreateTweet` response shape and for the
  authenticated composer DOM (see decision 10)

### Context

X's new client posts a thread as **one `Post all` click that issues one
`CreateTweet` per item, sequentially**, each replying to the previous id
(`research/web-client.md` §2.4). The alternative is to post tweet one, open its
status page, click Reply, and repeat. The composer is faster and is the normal
user path; the reply chain is slower but can be resumed after a crash because
each tweet's id is recorded before the next one starts. The public bundle study
also gives the exact response path for the new id:
`data.create_tweet.tweet_results.rest_id` (with `result` nesting fallbacks and a
`notetweet_create` variant for long posts).

### Decision

- `Strategy.COMPOSER` is the default: type every item into `/compose/post`, one
  `Post all` click, capture all ids.
- `Strategy.REPLIES` posts tweet one, then navigates to each previous status and
  replies; recorded indexes are skipped when resuming.
- Tweet ids are read **only** from X's `CreateTweet`/`DeleteTweet` responses
  through a `page.on("response")` listener registered before the click. The
  DOM and the success toast are never scraped.
- A 5xx proves the server created nothing, so one re-click is allowed when no id
  was captured. A 353 CSRF rejection waits for X's own client to refresh `ct0`
  and retry. Every other unproven post becomes `UNKNOWN`.
- The composer path is capped at 25 tweets, matching the client's `Add another
  post` limit.
- Line breaks are typed with **Shift+Enter**, never a bare Enter: X's composer
  is believed to submit on bare Return (`research/web-client.md` §1.1). This is
  **VERIFIED-LIVE** on the 2026-11-09/10 test account: two Shift+Enter presses
  made a blank line and never submitted. Recheck it after an X UI change; the
  Draft.js read-back below is what catches a composer that loses the break.
- The real-key-events requirement is also supported by an independent field
  report ([`research/field-reports.md`](research/field-reports.md)); that report
  is `REPORTED` evidence from one account, not proof or a safety claim.

### Consequences

- Composer is fast but **cannot resume a partial thread**: a crash after tweet 2
  leaves a `PENDING`/`FAILED` entry with recorded ids, and the composer refuses
  to continue it. The operator must inspect/delete the partial thread.
- Replies can resume because the ledger has each id and the next reply target
  is derivable. The cost is a page load per tweet and more page-level failure
  points.
- If X changes the response shape or the operation name, the id is not found and
  the outcome is `UNKNOWN` instead of a duplicate post. That is the safe
  failure, but it requires a human.
- The response listener is passive and read-only; it does not fabricate
  requests or headers.

### Alternatives considered

- **Scrape the success toast or the profile DOM for the new id.** Rejected:
  races the client render, breaks on localisation, and a missing toast would
  turn a successful post into a retry.
- **Always use replies.** Rejected: slower, more page loads, and the composer is
  the normal path for a thread. Replies remain the resumable option.
- **Call X's GraphQL mutation directly.** Rejected; see decision 11.

---

## 7. A JSON ledger with `UNKNOWN` as a quarantine state

- **Status:** Accepted
- **Verification:** VERIFIED by the ledger and poster tests (click barrier,
  crash between clicks, resume, duplicate prevention, force semantics,
  corrupt-file refusal, atomic writes) / UNVERIFIED only in that no live X run
  has exercised it

### Context

A duplicate thread is visible to the audience and cannot be undone. The
dangerous interval is between "the click that creates a post" and "the write
that records its id": a crash there leaves the process unable to know whether a
tweet exists. A plain "posted yes/no" flag cannot represent that state, and
retrying it blindly is exactly how duplicates happen.

### Decision

`Ledger` stores one entry per **content hash** with four states:
`PENDING`, `POSTED`, `UNKNOWN`, `FAILED`.

- `begin()` writes `PENDING` after the session is verified and before the
  composer is touched.
- A **click barrier** (`clicking_at`) is written and fsynced immediately before
  **every** Post click. A crash after the click leaves a `PENDING` row with the
  barrier set; `decide()` treats it like `UNKNOWN` and a plain rerun is blocked
  until the operator checks the account. A crash before the click is the only
  case where the barrier can be cleared.
- `record_tweet()` persists each id immediately; replaying an index is
  idempotent.
- `complete()` writes `POSTED` with the provider's record and clears the
  barrier.
- `mark_unknown()` quarantines a click whose outcome is unproven; `decide()`
  returns `BLOCKED_UNKNOWN`, and a retry raises `PostOutcomeUnknownError`
  without starting Chrome.
- `decide()` returns `ALREADY_POSTED` for a `POSTED` hash without `--force`,
  `BLOCKED_UNKNOWN` for an `UNKNOWN` row or a `PENDING` row with the click
  barrier when no recovery flag is given, `RESUME` when a recovery flag allows a
  partial thread to continue, and `NEW` otherwise.
- `--force` on a partly posted thread only **continues after the recorded
  tweets**; it never re-posts them (the replies strategy continues, while the
  composer strategy refuses because it cannot continue safely).
  `--discard-recorded` is the operator's explicit "I deleted those tweets" path
  for an `UNKNOWN` or click-barrier partial; a `FAILED` partial still takes the
  normal resume path. On a completed `POSTED` record, `--force` starts a new
  attempt while keeping the old record in `history`/`previous_completed`.
- `history` and `previous_completed` keep the earlier attempt visible when a
  forced re-run starts; a previous `POSTED` record is not silently lost.
- Writes are atomic (temp file, file fsync, `os.replace`, parent-directory
  fsync), files are `0600`, and a non-empty unreadable file raises
  `LedgerCorruptError`. A corrupt ledger **refuses to run** until the operator
  restores the file or explicitly runs `xposter ledger --reset-corrupt`; losing
  the rows would erase the duplicate-post evidence.

### Consequences

- The same content posted under a different `key` is a different thread. A
  renamed media file changes the hash; changing the bytes under the same name
  does not. Both are documented in the README.
- `--force` can duplicate when it re-opens a completed `POSTED` thread or an
  `UNKNOWN` with no recorded ids; a partial thread resumes instead. It is still
  an operator recovery action, not a repeat tool: use a new `--key` to publish
  the same text again on purpose.
- A partial composer thread must be deleted or completed manually; a partial
  replies thread can resume. For an `UNKNOWN`/click-barrier partial,
  `--discard-recorded` is the explicit assertion that the partial thread was
  deleted.
- The ledger stores only the last 500 characters of an error, redacted. It never
  stores cookies or payload bodies.
- Pruning drops old pacing events and old `FAILED` entries but never `POSTED`,
  `UNKNOWN` or `PENDING` rows, because those are the duplicate evidence.

### Alternatives considered

- **A database (SQLite).** Rejected: one process, a handful of rows per week, and
  a human-readable JSON file is easier to inspect and back up. The atomic write
  already covers the crash case.
- **No ledger; rely on X's duplicate detection.** Rejected: X's 187 window is
  undocumented (hours to 12–48 h in community reports), and a duplicate can
  land after the window. The ledger is local and permanent.
- **Delete and repost on doubt.** Rejected: deletion is a separate risky action,
  and the audience may have seen the first copy.

---

## 8. Local pacing and quiet hours before Chrome starts

- **Status:** Accepted
- **Verification:** VERIFIED by pacing/poster tests with an injected clock
  (gap, daily caps, rolling hour, quiet hours, oversized thread) / UNVERIFIED
  against X's real server-side thresholds, which are undocumented or
  contradictory

### Context

X's published limits are 50 original posts/day and 200 replies/day for
unverified accounts, with a contradictory legacy "2,400/day" sentence on the
same page, and no published hourly number. The research memo's recommendation is
to stay far below them: one thread/week, 20–25 tweets, 20–45 s between tweets,
quiet hours 23:00–07:00. The brief is a handful of threads per week, so the
tool's own brake is the useful one; relying on a 429 would mean the account is
already being throttled.

### Decision

`Pacer` enforces, before the browser starts:

- a minimum gap between threads, with a jittered `not_before` persisted in the
  ledger;
- a daily thread cap;
- a daily tweet cap;
- a rolling-hour tweet cap;
- quiet hours as a local `[start, end)` window that may wrap midnight.

Day boundaries and quiet hours use the machine's local timezone. A thread
larger than the **daily** cap is a `ValidationError` (it can never fit), not a
wait. A thread larger than the **hourly** cap is allowed only when the rolling
hour is empty: an author thread is published in one go, and splitting it to fit
the hourly number would change what readers see. `record_thread_done()` writes
the jittered next-thread time.

### Consequences

- Defaults (30 min gap, 6 threads/day, 40 tweets/day, 12 tweets/hour) are
  conservative relative to the researched caps but still allow a 25-tweet
  weekly thread. A 25-tweet thread consumes 25 of the 40 daily tweets and, after
  it, the hourly window blocks another thread until an hour has passed.
- The local caps are not a claim of safety. They reduce burst-shaped behaviour;
  they do not make the mechanism permitted.
- A wrong system clock distorts pacing. `xposter doctor` warns when the clock
  looks wrong (year < 2024).
- Quiet hours default to `None` (disabled). The runbook recommends setting
  `(23, 7)` through the Python API; there is no environment variable for it.
- Changing the policy while a run is in progress is impossible: the config is a
  frozen dataclass.

### Alternatives considered

- **Rely on X's 429/185 responses.** Rejected: the first 429 means the account
  is already being limited, and retrying escalates. Local pacing keeps the tool
  below the point where X has to react.
- **No quiet hours.** Rejected as a default choice for a human-looking account;
  it remains available because the code cannot know the operator's timezone.
- **A fixed inter-tweet delay.** Rejected: uniform timing is itself a pattern.
  The humanizer draws pauses from ranges and the pacer jitters the gap.

---

## 9. Park on a challenge, lock or rate limit; never retry

- **Status:** Accepted
- **Verification:** VERIFIED by guard, session and poster tests (challenge,
  automated flag, daily limit, locked account, parked-before-Chrome) /
  UNVERIFIED for X's live escalation behaviour, which is the reason for the
  conservative policy

### Context

The research memo's rule of thumb is unambiguous: anything mentioning
automation, verification, locking or suspension stops the run and parks the
tool; only capacity errors are retryable. Retrying a 226/228/263 challenge is
what turns a temporary block into a ban. A parked state also has to survive a
process restart so a cron or a human does not accidentally start Chrome again.

### Decision

- `guard` maps REST/GraphQL codes, HTTP statuses, redirects and visible text to
  one `Verdict` and then to a typed exception:
  `ChallengeError`, `AccountRestrictedError`, `RateLimitedError`,
  `NotLoggedInError`, `PostRejectedError`. The body-code and HTTP-status tables
  are separate (GraphQL body code 403 is an auth error; a bare HTTP 403 is a
  behavioural block). An unrecognised X error code or message **parks** instead
  of retrying, and an unparseable HTTP 403 also parks.
- During posting, visible-text rules are applied in **strict** mode to
  alert/dialog/toast text only (high-precision phrases), never the whole page,
  so a timeline post that merely discusses CAPTCHAs cannot park a healthy
  account. The broader text rules are used only by the login probe, where the
  whole page is the signal.
- The **only retry** is one re-click after a random **15–45 s** wait when an
  HTTP 5xx `CreateTweet` response was seen and no tweet id was captured yet; the
  5xx is treated as proof that the server created nothing. Every other failure
  parks, rejects or becomes `UNKNOWN`.
- `sessions.park_on()` writes `parked.json` for `ChallengeError`,
  `AccountRestrictedError` and `RateLimitedError`, preserving the first
  reason/kind/`since` on repeated parks and appending detail.
- Every entry point (`post_thread`, `delete_thread`, `login`, `verify`,
  `refresh`) calls `raise_if_parked()` before starting Chrome; the lock is taken
  only after that check.
- `PostRejectedError` (duplicate 187, too long 186) is **not** a park: the
  session is healthy, so the operator fixes the content and may post again as a
  fresh attempt. A missing reply target (385) stops the thread; after a click the
  poster records the partial outcome as `UNKNOWN`.
- A `UNKNOWN` outcome is also not a park: it blocks that thread until a human
  checks the profile, but it does not stop unrelated posts.

### Consequences

- A challenge stops the tool until a human resolves it and runs
  `xposter unpark`. `unpark` only removes the marker; it does not verify the
  account.
- A false-positive park costs an operator action; that is deliberate. The safe
  default is to stop.
- The first park's `since` is preserved, so a retry loop cannot hide how long
  the account has been waiting.
- A rate limit is parked rather than slept through. A scheduled job will fail
  with exit 4 until the operator unparks; that is visible and safe.

### Alternatives considered

- **Exponential backoff on 226/228/263.** Rejected: these are behavioural
  signals, and retrying deepens them. Only capacity errors (88/131, 5xx) get a
  bounded retry.
- **Auto-resume after a timer.** Rejected: a challenge needs a human, not a
  clock. The operator must confirm the account state.
- **Treat every failure as parked.** Rejected: a duplicate or a UI change is not
  an account problem, and parking on everything would make the tool unusable.

---

## 10. Layered selectors and `doctor --dom` calibration

- **Status:** Accepted
- **Verification:** **VERIFIED-LIVE** for the authenticated classic composer,
  two-composer scoping and a real 2-tweet thread on the 2026-11-09/10 test
  account; VERIFIED against the offline fake in both classic and new-client
  variants / the new-client authenticated DOM remains UNVERIFIED

### Context

The selector study in [`research/selectors.json`](research/selectors.json) was
made from the **logged-out public bundle**. The first authenticated runs on the
test account (2026-11-09/10) showed the live site serves the **classic
responsive-web client**, not the logged-out `x-web` client: the composer uses
`data-testid="tweetTextarea_0"` with a `contenteditable` `role="textbox"`,
`tweetButton`, `fileInput`, `toolBar` and `app-bar-close`; there is no
`<textarea>` and no `data-modal-slot-surface`.

`/compose/post` also renders **two composers**: the home-timeline inline
composer (its button is `tweetButtonInline`) sits under the compose **modal**,
and the modal itself is two nested dialogs — an outer full-screen wrapper and an
inner `[role="dialog"][aria-modal="true"]` plus a `mask`. An unscoped selector
matches both editors. The first live dry run failed safely with "no visible
match for composer_root" because the unscoped candidate resolved to the hidden
background compositor; nothing was typed or posted.

### Decision

- Every logical element has an ordered candidate list: **classic
  `data-testid` selectors first**, then new-client role/ARIA/exact-text
  selectors as fallbacks. Every composer selector is scoped to the innermost
  `[role="dialog"][aria-modal="true"]`; `composer_root` is anchored on a dialog
  containing `tweetTextarea_0`, and the new-client `data-modal-slot-surface`
  dialog is a fallback.
- `locate()` returns the first **visible** match while polling each candidate;
  `locate_all()` filters hidden duplicates out, so the background inline
  composer can never be typed into. `match_report_visible()` provides the
  visible-count companion to the raw calibration table.
- Only Playwright's CSS engine (with `:has()`, `:has-text()`, `:text-is()`) plus
  the `role=` and `text=` engines are used. No positional XPath.
- `xposter doctor --dom` opens `/compose/post`, **waits up to 20 s for the
  composer editor to render** (the first live run measured too early and
  produced false zeros), then measures every candidate in three phases: A, the
  empty composer; B, the harmless text `xposter calibration` typed into the
  first editor; C, one add-post click and `two` typed into the second editor
  (the classic `+` button only appears after text is typed). With the opt-in
  `--with-media`, phase D uploads a tiny generated PNG, records the live media
  controls/alt screens, removes the image and discards. It writes phase
  screenshots, the composer HTML with credential-shaped values sanitised and a
  JSON report to a private artifact directory, then closes and attempts to
  discard. It never submits. The live `Save post?` discard sheet itself remains
  **UNVERIFIED**; only the offline fake covers that branch.
- A missing composer selector is called out explicitly; other missing selectors
  (for example delete controls) are reported but do not block posting. If the
  composer never rendered, the report says the counts are **inconclusive**, not
  that the selectors are missing.

### Consequences

- Calibration is a required first step after login and after an X UI change. The
  code cannot auto-heal a selector table; it can only fail with artifacts and a
  report.
- The authenticated classic DOM is now the primary target. The new-client
  selectors remain useful because X can migrate the account or serve a different
  client, but they have only been exercised against the offline fake.
- The classic editor is a `contenteditable` Draft.js editor, not a `<textarea>`:
  each line is a `div[data-block="true"]`, and a blank line is an empty block
  containing only `br[data-text]`, so `innerText` over-counts newlines. The
  pre-Post read-back reconstructs the logical text from the blocks and joins
  them with one newline; the first dry run stopped safely on a false 91-vs-92
  read-back and posted nothing. A native textarea still uses `input_value()`.
- `doctor --drafts` reuses the same modal scoping and timeline-post filtering,
  but its first live run mis-read timeline posts as drafts; the scoped fix is
  offline-tested only and the command remains **UNVERIFIED live**.
- `UiChangedError` saves `screenshot.png`, sanitised `page.html` and `url.txt`
  through `SessionStore.write_artifact` (mode `0600`), prints the directory, and
  keeps only the newest `keep_artifacts` directories and those younger than
  `artifact_max_age_days` (14 days). The sanitizer redacts hidden-input values,
  CSRF `meta` tags, inline token-shaped JSON, `Authorization` headers and
  `Bearer` values; screenshots are still page content and must be treated as
  private.
- A selector that matched in the study but not in the authenticated client is an
  `UNVERIFIED` failure mode, not a test failure. A later client migration is
  the remaining risk.
- `doctor --dom` does not fail just because a selector is missing; it returns a
  report. The operator decides whether the composer can be driven.

### Alternatives considered

- **Keep new-client selectors first.** Rejected by the live observation: the
  authenticated test account served the classic DOM, so a new-client-first order
  would leave every composer selector unmatched on that account.
- **Positional XPath copied from DevTools.** Rejected: brittle and not
  diffable; a wrapper element breaks it silently.
- **Auto-generate selectors from the live DOM at runtime.** Rejected: an
  overfitted selector is worse than a clear `UiChangedError`, and it would make
  posting depend on heuristics.
- **A remote selector service.** Rejected: more moving parts and a new network
  dependency for a single-operator tool.

---

## 11. Drive the browser; do not call X's internal GraphQL directly

- **Status:** Accepted
- **Verification:** the bundle study is OBSERVED for the endpoint, query id and
  variables; the package does not implement a direct client, so there is nothing
  live to verify

### Context

`research/web-client.md` documents the `CreateTweet` endpoint
(`https://api.x.com/graphql/<queryId>/CreateTweet`), the `8TlLvl0sHLceb7rlGZZ6bg`
query id, the `createTweetNoteTweetMutation` variant, the variables and the
exact `rest_id` paths. A direct client would be much smaller than driving a
browser, and it would avoid the composer DOM entirely.

### Decision

Do not implement a direct GraphQL client. Drive the real web client and read its
responses.

### Consequences

- The browser client owns `ct0`, the CSRF refresh, the bearer token, the
  transaction/session headers and media upload. The package never fabricates
  those values or their signatures.
- Query ids and operation names rotate with deployments; the bundle study's ids
  are snapshots. A direct client would break on every rotate and would need its
  own version-tracking and error-envelope handling.
- The client enforces its own content and media limits before sending; a direct
  client would have to reimplement them, and a mistake would send malformed
  posts.
- The cost is browser weight and the DOM/selector risk (decision 10). For a few
  threads a week, that is the right trade.

### Alternatives considered

- **A direct GraphQL client with the observed query id.** Rejected: the query id,
  client headers and session assurance are deployment- and session-bound; this
  is more fragile, not less, and it bypasses the client's own checks.
- **A hybrid: browser for login, HTTP for posting.** Rejected for the same
  reason, plus it would need the transaction/session signatures the client
  obtains from X.

---

## 12. Why not the official API

- **Status:** Accepted by the operator, 2026-11-09, with the policy risk
  explicitly acknowledged
- **Verification:** the API rates and limits are `CONFIRMED` in the research
  memo; the browser path's policy exposure is also `CONFIRMED`. The decision is
  a risk acceptance, not a technical claim.

### Context

The research memo's recommendation is the opposite of this package: use the
official API. The Automation Rules permit automated posting through the API and
name non-API website scripting as a permanent-suspension trigger. The API also
gives typed errors, exact parent ids for threads and no login/session/fingerprint
surface. The memo estimates a 25-tweet thread with one link at about $0.56, or
about $2.42/month at one thread/week; if every tweet carries a link, about
$5.00/thread and $21.67/month. `POST /2/tweets` has a per-user limit of 100
requests/15 min and a per-app limit of 10,000/24 h; OAuth 1.0a access tokens do
not expire, while OAuth 2.0 PKCE access tokens last two hours without
`offline.access`.

### Decision

The operator chose the browser path for the OLSA weekly thread on 2026-11-09 and
accepted the policy risk stated in the README. The package documents the API as
the alternative that removes the mechanism risk and does not implement it. The
recommendation stays in the research memo and in [`integration.md`](integration.md),
so a later change can choose the API without rewriting this decision.

### Consequences

- No API credentials, no developer account and no paid write tier are needed.
- The account is exposed to the named suspension trigger. The tool cannot
  guarantee otherwise, and the README says so first.
- The API remains the correct fallback if the account is actioned, if the browser
  path becomes unreliable, or if the volume grows. It would be a separate
  backend (an `ApiBackend`) behind the same publisher port.
- The cost estimate is a snapshot of 2026-11-09 pricing; X can change it.

### Alternatives considered

- **Official API only.** Recommended by the research memo, rejected by the
  operator for this iteration. It remains the documented mechanism-risk
  alternative.
- **API with a browser fallback.** Rejected for now: two backends, two credential
  stores and a policy decision at runtime is more complexity than a weekly
  thread needs. The API backend can be added later behind the same port.
- **Manual posting.** Rejected: it removes the automation but also the point of
  the pipeline.

---

## 13. Mentions: type exactly, close the typeahead with a space, cap distinct mentions

- **Status:** Accepted (2026-11-10)
- **Verification:** **VERIFIED-LIVE** for a real 2-tweet post typing
  `@JayashakthiVis2` (the account's own handle) in the Draft.js composer;
  mentions of **other** accounts, notification delivery, hashtag/cashtag
  typeahead live and handles that do not exist remain **UNVERIFIED**.

### Context

A mention is not an API call: it is text typed into the composer as `@handle`.
The real run showed X recognises it (rendered blue/linked) mid-sentence, before
punctuation, before a blank line and at the end of a tweet. An earlier dry run
had exposed the hazard: typing `@handle`, `#hashtag` or `$cashtag` opens a
suggestion dropdown that stays open until a character that cannot belong to the
token is typed. A following Shift+Enter selects the highlighted suggestion
instead of inserting a newline, adds a trailing space and swallows one newline;
the pre-Post read-back caught that and stopped before any click. The succeeding
real run used the space guard and preserved the blank lines.

Mentions also carry a policy rule separate from the website-automation warning.
X's Automation Rules, section "Automated mentions and replies" (April 2026),
say: *"Automating these actions to reach many users on an unsolicited basis is
an abuse of the feature, and is not permitted."* Automated tagging of paper
authors or organisations is an unsolicited automated mention unless those users
opted in.

### Decision

- Type the **exact handle** with real key events; do not search for or select a
  dropdown item. A known username is enough for the composer to recognise it.
- When the current line — or the tweet — ends with a token matching
  `@handle`, `#hashtag` or `$cashtag`, type **one plain space** before the
  newline/end: never Enter, Tab or Escape. A space cannot belong to the token,
  so the dropdown closes harmlessly. The posted text may therefore end a line
  with a trailing space.
- The pre-Post read-back ignores **only trailing spaces at line ends and at the
  very end**; tabs, internal space runs, newlines and every other character
  still have to match exactly. A mismatch stops before the click.
- Enforce a hard cap of **5 distinct non-self mentions per thread** before any
  browser starts (`max_mentions_per_thread`). The posting account's own handle
  does not count, repeats count once, and e-mail addresses/URL paths are not
  mentions. Override deliberately with `XPOSTER_MAX_MENTIONS_PER_THREAD` or the
  global CLI `--max-mentions N`, preferably on a test account.
- Show the per-tweet mentions and thread count/cap in the preview; `doctor`
  prints the configured cap.

### Consequences

- A line-final mention leaves a trailing space in the posted text; that is
  accepted and is why the read-back tolerates trailing spaces only.
- No dropdown interaction is needed, so no menu markup/localisation dependency
  and no risk of selecting a lookalike suggestion.
- The cap will refuse an "every author in every tweet" design. That is
  intentional: the operator must decide which few accounts genuinely expect the
  tag. A wrong tag is public and permanent; a missing tag is invisible.
- A real post with the account's own handle was verified (including line-final
  and tweet-final mentions); mentions of **other** accounts, notification
  delivery, hashtag/cashtag typeahead live and handles that do not exist remain
  unverified.
- Hashtag/cashtag typeahead has the same space guard but is only
  offline-tested.

### Alternatives considered

- **Select the suggestion from the dropdown.** Rejected: menu markup and
  localisation are extra UI dependencies, and selecting a lookalike account is
  exactly the public-permanent mistake to avoid.
- **Send Tab/Enter/Escape to close the dropdown.** Rejected: Enter selects the
  suggestion, Tab moves focus, and Escape can close the composer. A plain space
  is the harmless character that cannot belong to the token.
- **Disable the typeahead with JavaScript.** Rejected: the package does not patch
  the page; it must behave like a person typing.
- **Cap per tweet instead of per thread.** Rejected: the policy concern is
  unsolicited automated mentions across the thread, so distinct handles are
  counted across all tweets.
- **No cap.** Rejected: the Automation Rules name automated mentions/replies as
  abuse; the cap is the tool's local brake.

---

## Live verification log — 2026-11-09 (test account)

**Scope:** one supervised session on the test account, home `~/.olsa/x-test`,
Chrome **156**, `--display visible`. The tag **VERIFIED-LIVE** applies only to
the observations below, on that account and that client version. It is not a
general guarantee and does not move any other item out of `UNVERIFIED`.

- **Login:** the one-time `xposter login` plain-Chrome flow completed, and the
  following `xposter verify` succeeded on the same profile. No Playwright/CDP was
  attached to the login window.
- **Cookie lifetime:** the auth cookie's expiry was about **one year** from the
  run. This is one observation; it is not a promise of session longevity.
- **Browser tells:** `xposter doctor --tells` in visible mode reported all
  checks `[ok]`: `navigator.webdriver` false, **no `HeadlessChrome` token** in
  the user agent, and real UA-CH brands.
- **Authenticated DOM:** the signed-in site served the **classic
  responsive-web client**, not the logged-out `x-web` client. The composer used
  `data-testid="tweetTextarea_0"` as a `contenteditable` `role="textbox"`, plus
  `tweetButton`, `fileInput`, `toolBar` and `app-bar-close`. There was **no
  `<textarea>`** and **no `data-modal-slot-surface`**. The new-client selectors
  from the logged-out bundle are therefore fallbacks; the selectors table now
  tries the classic testids first.
- **First `doctor --dom`:** it reported **all composer selectors missing**. That
  was a **race, not a selector failure**: it measured immediately after
  `domcontentloaded`, while React was still mounting the composer. `doctor
  --dom` now waits up to 20 s for a composer editor before measuring, then runs
  its empty/typed/thread phases without submitting.
- **Nothing was posted (at the time of this early log).** The first live thread
  test had not happened then; later runs below posted a 2-tweet thread. No
  `CreateTweet` response id, posting flow, media upload, alt editor, reply
  chain, delete or challenge path had been exercised in this early run.

### Still UNVERIFIED after this log

- The real `CreateTweet` response shape over the network and the `rest_id`
  extraction (the bundle study and the fake are the only evidence).
- A real composer thread, the replies strategy, the 25-item cap, media upload,
  alt-text editor, delete flow and the success toast.
- Live 403/429/226/challenge UIs and whether the guard's exact mapping matches
  them.
- Whether the session survives days/weeks, whether keepalive actually extends
  it, and `ct0` behaviour.
- Whether X flags, locks or suspends the account. This cannot be verified from a
  successful post; the policy warning remains unchanged.

*(These were the gaps before the first real runs; the next section records which
were closed and which remain.)*

---

## Live verification log — first real runs (2026-11-09/10)

**Scope:** test account **@JayashakthiVis2**, home `~/.olsa/x-test`, Chrome
**156–157**, visible mode except where headless is stated. These are the first
real runs. They supersede the earlier "nothing posted" note, but the earlier
observations above remain part of the record. Everything is `VERIFIED-LIVE` only
for this account/client/date and low volume; it is not a long-term guarantee.

1. **Login and cookie.** The plain-Chrome one-time login followed by
   `xposter verify` succeeded, and the auth cookie was valid for about
   **one year**.
2. **Classic authenticated DOM.** The signed-in site served the classic
   responsive-web client, not the logged-out `x-web` client. The composer used
   `tweetTextarea_0` as a `contenteditable` `role="textbox"`, `tweetButton`,
   `fileInput`, `toolBar` and `app-bar-close`; there was no `<textarea>` and no
   `data-modal-slot-surface`.
3. **Two composers on `/compose/post`.** The page renders the home-timeline
   inline composer (its button is `tweetButtonInline`) underneath the compose
   **modal**, and the modal itself is two nested dialogs: an outer full-screen
   wrapper and an inner `[role="dialog"][aria-modal="true"]` plus a `mask`.
   Unscoped selectors match both composers. Every composer selector is now
   scoped to the `aria-modal` dialog and `locate()` returns the first **visible**
   match. Before that fix the first live dry run failed safely with
   **"no visible match for composer_root"**; nothing was typed or posted.
4. **Real key events and the add-post button.** Per-key `keyboard.type` events
   enabled the Post button; clipboard/JS input had not. The add-post `+` button
   appears only after the first text is typed; once it appends a second editor,
   the footer button reads **"Post all"**.
5. **Blank lines.** Two `Shift+Enter` presses produced a blank line and never
   submitted the post. The earlier bundle-study inference is now confirmed live.
6. **Draft.js editor.** The editor is Draft.js: each line is a
   `div[data-block="true"]`, and a blank line is an empty block containing only
   `br[data-text]`. `innerText` therefore over-counts newlines. The read-back
   reconstructs the logical text from the blocks and joins them with one
   newline. The first dry run stopped safely on the old false alarm: expected
   **91** characters, saw **92** at the blank line; nothing was posted.
7. **Watched dry run.** After those fixes, a watched 2-tweet dry run passed:
   both tweets typed, the read-back matched exactly, the composer was
   discarded, nothing was posted and the ledger stayed empty.
8. **First real thread.** A 2-tweet **composer**-strategy thread was posted with
   "Post all". Both ids were read passively from X's own `CreateTweet`
   responses, the ledger recorded `POSTED`, opening tweet 2 showed tweet 1 above
   it in the same conversation (the reply chain and the blank line were
   correct), and local pacing then blocked the next post for the configured
   **30-minute** minimum gap.
9. **Delete.** `xposter delete` removed both test tweets through the UI; their
   status pages then showed **"this page doesn't exist"**.
10. **Headless session use.** In headless mode (user agent `HeadlessChrome`),
    `verify` and `refresh --force` both worked: X accepted the saved session,
    no challenge fired and the tool was not parked. That is one run each at low
    volume. It is **not** evidence that headless stays unflagged over time, and
    `HeadlessChrome` remains an explicit bot-class user agent in X's web client
    study; on Linux prefer Xvfb.
11. **Keepalive activity.** `refresh` only loads `/home` and scrolls; it does
    not like, follow, repost, post or click.

### Still UNVERIFIED after the first real runs

- The **REPLIES** strategy live (the composer strategy is verified for a
  2-tweet thread only).
- **Media upload and alt text** live.
- **Premium long posts / note-tweets** (`createTweetNoteTweetMutation`).
- **`doctor --drafts`** live: its first live run mis-read timeline posts as
  drafts; the scoped fix is offline-tested only. The operator should check the
  account's Drafts panel manually once to confirm no unsent draft was left by
  the earlier calibration run.
- The live **calibration discard sheet** (`Save post?` flow) — offline fake only.
- **Xvfb/Linux** operation.
- Anything about **long-term account safety**, challenge/lock pages, rate
  limits, or the 12-hour keep-alive cadence over weeks.
- Behaviour of a **20+ tweet thread**.
- The **submit-shortcut fallback** (`submit_shortcut_fallback`, off by default).

*(Image support was verified in the next log; the media/alt bullet above was
written before those runs.)*

---

## Live verification log — images (2026-11-10)

**Scope:** test account **@JayashakthiVis2**, home `~/.olsa/x-test`, Chrome
**157**, **visible** mode. `VERIFIED-LIVE` only for these images/date; nothing
here is a long-term safety claim.

1. **Attachment path and upload.** Images attach through the modal's hidden
   `[data-testid="fileInput"]` with `set_input_files`; the media picker is never
   clicked. X starts uploading immediately to `/i/media/upload.json`, and the
   image lands in the **focused** editor. Per-tweet images work: a real 2-tweet
   thread with one image on each tweet posted, the status pages showed a
   **1200×675** image on tweet 1 and an **800×450** image on the reply, the
   thread chain was intact, and both tweets were then deleted through
   `xposter delete`.
2. **Live media markup.** `[data-testid="attachments"]` contains a
   `div[role="group"][aria-label="Media"]` with `<img src="blob:…">`,
   `button[aria-label="Edit media"]` and `button[aria-label="Remove media"]`.
   "Tag people" and "Add description" are plain spans under the image, not
   labelled buttons.
3. **Readiness with a blob preview.** The preview `<img>` can report
   `naturalWidth`/`naturalHeight` 0 while it is visibly rendered. Readiness
   therefore accepts a rendered `blob:`/`data:` preview with a non-empty box plus
   its Edit/Remove controls, **and** requires the Post button to be enabled. The
   footer's always-present character-counter progressbar is never treated as
   upload progress.
4. **Empty attachments container.** It has no visible box, so counting must use
   dedicated locators for `img` inside the attachments/Media container. An
   earlier generic fallback counted the user's avatar (baseline 1); the first
   live image dry runs timed out safely before any click.
5. **Discard path.** Closing a dirty composer shows
   `confirmationSheetDialog` ("Save post?", `confirmationSheetConfirm` = "Save",
   `confirmationSheetCancel` = "Discard"). `Discard` is clicked; `Save` is never
   clicked.
6. **`doctor --dom --with-media` (opt-in).** Phase D uploads a tiny generated
   PNG, records the live attachment markup and the alt screens, removes the image
   and discards the composer. The first alt click shows a **one-time intro sheet**
   (`sheetDialog` "Add descriptions", buttons "Sure" / "No, thanks"); only
   **"No, thanks"** may be clicked. It sits over the "Edit image description"
   screen (back arrow, title, black "Save"). **Escape closes too much**; the
   editor must be left via the back arrow.

### Still UNVERIFIED for images

- **Alt text end to end.** The text field and Save on "Edit image description"
  have never been observed live; alt is **off by default** and requires
  `XPoster(allow_alt_text=True)` / CLI `--allow-alt`, otherwise a
  `ValidationError` is raised before any browser starts.
- More than **2 images**, or **4 images per tweet**, live.
- **GIF/WebP/JPEG** uploads live (only PNG was tested).
- Images near the **5 MiB** limit, slow uploads, and images on the **replies**
  strategy.
- **Video**: explicitly unsupported.
- The **submit-shortcut fallback** with media.
- Whether any of this changes X's account-level risk (it does not change the
  policy warning).

---

## Live verification log — mentions (2026-11-10)

**Scope:** test account **@JayashakthiVis2**, **own handle only**, Chrome **157**,
visible mode.

### Dry run (earlier)

- Typing `@handle` in the real Draft.js composer was recognised as a mention
  (rendered blue), both mid-sentence and before punctuation.
- **Hazard found live:** X opens a mention/hashtag/cashtag suggestion dropdown
  while typing a token and leaves it open until a character that cannot belong
  to the token arrives. A following `Shift+Enter` **selects the suggestion**,
  adds a trailing space and swallows one newline, which would lose a blank
  line.
- The pre-Post read-back caught the swallowed newline and stopped before any
  click. The fix in `_type`: when a token ends a line or the tweet, type one
  plain **space** first (never Enter/Tab/Escape). The read-back tolerates only
  trailing spaces at line ends and at the very end.
- That run posted nothing.

### Real post (same day)

A 2-tweet **composer** thread was posted. The status pages showed every
`@JayashakthiVis2` as a real link (`<a href="/JayashakthiVis2">`): two in tweet 1
and one in tweet 2, including a **line-final mention before a blank line** and a
**tweet-final mention**. The blank lines survived and the line-final guard space
was preserved in the posted text. Both tweets were then deleted through
`xposter delete`, and the status pages returned 404. Because only the account's
own handle was used, nobody else was notified.

### Still UNVERIFIED for mentions

- Mentions of **other accounts**, and whether X resolves/notifies those users as
  expected. Only the account's own handle has been posted.
- `#hashtag` and `$cashtag` typeahead live (the same space guard exists, but it
  is only offline-tested).
- Handles that do not exist (the composer may reject, mis-resolve or leave the
  text unresolved).
- The default cap is 5 distinct non-self handles per thread; see decision 13.
  A real author-tagging design must be a deliberate operator decision.

## Verification matrix

| Area | Evidence | Status |
| --- | --- | --- |
| Public API and lazy Playwright import | `tests/test_public_api.py` | VERIFIED (offline) |
| Weighted splitting, URLs, emoji, Unicode bold | `tests/test_splitter.py` against the twitter-text v3 rules | VERIFIED (offline; documented approximation for emoji sequences) |
| Error-code and text classification | `tests/test_guard.py` | VERIFIED (offline) |
| Ledger states, atomic writes, redaction | `tests/test_ledger.py` | VERIFIED (offline) |
| Pacing, quiet hours, day boundaries | `tests/test_pacing.py` | VERIFIED (offline, injected clock) |
| Session store, permissions, parking, backup | `tests/test_store.py`, `tests/test_sessions.py` | VERIFIED (offline) |
| Real Chrome launch, `webdriver`, viewport, headless token, cookie persistence | `tests/test_browser_integration.py` | VERIFIED (local Chrome, no network) |
| No init scripts, headers, UA/locale/timezone overrides, proxies or runtime viewport changes | `tests/test_no_injection.py` | VERIFIED (source guard) |
| Composer/replies posting, media, 5xx retry, challenge, UNKNOWN | `tests/test_poster.py` with `tests/fake_x/` | VERIFIED (real local Chrome, in-process fake; **not** live X) |
| Typed-text read-back before Post | `tests/test_poster.py` with `tests/fake_x/` | VERIFIED (offline fake) |
| Alt-text editor application | `tests/test_poster.py` with `tests/fake_x/` | VERIFIED (offline fake) / **UNVERIFIED live end to end** |
| Image validation (magic/extension, 5 MiB / 15 MiB GIF, GIF mixing, duplicates, zero-byte, video) | `tests/test_media.py`, `tests/test_poster.py` | VERIFIED (offline) |
| Per-tweet image attach/upload and delete | live test account, 2026-11-10 | **VERIFIED-LIVE** (2-tweet thread, 1200×675 + 800×450, chain intact, both deleted) |
| Image variants: >2 images, 4 per tweet, GIF/WebP/JPEG, near 5 MiB, slow upload, replies strategy | — | **UNVERIFIED live** (only PNG, one image per tweet, 2 tweets) |
| `doctor --dom --with-media` phase D | live test account, 2026-11-10 | **VERIFIED-LIVE** for upload/markup/remove/discard and the intro-sheet/back-arrow path; alt field/Save UNVERIFIED |
| Video upload | code validation only | **UNSUPPORTED by design** |
| Mention extraction/cap (distinct, own handle excluded, e-mails/URLs ignored) | `tests/test_mentions.py`, `tests/test_poster.py`, `tests/test_cli.py` | VERIFIED (offline) |
| Typed `@handle` recognition and typeahead space guard | live test account, 2026-11-10 (real 2-tweet post, own handle only) | **VERIFIED-LIVE** for own handle incl. line-final/tweet-final mentions and blank lines; other accounts/notifications/hashtag/cashtag/nonexistent handles UNVERIFIED |
| Mentions of other accounts, notification delivery, hashtag/cashtag typeahead, nonexistent handles | — | **UNVERIFIED live** |
| Selector fallbacks, classic and new-client DOM | `tests/test_selectors.py`, `tests/test_fake_x.py` + live `--dom` observation | VERIFIED (offline fake) / **classic authenticated DOM VERIFIED-LIVE**; new-client authenticated DOM UNVERIFIED |
| CLI commands and exit codes | `tests/test_cli.py` | VERIFIED (offline) |
| Plain-Chrome login + `verify` | live test account, 2026-11-09/10 | **VERIFIED-LIVE**; 2FA and unusual-activity block UNVERIFIED |
| `doctor --tells` in visible mode | live test account, 2026-11-09 | **VERIFIED-LIVE** (webdriver false, no `HeadlessChrome`, real UA-CH brands) |
| Authenticated classic composer DOM | live test account, 2026-11-09/10 | **VERIFIED-LIVE** (classic testids, `contenteditable` Draft.js, two-composer scoping) |
| Two composers / nested dialogs scoping | live test account, 2026-11-09/10 | **VERIFIED-LIVE** (dry run and 2-tweet thread); `doctor --dom` reports per-phase visible counts |
| Real `CreateTweet` response shape and `rest_id` paths | live 2-tweet composer thread, 2026-11-10 | **VERIFIED-LIVE** for the simple composer thread; note-tweet/Premium UNVERIFIED |
| Live composer thread, delete flow | live test account, 2026-11-10 | **VERIFIED-LIVE** (2 tweets, `POSTED` ledger, correct reply chain, delete removed both) |
| Replies strategy, alt text, 20+ tweets | — | **UNVERIFIED live** |
| `doctor --drafts` | first live run mis-read timeline posts; scoped fix offline-tested | **UNVERIFIED live**; check Drafts manually once |
| Calibration discard sheet (`Save post?`) | offline fake only | **UNVERIFIED live** |
| Headless `verify` / `refresh --force` | live test account, one run each | **VERIFIED-LIVE** at low volume; not long-term evidence |
| Xvfb/Linux | — | **UNVERIFIED** |
| `submit_shortcut_fallback` | offline fake only | **UNVERIFIED live** |
| `auth_token`/`ct0` lifetime and keepalive effectiveness | live test account (auth expiry ~1 year; `refresh` only `/home`/scroll) + research memo | `auth_token` expiry and one keepalive run **VERIFIED-LIVE**; `ct0` and 12-hour cadence over weeks UNVERIFIED |
| Long-term safety, challenges, rate limits, 20+ tweets | — | **UNVERIFIED** |
| Whether X flags, locks or suspends the account | — | **Not guaranteeable** |

The remaining live acceptance tests are the ones listed above as **UNVERIFIED**:
the replies strategy; alt text end to end; Premium note-tweets; the untested
image variants (>2 images, 4 per tweet, GIF/WebP/JPEG, near-limit/slow uploads,
images with replies); mentions of other accounts, notification delivery,
hashtag/cashtag typeahead and nonexistent handles; `doctor --drafts` and the
live discard sheet; Xvfb/Linux; 20+ tweet threads; the submit-shortcut fallback;
and long-term challenge/rate-limit/12-hour keepalive behaviour. The runbook
covers the sequence.
