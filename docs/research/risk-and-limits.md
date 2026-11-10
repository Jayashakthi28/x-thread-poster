# Posting threads to one's own X account: risk and limits

**Status:** research memo, compiled 2026-11-09. **Scope:** a standalone tool that posts threads to the operator's own X account
through a real Chrome driven by Playwright, one manual login, a long-lived saved session, a handful of threads per week.

**Confidence tags.** `CONFIRMED` — stated by X's docs/policies/help pages, Playwright docs, or a Chromium source. `REPORTED` — a
named community source (GitHub issue, dev forum, vendor post); directional only. `UNKNOWN` — no adequate source found; do not build
on it. URLs fetched 2026-11-09 unless the source carries its own date; community sources are dated individually because X's
behaviour drifts.

## 0. Executive finding — read this first

X's Automation Rules, *updated April 2026*, list under **"Don't!"**:

> "Use non-API-based forms of automation, such as scripting the >x< website. The use of these techniques may
> result in the permanent suspension of your account."
> — https://help.x.com/en/rules-and-policies/x-automation (updated April 2026) `CONFIRMED`

That describes this project's architecture exactly: a script driving x.com's web UI to post. It is not a grey area about velocity or
fingerprints — the mechanism itself is named.

X's Authenticity policy reinforces it: "**Unauthorized automation:** Automated or scripted accounts that do not comply with our
Developer Policy", with enforcement including "Anti-spam challenges" (accounts "may be locked and prompted to provide additional
information (e.g., a phone number) or to solve a puzzle"), "Temporary loss of access", and "Suspension" —
https://help.x.com/en/rules-and-policies/platform-manipulation (April 2025) `CONFIRMED`.

The ToS (effective 2026-10-09) adds a contractual layer: no access "by any means (automated or otherwise) other than through our
currently available, published interfaces", and no interference "by scripting the creation of Content in such a manner as to
interfere with or create an undue burden on the Services" — https://x.com/en/tos `CONFIRMED`.

**Consequence for the brief.** The operator's top requirement is "must not get flagged, locked or blocked". Browser automation of
posting is the one approach X explicitly reserves permanent suspension for. No amount of pacing, hygiene or "real Chrome" changes
that classification. The API path is not merely lower-risk; at this volume it is also cheap (§1.5). Sections 5–8 are a residual-risk
reference, not a way to make the browser path safe.

## 1. The official API as the zero-detection alternative

### 1.1 Tiers and pricing in 2026

The old Free/Basic/Pro ladder is gone for new developers. The model is **pay-per-usage**: "pay only for what you use", aimed at
"hobbyists, commercial prototyping, early-stage X product integrations, and supporting applications with limited end-users" —
https://docs.x.com/x-api/getting-started/pricing and https://docs.x.com/developer-terms/policy `CONFIRMED`.

| Item | Rate | Source |
| --- | --- | --- |
| Post: Create | **$0.015 / request** | docs.x.com pricing `CONFIRMED` |
| Post: Create **with URL** | **$0.200 / request** | docs.x.com pricing `CONFIRMED` |
| Post: Create (summoned) | $0.010 / request | docs.x.com pricing `CONFIRMED` |
| Post: Read | $0.005 / resource | docs.x.com pricing `CONFIRMED` |
| Owned Read (own posts/mentions/etc.) | $0.001 / resource | docs.x.com pricing `CONFIRMED` |
| Cap | 3,000,000 Post reads / monthly cycle, then Enterprise | docs.x.com pricing `CONFIRMED` |

**No free write tier exists for a new developer.** Secondary sources agree the free tier ended 2026-02-06, replaced by pay-per-use
with a one-time $10 voucher for then-active free users (https://www.outstand.so/blog/x-api-pricing, updated 2026) `REPORTED`. X's
docs no longer document any Free plan (`CONFIRMED` by absence). Enterprise is sales-led; ~$42k/month entry is commonly cited
(`REPORTED`).

### 1.2 Write limits for `POST /2/tweets`

| Endpoint | Per app | Per user |
| --- | --- | --- |
| `POST /2/tweets` | 10,000 / 24 hrs | **100 / 15 min** |
| `DELETE /2/tweets/:id` | — | 50 / 15 min |

— https://docs.x.com/x-api/fundamentals/rate-limits `CONFIRMED`. These are *transport* limits; the *account* caps in §3 also apply,
because "These limits include actions from all devices, including web, mobile, phone, API, etc." —
https://help.x.com/en/rules-and-policies/x-limits `CONFIRMED`.

### 1.3 Threads and replies

`POST /2/tweets` takes `reply.in_reply_to_tweet_id` (required if `reply` is present) and returns `data.id`, the parent id for the
next tweet — so a thread is N sequential calls, each independently retryable — https://docs.x.com/x-api/posts/create-post
`CONFIRMED`. Same page: `quote_tweet_id` (quote posts) **requires Enterprise** and is unavailable on pay-per-use; `reply_settings`
accepts `following | mentionedUsers | subscribers | verified`; `poll` takes 2–4 options of 1–25 chars; a post may carry up to 4
photos, 1 GIF or 1 video. All `CONFIRMED`.

### 1.4 Media, auth and token lifetimes

**Media.** v1.1 survives only for "some media upload endpoints". `POST /2/media/upload` handles images/small files; video uses
chunked `initialize → append → finalize`. Limits: 5 MB image, 15 MB GIF, 20 min / 8 GB video (125 min / 16 GB with Premium),
re-checked at post time — https://docs.x.com/x-api/media/introduction `CONFIRMED`.

**OAuth 2.0 PKCE.** "By default, the access token you create through the Authorization Code Flow with PKCE will only stay valid for
**two hours** unless you've used the `offline.access` scope." With `offline.access` you get a refresh token and can mint access
tokens without the user. Scopes: `tweet.read tweet.write users.read offline.access` (+ `media.write`). Native/SPA apps are public
clients; "Web App, Automated App or bots" are confidential clients with a secret. The auth code expires in **30 seconds** —
https://docs.x.com/resources/fundamentals/authentication/oauth-2-0/authorization-code `CONFIRMED`. Refresh-token lifetime is
undocumented; a dev-forum reply says 6 months and that rotation resets the clock —
https://devcommunity.x.com/t/access-token-expires-in/164425 (2022) `REPORTED`.

**OAuth 1.0a.** "These tokens do not expire but can be revoked by the user at any time" —
https://docs.x.com/fundamentals/authentication/oauth-1-0a/obtaining-user-access-tokens `CONFIRMED`; "Access tokens are not
explicitly expired ... invalidated if a user explicitly revokes an application ... or if X suspends an application" —
https://docs.x.com/fundamentals/authentication/faq `CONFIRMED`.

For a single-operator bot **OAuth 1.0a access token+secret is the pragmatic choice**: no refresh loop, no 2-hour clock, and it is
the credential X recommends for "acting as yourself ... Testing, personal bots, your own automation" —
https://docs.x.com/x-api/getting-started/getting-access `CONFIRMED`. OAuth 2.0 PKCE + `offline.access` is better only if the tool
may later serve other accounts.

### 1.5 What this actually costs for the OLSA use case

One 25-tweet thread where **only the first tweet carries the arXiv link**: 24 × $0.015 = $0.36, plus 1 × $0.200 = **≈ $0.56 per
thread → ≈ $2.42/month at one thread/week**. If every tweet carries a link: 25 × $0.20 = **$5.00 per thread → ≈ $21.67/month**.
Arithmetic from the rate card above; `CONFIRMED` inputs, my calculation. **Design implication:** put the link in one tweet and keep
the rest link-free — that decision is the difference between ~$2.50 and ~$22 a month.

### 1.6 Recommendation — API vs browser

**Use the API.** In order of weight: (1) **it is permitted** — "you may post automated posts for entertainment, informational, or
novelty purposes" (Automation Rules II.B.1, April 2026) `CONFIRMED`, and the Developer Policy requires write-actions to "follow the
Automation Rules" rather than forbidding them `CONFIRMED`; (2) **zero account-lock surface** — no login, session, fingerprint or
challenge; (3) **cost is trivial** (§1.5); (4) **failure modes are typed** and retryable (§8), unlike a UI that says "Something went
wrong"; (5) **threading is exact** — you get the parent id back and chain on it. The API is *worse* only for quote posts
(Enterprise-only) and web-UI-only features; neither applies to a plain digest thread.

**What a thin `ApiBackend` needs** (mirroring the existing `ports/` style):

- `submit_thread(tweets: list[str]) -> list[str]` — POST each, chain `in_reply_to_tweet_id`, return ids.
  Idempotency key = hash of thread content + scheduled slot, persisted before the first POST (ADR-0013).
- `upload_media(path) -> media_id` (only if images are needed); `check_credentials() -> bool` via one cheap
  `GET /2/users/me` at startup.
- Credentials from `~/.olsa/secrets.json` via the existing secrets store; never in the repo.
- Error enum: `RETRYABLE` (429/5xx/duplicate-race), `FATAL_AUTH` (401), `FATAL_POLICY` (403
  client-forbidden), `UNKNOWN`. Dry-run mode that renders and prices the thread without posting.

Rough size: ~200 lines plus tests against a fake — smaller than the Playwright path, not larger.

## 2. X's rules on automation — exact wording

**(a) Browser automation of posting to your own account.** Prohibited by name (the "Don't!" quote above) `CONFIRMED`. The rule is
about the *method*, not account ownership and not volume. Owning the account is no defence; the same page says "You are ultimately
responsible for the actions taken with your account".

**(b) Scraping.** "you cannot scrape the Services without X's express written permission" (ToS summary, effective 2026-10-09)
`CONFIRMED`; "crawling or scraping the Services in any form, for any purpose without our prior written consent is expressly
prohibited" (ToS §4) `CONFIRMED`. The Developer Policy separately prohibits circumventing rate limits `CONFIRMED`.

**(c) Automated posting without the API.** Covered by (a). Via the API it is allowed within the Automation Rules; outside the API it
is the named suspension trigger.

**(d) Consequences X lists.** From the Authenticity policy enforcement section, verbatim headings: "Anti-spam challenges" (accounts
"may be locked and prompted to provide additional information (e.g., a phone number) or to solve a puzzle"), "Denylisting URLs",
"Restricting Reach", "Temporary loss of access to >x< features or products", "Profile Modifications", "Suspension" ("For severe
violations, accounts will be permanently suspended at first detection"). April 2025 `CONFIRMED`. Note "permanently ... at first
detection": there is no guaranteed warning ladder for automation violations.

## 3. UI posting limits

| Limit | Value | Confidence |
| --- | --- | --- |
| Original posts/day, unverified | **50** | `CONFIRMED` help.x.com/x-limits |
| Replies/day, unverified | **200** | `CONFIRMED` same |
| Posts/day (legacy text on same page) | 2,400, "broken down into smaller limits for semi-hourly intervals" | `CONFIRMED` as text; **page contradicts itself** |
| Posts/day, Premium | 2,400 combined | `REPORTED` (press/community, May 2026) |
| DMs/day | 500 | `CONFIRMED` |
| Follows/day | 400 + ratio rules after 5,000 | `CONFIRMED` |
| Email changes/hour | 4 | `CONFIRMED` |

The page lists 50/200 and then, two paragraphs later, still says "The post limit of 2,400 updates per day...". Both sentences are on
https://help.x.com/en/rules-and-policies/x-limits (2026-11-09); May 2026 coverage:
https://gigazine.net/gsc_news/en/20260519-x-post-limits `REPORTED`. **Plan against 50/200** — a 25-tweet thread consumes half the
allowance.

- **Per-hour behaviour.** No published hourly number, only "semi-hourly intervals" `CONFIRMED`; treat as
  opaque and stay far below it.
- **Thread length in the composer.** No published maximum `UNKNOWN`; a 20–25 post thread is unremarkable in
  normal use (judgment).
- **Duplicate content (187).** v1.1 returns `{"code":187,"message":"Status is a duplicate"}`; v2 returns HTTP
  403 with detail "You are not allowed to create a Tweet with duplicate content."
  — https://devcommunity.x.com/t/error-codes/173956 `CONFIRMED` for both shapes. The comparison window is
  undocumented; community reports range "hours" to "12–48h" `REPORTED`.
- **Links.** Prohibited: "repeatedly posting or sending direct messages consisting of links shared without
  commentary, so that this comprises the bulk of your post/direct message activity" (Authenticity policy,
  April 2025) `CONFIRMED`. A digest thread with commentary per tweet is fine; a wall of bare links is not.
- **Hashtags.** Prohibited: "posting with excessive, unrelated hashtags in a single post or across multiple
  posts". Same page `CONFIRMED`.
- **New / low-trust accounts.** No published trust score; the 50/200 cap is defined by *unverified* status,
  not account age `CONFIRMED`. Anecdotes about harder throttling of new accounts are unverifiable
  `REPORTED`/low confidence.
- **Error 226.** HTTP **403** carrying body code **226**: "This request looks like it might be automated. To
  protect our users from spam and other malicious activity, we can't complete this action right now. Please
  try again later." X staff confirmed the code and purpose —
  https://devcommunity.x.com/t/error-code-226-this-request-looks-like-it-might-be-automated/12872
  `CONFIRMED`. Reported triggers: too fast, too repetitive, or a low-trust account `REPORTED`. Behavioural,
  not a rate limit — retrying immediately makes it worse.

## 4. How X detects automation (publicly known)

**At login — the most scrutinised moment.** The observed block string, captured from `api.x.com/1.1/onboarding/task.json`:

> "We blocked an attempt to access your account because we weren't sure it was really you. This happens when
> we notice unusual login activity, like an attempt to log in too many times, or from a different location or
> device. You'll need to wait before trying to log in again."
> — https://github.com/daijro/camoufox/issues/274 (opened 2025-04-16) `CONFIRMED` as an observed string

In that thread a plain Playwright browser *could* still log in ("they may hit you with a verification challenge but don't block at
this stage") while a fingerprint-hardened Firefox build was blocked every time — evidence that X's login checks are not a simple "is
this automated" test, and that over-clever fingerprints can be *worse* `REPORTED`. Arkose/FunCaptcha challenges on X login are
widely reported and X is a known Arkose customer, but no first-party page names the vendor `REPORTED`, medium confidence. "Could not
log you in now" I could not trace to a dated credible source — `UNKNOWN`, do not build logic on it.

**At session use.**

- **IP / ASN / geo change.** Reported by every practitioner source; no X documentation `REPORTED`, medium
  confidence. Datacenter ASNs are widely claimed to be treated worse than residential — vendor blogs only,
  all selling proxies: https://xautodm.com/blog/best-proxies-for-twitter-x-automation-2026 (2026) `REPORTED`,
  low confidence.
- **Device change / new-device email.** X sends login and security notification emails; a new device is
  visible to the user and presumably to X `REPORTED`.
- **Cookie transplant across machines/tools.** Directly implicated in bans: users who logged in via the
  twikit library were suspended, while "for any account that i manually extract the cookies from the browser,
  i dont get banned" — then later in the same thread others report bans *with* browser-extracted cookies too.
  https://github.com/d60/twikit/issues/359 (May–Sep 2025) `REPORTED`. Read it as: transplanting a session
  beats logging in from the tool, and is still not safe.

**At posting time.** Velocity, identical/near-identical content, link-only posts and unsolicited mentions are the four signals with
actual policy text behind them (Authenticity policy, April 2025) `CONFIRMED`. Community adds posting the same text to multiple
accounts, and many posts within minutes of a fresh login `REPORTED`.

**Which of these applies to a long-lived session on a stable machine/IP?** Judgment, flagged as such: **login challenges** are
*mostly avoided* if you log in once by hand and never re-authenticate; **IP/geo/device instability** is *avoided* if the machine and
network never change (the strongest argument for the "log in by hand, reuse the profile" pattern); **posting-time behavioural
signals** are *not avoided at all* — they are content- and cadence-based, and this is where a digest bot actually gets caught; and
**the policy violation is never avoided**, because it is about the mechanism.

**Why the community recommends "log in once by hand in a normal browser, then reuse the profile".** The reasoning in the sources is
consistent: login is the highest-scrutiny event, and every extra login is another chance to trip the unusual-activity block; reusing
a session avoids repeated logins and keeps the device/IP constant. The twikit thread states the pattern most clearly
(https://github.com/d60/twikit/issues/359, 2025-06-11) `REPORTED`. It is a real reduction in one class of risk, not a defence
against §4.3 or §2.

**What actually got accounts locked in 2025–2026, per the sources I could read:** library-driven login (twikit, repeatedly),
high-frequency write bursts ("after 6 tweets in a short period of time your account will get suspended" — twikit #359, 2025-09-10)
`REPORTED`, and cookie/session sharing across tools. I found **no** credible report of a low-volume, human-paced, single-account,
stable-IP browser poster suspended *specifically for pacing*. That is absence of evidence, not evidence of safety.

## 5. Browser hygiene — what each signal is evidence of

None of these make the activity permitted; they change how *visible* the automation is. This project **excludes CAPTCHA solving,
fingerprint spoofing and proxy rotation**; where a source discusses them they are noted only as existing.

| Signal | What it is evidence of | Likely to matter at low volume on an established account? |
| --- | --- | --- |
| Bundled Chromium vs real Google Chrome | Playwright defaults to open-source Chromium; `channel: 'chrome'` runs branded Chrome — https://playwright.dev/docs/browsers `CONFIRMED`. Chromium's `Sec-CH-UA` brand list lacks `"Google Chrome"` while Chrome's includes it — https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Sec-CH-UA `CONFIRMED`. A UA claiming Chrome with a brand list lacking it is an inconsistency. | Low-moderate. Removes one cheap inconsistency; does not hide CDP. |
| Headless shell vs new headless | Playwright ships a separate `chromium headless shell`; new headless is `channel: 'chromium'` and is "the real Chrome browser" — playwright.dev/docs/browsers `CONFIRMED`. New headless has been observed sending `Sec-CH-UA: "HeadlessChrome";v="113"` — https://issues.chromium.org/issues/40768416 (2023) `REPORTED`; detection vendors still list `HeadlessChrome` as live in 2026 — https://www.verisoul.ai/agents/playwright (2026) `REPORTED`. Some posts claim it was removed; **conflicting**. | Moderate if headless. Use headed or new-headless and verify the actual UA/CH on the target machine. |
| Headed Chrome under Xvfb (Linux) | A real window and rendering path; avoids the headless shell. Community argues headful is a "more natural baseline" but not undetectable — https://scrapingant.com/blog/headless-vs-headful-browsers-in-2025-detection-tradeoffs (2025) `REPORTED`. | Marginal vs new headless for X specifically; no X-specific evidence either way `UNKNOWN`. |
| Persistent `user-data-dir` vs `storage_state` | `storageState` saves cookies + localStorage (+ IndexedDB if requested), **not** sessionStorage — https://playwright.dev/docs/auth `CONFIRMED`. A persistent context keeps the whole profile. Playwright warns **not** to point it at your real Chrome profile — https://playwright.dev/docs/api/class-browsertype#browser-type-launch-persistent-context `CONFIRMED`. | Persistent dir is the better fit: full cookie jar and device-bound state survive across runs. |
| Stable IP / timezone / locale / UA | Consistency, not authenticity. A machine that never changes region looks like a person who never travels. | **High value, low cost.** Cheapest real risk reduction available. |
| `--enable-automation` / `navigator.webdriver` | Playwright passes `--enable-automation` by default (`chromiumSwitches.ts`: `assistantMode ? "" : "--enable-automation"`) — https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/chromium/chromiumSwitches.ts `CONFIRMED`; corroborated by https://github.com/NousResearch/hermes-agent/issues/11073 (2026) `REPORTED`. `navigator.webdriver` is true when headless, when `--enable-automation` is set, **or when a remote-debugging pipe/port is used** — https://github.com/microsoft/playwright-python/issues/527 `CONFIRMED`. | **Yes.** A one-line deterministic check any site can run; the strongest single "this is Playwright" signal. |
| CDP detection | `navigator.webdriver` plus CDP side effects (`Runtime.enable` etc.); vendors classify Playwright sessions as `cdp_automation` — https://www.verisoul.ai/agents/playwright (2026) `REPORTED`. | Yes in aggregate; individually weak, collectively decisive. |
| WebGL SwiftShader on VMs | No GPU → software rendering; `UNMASKED_RENDERER_WEBGL` shows SwiftShader/ANGLE. Playwright even passes `--enable-unsafe-swiftshader` by default — chromiumSwitches.ts `CONFIRMED`. | Low-moderate. Common on real VMs and CI, so not conclusive; adds weight in combination. |
| Datacenter vs residential IP | Vendor claim that hosting ASNs are treated as guilty by default — https://xautodm.com/blog/best-proxies-for-twitter-x-automation-2026 (2026) `REPORTED`, low confidence; all such sources sell proxies. | Plausible but unproven for X. A stable *residential* IP is the safer default; **proxy rotation is out of scope.** |

## 6. Session lifetime

**How long does an X `auth_token` stay valid?** X publishes nothing `UNKNOWN`; no first-party page states a TTL and I found no
reproducible measurement. Treat any specific number as folklore.

**What invalidates it** (`CONFIRMED` for the OAuth analogue, `REPORTED` for the cookie): explicit "log out of all other sessions" in
Settings → Apps and sessions `REPORTED`, high confidence; password change or a security event (suspicious-login lock, 2FA change)
`REPORTED`, high confidence; logging in elsewhere and ending other sessions `REPORTED`; a security block (the §4.1 state), which can
invalidate sessions as a side effect `REPORTED`; ordinary expiry — X's OAuth FAQ says tokens "are not explicitly expired" but "may
become invalid at any time" — https://docs.x.com/fundamentals/authentication/faq `CONFIRMED` for OAuth, while the web cookie is
undocumented `UNKNOWN`.

**Does light activity extend it?** Undocumented. Community practice assumes yes (a session used regularly survives longer than one
left cold for months) `REPORTED`, low confidence. The counter-observation is that twscrape accounts "de-authed eventually" even on a
cron — https://github.com/vladkens/twscrape/issues/175 `REPORTED`.

**Sensible keepalive cadence (judgment).** One lightweight authenticated page load per day plus the real posting run. Do not poll,
do not refresh the timeline repeatedly, do not "warm up" with likes or follows — automated likes are separately prohibited ("You may
not like posts or hide replies in an automated manner", Automation Rules §II.D, April 2026) `CONFIRMED`.

**Signature of a revoked session in the browser:** `x.com/home` redirects to `x.com/i/flow/login` (or renders the logged-out landing
page); authenticated XHRs return 401; composer actions fail with a generic rather than a specific error `REPORTED`, high confidence.
Detect it as "final URL contains `/i/flow/login` OR the account menu is absent", not by string-matching an error toast.

## 7. Safe-pacing recommendation

Only the caps are sourced; everything else is judgment and is labelled.

| Parameter | Recommendation | Basis |
| --- | --- | --- |
| Gap between tweets in a thread | 20–45 s, jittered, not uniform | Judgment |
| Total thread duration | ≤ 15 min for 25 tweets | Judgment (~35 s average) |
| Gap between threads | ≥ 48 h; ideally same weekday/time each week | Judgment |
| Threads per day | 1 | Judgment; 2 would consume the whole 50-post unverified allowance `CONFIRMED` input |
| Original posts/day (incl. thread tweets) | ≤ 25 unverified, ≤ 40 if Premium | Judgment under the 50/200 cap `CONFIRMED` |
| Replies/day | ≤ 50 | Judgment under the 200 cap `CONFIRMED` |
| Quiet hours | 23:00–07:00 local, no posting | Judgment |
| Pre-post dwell | 3–10 s after text is in the composer before clicking Post | Judgment |
| Typing cadence | 40–120 ms/char with jitter, or paste-then-pause | Judgment |
| Weekly volume | 1 thread + ≤ 2 single posts | Matches the brief |

No source states a "safe" inter-tweet interval for X; anyone quoting one is quoting folklore. These numbers are chosen to look like
a person writing a thread, not to satisfy a documented threshold.

## 8. Failure handling

| UI state | What it means | Action |
| --- | --- | --- |
| Login wall / redirect to `/i/flow/login` | Session revoked or expired | **STOP.** Park. Human re-login. Never auto-login. |
| "We blocked an attempt to access your account…" | Security block on login | **STOP.** Park ≥ 24 h. Human only. Retrying deepens it. |
| Arkose / puzzle challenge | Login or action challenge | **STOP.** Park. Human solves it (out of scope here). |
| "This request looks like it might be automated" (403 / code 226) | Behavioural automation block | **STOP.** Park ≥ 24–48 h and reduce volume. Retry is counterproductive. |
| "You are over the daily limit for sending Tweets" / daily-limit toast | Daily or semi-hourly cap | **Retryable** after the window; reschedule to next day. |
| "Status is a duplicate" / 187 / "not allowed to create a Tweet with duplicate content" | Identical text already posted | **Do not retry as-is.** Treat as success-if-already-posted, else vary text. Check whether the earlier POST landed. |
| "Something went wrong. Try again." | Generic client-side failure; transient or silent block | **Retry once** after 30–60 s. If it repeats, **STOP** and park. |
| Account locked notice | Enforcement action | **STOP.** Human appeal only. |
| Suspended notice | Enforcement action | **STOP.** Terminal for automation. |
| "Sorry, you are rate limited" (read path) | Read throttle | Retryable later; irrelevant to a write-only tool. |

Rule of thumb: **anything mentioning automation, verification, locking or suspension stops the run and parks the tool.** Only
capacity errors (daily limit, 429, 5xx, duplicate-race) are retryable, and only with backoff.

## Recommended defaults

| Parameter | Value | Rationale | Source or judgment |
| --- | --- | --- | --- |
| **Backend** | `ApiBackend` (OAuth 1.0a access token) | Only permitted path; zero lock surface | Automation Rules Apr 2026 `CONFIRMED` |
| Fallback backend | `BrowserBackend`, disabled by default, explicit opt-in + warning | Policy risk is the operator's to accept | Judgment |
| Cost ceiling | ~$3/month at 1 link per thread | 24×$0.015 + 1×$0.200 ≈ $0.56/thread | Rate card `CONFIRMED`, my maths |
| Links per thread | 1 (root tweet) | $0.20 vs $0.015 per post | Rate card `CONFIRMED` |
| Volume | 1 thread/week, 20–25 tweets, ≤ 2 single posts | Brief; within composer norms | Judgment |
| Inter-tweet gap | 20–45 s jittered; ≤ 15 min per thread | Human-like cadence | Judgment |
| Threads/day | 1 | 50-post unverified cap | help.x.com `CONFIRMED` input |
| Daily original posts | ≤ 25 | Half the unverified cap, headroom | Judgment |
| Quiet hours | 23:00–07:00 local | Human-like | Judgment |
| Timezone/locale/IP | Fixed to the operator's real ones; never change | Consistency is the cheapest real signal reduction | Judgment |
| Browser (if used) | Real Chrome, `channel: 'chrome'`, headed or new headless | Avoids Chromium brand mismatch and headless shell | playwright.dev `CONFIRMED` |
| Profile (if used) | Dedicated persistent `user-data-dir`, never the daily-driver profile | Preserves full session; Playwright warns against the real profile | playwright.dev `CONFIRMED` |
| Login | Manual, once, by hand, same machine/IP; never auto-login | Repeated logins are the documented ban correlate | Community `REPORTED` |
| Keepalive | 1 authenticated page load/day | Maintains session without noise | Judgment |
| Session check | Final URL contains `/i/flow/login` → park | Robust revoked-session signal | `REPORTED` |
| Retry policy | 1 retry on 5xx/429/duplicate-race; 0 on 226/verification/lock | Retrying blocks escalates | Judgment |
| Parking | Any policy/verification/lock state → stop, alert, human | Prevents compounding | Judgment |
| Excluded | CAPTCHA solving, fingerprint spoofing, proxy rotation | Out of scope; also escalatory | Project decision |

## What we cannot guarantee

We cannot guarantee that an account driven through the browser will not be flagged, locked or suspended. X's
Automation Rules name "scripting the >x< website" as a technique that "may result in the permanent suspension
of your account", and its Authenticity policy reserves permanent suspension "at first detection" for severe
violations. Those are policy statements, not thresholds: no pacing profile, browser choice, session-reuse
pattern or IP strategy converts a prohibited mechanism into a permitted one, and none of the community
evidence here is a controlled study — it is self-reported anecdotes from people who were mostly doing
something more aggressive than this tool would. Conversely, the API path is not risk-free either: an account
can still be actioned for its *content* under the spam and authenticity rules, the API can change price or
terms without notice, and a misconfigured client that retries a failed post can produce duplicate content and
trip 187. What the API does guarantee is narrower and more useful: the account is never logged into by
software, so none of the login, session, device or fingerprint risk in §4 and §5 exists at all. At roughly
$2.50 a month for the described workload, that is the trade we recommend taking.
