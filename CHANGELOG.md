# Changelog

All notable changes to this package are recorded here. The format is loosely
based on Keep a Changelog; this package has no released history before 0.1.0.

## [0.2.0] — 2026-11-10

### Added

- **Resumable Python API:** `XPoster.post_chain(thread, *,
  already_posted=(), on_posted=None) -> PostedThread` for callers that own the
  durable record (the OLSA pipeline). An empty prefix uses the verified composer
  `Post all` path and calls `on_posted(PostedTweet)` in capture order as each
  `CreateTweet` response arrives; a non-empty accepted prefix resumes reply by
  reply (**not verified against the live site**). xposter's own ledger never
  blocks a chain call — the caller is the authority — but pacing, the session
  lock, login/handle checks, park-on-challenge, the click barrier and the
  read-back still apply. A prefix covering the whole thread returns without a
  browser; a 26-tweet thread is refused before Chrome. The ledger is seeded
  with the caller's prefix as forensic evidence.
- **Reply calibration:** `xposter doctor --dom-reply STATUS_URL` opens one of the
  configured account's own tweets and calibrates the reply composer read-only.
  It accepts a status URL or numeric id, refuses another account before Chrome,
  clicks only the focal article's reply icon under the phase-R guard, types a
  harmless marker, records modal/inline composer details, submit state and
  Draft.js, then discards (modal) or clears and navigates away (inline). It
  never submits. The reply composer is **UNVERIFIED-LIVE** (offline fake only),
  and so is the reply-by-reply `post_chain` resume path.
- **Docs:** README Python API section for `post_chain`; runbook reply-calibration
  procedure and artifacts; design decision 10 and the verification matrix cover
  the reply composer and resume path; `docs/integration.md` now describes the
  implemented OLSA adapter on the unmerged `feat/x-thread-publish` branch.

## [0.1.0] — 2026-11-09

Initial functionally complete release. The package is standalone and has not yet
been integrated into the OLSA pipeline.

### Added

- **Configuration and models:** frozen `XPosterConfig`, `HumanPolicy`,
  `PacingPolicy` and `KeepalivePolicy` dataclasses; `Display`, `Strategy`;
  `Thread`, `Tweet`, `PostedThread`, `PostedTweet` and `SessionSummary`.
- **Public API:** `XPoster`, `SessionInfo` and `RefreshResult` are lazy imports;
  the splitter helpers (`weighted_length`, `validate_tweet`, `split_thread`,
  `pack_segments`, `split_with_first`), `ParkInfo`, `Decision` and `LedgerStatus`
  are exported from `xposter`.
- **X-aware text splitter:** twitter-text v3 weighting (280 weighted characters,
  URLs = 23, code points outside twitter-text's light ranges = 2, one emoji
  sequence = 2), Unicode-bold letters cost 2 each, paragraph/sentence/word/
  grapheme cut preferences, and protection for URLs, hashtags, mentions and
  emoji sequences.
- **Browser layer:** discovers the installed Google Chrome, launches a dedicated
  persistent profile, removes `--enable-automation`, supports headless/Xvfb/
  visible displays, and provides the plain-Chrome one-time login.
- **Session lifecycle:** cookie-free verification, account-handle checks,
  `storage_state.json` backup with `.bak`, same-machine restore into an empty
  profile, single-process profile lock held across the plain login,
  `xposter logout` (profile reset with a `.pre-reset` credential copy), and
  cookie-free session summaries.
- **Safety guard:** maps REST/GraphQL error codes, HTTP statuses, redirects and
  visible text to retry/relogin/park/reject/stop decisions. HTTP status and body
  codes are separate; an unrecognised code/message parks instead of retrying.
- **Posting:** real-Chrome composer and reply-chain strategies, real per-key
  typing with a Draft.js block read-back before any Post click, passive
  `CreateTweet` response capture, per-tweet image attachment with local
  validation, one safe 5xx re-click, dry-run rehearsal, and test-post deletion.
- **Images:** up to four PNG/JPEG/GIF/WebP images per tweet, extension/magic-byte
  agreement, 5 MiB image / 15 MiB GIF limits, no GIF mixed with another type, no
  duplicate or zero-byte files, video rejection, `--media PATH|N:PATH`,
  `--alt TEXT|N.M:TEXT`, thread-JSON `media`/`alt`, and opt-in `--allow-alt`
  (alt text off by default).
- **Mentions:** typed `@handle` support with a typeahead guard (a plain space
  after a line-final/final token), trailing-space-tolerant read-back, a default
  cap of 5 distinct non-self handles per thread checked before Chrome,
  `XPOSTER_MAX_MENTIONS_PER_THREAD`, global `--max-mentions N`, and mention
  counts in the preview/`doctor` output.
- **Ledger:** atomic JSON ledger with `PENDING`, `POSTED`, `UNKNOWN` and `FAILED`
  states, a `clicking_at` barrier fsynced before the click, immediate tweet-id
  recording, content hashing, `--force`/`--discard-recorded` recovery semantics,
  attempt history, directory fsync, and `LedgerCorruptError` instead of silently
  discarding a corrupt file.
- **Pacing:** gap between threads with persisted jitter, daily thread/tweet
  caps, rolling-hour cap and wrap-around quiet hours, using local time.
- **Parking:** `parked.json` marker for challenge/lock/rate-limit states, first
  reason preserved, and `xposter unpark` after human resolution.
- **Diagnostics:** `xposter doctor` offline checks (including artifact
  permissions), `--tells` browser measurements, `--dom` live selector
  calibration (waits for the composer, scopes to the `aria-modal` dialog and
  measures empty/typed/thread phases), opt-in `--with-media` phase D (uploads a
  generated PNG, records attachment/alt screens, removes and discards) and
  read-only `--drafts`, with private, sanitised artifacts.
- **CLI:** `login`, `logout`, `status`, `verify`, `refresh`, `unpark`, `post`
  (with `--key`, `--media`/`--alt`, `--allow-alt` and `--discard-recorded`),
  `delete`, `ledger` (with `--reset-corrupt`), `doctor` (with `--drafts` and
  `--with-media`) and the global `--max-mentions`, with documented exit codes
  0–8 and a policy reminder.
- **Docs:** README with the policy warning first, design decision log, operating
  runbook (including logging in from a server via X11 forwarding/VNC and the
  last-resort `storage_state.json` transfer), OLSA integration notes, and the
  two research memos plus the selector study.
- **Tests:** offline unit tests, a real-local-Chrome browser integration suite,
  an in-process fake of `x.com`, a no-injection/override source guard, and a
  public-API test that pins every export and the lazy Playwright import.

### Live verification — 2026-11-09/10 (test account)

`VERIFIED-LIVE` on `@JayashakthiVis2`, home `~/.olsa/x-test`, Chrome 156–157.
These are first real runs, not a long-term safety result. **The policy warning is
unchanged:** X's Automation Rules still name non-API website scripting as a
permanent-suspension trigger, and a successful 2-tweet run is not evidence of
safety.

- Plain-Chrome one-time login + `verify`; auth cookie valid about one year.
- Authenticated site uses the **classic DOM** (`tweetTextarea_0`
  `contenteditable` textbox, `tweetButton`, `fileInput`, `toolBar`,
  `app-bar-close`; no `<textarea>`, no `data-modal-slot-surface`).
- `/compose/post` has **two composers** and two nested modal dialogs; unscoped
  selectors matched both. Every composer selector is now scoped to the
  `aria-modal` dialog and `locate()` takes the first **visible** match. The first
  dry run failed safely with "no visible match for composer_root" before that
  fix; nothing typed or posted.
- Real per-key `keyboard.type` enabled the Post button; the add-post `+` appears
  after text is typed and the footer then reads "Post all".
- Two `Shift+Enter` presses made a blank line and never submitted.
- Draft.js blocks (`div[data-block="true"]`, blank line = empty block with
  `br[data-text]`) made `innerText` over-count newlines; the read-back rebuilds
  text from blocks. The first dry run stopped safely on a false 91-vs-92
  mismatch.
- A watched 2-tweet dry run passed: read-back exact, composer discarded, nothing
  posted, ledger empty.
- A real 2-tweet composer thread posted with "Post all"; both ids came from
  `CreateTweet`, the ledger recorded `POSTED`, tweet 2 showed tweet 1 above it
  in the same conversation, and pacing blocked the next post for 30 minutes.
- `xposter delete` removed both test tweets; their status pages then showed
  "this page doesn't exist".
- Per-tweet images: a real 2-tweet thread with one image on each tweet posted
  (1200×675 on tweet 1, 800×450 on the reply, chain intact) and both tweets were
  deleted. Images attach through the modal's hidden `fileInput` with
  `set_input_files` (never the picker), upload immediately to X, and land in the
  focused editor. A blob preview with `naturalWidth/Height` 0 is accepted only
  when it has a non-empty box plus Edit/Remove controls and Post is enabled;
  the footer character counter is never counted as upload progress.
- `doctor --dom --with-media` (opt-in) uploads a tiny generated PNG, records the
  live attachment markup and alt screens, removes the image and discards. The
  first alt click shows a one-time `sheetDialog` "Add descriptions" sheet; only
  "No, thanks" is clicked, and the real editor is left via the back arrow
  because Escape closes too much.
- Headless `verify` and `refresh --force` worked once each at low volume: X
  accepted the saved session, no challenge, not parked. This is not evidence
  that headless stays unflagged; `HeadlessChrome` remains a bot-class UA, so
  prefer Xvfb on Linux.
- `refresh` only loads `/home` and scrolls.
- Mentions (real 2-tweet post, own handle only): typed `@JayashakthiVis2` was
  recognised in the Draft.js composer, including a line-final mention before a
  blank line and a tweet-final mention; the status pages showed real
  `<a href="/JayashakthiVis2">` links (two in tweet 1, one in tweet 2), the
  blank lines and guard space survived, and both tweets were deleted through
  `xposter delete` (pages then 404). Mentions of other accounts, notification
  delivery, hashtag/cashtag typeahead and nonexistent handles remain unverified.

### Known limitations

- Still **UNVERIFIED live**: the replies strategy; the reply composer
  (`doctor --dom-reply`) and the reply-by-reply `post_chain` resume (offline
  fake only); no pipeline-driven run through OLSA yet; alt text end to end (off
  by default); more than two images or four images per tweet; GIF/WebP/JPEG
  uploads (only PNG tested); images near 5 MiB or slow uploads; images with the
  replies strategy; `doctor --drafts` (first live run mis-read timeline posts;
  scoped fix offline-tested only — check Drafts manually, including for a
  possible earlier calibration leftover); Premium long posts/note-tweets; the
  live calibration `Save post?` discard sheet; mentions of other accounts,
  notification delivery, hashtag/cashtag typeahead and nonexistent handles;
  Xvfb/Linux; 20+ tweet threads; the submit-shortcut fallback; and all long-term
  account safety, challenge/rate-limit and 12-hour keepalive behaviour. Video is
  explicitly unsupported and xposter never resizes images. See `docs/design.md`.
- No CAPTCHA solving, fingerprint/UA spoofing, JavaScript patching, proxies,
  automatic password login, multi-account automation or official-API backend.
- The account can still be flagged, locked or suspended. X's Automation Rules
  name scripting the website as a permanent-suspension trigger. The official API
  removes the mechanism risk and remains the recommended alternative.
- Windows is not supported: the session store uses POSIX `fcntl` locking.
