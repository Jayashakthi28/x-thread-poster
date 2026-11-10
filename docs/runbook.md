# Runbook

Operational guide for `x-thread-poster`. It assumes the package is installed in a
virtual environment at `/srv/x-thread-poster/.venv` and the state lives in
`~/.olsa/x` (the default). Replace the paths with your own. For the policy risk
and the API alternative, read the warning in [`../README.md`](../README.md)
first; this runbook only covers operating the browser path.

Commands below use `xposter`; inside a checkout without activation that is
`.venv/bin/xposter`.

## Mentions: policy cap and typing hazard

X's Automation Rules, section **“Automated mentions and replies”** (April 2026),
say:

> "Automating these actions to reach many users on an unsolicited basis is an
> abuse of the feature, and is not permitted."
> — <https://help.x.com/en/rules-and-policies/x-automation>

Tagging authors or organisations from an automated account is an unsolicited
automated mention unless those users opted in. xposter therefore enforces a hard
cap **before any browser starts**:

- default **5 distinct non-self `@handles` per thread**
  (`max_mentions_per_thread`);
- the posting account's own handle is not counted; repeats count once; e-mail
  addresses and URL paths are not mentions;
- override only deliberately, and preferably on a test account, with the global
  `--max-mentions N` option or `XPOSTER_MAX_MENTIONS_PER_THREAD=N`;
- `xposter doctor` prints the configured cap.

Typing a mention works, but there is a live typeahead hazard. X opens a
suggestion dropdown for a line-final `@handle`, `#hashtag` or `$cashtag`; the
next Shift+Enter **selects the suggestion** instead of inserting the line break,
adds a trailing space and can swallow a blank line. xposter closes such a token
with **one plain space** before the newline (never Enter/Tab/Escape) and the
read-back tolerates **only trailing spaces at line ends**. A posted line may
therefore end with a space. The read-back caught the bad case and stopped before
the Post click; if it ever reports a mismatch near a line-final token, inspect
`typed_mismatch.txt` and do not force the post.

`VERIFIED-LIVE` on 2026-11-10: a real 2-tweet composer thread posted every
`@JayashakthiVis2` (the account's **own** handle) as a real link —
`<a href="/JayashakthiVis2">` — two in tweet 1 and one in tweet 2, including a
line-final mention before a blank line and a tweet-final mention. The blank
lines and the guard space survived, and both tweets were deleted through
`xposter delete` (pages then 404). Because only the own handle was used, nobody
else was notified. Mentions of **other** accounts, notification delivery,
`#hashtag`/`$cashtag` typeahead live and handles that do not exist remain
**UNVERIFIED**.

## 0. Prerequisites

- Python 3.11 or newer, and the package installed (`pip install -e .` or
  `make dev-install`).
- Google Chrome installed normally. No `playwright install` is needed: the
  package launches the installed Chrome, never Playwright's Chromium.
- A desktop display for the one-time login and for `--display visible`. On
  macOS that is the logged-in desktop; on Linux it is `DISPLAY` or
  `WAYLAND_DISPLAY` (or an Xvfb/VNC session you can see).
- On Linux, for unattended posting: `xvfb` (and the usual Chrome font
  packages, for example `fonts-liberation`).
- A dedicated OS user or a home directory that other users cannot read. The
  home is forced to `0700`.

Never run two `xposter` processes against the same `home`: Chrome corrupts a
`user-data-dir` used by two processes. The package takes a non-blocking lock and
the second run exits 8 with the holder's pid.

## 1. First login

```bash
xposter login
```

What happens:

1. A **plain Chrome window** opens at `https://x.com/i/flow/login` with the
   dedicated profile. No Playwright/CDP is attached to this window.
2. Sign in as the account that will post. Complete 2FA in the window if asked.
3. **Quit Chrome completely** (`Cmd+Q` on macOS, or close every Chrome window)
   once X shows your home timeline. Closing only the tab is not enough: Chrome
   flushes cookies to disk when it exits normally.
4. The command reopens the profile through Playwright, verifies the login and
   prints the handle, Chrome version, auth-cookie expiry, verification time and
   display mode. It then reminds you to run `xposter doctor --dom`.

The login window has a 15-minute budget. On timeout the command asks Chrome to
quit normally, then terminates it, then kills it if necessary. If the window is
closed before sign-in finished, verification fails with exit 3 and the message
tells you to run `xposter login` again.

Use `--handle` to make a mismatch fail early:

```bash
xposter --handle olsa login
```

The handle is compared case-insensitively. A wrong handle exits 3 with
`Account mismatch`; it does not post.

After a successful login:

```bash
xposter status
```

should show `Session: ok (@handle)`, the auth-cookie expiry, `Parked: no` and the
next allowed post.

**VERIFIED-LIVE (2026-11-09, test account `~/.olsa/x-test`, Chrome 156,
visible):** the plain-Chrome login followed by `xposter verify` succeeded on the
same profile, and the auth cookie's expiry was about **one year** from the run.
That is one observation, not a promise of session longevity.

**On a headless Linux server:** the login window is always headed (that is the
point), so a bare `xvfb-run xposter login` gives you no window to type into. Run
it on a display you can actually see: an SSH X11-forwarded session, a VNC desktop
on the server, or a temporary Xvfb display exposed through VNC. The login must
happen on the machine and IP that will post; see section 6.

## 2. Calibration after login

Run the offline checks first:

```bash
xposter doctor
```

This checks Chrome, the display resolution, Xvfb availability, file permissions,
the saved session, the parked marker, ledger writability, the clock, and prints
the policy-risk reminder. Warnings are normal (`session` before login, `risk`
always, `display` when auto resolves to headless, `xvfb` on a Linux host without
it). A `[fail]` makes the command exit 1; fix it before posting. A parked
account is a `[fail]` until you unpark.

On macOS and Windows, `auto` resolves to **headless**, and `doctor` reports the
display as a `[warn]` because the `HeadlessChrome` token is visible to X. For the
first supervised runs use a visible window:

```bash
xposter --display visible doctor --dom
xposter --display visible post --file thread.txt --dry-run
```

On a Linux server, install Xvfb and let `auto` resolve to `xvfb` instead; that
gives a headed Chrome without the token (section 6).

Measure the live browser:

```bash
xposter doctor --tells
```

This opens the saved profile (login not required) and reports what a page can
observe: `navigator.webdriver`, user agent and `HeadlessChrome` token, UA-CH
brands, plugins, languages, screen/window sizes, timezone, WebGL renderer,
visibility and focus. It never patches anything. Expected results:

- `navigator.webdriver` = `false` — a `true` here is a `[warn]`; do not post.
- Headless mode shows a `HeadlessChrome` token as a `[note]`; that is truthful
  for headless. On Linux prefer `--display xvfb` to avoid the token.
- A screen below 1024×768 is a `[note]`; a hidden page is a `[warn]`.
- Software WebGL (SwiftShader/llvmpipe) is a `[note]`, common in VMs.

**VERIFIED-LIVE (2026-11-09, test account, Chrome 156, visible):**
`doctor --tells` reported all checks `[ok]`: `navigator.webdriver` false, no
`HeadlessChrome` token, and real UA-CH brands.

Then calibrate the selectors against X's live composer:

```bash
xposter doctor --dom
```

The authenticated site on the test account uses the **classic responsive-web
client**: `tweetTextarea_0` is a `contenteditable` `role="textbox"`, and the
composer exposes `tweetButton`, `fileInput`, `toolBar` and `app-bar-close`; there
is no `<textarea>` and no `data-modal-slot-surface`. The selector table therefore
tries the classic testids first and keeps the logged-out `x-web` selectors as
fallbacks.

`/compose/post` renders **two composers**: the home-timeline inline composer
(`tweetButtonInline`) sits under the compose **modal**, and the modal itself is
two nested dialogs — an outer full-screen wrapper and an inner
`[role="dialog"][aria-modal="true"]` plus a `mask`. Every composer selector is
scoped to the `aria-modal` dialog, and `locate()` returns the first **visible**
match. Before that fix the first live dry run failed safely with
`no visible match for composer_root`; nothing was typed or posted.

`doctor --dom` opens `https://x.com/compose/post` and waits up to **20 s** for the
composer editor to render before measuring anything. It then measures every
logical selector and fallback candidate in three phases — **A** empty composer,
**B** the harmless text `xposter calibration` typed into the first editor,
**C** one add-post click with `two` typed into the second — saves phase
screenshots, the composer HTML and a JSON report under `~/.olsa/x/artifacts/`,
and closes, attempting Discard. It never clicks Post/Reply/Schedule/Save. The
live `Save post?` discard sheet is still **UNVERIFIED**; only the offline fake
covers that branch.

**The first `doctor --dom` on 2026-11-09 reported all composer selectors
missing.** That was a race: it measured before React rendered the composer, not
a selector failure. The command now waits for the editor before measuring. If it
ever says the composer never rendered, the counts are **inconclusive**; re-run
after confirming the login rather than editing the selector table.

The table ends with one of:

- `All composer selectors matched at least one element.` — the composer can be
  driven.
- `Composer selectors missing: ...` — **do not post** until you have inspected
  the screenshot/HTML and updated `src/xposter/selectors.py`, then re-run.
- `Composer DOM was not available; selector misses are inconclusive.` — fix the
  login/render problem and re-run; do not treat this as selector evidence.

If `--dom` is run while logged out, the composer does not render and the report
is inconclusive rather than a selector verdict.

With **`--with-media`** (opt-in, used as `xposter doctor --dom --with-media`),
phase D attaches a tiny generated PNG, records the live attachment markup and
alt screens, removes the image and dismisses the composer with Discard. It does
not post. The first alt click shows a **one-time intro sheet** (`sheetDialog`
"Add descriptions", buttons "Sure" / "No, thanks"); only **"No, thanks"** may be
clicked. It sits over the "Edit image description" screen (back arrow, title,
black "Save"). **Escape closes too much**; leave that editor via the **back
arrow**. The alt text field and Save are still UNVERIFIED live; phase D records
the screens but does not claim the full alt flow works.

`doctor --drafts` is read-only: it opens the composer, clicks only the Drafts
control inside the modal, lists what is visible, and dismisses with Escape. It
never edits or deletes a draft. Its first live run mis-read timeline posts as
drafts; the scoped fix is **offline-tested only**, so treat the output as
**UNVERIFIED live** and check the account's Drafts panel manually once —
especially after an earlier calibration run.

## 3. First live test

Use a test account if at all possible. The sequence below is the acceptance
test; the first real runs completed it on `@JayashakthiVis2` (2026-11-09/10).

**First real runs (VERIFIED-LIVE, 2026-11-09/10, test account @JayashakthiVis2,
home `~/.olsa/x-test`, Chrome 156–157):** after the scoping and Draft.js read-back
fixes, a watched 2-tweet dry run typed both tweets, read the text back exactly,
discarded the composer and posted nothing (ledger empty). A real 2-tweet
composer thread then posted with "Post all": both ids came from X's own
`CreateTweet` responses, the ledger recorded `POSTED`, tweet 2 showed tweet 1
above it in the same conversation, and pacing then blocked the next post for the
configured 30-minute minimum gap. `xposter delete` removed both test tweets and
their status pages showed "this page doesn't exist".

**Images (VERIFIED-LIVE, 2026-11-10, Chrome 157, visible):** a real 2-tweet
thread with one image on each tweet posted; the status pages showed a 1200×675
image on tweet 1 and an 800×450 image on the reply, the thread chain was intact,
and both tweets were deleted through `xposter delete`. Images attach through the
modal's hidden `[data-testid="fileInput"]` (`set_input_files`, never the picker),
X uploads immediately to `/i/media/upload.json`, and the image lands in the
**focused** editor. The live markup is
`[data-testid="attachments"]` > `div[role="group"][aria-label="Media"]` >
`img[src="blob:…"]` + `button[aria-label="Edit media"]` +
`button[aria-label="Remove media"]`; "Tag people" and "Add description" are
plain spans.

Still **UNVERIFIED live** for images: alt text end to end (off by default),
more than two images / four per tweet, GIF/WebP/JPEG (only PNG tested), near
5 MiB or slow uploads, and images with the replies strategy. Video is
unsupported; xposter never resizes, so callers must keep images under 5 MiB.

Still **UNVERIFIED live**: the replies strategy, alt text end to end, the
untested image variants above, Premium note-tweets, `doctor --drafts` (check
Drafts manually once, including for a possible earlier calibration leftover),
the calibration `Save post?` discard sheet, Xvfb/Linux, 20+ tweet threads, the
submit-shortcut fallback, and all long-term account safety/challenge/rate-limit/
12-hour keepalive behaviour.

```bash
# 1. A short thread, rehearsed. This opens Chrome and types, but posts nothing
#    and writes no ledger row.
xposter --handle your_test_handle post --text "First line. Second line of the same thread." --dry-run

# 2. A real short thread. It asks for confirmation; --yes skips the prompt.
xposter --handle your_test_handle post --text "First line. Second line of the same thread." --yes

# 3. Confirm what the ledger recorded.
xposter ledger --limit 5
```

Check the printed status URLs in a normal browser. Then clean up the test posts:

```bash
xposter delete https://x.com/your_test_handle/status/1234567890 --yes
```

**Line breaks (VERIFIED-LIVE, 2026-11-09/10).** The poster types a line break as
**Shift+Enter**, never a bare Enter, because the composer submits on bare
Return. Two Shift+Enter presses produced a blank line and did not submit; the
blank line was preserved in the real posted thread. Recheck this after any X UI
change. The Draft.js read-back below is the safety net if the composer loses a
break.

### Known live gotchas

| Gotcha | What the live site does | What xposter does |
| --- | --- | --- |
| Two composers | `/compose/post` keeps the home inline composer (`tweetButtonInline`) behind the compose modal, which is itself two nested dialogs (outer wrapper, inner `aria-modal` + `mask`). Unscoped selectors match both editors. | Every composer selector is scoped to the `aria-modal` dialog; `locate()` takes the first **visible** match. The first dry run failed safely before this fix. |
| Draft.js blocks | The classic editor is Draft.js: each line is `div[data-block="true"]`; a blank line is an empty block with only `br[data-text]`, so `innerText` over-counts newlines. | The pre-Post read-back reconstructs text from blocks and joins them with one newline; the first dry run stopped at a false 91-vs-92 mismatch, before any click. |
| Classic DOM | The authenticated account serves the classic testids, not the `x-web` bundle DOM. | Classic testids are primary; new-client selectors are fallbacks. `doctor --dom` reports per-phase visible counts. |
| Read-back mismatch | A composer can silently drop or add characters. | xposter never clicks Post on a mismatch: it saves artifacts, raises a UI error before dispatch, and the ledger stays failed/not posted. |
| Pacing after a thread | A completed thread starts the minimum-gap clock. | The next post is blocked for the configured gap (30 minutes by default); `status` shows the next allowed time. |
| Image input | The picker button opens the OS dialog; the hidden `[data-testid="fileInput"]` is the reliable path, and the image attaches to the **focused** editor. | xposter calls `set_input_files` on the hidden input and never clicks the picker; it focuses/click the target editor first, so per-tweet images stay on their tweet. |
| Empty attachments / avatar | The empty attachments container has no visible box; a generic `img` fallback counts the account avatar (baseline 1). | Media counting is restricted to `img` inside the attachments/Media container, with the baseline taken immediately before `set_input_files`. Earlier avatar-counting image dry runs timed out safely before any click. |
| Blob preview readiness | A rendered image can report `naturalWidth`/`naturalHeight` 0; the footer always has a character-counter progressbar. | Readiness accepts a rendered `blob:`/`data:` preview with a non-empty box plus Edit/Remove controls **and** requires Post enabled; progress is read only inside `attachments`. |
| Discard on close | A dirty composer close shows `confirmationSheetDialog` ("Save post?", Save / Discard). | Discard/Cancel is clicked, never Save. |
| Alt intro/back | First "Add description" shows a one-time `sheetDialog` "Add descriptions" over "Edit image description"; Escape closes too much. | Only "No, thanks" is clicked; the real editor is left via the back arrow. Alt end to end remains UNVERIFIED. |
| Line-final mention/hashtag/cashtag | Typing `@handle`, `#hashtag` or `$cashtag` opens a suggestion dropdown; a following Shift+Enter selects the suggestion (adds a space, may swallow a blank line). | `_type` types one plain space after a line-final/final token, never Enter/Tab/Escape; read-back tolerates trailing spaces at line ends only. A real post with the account's **own** handle is VERIFIED-LIVE; other accounts and hashtag/cashtag typeahead are UNVERIFIED. |

`delete` removes the posts newest-first through the web UI and prints each
deleted id. It does **not** clear the ledger: `POSTED` remains, because the
thread really was posted and a rerun must not duplicate it. If you want to reuse
the exact same text on the test account, give the new post a different `--key`
(or change the text); a plain rerun will be skipped, and `--force` is recovery
rather than a repeat tool.

A good first test also exercises a thread longer than one tweet (the splitter
adds `1/n` markers) and one with a small image:

```bash
xposter post --text "$(cat long_thread.txt)" --media cover.png --dry-run
```

## 4. Normal operation

```bash
# Show the saved session, parked state, next allowed post and ledger counts.
xposter status

# After a login change or a break: prove the session still works.
xposter verify

# Post from a file, with a confirmation prompt.
xposter post --file thread.txt

# Post the weekly index verbatim and split the rest into numbered replies.
xposter post --file replies.txt --first-file index.txt

# Machine-readable result for a scheduler; preview/notices go to stderr.
xposter post --file thread.txt --yes --json

# What happened recently.
xposter ledger --limit 20 --json
```

Behaviour to expect:

- `post` prints a preview with each tweet's weighted length and the media file
  names, then asks `Post N tweet(s) as @handle?` unless `--yes`.
- Cancelling at the prompt exits 0 and posts nothing.
- `--dry-run` still opens Chrome, requires a valid login and enforces the parked
  check, but skips local pacing, writes no ledger row and posts nothing. It
  really attaches/uploads any specified media and then discards the composer, so
  it is also an upload rehearsal. It also refuses to run on an `UNKNOWN` thread,
  because the safe answer there is to check the profile first.
- The `composer` strategy is the default and is VERIFIED-LIVE for a 2-tweet
  thread. Use `--strategy replies` if you want a thread that can resume after a
  crash; the replies strategy is still **UNVERIFIED live** (it costs one page
  load per reply and is more exposed to UI changes).
- `xposter status` is offline. It does not open Chrome and does not touch X.

Do not put `xposter post` in a schedule that assumes success. Pacing may exit 5,
a challenge may exit 4, and an unknown outcome may exit 6; those are signals for
a human, not conditions to retry blindly.

## 5. Session refresh cadence

An idle session can be invalidated by X at any time. The package offers one
lightweight, read-only keepalive:

```bash
xposter refresh
```

It runs only when `last_refresh_at` is missing or older than
`keepalive.min_interval_s` (default **43,200 s = 12 h**). When it runs, it opens
`/home` under the normal session checks and scrolls the timeline for a random
20–60 s. It does not like, follow, repost, post or click anything. It updates
`last_refresh_at` only after a successful run and prints the next due time.
`VERIFIED-LIVE` on 2026-11-09/10: `refresh --force` loaded `/home` and scrolled
only; no challenge, not parked. That is one low-volume run, not evidence about
the 12-hour cadence over weeks.

```bash
xposter refresh --force   # manual only: ignore the 12 h interval, floored at 10 min
```

`--force` is for an operator who wants a check now. It is still refused when the
last successful refresh is under ten minutes old (`forced refresh too soon: min
10 minutes between forced refreshes`), so even a mistaken cron cannot turn the
keepalive into polling. Do not put `--force` in a timer: it defeats the interval
and creates the repeated-activity pattern the keepalive exists to avoid.

### cron (daily, comfortably longer than the 12 h minimum)

```cron
# /etc/cron.d/xposter-refresh
SHELL=/bin/sh
17 9 * * * olsa cd /srv/x-thread-poster && XPOSTER_HOME=/home/olsa/.olsa/x .venv/bin/xposter refresh >> /var/log/xposter-refresh.log 2>&1
```

A daily run is 24 h apart, so it can never be more frequent than the default
12 h interval. Running every 12 h is also acceptable if the scheduler is exact;
a run that arrives a few seconds early simply prints "not needed" and the next
one catches up. Do not run every few minutes: the command will refuse, but the
log noise is pointless.

### systemd timer (12 h plus jitter, never early)

`/etc/systemd/system/xposter-refresh.service`:

```ini
[Unit]
Description=Keep the xposter X session warm
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=olsa
WorkingDirectory=/srv/x-thread-poster
Environment=XPOSTER_HOME=/home/olsa/.olsa/x
ExecStart=/srv/x-thread-poster/.venv/bin/xposter refresh
# A parked or logged-out session fails visibly; do not retry it here.
```

`/etc/systemd/system/xposter-refresh.timer`:

```ini
[Unit]
Description=Run the xposter keepalive every 12 hours

[Timer]
OnBootSec=10min
OnUnitActiveSec=12h
RandomizedDelaySec=15min
AccuracySec=1s
Persistent=true
Unit=xposter-refresh.service

[Install]
WantedBy=timers.target
```

`RandomizedDelaySec` only delays; it cannot make the timer fire earlier than
12 h after the previous activation. `Persistent=true` catches up after downtime
once. Enable with `systemctl enable --now xposter-refresh.timer` and check with
`systemctl list-timers xposter-refresh.timer`.

`refresh` exits 3 when the session is gone, 4 when parked, 7 on a UI change and
8 on a browser/profile problem. A timer failure is a signal to open the account
manually, not to restart the timer in a loop.

## 6. Logging in from a server / moving to Linux

**Short answer to “can I transfer my cookies?”:** not recommended. The safe path
is to log in **on the server** with `xposter login` on a display you can see.
Transferring `storage_state.json` is a last resort, is explicitly untested on a
real server, and can get the account challenged, logged out or locked.

The session is tied to the machine and network it was created on. X's edge
tracks session/device/audit context (the bundle study shows the headers; the
exact binding is inferred, not documented). Keep this rule:

> **The login must happen on the machine and IP that will post.**

### (a) RECOMMENDED: log in on the server, on a display you can see

`xposter login` starts a **plain, headed Chrome window**; it cannot show a window
on a bare headless host. Use one of these two recipes. In both, the human types
the credentials and completes any 2FA/challenge in that window; xposter never
automates the login form.

**SSH X11 forwarding** (simplest; slow, but fine for one login):

```bash
# On your local machine (macOS needs XQuartz installed; log out/in after
# installing it if `ssh -X` does not work):
ssh -X user@server

# On the server:
echo "$DISPLAY"          # normally localhost:10.0
cd /srv/x-thread-poster
.venv/bin/xposter --display visible login
```

A datacenter IP may trigger X's unusual-login challenge. That is expected;
complete it in the Chrome window. When X shows the home timeline, **quit Chrome
completely** (Chrome's Quit menu item, or close every Chrome window) so the
cookies are flushed.

**Temporary VNC desktop** (more responsive; use a VNC client; install `x11vnc`
on the server first):

```bash
# On the server, as the xposter user:
Xvfb :99 -screen 0 1280x900x24 &
x11vnc -display :99 -localhost -forever -shared -rfbport 5900 &

# On your local machine: tunnel the VNC port, then connect a VNC client to
# localhost:5900
ssh -L 5900:localhost:5900 user@server

# Back on the server, in a shell with the virtual display:
cd /srv/x-thread-poster
DISPLAY=:99 .venv/bin/xposter login

# After quitting Chrome completely, stop the temporary VNC pieces:
pkill -f 'x11vnc.*:99'
pkill -f 'Xvfb :99'
```

The VNC desktop has no window manager; the Chrome window still appears and is
interactive. Both recipes end with quitting Chrome fully; do not just close the
tab.

Once the login is verified, normal posting can use `--display xvfb` (or `auto`
on a Linux host with Xvfb installed). The login itself must not be headless.

### (b) The session belongs to the machine and IP

- The server should be the machine that posts. If the server's public IP differs
  from where the operator normally uses X, expect extra challenges.
- Do not use a VPN or proxy for the login and then post without it, or the
  reverse. Proxy rotation is deliberately not implemented.
- If X challenges the login, resolve it in the same Chrome window. If the tool
  ends up parked, resolve the account on `x.com` and run `xposter unpark`, then
  `verify`.

### (c) Alternative: use a residential always-on machine or the laptop

To avoid a datacenter IP, run xposter on a home/office always-on machine whose
public IP matches the operator's normal X use, or on the laptop. The OLSA server
can hand that machine posts to publish; the browser session then stays on the
residential machine. The laptop is the simplest option when no always-on
residential machine exists.

### (d) LAST RESORT: transfer only `storage_state.json`

This is **explicitly not recommended** and has **not been tested on a real
server**. A new OS/Chrome build/screen/timezone/IP is a new device fingerprint;
cookie transplant is the commonest trigger in the research memo's suspension
reports. X may challenge, log out other sessions or lock the account.

**What must not travel:**

- **`profile/` must not be copied.** The Chrome profile folder is **not portable
  across operating systems**: Chrome encrypts cookies with an OS-specific
  scheme, so a macOS profile cannot be read on Linux.
- **`ledger.json` must not be copied.** The ledger belongs to the machine that
  posts; copying an old ledger over a new one can hide or duplicate posts.

**What travels:** only `storage_state.json`, which is plaintext JSON and a
**full-account credential**. Treat it like a password. On the source machine,
run `xposter verify` first: a clean, healthy run refreshes that backup.

```bash
# On the server: create the private home first.
install -d -m 700 ~/.olsa/x

# On the source machine: copy ONLY that file, keeping its 0600 mode.
scp -p ~/.olsa/x/storage_state.json user@server:~/.olsa/x/storage_state.json

# On the server: confirm the mode and that no profile exists yet.
chmod 600 ~/.olsa/x/storage_state.json
ls -la ~/.olsa/x
# The profile MUST be empty (or absent) so authenticated_page restores the
# cookie backup into a new Chrome profile; do not create/log in to a profile.
```

`authenticated_page` restores the backup **only when the profile is empty**; if
a profile already exists, the transferred state is ignored. Then:

```bash
# First action: verify the restored session.
xposter --display xvfb verify

# Read-only browser checks; still no post.
xposter doctor --tells
```

Do not post for at least 24 hours. Expect a challenge; if one appears, the tool
parks and a human resolves it on `x.com`. Do not post, retry in a loop, or
immediately run a thread. If X logs out other sessions or locks the account,
follow the parked-account steps in section 7.

After the server is verified, delete any temporary copies and, if the session is
being moved rather than kept, securely delete the source copy too: `rm -P` on
macOS (best-effort overwrite then remove) or `shred -u` on Linux. Do not leave a
credential in `/tmp`, shell history, a backup archive or a chat message.

### Server setup checklist

1. Install Google Chrome and Xvfb (`xvfb` plus the distribution's Chrome
   dependencies and fonts). Verify with `google-chrome-stable --version` and
   `Xvfb -help`.
2. Create the service user and home:
   `install -d -m 0700 -o olsa -g olsa /home/olsa/.olsa/x`. The Chrome profile
   must be owned by the user that runs Chrome.
3. Install the package in the server checkout:
   `python3 -m venv .venv && .venv/bin/pip install -e .`.
4. Log in with recipe (a), then verify:
   ```bash
   XPOSTER_DISPLAY=auto xposter doctor
   XPOSTER_DISPLAY=auto xposter doctor --tells
   XPOSTER_DISPLAY=auto xposter doctor --dom
   ```
   With Xvfb installed, `auto` resolves to `xvfb`: a real headed Chrome on a
   1920×1080 virtual display, without the `HeadlessChrome` token.
5. Keep the login and posting on the same host and network; never rotate IPs.
6. If the server runs as root in a container, the browser layer adds
   `--no-sandbox` automatically on Linux root because Chrome refuses to start
   otherwise. Prefer a non-root user; `--no-sandbox` weakens Chrome's sandbox.

## 7. What "parked" means, and the exact steps before `unpark`

`parked.json` is written after a challenge, account lock/suspension, automation
flag or rate limit. While it exists:

- every command that would open Chrome raises `ParkedError` (exit 4) before the
  lock or the browser;
- nothing retries automatically;
- the marker keeps the **first** reason, kind and `since`, and appends later
  detail, so you can see how long the account has been waiting.

Read it with:

```bash
xposter status                 # human-readable
cat ~/.olsa/x/parked.json      # reason, kind, since, detail
```

Kinds:

| `kind` | Typical cause | Meaning |
| --- | --- | --- |
| `challenge` | "Verify you are a human", Arkose/CAPTCHA, "unusual activity", code 226/263/315 | A human must complete verification on the account. |
| `locked` | Account locked/restricted/suspended, code 27/64/159 | The account needs manual resolution; automation is terminal until then. |
| `rate_limit` | Daily post limit (185/501), HTTP 429, code 344 | Wait for the limit to reset; do not retry today. |

Do this, in order:

1. Open `https://x.com` in a **normal browser** (not xposter), on the same
   machine/IP, and sign in as the expected account.
2. Complete the challenge/2FA/identity flow if one is shown. Check
   Settings → Security and the account notifications for a lock or a new-device
   warning.
3. For a rate limit or an automation flag, wait. The research memo recommends
   at least 24–48 hours for automation flags and until the daily limit resets for
   185/501. Posting less afterwards matters more than the timer.
4. Confirm the account can load the home timeline and that you can post
   manually if you intend to. Do not use xposter while the marker exists.
5. Clear the marker:
   ```bash
   xposter unpark          # asks for confirmation
   xposter unpark --yes    # non-interactive
   ```
6. Prove the session again:
   ```bash
   xposter verify
   xposter doctor
   ```
7. If the same challenge returns, the account is not ready. It will park again;
   stop and wait longer. Do not loop `unpark → post`.

`unpark` only deletes `parked.json`. It does not log in, verify the challenge or
change the account; steps 1–4 are the actual fix.

## 8. Reading exit codes

Every command prints one operator message and exits with one documented code:

| Code | Meaning | First action |
| ---: | --- | --- |
| 0 | Success, or a deliberate no-op (cancelled, refresh not due, nothing to unpark). | — |
| 1 | Unexpected/unclassified failure, or a failing offline `doctor` check. | Re-run with `-v`; run `xposter doctor`. |
| 2 | Configuration or input validation. | Fix the flag, file, JSON, handle or content. |
| 3 | Session/auth: not logged in, wrong account, unrecognised login state. | `xposter login`, or log in as the expected account. |
| 4 | Parked, challenge, locked/restricted, rate limited. | Section 7; do not retry in a loop. |
| 5 | Local pacing. | Wait for the printed local time. |
| 6 | Post outcome unknown, or the ledger is unreadable/corrupt. | Check the profile; recover per section 9. For a corrupt ledger, restore it or run `xposter ledger --reset-corrupt`. |
| 7 | UI changed / selector mismatch. | Read the artifacts, run `doctor --dom`. |
| 8 | Browser unavailable or profile/session busy. | Install Chrome, close the other Chrome/xposter. |

A shell script can branch on these, but should treat 3, 4, 6 and 7 as "stop and
tell a human", never as "retry in a loop".

## 9. `UNKNOWN` ledger entries and `--force`

`UNKNOWN` means: a Post click may have landed, but the package could not read a
new tweet id. A `PENDING` row with a `clicking_at` barrier is treated the same
way: the click may have happened before the process died. That is not a normal
failure and it is not a park. The entry blocks a plain retry so the thread
cannot be duplicated.

Inspect it:

```bash
xposter ledger --limit 20 --json
```

Look for `"status": "unknown"` in `xposter ledger --json`, the `error`, the
`first_url` (empty when no id was captured) and the `thread_hash`. A `PENDING`
row may also carry the click barrier; the CLI summary does not print that field,
so inspect `~/.olsa/x/ledger.json` for a non-empty `"clicking_at"` when you need
to distinguish a pre-click interruption from a possible click. Then check the
account in a normal browser:

- open the profile and look for the exact text (X search:
  `from:your_handle "a distinctive phrase"`);
- check the thread/replies around the time of the run;
- if the run was a reply chain, check whether later replies exist after the last
  captured id.

Decision:

| What you found | Action |
| --- | --- |
| Nothing was posted, no ids recorded | Re-run the same input with `--force`. The forced run starts a clean attempt. |
| The whole thread is live | Do nothing. Leave the `UNKNOWN` row as evidence. Delete the thread manually only if it was a test. |
| A partial thread is live and you want to continue | For `replies`, re-run with `--force` (or a plain re-run if the row is `FAILED`); it resumes after the recorded ids. The composer still refuses, because it cannot continue a partial thread safely. |
| A partial thread is live and you deleted it by hand | If the row is `UNKNOWN` or carries a click barrier, re-run with `--discard-recorded`; it starts over and asks for confirmation unless `--yes`. A `FAILED` partial row still resumes and is not cleared by that flag. |

`--force` semantics, exactly:

- It re-opens a `POSTED` or `UNKNOWN` record. On a partial thread it returns
  `RESUME`; the replies strategy continues after the recorded tweets, and the
  composer strategy refuses. A completed `POSTED` thread is posted again as a
  new attempt, with the old record kept in ledger history.
- `--discard-recorded` starts an `UNKNOWN` or click-barrier partial thread over;
  it is the explicit assertion that the recorded tweets were deleted by hand.
  A `FAILED` partial row still takes the normal resume path, so this flag cannot
  turn it into a clean start; use a new `--key` only if you intend a deliberate
  fresh thread.
- It does not change the content hash: use the same text and the same media file
  names, or the run is a different thread anyway. To repeat the same text on
  purpose, give it a new `--key`.
- `--dry-run --force` still writes no ledger row and posts nothing; it is not a
  way to clear an `UNKNOWN` entry.

## 10. Artifacts

Posting failures (`UiChangedError`, exit 7) and `doctor --dom` write evidence
under `~/.olsa/x/artifacts/`:

| Directory | Files | Source |
| --- | --- | --- |
| `<UTC timestamp>-<label>/` | `screenshot.png`, `page.html`, `url.txt` | A posting/delete UI failure. Label is the strategy and thread hash, or `delete-<id>`. |
| `<UTC timestamp>-dom-calibration/` | `phase-a.png`, `phase-b.png`, `phase-c.png` (when rendered), `compose-dialog.html`, `report.json`; with `--with-media` also `phase-d.png`, `phase-d-alt.png`, `phase-d-edit.png`, `media-attachments.html`, `media-modal.html`, `media-alt-intro.html`, `media-alt-editor.html`, `media-edit-editor.html` (when captured) | `xposter doctor --dom [--with-media]`. |
| `<UTC timestamp>-drafts-audit/` | `drafts.png`, `drafts.html`, `drafts.json` | `xposter doctor --drafts` (read-only; still UNVERIFIED live). |

- Only the newest `keep_artifacts` directories (default 10) and those younger
  than `artifact_max_age_days` (default 14) are kept; older ones are deleted when
  a new one is created. The newest is always kept.
- Directories are `0700`; every artifact file is written mode `0600`. The
  sanitizer redacts hidden-input values, CSRF `meta` tags, inline token-shaped
  JSON, `Authorization` headers and `Bearer` values, and caps the composer HTML
  at 200 KB. Screenshots are still page content: treat all artifacts as private
  and do not paste them into a public issue without checking.
- `report.json` lists `rendered`, `waited_s`, a note, per-phase (`A`/`B`/`C`)
  counts for every selector candidate, the dialog buttons and the discard
  action. If a primary is missing but a fallback matched, the report shows the
  fallback; the fix is to promote or repair the selector only after confirming
  on the live DOM.

## 11. Backup and restore

The home is a full-account credential, not just settings. Back it up only to
encrypted, access-controlled storage, and restore it **only on the same machine
and IP**.

What to back up:

```bash
# Stop xposter and any Chrome using the profile first.
tar -C "$HOME/.olsa" -czf x-thread-poster-backup.tgz x
chmod 600 x-thread-poster-backup.tgz
```

The archive contains `profile/`, `storage_state.json` (+ `.bak`, and
`.pre-reset` if `logout` made one), `session.json`, `ledger.json` (plus any
`ledger.json.corrupt-*` files), `parked.json` and `artifacts/`. The ledger is the
duplicate-post evidence; do not restore an old ledger over a newer one without
understanding which posts it knows about.

Restore on the same machine:

```bash
# Stop every xposter/Chrome process first.
mv "$HOME/.olsa/x" "$HOME/.olsa/x.old"
tar -C "$HOME/.olsa" -xzf x-thread-poster-backup.tgz
chmod -R go-rwx "$HOME/.olsa/x"
xposter status
xposter verify        # restores storage_state only if the profile is empty
xposter doctor
```

If the profile is intact, the backup state file is redundant. If the profile is
missing but `storage_state.json` survives, the next run restores the cookies into
an empty profile; that is same-machine recovery, not a migration. Never use it
to move a session to another host or IP.

To reset the saved login without touching parking, the ledger or artifacts:

```bash
xposter logout          # keeps storage_state.json as storage_state.json.pre-reset
xposter logout --no-backup  # delete the backup too
xposter login
```

`logout` holds the store lock and deletes the Chrome profile contents. With the
default backup kept, the old login is preserved as
`storage_state.json.pre-reset`; a fresh profile does **not** read that file
automatically, so copy it back to `storage_state.json` (mode `0600`) before
starting if you intend to reuse the same login. That is a deliberate
same-machine recovery step, not a migration. `--no-backup` deletes
`storage_state.json` and its `.bak` and skips creating a new `.pre-reset`; an
older `.pre-reset` from a previous logout is not removed, so delete it yourself
if you want the credential gone. There is also a Python API
(`xposter.sessions.reset_profile`) for the same operation.

If the **ledger** is unreadable, posting stops with exit 6 (`Ledger corrupt`)
rather than silently starting with no history. Restore `ledger.json` from a
backup, or, after checking the account in a normal browser, quarantine it:

```bash
xposter ledger --reset-corrupt   # asks for confirmation
```

That moves the file to `ledger.json.corrupt-<timestamp>` and starts an empty
ledger. Only do it when you accept that the old duplicate-post evidence will no
longer be consulted.

## 12. Upgrading Chrome

`xposter` finds the installed Chrome on every run; there is no pinned browser
version and no bundled browser to upgrade. Chrome's own auto-update is fine.

After a major Chrome upgrade:

```bash
xposter doctor
xposter doctor --tells
xposter doctor --dom     # especially if the last post failed with exit 7
```

Chrome migrates the profile forward. Do not downgrade Chrome against a profile
that a newer version has already migrated, and do not copy the profile to an
older Chrome. If a profile is corrupted after an upgrade, stop Chrome, back up
the home, run `xposter logout` and then `xposter login` on the same machine/IP;
the ledger is retained, so previously posted threads remain protected from
duplicates.

## 13. Troubleshooting

| Symptom / exit | Likely cause | Fix |
| --- | --- | --- |
| `Google Chrome was not found` (8) | Chrome is not installed in a known location. | Install Google Chrome or set `XPOSTER_CHROME_PATH` to its executable; `xposter doctor` shows what it found. |
| `Chrome at ... could not be started` (8) | Bad path, missing shared libraries, or Chrome refused to run. | Run the executable by hand; on Linux install the Chrome dependencies; check `doctor`. |
| `another Chrome is using the profile` (8) | A live Chrome owns the profile or `SingletonLock`. | Close every Chrome window using this profile; wait for the other xposter run. Do not delete `SingletonLock` while a Chrome process is alive. |
| `Another xposter run holds the session lock (pid N)` (8) | A second xposter process, a stuck cron, or a crashed run that still holds the lock. | Wait for pid N; if it is gone, the OS released the lock and a retry works. Never run two posts in parallel. |
| `Not logged in` / `X redirected to the login flow` (3) | No saved session or it was revoked/expired. | `xposter login` on this machine/IP. |
| `Account mismatch` (3) | The profile is signed in as a different handle than `--handle`/`XPOSTER_HANDLE`. | Log in as the expected account, or pass the right `--handle`. |
| `Could not confirm that X is signed in` (3) | The page shows no recognisable login state; markup may have changed or the page is mid-redirect. | Retry; if it persists, `xposter login`, then `doctor --dom`. |
| `Parked` / `Challenge required` / `Account restricted` / `Rate limited` (4) | `parked.json` exists. | Section 7. Do not retry until resolved and unparked. |
| `Local pacing rule ...` (5) | Gap, daily/hourly cap or quiet hours. | Wait for the printed local time; `xposter status` shows the next allowed post. |
| `no visible match for composer_root` (7) | `/compose/post` has two composers and nested dialogs; an old/unscoped selector can resolve to the hidden one. | Confirm the classic DOM and `doctor --dom`; the current selectors are scoped to `[role="dialog"][aria-modal="true"]` and `locate()` takes the first visible match. |
| `The composer did not keep the text that was typed` (7) | Draft.js changed the read-back, often around a blank line or a line-final `@handle`/`#hashtag`/`$cashtag`; nothing was clicked. | Inspect `typed_mismatch.txt`; check the Draft.js block read-back and the typeahead space guard. Nothing was posted. |
| `N distinct @mentions exceed max_mentions_per_thread=...` (2) | The thread tags more distinct accounts than the policy cap allows. | Tag only accounts that expect it; use fewer mentions, or raise `--max-mentions`/`XPOSTER_MAX_MENTIONS_PER_THREAD` deliberately on a test account. |
| `doctor --drafts` lists timeline posts as drafts | First live-run scoping bug; the scoped fix is offline-tested only. | Treat the output as unverified; check the account's Drafts panel manually. |
| `doctor --dom` reports every composer selector missing or the composer never rendered | The measurement raced React's render (the first live run did exactly this), or the login/profile is wrong. | Re-run; the command now waits up to 20 s for the editor. Confirm `status`/`verify` first. Do not edit selectors from an inconclusive report. |
| `Ledger corrupt` (6) | `ledger.json` exists but cannot be parsed or read. | Restore it from backup, or check the account and run `xposter ledger --reset-corrupt`; posting stays blocked until one is done. |
| `Post outcome unknown` (6) | A click may have landed; no id captured. | Section 9. Check the profile before `--force`. |
| `UI changed` / selector message (7) | X changed the DOM, the account is on a different client, or a modal is covering the target. | Read the newest artifact; run `doctor --dom`; update `selectors.py` only from the live report. |
| `The Post button stayed disabled` (7) | Content over the weighted limit, a media upload still pending, or a markup change. | The message names the weight/limit when it is content; otherwise inspect artifacts and media. |
| `The media preview did not appear` (7) | Unsupported file, too large, upload failure, or selector drift. | Try a small JPEG/PNG; check X's limits; re-run `doctor --dom --with-media`. |
| `The media upload did not finish within ...` (7) | Slow upload, near-limit image, or the preview counter is outside the attachments container. | Check the diagnostics; resize/re-encode, or run `doctor --dom --with-media` to see the live markup. |
| `media ... is ..., over X's 5.0 MiB limit` (2) | Image too large; xposter never resizes. | Downscale/re-encode below 5 MiB (GIF below 15 MiB) before posting. |
| `video upload is not supported` (2) | A video file is attached. | Use a PNG/JPEG/GIF/WebP image; video is explicitly unsupported. |
| `alt text is not verified ... --allow-alt` (2) | Non-blank alt was supplied without the opt-in. | Re-run with `--allow-alt` on a test account, or remove the alt text. Alt is off by default. |
| `Xvfb was not found` (8) | `--display xvfb`/`XPOSTER_DISPLAY=xvfb` on a host without Xvfb. | `sudo apt install xvfb`, or use `headless`. |
| `visible needs a desktop session` (8) | Linux `--display visible` without `DISPLAY`/`WAYLAND_DISPLAY`. | Run inside the desktop/VNC session, use Xvfb, or use headless. |
| `doctor` exits 1 with no live error | An offline check failed: parked, Chrome missing, bad display/configuration, private paths too open, or the home is not writable. | Read the `[fail]` line; fix it; re-run. |
| `Session refresh not needed ... not due` (0) | The keepalive interval has not elapsed. | Normal; do not use `--force` in a loop. |
| `The login window was closed before sign-in finished` (3) | Chrome was quit before X showed the timeline. | `xposter login` again; quit Chrome only after sign-in completes. |
| Auth cookie expired (3) | X invalidated/expired the session. | `xposter login`; this is expected eventually and is why the keepalive exists. |
| `xposter: command not found` | The virtual environment is not active. | Use `/srv/x-thread-poster/.venv/bin/xposter` or activate the venv. |

When asking for help, include the command, the exit code, the full message and
the `doctor` output. Do not include `storage_state.json`, `profile/` or
unredacted artifacts; they are account credentials or page content.
