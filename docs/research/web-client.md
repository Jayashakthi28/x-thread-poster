# x.com web client — how the thread composer posts (public bundle study)

**Status:** static study of the bundle served to logged-out visitors on **2026-11-09**.
**Scope:** `https://x.com` HTML shell + `https://abs.twimg.com/x-web/x-web/*` JavaScript. No account was used, no authenticated request was made, and no post/like/follow request was sent.
**Artifact identity:** app version `82d844c4518064e72fb1d8c4bb196fec3571ba4f`; entry `https://abs.twimg.com/x-web/x-web/entry-client-logged-out-BgOPDRqa.js`; 86 chunks (~9.1 MB) under `/private/var/folders/9z/yv486y_s509gc10_nhhw5p640000gn/T/opencode/x-bundles/js`.
**Method:** `curl` of the shell + sequential chunk download, then static reading of minified modules. Corroborated with REA `analyze_javascript_application` on 8 key chunks (evidence `ev_4d641790b622793bd1ea4ea5e53c4f25964ca7911910e11afc19403b5d88db01`).
**Confidence tags:** **OBSERVED** = string/attribute read in the served bundle; **INFERRED** = follows from an observed route/role/attribute but not seen in a hydrated logged-in DOM; **UNKNOWN** = not reconstructable from the public bundle.

## Headline: this is a new client, and the classic testids are gone

The logged-out shell no longer references `responsive-web/client-web/main.*.js`. It loads **`x-web`**, a Vite + React +
TanStack Router application (`entry-client-logged-out-*.js`). The requested classic attributes —
`tweetTextarea_0`, `tweetButton`, `tweetButtonInline`, `addButton`, `fileInput`, `altTextInput`,
`confirmationSheetConfirm/Cancel`, `SideNav_NewTweet_Button`, `AppTabBar_Profile_Link`,
`SideNav_AccountSwitcher_Button`, `reply`, `tweet`, `primaryColumn` — **do not occur anywhere in the 86-chunk corpus**
(OBSERVED negative). The new UI is built from ARIA labels, roles, visible text and a few structural attributes
(`data-modal-slot-surface`, `data-sonner-toast`, `itemprop="SocialMediaPosting"`). `selectors.json` therefore gives
new-client primaries and keeps the classic testids only as explicitly marked `LEGACY_UNVERIFIED` fallbacks.

The composer route still exists while logged out: `QM5xmrcI.js` registers `path:"/compose/post"` with a lazy
`TweetComposerSlot` (`import("./YPo-umLn.js")`), then redirects anonymous visitors to
`/i/jf/onboarding/web?redirect_after_login=%2Fcompose%2Fpost&mode=login`.

## 1. Composer DOM

### 1.1 Modal composer (`/compose/post`) — OBSERVED structure

`YPo-umLn.js` renders the composer into a dialog content node:

```js
w.Content,{"data-modal-slot-surface":``,className:`flex flex-col gap-3 p-3 ...`}
// sr-only Title: a(`Create post`) / a(`Edit post`)
```

- Root: `[role="dialog"]:has([data-modal-slot-surface])`; the dialog's accessible name comes from the sr-only title
  **“Create post”** (or “Edit post”).
- Text entry: the form module `form-_RszDN-r` (`DcNnCk6K.js`) renders a **native `<textarea>`**, not a contenteditable:

```js
textarea,{ref:b,"aria-disabled":xe,"aria-label":T,"aria-multiline":`true`, ... writingsuggestions:`false`}
```

  The `aria-label` is the current placeholder: `Post your reply` (reply), `Add a comment` (quote), `Ask a question`
  (poll), otherwise absent for a brand-new post. Use `textarea[aria-multiline="true"]` inside the dialog, not a
  placeholder. Because it is a real textarea, Playwright `fill()`/`type()` dispatches the `input` events the composer
  listens for.
- Submit: `ao()` chooses the label `Schedule` / `Reply` / `Post`; `oo()` chooses `Schedule` / **`Post all`** for a thread.
  Both render a normal `<button>` with visible text; Enter triggers `form.requestSubmit()`, and the Post button lives
  in that form. Use exact text within the dialog.
- Add another post: `ro()` renders `<W {"aria-label": d, ...}>` where `d` is **`Add another post`** (or `Add Reply` in a
  reply context). This is the thread builder.
- Media: the picker renders a visible `<button aria-label="Add media">` and a hidden sibling
  `<input type="file" accept=... multiple aria-hidden="true" class="sr-only" tabIndex="-1">`. The accept list is
  `image/jpeg,image/png,image/webp,image/gif,video/mp4,video/quicktime` (`CiL7gPmy.js`). `setInputFiles()` on the hidden
  input is the reliable path; clicking “Add media” opens the OS picker.
- Attachment preview / remove: previews are `<img>` tiles; each tile carries a button whose aria-label is
  **`Remove media`**. There is no attachments wrapper testid in this client.
- Alt text: the composer's description button has an aria-label of **`Add {n} description(s)`** when empty,
  **`Image description: {desc}`** for one, or **`{n} image descriptions`**; the media viewer shows a badge whose text is
  **`ALT`** and a popover titled **`Image description`**. The alt editor itself is opened via `editMedia(ids,"alt")`; its
  textarea/save markup was not isolated (INFERRED in `selectors.json`).
- Draft-save / discard: closing a dirty composer opens `Ui()` — title **`Save post?`**, description
  “You can save this to send later from your drafts.”, footer **`Discard`** and **`Save`**. Discarding from the composer
  uses `role="alertdialog"`, title **`Discard post?`**, description “This can't be undone and you'll lose your post.”,
  confirm **`Discard`**. This replaces the old confirmation sheet.
- Success toast: Sonner (`kccSSoK9.js`, `data-sonner-toast` / `data-sonner-toaster`). Text:
  **`Your post was sent.`** for one, **`Your posts were sent.`** for a thread, **`Your post has been edited.`** for an
  edit. The action is a button labelled **`View`** that navigates to the new permalink.

### 1.2 Home timeline composer

The inline home composer is not present in the logged-out bundle (no timeline). The code path is shared: the same
`TweetComposerSlot` component and `form-_RszDN-r` textarea are used by the modal route, and the home inline composer
would use the same form module. Treat `/compose/post` as the driver's entry point; it is the only composer route that
can be addressed deterministically without relying on the home timeline DOM. (OBSERVED route; inline home DOM UNKNOWN.)

### 1.3 Navigation, identity and login

- Left-nav **Post**: route `/compose/post`; selector `a[href="/compose/post"]` (INFERRED from TanStack `<Link>`).
  The classic `SideNav_NewTweet_Button` is absent.
- Profile link: `a[href="/{screen_name}"]` (INFERRED). `AppTabBar_Profile_Link` is absent.
- Account switcher: trigger aria-label **`Account menu`**; menu items **`Add an existing account`**,
  **`Manage accounts`**, **`Log out @{screenName}`** (`QM5xmrcI.js`). Classic `SideNav_AccountSwitcher_Button` absent.
- Tweet article: `<article itemProp="SocialMediaPosting" itemScope itemType="https://schema.org/SocialMediaPosting">`
  plus class `group/post` (`CwBEAaFb.js`). Reply action button aria-label **`Reply`**. Article `data-testid="tweet"`
  does not exist here.
- `primaryColumn`: absent; the app renders a single `<main>`.
- Logged-in vs logged-out: logged-out pages contain `a[href*="/i/jf/onboarding/web"][href*="mode=login"]` and
  “Log in”/“Sign up”; the authenticated shell shows the composer nav and the account menu. `twid` is readable by JS
  (`Bfx2Rbnk.js` reads `twid`); `auth_token` is believed HttpOnly (UNVERIFIED here) and must not be probed from page scripts.
- Login flow: `/i/flow/login` now **redirects** to `/i/jf/onboarding/web?mode=login` (`beforeLoad` in `QM5xmrcI.js`).
  The landing page has a button/link **`Log in with username or email`** and Google/Apple buttons
  (`Sign in with Google`, `Sign in with Apple`). The username/password form chunk was not isolated; its selectors are
  marked UNKNOWN in `selectors.json` (legacy `input[name="text"]` / `input[type="password"]` are fallbacks only).

## 2. The `CreateTweet` request

### 2.1 Endpoint and query id

The new Relay network layer (`Bfx2Rbnk.js`, `zbe({graphqlUrl,...})`) builds the URL as
`graphqlUrl + "/" + operation.id + "/" + operation.name`. The configured base is:

```js
pG = new URL(`https://api.x.com/graphql`)
t.pathname = `${t.pathname.replace(/\/$/,``)}/${e.id}/${e.name}`
```

So the **observed** call is `POST https://api.x.com/graphql/8TlLvl0sHLceb7rlGZZ6bg/CreateTweet`. The older same-origin
`/i/api/graphql/<id>/<name>` shape is still recognised by the client's URL classifier (`Dn()` checks both
`/graphql/` and `/i/api/graphql/`), so a driver should match `**/graphql/*/CreateTweet` on either host.

| Item | Observed value | Confidence |
|---|---|---|
| Operation | `CreateTweet` (mutation) | OBSERVED |
| Query id | `8TlLvl0sHLceb7rlGZZ6bg` (`DcNnCk6K.js`, `params:{id:...}`) | OBSERVED |
| Body | `{"queryId":"8TlLvl0sHLceb7rlGZZ6bg","variables":{...}}` | OBSERVED |
| Long-post op | `createTweetNoteTweetMutation`, id `Nff4GppYFB2cZhIO5UZi_A`, field `notetweet_create` | OBSERVED |
| Method | POST for mutations, with `AbortSignal.timeout(...)` | OBSERVED |

### 2.2 Variables

`CreateTweet` argument definitions (`DcNnCk6K.js`, `jr()` / `Ar`):

`url`, `attachment_url`, `batch_compose`, `card_uri`, `cashtag_attachment_options`,
`content_disclosure_options`, `conversation_control`, `edit_options`, `engagement_request`,
`exclusive_tweet_control_options`, `geo`, `media`, `premium_tweet_control_options`, `reply`,
`semantic_annotation_ids`, `tweet_text` — plus the literal `safety_level:"TweetWritesApi"`.

For a plain thread post the driver cares about:

```js
g = { tweet_text: e.text };
g.reply = e.reply;                 // {exclude_reply_user_ids:[], in_reply_to_tweet_id:"<id>"}
g.media = e.media;                 // {media_entities:[{media_id, tagged_users?}], possibly_sensitive:false}
g.semantic_annotation_ids = [...]; // only when non-empty
```

`batch_compose` is **not** a thread array: it is a per-request marker (`BatchFirst` / `BatchSubsequent`) that tells the
server this request is part of a multi-post compose. `dark_request` is **absent** from this client's variable list
(OBSERVED negative — it existed in older clients). `attachment_url` is the URL-card attachment, not media.

Media payload construction (`iKW7XSmR.js`, composer media slice):

```js
return {media:{media_entities:t.flatMap(e=>e.status.stage===`ready`?[{media_id:e.status.mediaId,...r}]:[]),possibly_sensitive:!1}}
```

### 2.3 Response and exact `rest_id` path

The mutation selects `create_tweet { tweet_results { rest_id ... result { ... } } }`. The client's own completion
handler reads the id from `tweet_results`, not from `result`:

```js
let o=n?.rest_id; if(!o){let e=Error(`Tweet created but no ID returned`); ...}
let c=n.result?_t(n.result):void 0;
```

Therefore, in the raw HTTP response:

- **Primary:** `data.create_tweet.tweet_results.rest_id`
- Fallback: `data.create_tweet.tweet_results.result.rest_id`
- If `result.__typename === "TweetWithVisibilityResults"`: `data.create_tweet.tweet_results.result.tweet.rest_id`

For a long post (`createTweetNoteTweetMutation`): `data.notetweet_create.tweet_results.rest_id` (same nesting pattern).
A Playwright `page.on("response")` handler can read this without any DOM scraping; match the URL suffix
`/CreateTweet` and parse `json().data.create_tweet.tweet_results.rest_id`.

### 2.4 How “Post all” chains a thread

One `CreateTweet` per composer item, **sequentially**; each later item replies to the previous id. The exact loop
(`DcNnCk6K.js`, `hi()` → `E`):

```js
let o=r.at(-1), s=o==null?a.reply:{exclude_reply_user_ids:[],in_reply_to_tweet_id:o.tweetId},
c=e.length===1?void 0:i===0&&e[0]?.continues_self_thread!==!0?`BatchFirst`:`BatchSubsequent`,
l=await T({...a,batch_compose:c,reply:s},n)
```

So: item 0 uses the outer reply target (or none) and `batch_compose:"BatchFirst"` unless `continues_self_thread` is set (then `BatchSubsequent`); items 1..n-1 set
`reply.in_reply_to_tweet_id` to the previous item's returned `tweetId` and `batch_compose:"BatchSubsequent"`. The loop
is `for (const [i,a] of e.entries())` and `await`s each call before the next, so ordering is guaranteed. The toast text
switches to `Your posts were sent.` when `r.length > 1`.

### 2.5 Media upload (needed before `CreateTweet`)

`CiL7gPmy.js` implements the chunked upload used by the picker:

- `https://upload.x.com/i/media/upload.json` (and `upload2.json` for long video)
- commands `INIT` → `APPEND` / `APPENDMULTI` → `FINALIZE`, fields include `media_category`
  (`tweet_image`, `tweet_gif`, `amplify_video`), `media_type`, `total_bytes`, `media_id`, `segment_index`,
  `allow_async:"true"` for video
- returns `media_id_string` (and `media_key`); the composer stores `media_id` and puts it in `media.media_entities`.

If the driver uses the visible file input, X performs this upload itself; the driver only needs to wait for the preview
and for the item's status to become `ready`.

## 3. API error codes and client behaviour

The bundle contains a 496-entry code→name map (`Bfx2Rbnk.js`, `mr={...}`). User-facing handling is centralised in
`QM5xmrcI.js` `Mye()` plus the composer's own `OverStatusUpdateLimit` / `RateLimitUpsellError` handling
(`DcNnCk6K.js`). GraphQL errors carry `code`, `message`, `bounce_location`, `bounce_not_suppressible`, `trace_id`,
`retryable` (`Bre()` / `Er()` in `Bfx2Rbnk.js`).

| Code | Name / meaning | What the client does | Driver reaction |
|---:|---|---|---|
| 27 | `AccountLocked` | Default toast (no dedicated branch found) | **Stop and park**; manual unlock. |
| 32 | `InvalidCredentials` | Generic “account may not be allowed…” toast (default branch) | **Re-login**; stop. |
| 64 | `CurrentUserSuspended` | Toast “Your account is suspended and is not permitted to perform this action.” + Learn more | **Stop and park**; do not retry. |
| 88 | `RateLimitExceeded` | Toast “Sorry, you are rate limited. Please wait a few moments then try again.” | **Backoff + retry**; honour `retry_after`. |
| 89 | `BadOauthToken` | Navigates to `/logout` | **Re-login**. |
| 131 | `InternalError` | Default toast “Something went wrong…” | Retry once with backoff. |
| 135 | `OauthTimestampException` | Default toast | **Re-login** (clock/session skew). |
| 179 | `StatusViewForbidden` | Default toast | Stop; read-only context. |
| 185 | `OverStatusUpdateLimit` | Toast “You are over the daily limit for sending posts.” + Learn more | **Stop until daily reset**; do not retry. |
| 186 | `StatusTooLongError` | Default toast; composer normally blocks before send (weighted length) | Trim text; stop. |
| 187 | `DuplicateStatusError` | Toast “Whoops! You already said that.” | **Stop**; verify timeline before retrying. |
| 215 | `BadAuthenticationData` | Default toast | **Re-login**. |
| 226 | `TieredActionTweetSpammer` | Toast “This request looks like it might be automated… can’t complete this action right now.” | **Stop and park**; likely challenge/manual review. |
| 261 | `AppInReadOnlyMode` | Default toast | **Stop**; platform read-only. |
| 263 | `TieredActionChallengeCaptcha` | Same “might be automated” toast | **Stop**; solve challenge manually. |
| 315 | `ClientCaptchaRequired` | Challenge/captcha path | **Stop**; manual captcha. |
| 326 | `AccessDeniedByBouncer` | No toast; `x-bouncer` interceptor redirects to `bounce_location` or `/account/access` | **Stop and park**; manual identity/access flow. |
| 344 | `UserActionRateLimitExceeded` | Toast “daily limit for this action… add a phone” + `/settings/phone` | **Stop**; do not retry today. |
| 349 | `DirectMessageCannotDmOtherUser` | Default toast | Not a posting code; stop. |
| 353 | `DeniedByApiCsrfProtection` | `x-csrf-token-refresh` re-reads `ct0` from the 403 response and retries once | **Automatic**; if it persists, re-login. |
| 385 | `InReplyToTweetNotFound` | Toast “The post you are trying to reply to has been deleted or is not visible…” | **Stop thread**; do not continue. |
| 386 | `AttachmentTypesLimitInTweetExceeded` | Default toast | Remove unsupported media; stop. |
| 403 | `IneligibleFor2faAfterModification` | Default toast | **Re-login**; stop. |
| 408 | `InvalidUrl` | Default toast | Fix URL/card; stop. |
| 433 | `ConversationControlReplyRestricted` | Default toast | Change reply settings; stop. |
| 236 | `IncorrectChallengeResponse` | Challenge failure UI | Manual challenge; stop. |
| 501 | `RateLimitUpsellError` | Toast “You’ve hit the daily post limit. Subscribe to {tier}…” + upgrade action | **Stop**; daily limit. |

Additional strings relevant to automated/locked/suspended behaviour:

- “This request looks like it might be automated. To protect our users from spam and other malicious activity, we can’t
  complete this action right now. Please try again later.” — `TieredAction*` codes (226/263/225/283/292).
- “To protect our users from spam and other malicious activity, this account is temporarily blocked from sending posts.”
  — `TieredActionTweetCreeper` (228).
- “Your account is suspended…” (64) and “Sorry, this account has been suspended.” (`GenericSuspended`, 159).
- Challenge UI: **“Verify you are a human”**, **“Verifying you are a human…”**, Arkose module keys
  (`arkose_challenge_login_web_prod`, etc.), `/account/access`, `/account/login_challenge`. The literal strings
  `ConfirmYourIdentity`, `LoginFlow`, and `unusual` do **not** occur in this bundle (OBSERVED negative; old-client
  strings).

## 4. Client-side signals relevant to automation/bot detection

| Signal | Collected? | Sent? | Implication for the driver |
|---|---|---|---|
| `navigator.webdriver` | **No** reference in the bundle | — | No client-side webdriver check found. |
| `HeadlessChrome` / `Playwright` / `puppeteer` | Yes — shared SSR/bot-classification code | Yes, via UA header | `Fx` maps `/HeadlessChrome|puppeteer/` → `puppeteer`, `/Playwright/` → `playwright`; **do not spoof these UAs**. A real headed Chrome UA is what an ordinary user has. |
| `__playwright`, `__pw_`, `callPhantom`, `_selenium`, `cdc_` | No occurrence | — | No JS-side property names to clean up in this client. |
| `navigator.userAgentData` / `navigator.userAgent` | Yes (`Of()`, `jf()`) | UA is sent on every request | Browser/device routing; keep a genuine Chrome UA and client hints. |
| `maxTouchPoints`, `display-mode: standalone`, `window.external`, `Android`, `_cordovaNative`, `Ti`, `webView` | Yes (`zf()`, `Tf()`) | Locally cached only | App/webview detection, not a bot score. A normal desktop Chrome yields the ordinary values. |
| `navigator.plugins` | No real enumeration | — | The `plugins` strings found are Module Federation, not browser plugins. |
| `outerWidth` / `getHighEntropyValues` | No occurrence | — | No extra window/UA-CH probing found; `innerWidth` is used only for layout. |
| Canvas/WebGL/audio fingerprint | **No** `toDataURL`, `webgl`, `OfflineAudioContext`; `AudioContext` only in media playback | — | No client-side canvas fingerprinting found. |
| Scribe `client_event` | Yes — action/impression/dwell events | Yes — `POST /1.1/flow/viewer.json`, `POST /i/api/1.1/graphql/viewer_context.json` (`sendBeacon` on unload) | Behavioural telemetry includes `client_event_sequence_number`, `client_event_sequence_start_timestamp`, `triggered_on`, namespace `{client:"m5", page, action}`. Ordinary timing/order is what it measures. |
| Impression/dwell | Yes — IntersectionObserver, scroll/resize/visibility listeners, `performance.now()` dwell | Yes, batched | Reads/lurks are measured; posting automation that never scrolls/reads looks different from a human session. |
| `x-client-transaction-id` | Header allowlist; a transaction id is fetched from `POST /1.1/graphql/viewer_context.json` for telemetry (link INFERRED) | Allowlisted/forwarded; `rweb_client_transaction_id_enabled` flag | Not a browser fingerprint; the page obtains it from X. Don’t fabricate it. |
| `x-client-uuid` | No generator in this bundle; only header allowlist | Only if upstream supplies it | Native-app/SSR header; web client does not create it. |
| `x-twitter-client-language`, `x-twitter-active-user` | Yes (client headers interceptor) | Yes on api.x.com/x.com requests | Set from viewer/session; harmless for a normal browser. |
| `x-csrf-token` (= `ct0`) | Yes (`cr()`) | Yes on state-changing requests | Must match the `ct0` cookie; browser handles it automatically. |
| `x-twitter-auth-type: OAuth2Session` | Yes when `auth_token`/`ct0` present | Yes | Derived from cookies; browser handles it. |
| `ui_metrics` / `js_instrumentation` | `ui_metrics`/`signup_ui_metrics` only as legacy cookie names; `js_instrumentation` has no occurrence | Not observed | Legacy names; no active JS instrumentation in this bundle. |
| `cf_clearance` / `__cf_bm` | `__cf_bm` set by edge (OBSERVED); `cf_clearance` not set on a clean visit | Yes, cookies | A normal browser obtains `__cf_bm`; `cf_clearance` is expected only after a Cloudflare challenge (INFERRED). |

Header allowlists (`Bfx2Rbnk.js`) also name `x-twitter-session-signature`, `x-twitter-session-timestamp`,
`x-twitter-session-tokenhash`, `x-twitter-audit-ip`, `x-tfe-user-id`, `x-tfe-session-hash`,
`x-tfe-authentication-assurance-level`, `x-act-as-user-id`. These are **set upstream / by SSR**, not by page JS; their
presence shows the server tracks session assurance and IP audit data. The client does not compute them.

**Descriptive conclusion:** the public client does not ship obvious webdriver/canvas fingerprint code. Its strongest
automation signals are (a) the UA string (server-side classifier explicitly tags Playwright/HeadlessChrome), (b)
behavioural Scribe events with sequence numbers and dwell/impression timing, and (c) server-side session/audit headers.
A real headed Chrome with a persistent profile satisfies the browser-environment checks without patching.

## 5. Session and cookies

### 5.1 Cookies observed on a clean logged-out visit (Set-Cookie, OBSERVED)

| Cookie | Lifetime | Notes |
|---|---|---|
| `guest_id` | 2 years (`Max-Age=63072000`) | guest identity |
| `guest_id_ads`, `guest_id_marketing` | 2 years | ad variants |
| `personalization_id` | 2 years | personalization |
| `gt` | 2.5 h (`Max-Age=9000`) | guest token |
| `ct0` | **expired** (`Max-Age=-1794236546`) | only meaningful after login |
| `__cf_bm` | 30 min | Cloudflare bot-management cookie |

The bundle's cookie allowlist (`Bfx2Rbnk.js`) confirms the authenticated names: `auth_token`, `ct0`, `twid`, `att`,
`kdt`, `dtab_local`, `auth_multi`, `guest_id`, `personalization_id`, `lang`, `lang-preference`, `night_mode`,
`d_prefs`, `csrf_id`, `csrf_same_site`, `sessionid`, `m_session`, `user_id`, `ui_metrics`, `signup_ui_metrics`.
It does not contain lifetimes for the auth cookies; those are set by the login response (UNKNOWN without login).

### 5.2 What the client does to keep a session fresh

- Every state-changing request derives headers from cookies: `Authorization: Bearer <public web bearer>`,
  `x-csrf-token: <ct0>`, and `x-twitter-auth-type: OAuth2Session` when `auth_token` or `ct0` is present (`cr()`).
- **`ct0` rotation:** on HTTP 403 with GraphQL code **353** (`DeniedByApiCsrfProtection`), the `x-csrf-token-refresh`
  interceptor re-reads the `ct0` value from the 403 response's `Set-Cookie`, rewrites the cookie/header, and retries the
  request once (`Bfx2Rbnk.js`, `Gr=353`).
- **Revoked/invalid session:** `BadOauthToken` (89) and `RestrictedAuthToken` (220) navigate to `/logout`;
  `AccessDeniedByBouncer` (326) / `ReadOnlyTokenAuthenticationNotAllowed` (445) trigger the `x-bouncer` interceptor,
  which sends the browser to `bounce_location` or **`/account/access`**; `InvalidCredentials` (32) and other auth errors
  fall through to the generic “account may not be allowed…” toast. There is no silent refresh endpoint in the bundle;
  the only refresh is the ct0 403 retry. A hard 401/403 does not auto-redirect except through the bouncer path.
- `/i/flow/login` is no longer the live route: it redirects to `/i/jf/onboarding/web?mode=login` (`QM5xmrcI.js`).
- Multi-account: `viewer_act_as` cookie (`loggedInUserId:actAsUserId`, 1 year) and `x-web-auth-multi-user-id` header;
  account switching calls `Rae()` then `window.location.assign("/")`.

### 5.3 IP/device binding — what is visible

The client bundle has no hard-coded IP lock. What it does show is server-side session assurance plumbing: the outbound
header allowlist includes `x-tfe-authentication-assurance-level`, `x-tfe-session-hash`, `x-tfe-transaction-id`,
`x-twitter-session-signature`, `x-twitter-session-timestamp`, `x-twitter-session-tokenhash`, `x-twitter-audit-ip`,
`x-twitter-audit-ip-thrift`, `x-tfe-user-id`, `x-tfe-user-type`, `x-tfe-country-code`, and `kdt` is appended to TIA
cookies. These are produced by X's edge/SSR, not by browser JS. **INFERRED:** sessions are validated server-side
against device/session signatures and IP/ASN context; a cookie copy alone is not proof of the same device, and a
changed IP/UA increases challenge risk. The bundle does not reveal thresholds or whether a hard bind exists.

## 6. Limits encoded in the bundle

| Limit | Value | Evidence / confidence |
|---|---|---|
| Standard weighted length | **280** | `form-_RszDN-r` default `max=280`; client blocks send when `weightedLength > max` (OBSERVED). The bundle computes `weightedLength` locally; it does not use the old `maxWeightedTweetLength` field name. |
| Premium / long post | **25,000** | `Tr={max:25e3,warningAt:24920}`, applied when `longPosts` slice/flag is on (OBSERVED). |
| Warning threshold | 260 (standard), 24,920 (long) | `warningAt` values above (OBSERVED). |
| Weighted units | URL = 23, emoji = 2, most code points = 1 | `a+=2300` / `a/=100` for URLs; `T=200` for emoji; ranges weight 100 (OBSERVED). |
| Thread length | **25** posts | `YPo-umLn.js` `yo=25`, `y.length<yo` gates the “Add another post” button (OBSERVED). |
| Media per post | **4** | `composer_max_media_count:4` in feature config (OBSERVED). |
| Image upload max | **5 MB** | `I=5242880` in `CiL7gPmy.js`; `upload.json` INIT/APPEND path (OBSERVED constant, exact per-type use INFERRED). |
| GIF upload max | **15 MB** | `L=15728640` in `CiL7gPmy.js` (OBSERVED constant). |
| Video max (standard) | **512 MB / 1200 s** | `media_async_upload_max_video_size:512`, `media_async_upload_max_video_duration:1200` (OBSERVED). |
| Video max (longer/Premium) | **16384 MB / 21660 s** | `media_async_upload_longer_video_max_video_size:16384`, `..._duration:21660` (OBSERVED). |
| Daily post limit | server-side; no number in bundle | Codes 185 (“over the daily limit for sending posts”) and 501 (“hit the daily post limit… Subscribe”) (OBSERVED behaviour; threshold UNKNOWN). |
| Other media errors | 191 `OverPhotoLimit`, 192 `OverMediaEntitiesPerUpdateLimit`, 386 `AttachmentTypesLimitInTweetExceeded`, 323 `AnimatedGifMultipleImages` | Enum + default toast (OBSERVED names, UI default). |

## Implications for the driver

1. **Target the new `x-web` DOM.** Use `selectors.json` primaries; the classic testids are absent and only kept as
   `LEGACY_UNVERIFIED` fallbacks.
2. **Go straight to `/compose/post` after login** and wait for `[role="dialog"] [data-modal-slot-surface]`; do not
   depend on the home timeline inline composer, which is not in the logged-out bundle.
3. **Fill the native `<textarea aria-multiline="true">`** with `locator.fill()`; it is not contenteditable and the
   composer listens to normal `input` events.
4. **Submit with the exact button text** `Post` (single) or `Post all` (thread), inside the dialog. Treat the button's
   enabled state as the client-side readiness check.
5. **Build a thread by clicking `button[aria-label="Add another post"]`**, then fill the newly appended textarea; the
   composer caps threads at 25 items.
6. **Attach media through the hidden `input[type=file]`** with `setInputFiles()`; wait for the preview and for
   “Remove media” to appear. Use the “Add … description” button for alt text.
7. **Read the new id passively** from the response JSON at `data.create_tweet.tweet_results.rest_id` (fallbacks in
   §2.3). Never scrape the DOM or the toast for the id.
8. **Expect a different operation for >280 weighted chars** (`createTweetNoteTweetMutation`, `notetweet_create`) and
   parse that response shape instead.
9. **Classify failures:** stop-and-park on 64/185/226/263/315/326/344/501; retry with backoff on 88/131; re-login on
   32/89/135/215/403; treat 353 as an automatic ct0 refresh; stop the thread on 385.
10. **Do not spoof a Playwright/HeadlessChrome UA.** X's shared classifier maps those strings to bot tags; use a real
    headed Chrome with a persistent profile instead.
11. **Keep the session honest:** persist the browser profile/cookies, let the browser own `ct0`, and handle the
    403/353 refresh path; do not fabricate `x-client-*`/`x-tfe-*` headers.
12. **Rate-limit yourself.** The daily post limit and action limits are server-side and the bundle exposes no reset
    time; back off on the first 185/344/501.

## Unknowns that need a logged-in session

- The hydrated composer DOM: exact accessible name of a brand-new textarea, media tile markup, alt-editor textarea and
  Save button, and whether any hidden testids are added only for authenticated users.
- Whether authenticated users still get `x-web` or are served the legacy `responsive-web` client (the logged-out shell
  only proves the anonymous client).
- Exact `auth_token` / `ct0` / `att` / `kdt` lifetimes, HttpOnly/SameSite flags, and rotation cadence after login.
- Whether the inline home composer and the `/compose/post` modal share identical DOM (the code suggests yes; unverified).
- Stability of query ids `8TlLvl0sHLceb7rlGZZ6bg` / `Nff4GppYFB2cZhIO5UZi_A` across deploys (hashes change).
- The media upload flow as seen from the browser (CORS, `upload.x.com` cookies, `media_key` propagation) and whether
  the hidden file input accepts the same types in the authenticated UI.
- Daily post/action thresholds and reset timezone; the bundle only carries the error codes.
- The `/account/access` and Arkose challenge flows after a 226/263/315/326: what the user sees and what unblocks it.
- `ConfirmYourIdentity` / `LoginFlow` / “unusual activity” strings: absent from this bundle; verify whether any legacy
  route still emits them.
- Account-switcher/profile-link behaviour and the exact nav markup for “Post”.
- Toast accessibility markup after login (Sonner is observed, but the live action button markup should be confirmed).

## Evidence index

| Topic | File(s) | Confidence |
|---|---|---|
| Shell / entry / app version | `shell.html`, `entry-client-logged-out-BgOPDRqa.js` | OBSERVED |
| Composer route + lazy slot | `QM5xmrcI.js` (`path:"/compose/post"`, `TweetComposerSlot`) | OBSERVED |
| Composer UI, Post/Post all/Add post, draft dialogs | `YPo-umLn.js` | OBSERVED |
| Textarea, CreateTweet operation, thread loop, error handling | `DcNnCk6K.js` | OBSERVED |
| Media slice / `media_entities` | `iKW7XSmR.js` | OBSERVED |
| Media picker input, accept list, upload client | `DcNnCk6K.js`, `CiL7gPmy.js` | OBSERVED |
| Relay URL/body, error enum (496), bouncer, ct0 refresh, headers | `Bfx2Rbnk.js` | OBSERVED |
| Central API error → toast mapping | `QM5xmrcI.js` (`Mye`) | OBSERVED |
| Login landing, challenge UI | `DF3Cg0I9.js`, `BflhFQAN.js`, `C3KtfKl5.js` | OBSERVED / partial |
| Toasts | `kccSSoK9.js` (Sonner) | OBSERVED |
| REA corroboration | `analyze_javascript_application`, `ev_4d641790b622793bd1ea4ea5e53c4f25964ca7911910e11afc19403b5d88db01` | derived |
