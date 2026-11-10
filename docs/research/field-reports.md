# Independent field reports

This page records third-party field reports that informed xposter's design. They
are **evidence, not proof**: each is one person's experience with one account,
and none is a controlled study or a statement by X. A report is tagged
`REPORTED` and never upgrades a policy or safety claim. The policy warning in
the README still applies unchanged.

## Report 1 — "How I managed to auto-post to X for zero cost"

- **Source:** <https://note.com/tsukinami120/n/n035730bea32f>
- **Author:** 月波 (Tsukinami)
- **Published:** 2026-03-21
- **Scope:** a two-week personal project; one person, one account, one
  note.com-to-X workflow. The article describes what worked for that author and
  says nothing about long-term account safety, enforcement history or whether
  the approach remains viable after X changes its client.

### (a) A Playwright-driven login was blocked; manual login plus a saved session worked

`REPORTED`: the author first tried to automate the whole flow, including login.
After filling the email field and pressing "Next", the form reset to its initial
state with the field empty. It happened three times. The author attributes this
to X's bot detection and concluded that automating the login itself was the
problem.

The fix was to sign in **once by hand** in the browser, detect the `auth_token`
cookie, save the browser's authenticated state to a file (`auth.json`), and load
that state on later runs. After that, the author reports, no bot check fired and
the timeline opened already signed in.

**How xposter relates:** same strategy, stronger. `xposter login` does not drive
the login form with Playwright at all: it starts an ordinary Chrome process with
the dedicated profile and no Playwright/CDP attached, the human signs in, quits
Chrome, and only then does xposter reopen the same profile to verify it. The
persistent Chrome profile is the primary session; `storage_state.json` is a
same-machine backup. The author's cookie-file approach is closer to the backup
than to the primary profile, which is why xposter keeps both.

### (b) The Post button stayed disabled after paste or synthetic events

`REPORTED`: after typing the tweet, the Post button remained
`aria-disabled="true"`. Copying text to the clipboard and pasting it did not
enable the button. Dispatching a JavaScript click event did not enable it
either. The author attributes this to React's event system: the composer only
recognises input that arrives through the events a real keyboard produces. The
fix was Playwright's `keyboard.type()`, which sends per-character key events;
after that the button became enabled.

**How xposter relates:** `_type` uses real key events only for the tweet body.
Multi-character chunks are still typed with `keyboard.type`, line breaks are
`Shift+Enter`, and the editor's text is read back and compared with the intended
tweet before any Post click. There is no clipboard-paste path and no synthetic
JavaScript input path. If the read-back differs, xposter stops before the click,
saves the evidence and reports a UI error rather than clicking a disabled or
wrong composer.

### (c) Cmd/Ctrl+Enter submitted more reliably than finding the button

`REPORTED`: the author switched from locating and clicking the Post button to
the `Meta+Enter` shortcut on macOS (Cmd+Enter), and reports it as more reliable
and faster than finding the button element.

**How xposter relates:** the shortcut is **not** the default. xposter clicks
the button through its two click paths (a human-like pointer click, then
Playwright's actionability-checked click). An opt-in `submit_shortcut_fallback`
flag exists, is off by default, and is used only when both click paths fail
**before dispatching anything**. When enabled, it presses `Meta+Enter` on macOS
and `Control+Enter` elsewhere, from the focused editor. The click barrier stays
recorded because a keystroke may submit.

### (d) "The modal disappeared" was a false success signal

`REPORTED`: the author first judged success by whether the composer modal
disappeared. That produced false failures: the home timeline has its own
composer, so after closing the modal the home composer was still present and
the script believed the modal had not closed. The author fixed the check by
counting composer areas and waiting for the count to drop from two to one.

**How xposter relates:** it never judges post success from the UI. A tweet is
recorded as posted only when X's own `CreateTweet` response yields the new tweet
id. A missing id becomes `UNKNOWN`; the operator checks the profile rather than
trusting a disappearance, a toast, a counter or a home-timeline composer.

### What this report does not establish

- It is one person's two-week experience, not evidence that the account is safe
  over months or that X will not change its checks.
- It says nothing about X's enforcement against the account after the article
  was published, about long-term `auth_token` lifetime, or about the risk of a
  permanent suspension under the Automation Rules.
- It does not make browser automation permitted. The policy warning and the
  official-API alternative in the README are unchanged.
