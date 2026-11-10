"""Diagnostics tests: offline checks, formatting and DOM calibration with fakes."""

from __future__ import annotations

import json
import os
import struct
import zlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import xposter.diagnose as diagnose
from tests.fake_x import FakeTweet
from xposter import ConfigurationError, Display, XPosterConfig
from xposter.selectors import SELECTORS
from xposter.store import SessionStore

_CANARY = "password-value-never-write-me"


@pytest.fixture
def config(tmp_home: XPosterConfig, monkeypatch: pytest.MonkeyPatch) -> XPosterConfig:
    os.chmod(tmp_home.home, 0o700)
    monkeypatch.setattr(diagnose, "find_chrome", lambda item: "/Applications/Google Chrome")
    monkeypatch.setattr(diagnose, "chrome_version", lambda path: "156.0.0.0")
    monkeypatch.setattr(diagnose, "resolve_display", lambda item: Display.HEADLESS)
    return tmp_home


def check_by_name(checks: list[diagnose.Check], name: str) -> diagnose.Check:
    return next(check for check in checks if check.name == name)


def test_offline_report_fails_without_chrome(
    config: XPosterConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnose, "find_chrome", lambda item: None)

    checks = diagnose.offline_report(config)

    chrome = check_by_name(checks, "chrome")
    assert chrome.status == "fail"
    assert "not found" in chrome.detail
    assert "XPOSTER_CHROME_PATH" in chrome.detail


def test_offline_report_reports_a_found_chrome(config: XPosterConfig) -> None:
    chrome = check_by_name(diagnose.offline_report(config), "chrome")

    assert chrome.status == "ok"
    assert "156.0.0.0" in chrome.detail


def test_offline_report_fails_on_loose_permissions(config: XPosterConfig) -> None:
    os.chmod(config.home, 0o755)
    state = config.storage_state_path
    state.write_text("{}", encoding="utf-8")
    state.chmod(0o644)

    permissions = check_by_name(diagnose.offline_report(config), "permissions")

    assert permissions.status == "fail"
    assert str(config.home) in permissions.detail
    assert str(state) in permissions.detail
    assert "0755" in permissions.detail
    assert "0644" in permissions.detail


def test_offline_report_fails_on_a_loose_artifact_file(config: XPosterConfig) -> None:
    store = SessionStore(config)
    run = store.new_artifacts_dir("run-1")
    artifact = store.write_artifact(run, "compose.html", "<html></html>")
    artifact.chmod(0o644)

    permissions = check_by_name(diagnose.offline_report(config), "permissions")

    assert permissions.status == "fail"
    assert str(artifact) in permissions.detail
    assert "0644" in permissions.detail


def test_offline_report_accepts_private_paths(config: XPosterConfig) -> None:
    config.home.chmod(0o700)
    config.profile_dir.mkdir(mode=0o700)
    state = config.storage_state_path
    state.write_text("{}", encoding="utf-8")
    state.chmod(0o600)
    store = SessionStore(config)
    run = store.new_artifacts_dir("run-1")
    store.write_artifact(run, "compose.html", "<html></html>")

    permissions = check_by_name(diagnose.offline_report(config), "permissions")

    assert permissions.status == "ok"


def test_offline_report_warns_about_the_headless_user_agent(
    config: XPosterConfig,
) -> None:
    display = check_by_name(diagnose.offline_report(config), "display")

    assert display.status == "warn"
    assert "HeadlessChrome user agent is visible to X" in display.detail
    assert "--display visible" in display.detail
    assert "Xvfb on Linux" in display.detail


def test_offline_report_flags_parking(config: XPosterConfig) -> None:
    SessionStore(config).park("challenge shown", kind="challenge")

    checks = diagnose.offline_report(config)

    assert check_by_name(checks, "parked").status == "fail"
    assert "xposter unpark" in check_by_name(checks, "parked").detail


def test_offline_report_fails_on_a_corrupt_ledger(config: XPosterConfig) -> None:
    config.ledger_path.write_text("{broken", encoding="utf-8")

    ledger = check_by_name(diagnose.offline_report(config), "ledger")

    assert ledger.status == "fail"
    assert "corrupt" in ledger.detail
    assert "ledger --reset-corrupt" in ledger.detail


def test_offline_report_includes_every_expected_check(config: XPosterConfig) -> None:
    names = {check.name for check in diagnose.offline_report(config)}

    assert names == {
        "chrome",
        "display",
        "xvfb",
        "permissions",
        "session",
        "parked",
        "ledger",
        "clock",
        "risk",
    }


def test_format_checks_is_stable() -> None:
    lines = diagnose.format_checks(
        [
            diagnose.Check("chrome", "ok", "found"),
            diagnose.Check("session", "warn", "missing"),
        ]
    )

    assert lines == ["[ok] chrome: found", "[warn] session: missing"]


def test_sanitise_html_strips_password_values_in_every_attribute_order() -> None:
    html = (
        '<form><input type="text" name="q" value="keep">'
        '<input type="password" name="pw" value="SECRET1">'
        "<INPUT VALUE='SECRET2' TYPE=password NAME=pw2>"
        '<input name="pw3" TYPE="Password" value=SECRET3 autocomplete="current-password">'
        "</form>"
    )

    cleaned = diagnose.sanitise_html(html)

    assert "SECRET1" not in cleaned
    assert "SECRET2" not in cleaned
    assert "SECRET3" not in cleaned
    assert "keep" in cleaned
    assert cleaned.lower().count('value="[redacted]"') == 3
    assert 'autocomplete="current-password"' in cleaned


def test_sanitise_html_is_reexported_from_store() -> None:
    from xposter.store import sanitise_html as store_sanitise_html

    assert diagnose.sanitise_html is store_sanitise_html


def test_sanitise_html_redacts_hidden_and_csrf_values() -> None:
    html = (
        '<input type="hidden" name="csrf" value="hidden-canary">'
        '<meta name="csrf-token" content="meta-canary">'
        '{"auth_token": "json-canary"}'
    )

    cleaned = diagnose.sanitise_html(html)

    assert "hidden-canary" not in cleaned
    assert "meta-canary" not in cleaned
    assert "json-canary" not in cleaned


def test_truncate_html_caps_the_byte_budget() -> None:
    truncated = diagnose._truncate_html("x" * (diagnose.MAX_DIALOG_HTML_BYTES + 5000))

    assert len(truncated.encode("utf-8")) < diagnose.MAX_DIALOG_HTML_BYTES + 64
    assert "truncated at 200 KB" in truncated


def test_format_dom_report_highlights_missing_composer_rows() -> None:
    report = diagnose.DomReport(
        rows=(
            diagnose.DomRow("composer_root", "[role=dialog]", 1, False),
            diagnose.DomRow("composer_textarea_0", None, 0, True),
            diagnose.DomRow("toast", None, 0, True),
        ),
        artifacts=Path("/tmp/artifacts/dom"),
    )

    lines = diagnose.format_dom_report(report)
    text = "\n".join(lines)

    assert "MISSING" in text
    assert "composer_textarea_0" in text
    assert "Composer selectors missing: composer_textarea_0" in text
    assert "composer selector missing" in text
    assert "Artifacts: /tmp/artifacts/dom" in text


def test_format_dom_report_reports_all_composer_selectors_matched() -> None:
    report = diagnose.DomReport(
        rows=(diagnose.DomRow("composer_root", "[role=dialog]", 1, False),),
        artifacts=Path("/tmp/artifacts/dom"),
    )

    text = "\n".join(diagnose.format_dom_report(report))

    assert "All composer selectors matched" in text


def _skip_without_chrome(config: XPosterConfig) -> None:
    from xposter.browser import find_chrome

    if find_chrome(config) is None:
        pytest.skip("Google Chrome is not installed")


def _delayed_compose_hook(fake: Any, delay_ms: int = 1500) -> Any:
    """Serve the fake composer, but keep it hidden for ``delay_ms``.

    This reproduces the live failure: ``domcontentloaded`` fires while the
    composer is not yet visible. The style is removed by a timer, so the wait
    helper must poll before measuring anything.
    """

    async def hook(context: Any) -> None:
        await fake.install(context)
        _status, html = fake._render("/compose/post")
        markup = str(html)
        injected = (
            '<style id="fx-calibration-delay">'
            '[role="dialog"], [data-testid="tweetTextarea_0"] { display: none !important; }'
            "</style>"
            "<script>setTimeout(() => { const node = document.getElementById("
            '"fx-calibration-delay"); if (node) node.remove(); }, '
            f"{delay_ms});</script>"
        )
        if "</head>" in markup:
            markup = markup.replace("</head>", injected + "</head>", 1)
        else:
            markup = injected + markup

        async def handler(route: Any) -> None:
            if route.request.resource_type != "document":
                await route.fallback()
                return
            await route.fulfill(
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=markup,
            )

        await context.route("**/compose/post", handler)

    return hook


def _blank_compose_hook(fake: Any) -> Any:
    """Serve a page that never renders a composer, for the timeout path."""

    async def hook(context: Any) -> None:
        await fake.install(context)

        async def handler(route: Any) -> None:
            await route.fulfill(
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body="<html><body><p id='loading'>Loading…</p></body></html>",
            )

        await context.route("**/compose/post", handler)

    return hook


async def _serve_compose(context: Any, markup: str) -> None:
    async def handler(route: Any) -> None:
        if route.request.resource_type != "document":
            await route.fallback()
            return
        await route.fulfill(
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=markup,
        )

    await context.route("**/compose/post", handler)


def _status_route_hook(fake: Any, path: str, *, extra_body: str = "", extra_head: str = "") -> Any:
    """Serve the fake status page with reply-calibration test markup injected."""

    async def hook(context: Any) -> None:
        await fake.install(context)
        _status, html = fake._render(path)
        markup = str(html)
        if extra_head:
            if "</head>" in markup:
                markup = markup.replace("</head>", extra_head + "</head>", 1)
            else:
                markup = extra_head + markup
        if extra_body:
            if "</body>" in markup:
                markup = markup.replace("</body>", extra_body + "</body>", 1)
            else:
                markup += extra_body

        async def handler(route: Any) -> None:
            if route.request.resource_type != "document":
                await route.fallback()
                return
            await route.fulfill(
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=markup,
            )

        await context.route(f"**{path}", handler)

    return hook


_INLINE_REPLY_SCRIPT = """
<script>
(() => {
  const inline = (el) => {
    if (!el || el.dataset.fxInline === '1') return;
    el.dataset.fxInline = '1';
    el.removeAttribute('role');
    el.removeAttribute('aria-modal');
    el.removeAttribute('data-fx-composer');
    el.id = 'fx-inline-reply';
  };
  new MutationObserver(() => {
    document
      .querySelectorAll('#fx-reply-slot [role="dialog"]')
      .forEach(inline);
  }).observe(document.body, { childList: true, subtree: true });
})();
</script>
"""

_DECOY_REPLY_SCRIPT = """
<script>
(() => {
  const cfg = window.__FAKE_X__ || {};
  const handle = cfg.handle || '';
  const main = document.querySelector('main');
  if (!main) return;
  const decoy = document.createElement('article');
  decoy.setAttribute('data-testid', 'tweet');
  decoy.innerHTML =
    '<a href="/' + handle + '/status/999">decoy</a>' +
    '<button type="button" data-testid="reply" aria-label="Reply">Reply</button>';
  main.insertBefore(decoy, main.firstChild);
  const focal = document.querySelector('article[data-fx-tweet]');
  if (focal && !focal.querySelector('a[href$="/status/111"]')) {
    const link = document.createElement('a');
    link.href = '/' + handle + '/status/111';
    link.textContent = 'permalink';
    focal.appendChild(link);
  }
})();
</script>
"""


def _reply_fixture(fake: Any) -> str:
    fake.tweets.append(
        FakeTweet(
            id="111",
            text="focal tweet for reply calibration",
            author=fake.handle,
            in_reply_to=None,
            media=(),
        )
    )
    return f"/{fake.handle}/status/111"


def _compose_route_hook(fake: Any, *, extra_head: str = "", extra_body: str = "") -> Any:
    """Serve the fake composer with extra test-only markup injected."""

    async def hook(context: Any) -> None:
        await fake.install(context)
        _status, html = fake._render("/compose/post")
        markup = str(html)
        if extra_head:
            if "</head>" in markup:
                markup = markup.replace("</head>", extra_head + "</head>", 1)
            else:
                markup = extra_head + markup
        if extra_body:
            if "</body>" in markup:
                markup = markup.replace("</body>", extra_body + "</body>", 1)
            else:
                markup += extra_body
        await _serve_compose(context, markup)

    return hook


_CLOSE_NOOP_SCRIPT = """
<script>
document.addEventListener('click', (event) => {
  const close = event.target.closest(
    '[data-testid="app-bar-close"], button[aria-label="Close"]'
  );
  if (!close) return;
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);
</script>
"""


def _close_noop_hook(fake: Any) -> Any:
    """Make the Close button dispatch but do nothing, for the retry path."""
    return _compose_route_hook(fake, extra_head=_CLOSE_NOOP_SCRIPT)


def _discard_sheet_hook(fake: Any) -> tuple[Any, dict[str, bool]]:
    """Show a fake save sheet on Close and record which button was clicked."""
    events: dict[str, bool] = {}

    async def record(source: Any, payload: Any) -> None:
        events[str(payload)] = True

    script = """
<script>
document.addEventListener('click', (event) => {
  const close = event.target.closest(
    '[data-testid="app-bar-close"], button[aria-label="Close"]'
  );
  if (!close) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  if (document.getElementById('fake-sheet')) return;
  const sheet = document.createElement('div');
  sheet.id = 'fake-sheet';
  sheet.setAttribute('role', 'dialog');
  sheet.setAttribute('data-testid', 'sheetDialog');
  sheet.setAttribute('aria-label', 'Save post?');
  sheet.innerHTML =
    '<h2>Save post?</h2><p>You can save this to send later from your drafts.</p>' +
    '<input type="password" value="after-close-canary">' +
    '<button type="button" id="fake-discard">Discard</button>' +
    '<button type="button" id="fake-save" data-testid="confirmationSheetConfirm">Save</button>';
  const closeComposer = () => {
    const composer = document.querySelector('[data-fx-composer]');
    if (composer) composer.remove();
    sheet.remove();
  };
  sheet.querySelector('#fake-discard').addEventListener('click', (e) => {
    e.stopPropagation();
    window.xposterRecord('discard');
    closeComposer();
  });
  sheet.querySelector('#fake-save').addEventListener('click', () => {
    window.xposterRecord('save');
    closeComposer();
  });
  const modal = document.querySelector('[role="dialog"][aria-modal="true"]');
  (modal || document.body).appendChild(sheet);
}, true);
</script>
"""

    async def hook(context: Any) -> None:
        await fake.install(context)
        await context.expose_binding("xposterRecord", record)
        _status, html = fake._render("/compose/post")
        markup = str(html)
        if "</head>" in markup:
            markup = markup.replace("</head>", script + "</head>", 1)
        else:
            markup = script + markup
        await _serve_compose(context, markup)

    return hook, events


_DRAFTS_UI_SCRIPT = """
<script>
(() => {
  const modal = document.querySelector('[role="dialog"][aria-modal="true"]');
  const main = document.querySelector('main');
  if (main) {
    main.insertAdjacentHTML(
      'beforeend',
      '<article data-testid="tweet"><p>background timeline post one</p></article>' +
      '<div data-testid="cellInnerDiv"><p>background timeline cell two</p></div>'
    );
  }
  if (!modal) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.setAttribute('data-testid', 'unsentButton');
  button.textContent = 'Drafts';
  modal.appendChild(button);
  button.addEventListener('click', () => {
    const sheet = document.createElement('div');
    sheet.id = 'fake-drafts';
    sheet.setAttribute('role', 'dialog');
    sheet.setAttribute('aria-label', 'Drafts');
    sheet.innerHTML =
      '<h2>Drafts</h2>' +
      '<div data-testid="draft">xposter calibration two</div>' +
      '<div data-testid="draft">older report draft</div>';
    modal.appendChild(sheet);
  });
})();
</script>
"""


def _drafts_ui_hook(fake: Any) -> Any:
    return _compose_route_hook(fake, extra_body=_DRAFTS_UI_SCRIPT)


_DRAFTS_UNDER_MASK_SCRIPT = """
<script>
(() => {
  const button = document.createElement('button');
  button.type = 'button';
  button.setAttribute('data-testid', 'unsentButton');
  button.textContent = 'Drafts';
  button.style.position = 'fixed';
  button.style.left = '8px';
  button.style.top = '8px';
  button.style.zIndex = '29';
  document.body.appendChild(button);
})();
</script>
"""


def _drafts_under_mask_hook(fake: Any) -> Any:
    return _compose_route_hook(fake, extra_body=_DRAFTS_UNDER_MASK_SCRIPT)


_DRAFTS_NO_PANEL_SCRIPT = """
<script>
(() => {
  const main = document.querySelector('main');
  if (main) {
    main.insertAdjacentHTML(
      'beforeend',
      '<article data-testid="tweet"><p>background timeline post one</p></article>' +
      '<div data-testid="cellInnerDiv"><p>background timeline cell two</p></div>'
    );
  }
  const modal = document.querySelector('[role="dialog"][aria-modal="true"]');
  if (!modal) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.setAttribute('data-testid', 'unsentButton');
  button.textContent = 'Drafts';
  modal.appendChild(button);
})();
</script>
"""


def _drafts_no_panel_hook(fake: Any) -> Any:
    return _compose_route_hook(fake, extra_body=_DRAFTS_NO_PANEL_SCRIPT)


_NO_MEDIA_INPUT_SCRIPT = """
<script>
(() => {
  const modal = document.querySelector('[role="dialog"][aria-modal="true"]');
  if (!modal) return;
  modal.querySelectorAll('input[type="file"]').forEach((node) => node.remove());
})();
</script>
"""


def _no_media_input_hook(fake: Any) -> Any:
    return _compose_route_hook(fake, extra_body=_NO_MEDIA_INPUT_SCRIPT)


_NO_PREVIEW_SCRIPT = """
<style>
[role="dialog"][aria-modal="true"] [data-testid="attachments"] {
  display: none !important;
}
</style>
"""


def _no_preview_hook(fake: Any) -> Any:
    return _compose_route_hook(fake, extra_head=_NO_PREVIEW_SCRIPT)


_FOOTER_PROGRESS_SCRIPT = """
<script>
(() => {
  const modal = document.querySelector('[role="dialog"][aria-modal="true"]');
  if (!modal) return;
  const progress = document.createElement('div');
  progress.setAttribute('role', 'progressbar');
  progress.id = 'footer-progress';
  progress.textContent = 'counter';
  (modal.querySelector('.fx-actions') || modal).appendChild(progress);
})();
</script>
"""


def _footer_progress_hook(fake: Any) -> Any:
    return _compose_route_hook(fake, extra_body=_FOOTER_PROGRESS_SCRIPT)


_ATTACHMENT_PROGRESS_SCRIPT = """
<script>
(() => {
  const observer = new MutationObserver(() => {
    document
      .querySelectorAll('[role="dialog"][aria-modal="true"] [data-testid="attachments"] img')
      .forEach((img) => {
        const parent = img.parentElement;
        if (!parent || parent.querySelector('#stuck-attachment-progress')) return;
        const progress = document.createElement('div');
        progress.setAttribute('role', 'progressbar');
        progress.id = 'stuck-attachment-progress';
        progress.textContent = 'uploading';
        parent.appendChild(progress);
      });
  });
  observer.observe(document.body, { childList: true, subtree: true });
})();
</script>
"""


def _attachment_progress_hook(fake: Any) -> Any:
    return _compose_route_hook(fake, extra_body=_ATTACHMENT_PROGRESS_SCRIPT)


_EXTRA_MEDIA_CONTROLS_SCRIPT = """
<script>
(() => {
  const openOverlay = (heading) => {
    const overlay = document.createElement('div');
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.innerHTML =
      '<h2>' + heading + '</h2>' +
      '<textarea aria-label="' + heading + '"></textarea>' +
      '<button type="button">Back</button>';
    document.body.appendChild(overlay);
    overlay.querySelector('button').addEventListener('click', () => overlay.remove());
  };
  const observer = new MutationObserver(() => {
    document
      .querySelectorAll('[role="dialog"][aria-modal="true"] [data-testid="attachments"] img')
      .forEach((img) => {
        const parent = img.parentElement;
        if (!parent || parent.querySelector('#extra-add-description')) return;
        const add = document.createElement('a');
        add.id = 'extra-add-description';
        add.href = '#';
        add.tabIndex = 0;
        add.setAttribute('role', 'button');
        add.textContent = 'Add description';
        add.addEventListener('click', (event) => {
          event.preventDefault();
          openOverlay('Add description');
        });
        parent.appendChild(add);
        const tag = document.createElement('a');
        tag.href = '#';
        tag.textContent = 'Tag people';
        parent.appendChild(tag);
        const edit = document.createElement('button');
        edit.type = 'button';
        edit.setAttribute('aria-label', 'Edit media');
        edit.textContent = 'Edit';
        edit.addEventListener('click', () => openOverlay('Edit media'));
        parent.appendChild(edit);
      });
  });
  observer.observe(document.body, { childList: true, subtree: true });
})();
</script>
"""


def _extra_media_controls_hook(fake: Any) -> Any:
    return _compose_route_hook(fake, extra_body=_EXTRA_MEDIA_CONTROLS_SCRIPT)


_ALT_INTRO_SCRIPT = """
<script>
(() => {
  const wire = (editor) => {
    if (!editor || editor.dataset.fxIntroWrapped === '1') return;
    if (!(editor.textContent || '').includes('Image description')) return;
    editor.dataset.fxIntroWrapped = '1';

    const back = document.createElement('button');
    back.type = 'button';
    back.setAttribute('aria-label', 'Back');
    back.textContent = '\\u2190';
    back.addEventListener('click', () => editor.remove());
    editor.insertBefore(back, editor.firstChild);

    const sheet = document.createElement('div');
    sheet.setAttribute('data-testid', 'sheetDialog');
    sheet.style.cssText =
      'position:fixed;z-index:60;top:20%;left:25%;right:25%;background:#fff;' +
      'border:1px solid #cfd9de;border-radius:16px;padding:16px';
    sheet.innerHTML =
      '<h2>Add descriptions</h2>' +
      '<button type="button" data-testid="app-bar-close" aria-label="Close">X</button>' +
      '<button type="button" data-testid="altTextEducationSheetPrimaryAction">Sure</button>' +
      '<button type="button">No, thanks</button>';
    document.body.appendChild(sheet);
    const decline = sheet.querySelectorAll('button')[2];
    decline.addEventListener('click', () => sheet.remove());
  };
  const observer = new MutationObserver(() => {
    document
      .querySelectorAll('[role="dialog"][aria-modal="true"]')
      .forEach((editor) => wire(editor));
  });
  observer.observe(document.body, { childList: true, subtree: true });
})();
</script>
"""


def _alt_intro_hook(fake: Any) -> Any:
    return _compose_route_hook(fake, extra_body=_ALT_INTRO_SCRIPT)


def test_assert_safe_click_refuses_submit_and_save_controls() -> None:
    with pytest.raises(diagnose.UnsafeClickError):
        diagnose.assert_safe_click(text="Post", aria_label="Post", data_testid="tweetButton")
    with pytest.raises(diagnose.UnsafeClickError):
        diagnose.assert_safe_click(text="Post all")
    with pytest.raises(diagnose.UnsafeClickError):
        diagnose.assert_safe_click(
            text="Save", aria_label="Save", data_testid="confirmationSheetConfirm"
        )
    with pytest.raises(diagnose.UnsafeClickError):
        diagnose.assert_safe_click(text="Schedule post")
    with pytest.raises(diagnose.UnsafeClickError):
        diagnose.assert_safe_click(text="Delete")
    with pytest.raises(diagnose.UnsafeClickError):
        diagnose.assert_safe_click(text="Edit")

    diagnose.assert_safe_click(text="Add another post", data_testid="addButton")
    diagnose.assert_safe_click(text="", aria_label="Add another post")
    diagnose.assert_safe_click(text="Discard", data_testid="confirmationSheetCancel")
    diagnose.assert_safe_click(text="", aria_label="Close", data_testid="app-bar-close")


def test_assert_safe_click_allows_media_controls_only_in_phase_d() -> None:
    with pytest.raises(diagnose.UnsafeClickError):
        diagnose.assert_safe_click(text="", aria_label="Remove media")
    with pytest.raises(diagnose.UnsafeClickError):
        diagnose.assert_safe_click(text="Add description")

    diagnose.assert_safe_click(text="", aria_label="Remove media", phase="D")
    diagnose.assert_safe_click(text="Add description", phase="D")
    diagnose.assert_safe_click(text="", aria_label="Edit media", phase="D")
    diagnose.assert_safe_click(text="No, thanks", phase="D")
    diagnose.assert_safe_click(text="", aria_label="Back")

    with pytest.raises(diagnose.UnsafeClickError):
        diagnose.assert_safe_click(text="Add description extra", phase="D")
    with pytest.raises(diagnose.UnsafeClickError):
        diagnose.assert_safe_click(text="No, thanks")
    for label in ("Post", "Post all", "Reply", "Schedule", "Save", "Delete", "Edit"):
        with pytest.raises(diagnose.UnsafeClickError):
            diagnose.assert_safe_click(text=label, phase="D")


def test_assert_safe_click_never_accepts_the_intro_sure_button() -> None:
    for kwargs in (
        {"text": "Sure", "phase": "D"},
        {"text": "Sure"},
        {"data_testid": "altTextEducationSheetPrimaryAction", "phase": "D"},
        {
            "text": "Sure",
            "data_testid": "altTextEducationSheetPrimaryAction",
            "phase": "D",
        },
    ):
        with pytest.raises(diagnose.UnsafeClickError):
            diagnose.assert_safe_click(**kwargs)


def test_media_ready_state_ignores_footer_counter_progressbar() -> None:
    assert diagnose.media_ready_state(True, False, True) is True
    assert diagnose.media_ready_state(True, True, True) is False
    assert diagnose.media_ready_state(False, False, True) is False
    assert diagnose.media_ready_state(True, False, False) is False


def test_redact_blob_urls() -> None:
    html = (
        '<img src="blob:https://x.com/95443222-3ca2-4d98">'
        "<style>x{background:url(&quot;blob:https://x.com/95443222-3ca2-4d98&quot;)}</style>"
    )

    redacted = diagnose._redact_blob_urls(html)

    assert redacted.count("blob:REDACTED") == 2
    assert "blob:http" not in redacted
    assert "&quot;blob:REDACTED&quot;" in redacted


def test_make_calibration_png_is_a_valid_small_png() -> None:
    data = diagnose.make_calibration_png()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks: list[tuple[bytes, bytes]] = []
    offset = 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        tag = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        crc = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])[0]
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF
        chunks.append((tag, payload))
        offset += 12 + length
    tags = [tag for tag, _payload in chunks]
    assert tags[0] == b"IHDR"
    assert tags[-1] == b"IEND"
    width, height, depth, color_type, _compression, _filter, _interlace = struct.unpack(
        ">IIBBBBB", chunks[0][1]
    )
    assert (width, height, depth, color_type) == (320, 180, 8, 2)
    raw = zlib.decompress(b"".join(payload for tag, payload in chunks if tag == b"IDAT"))
    assert len(raw) == height * (1 + width * 3)
    assert len(data) < 20_000


@pytest.mark.browser
async def test_dom_report_waits_for_legacy_composer_and_measures_three_phases(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.dom_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=_delayed_compose_hook(fake),
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=0,
    )

    rows = {row.name: row for row in report.rows}
    assert report.rendered is True
    assert report.waited_s >= 1.0
    assert len(rows) == len(SELECTORS)
    assert rows["composer_root"].phase_counts[0] >= 1
    assert rows["composer_textarea_0"].phase_counts[0] == 1
    assert "[aria-modal=" in (rows["composer_textarea_0"].candidate or "")
    assert rows["composer_textarea_n"].phase_counts[2] >= 2
    assert any(button.data_testid == "tweetButton" for button in report.dialog_buttons)
    assert any(
        button.data_testid == "tweetButton" and not button.disabled
        for button in report.dialog_buttons
    )
    assert any(button.data_testid == "addButton" for button in report.dialog_buttons)
    assert report.discard_sheet
    assert any(button.text.strip().lower() == "discard" for button in report.discard_sheet)
    assert any(button.data_testid == "confirmationSheetCancel" for button in report.discard_sheet)
    assert report.discard_action == "discard"
    assert report.dialog_closed is True
    assert len(report.screenshots) == 3
    assert report.dialog_html is not None and report.dialog_html.exists()
    assert report.report_json is not None and report.report_json.exists()
    assert report.screenshot is not None
    assert report.screenshot.stat().st_mode & 0o777 == 0o600
    assert report.dialog_html.stat().st_mode & 0o777 == 0o600
    assert report.report_json.stat().st_mode & 0o777 == 0o600

    payload = json.loads(report.report_json.read_text(encoding="utf-8"))
    assert payload["rendered"] is True
    assert payload["dialog_buttons"]
    assert payload["discard_sheet"]
    assert payload["dialog_closed"] is True
    assert len(payload["rows"]) == len(SELECTORS)
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_dom_report_measures_the_new_client_without_posting(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="new")

    report = await diagnose.dom_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=fake.install,
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=1000,
    )

    rows = {row.name: row for row in report.rows}
    assert report.rendered is True
    assert rows["composer_textarea_0"].phase_counts[0] >= 1
    assert rows["composer_textarea_0"].phase_counts[2] >= 2
    assert any("Post" in button.text for button in report.dialog_buttons)
    assert any("Post" in button.text and not button.disabled for button in report.dialog_buttons)
    assert report.discard_sheet
    assert report.dialog_closed is True
    assert report.discard_action == "discard"
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_dom_report_never_rendered_is_reported_as_inconclusive(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.dom_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=_blank_compose_hook(fake),
        display=Display.HEADLESS,
        wait_timeout_s=0.3,
        wait_interval_s=0.05,
        networkidle_timeout_ms=0,
    )

    text = "\n".join(diagnose.format_dom_report(report))
    assert report.rendered is False
    assert "composer never rendered within" in text.lower()
    assert "not evidence that the selectors are missing" in text.lower()
    assert "Composer selectors missing" not in text
    assert all(not row.missing for row in report.rows)
    assert report.report_json is not None and report.report_json.exists()
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_dom_report_close_noop_retries_escape_and_warns(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.dom_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=_close_noop_hook(fake),
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=0,
        cleanup_timeout_s=0.3,
        cleanup_poll_s=0.05,
    )

    text = "\n".join(diagnose.format_dom_report(report))
    assert report.cleanup_ok is False
    assert report.dialog_closed is False
    assert report.close_attempts == 2
    assert report.close_path == "failed"
    assert report.close_click is not None
    assert report.close_click["dispatched"] is True
    assert report.close_click["box"]
    assert report.close_click["hit"]
    assert report.close_retry is not None and report.close_retry["dispatched"] is True
    assert "CLEANUP FAILED" in text
    assert "The composer is still open and may hold unsent text" in text
    assert "xposter doctor --drafts" in text
    assert report.after_close_screenshot is not None
    assert report.after_close_screenshot.exists()
    assert report.after_close_screenshot.stat().st_mode & 0o777 == 0o600
    assert report.after_close_html is not None and report.after_close_html.exists()
    assert report.after_close_html.stat().st_mode & 0o777 == 0o600
    payload = json.loads(report.report_json.read_text(encoding="utf-8"))
    assert payload["cleanup_ok"] is False
    assert payload["dialog_closed"] is False
    assert payload["close_attempts"] == 2
    assert payload["close_click"]["dispatched"] is True
    assert "after_close" in payload
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_dom_report_phase_d_attaches_and_removes_media(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.dom_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=fake.install,
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=0,
        cleanup_timeout_s=0.5,
        cleanup_poll_s=0.05,
        with_media=True,
        media_timeout_s=10.0,
        media_poll_s=0.05,
    )

    media = report.media
    rows = {row.name: row for row in report.rows}
    assert media is not None
    assert media.attached is True
    assert media.ready is True
    assert media.removed is True
    assert media.preview_gone is True
    assert media.note in {"", "Edit media control not found"}
    assert "/i/media/upload.json" in media.requests
    assert media.matches["composer_media_preview"]
    assert rows["composer_media_preview"].phase_counts[3] >= 1
    assert any(button.aria_label == "Remove media" for button in media.dialog_buttons)
    assert media.attachment_buttons
    assert media.attachment_html is not None and media.attachment_html.exists()
    assert media.attachment_html.stat().st_mode & 0o777 == 0o600
    assert media.screenshot is not None and media.screenshot.exists()
    assert media.screenshot.name == "phase-d.png"
    assert media.alt_editor is not None
    assert media.alt_editor.opened is True
    assert media.alt_editor.closed is True
    assert media.alt_editor.heading == "Image description"
    assert media.alt_editor.composer_image_visible is True
    assert media.alt_editor.html is not None and media.alt_editor.html.exists()
    assert media.alt_editor.screenshot is not None and media.alt_editor.screenshot.exists()
    assert media.alt_editor.inputs
    assert media.alt_intro is None
    assert media.alt_back_control is not None
    assert media.alt_back_control.found is True
    assert media.alt_back_control.clicked is True
    assert media.controls
    assert (
        next(item for item in media.controls if item["name"] == "add_description")["found"] is True
    )
    assert media.media_modal_html is not None and media.media_modal_html.exists()
    assert media.media_modal_html.stat().st_mode & 0o777 == 0o600
    modal_html = media.media_modal_html.read_text(encoding="utf-8")
    assert "blob:REDACTED" in modal_html
    assert "blob:http" not in modal_html
    payload = json.loads(report.report_json.read_text(encoding="utf-8"))
    assert payload["media"]["attached"] is True
    assert payload["media"]["requests"]
    assert payload["media"]["media_modal_html"]
    assert payload["media"]["controls"]
    assert payload["media"]["alt_intro"] is None
    assert payload["media"]["alt_back_control"]["found"] is True
    assert payload["media"]["alt_back_control"]["clicked"] is True
    assert payload["media"]["alt_closed"] is True
    assert set(payload["phases"]) == {"A", "B", "C", "D"}
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_dom_report_phase_d_footer_counter_does_not_block_ready(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.dom_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=_footer_progress_hook(fake),
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=0,
        cleanup_timeout_s=0.5,
        cleanup_poll_s=0.05,
        with_media=True,
        media_timeout_s=5.0,
        media_poll_s=0.05,
    )

    assert report.media is not None
    assert report.media.ready is True
    assert "did not settle" not in report.media.note
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_dom_report_phase_d_attachment_progress_blocks_ready(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.dom_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=_attachment_progress_hook(fake),
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=0,
        cleanup_timeout_s=0.5,
        cleanup_poll_s=0.05,
        with_media=True,
        media_timeout_s=0.5,
        media_poll_s=0.05,
    )

    assert report.media is not None
    assert report.media.ready is False
    assert "did not settle" in report.media.note
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_dom_report_phase_d_captures_add_description_and_edit_controls(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.dom_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=_extra_media_controls_hook(fake),
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=0,
        cleanup_timeout_s=0.5,
        cleanup_poll_s=0.05,
        with_media=True,
        media_timeout_s=5.0,
        media_poll_s=0.05,
    )

    media = report.media
    assert media is not None
    controls = {item["name"]: item for item in media.controls}
    assert controls["add_description"]["found"] is True
    assert controls["add_description"]["tag"] == "a"
    assert controls["add_description"]["href"] == "#"
    assert controls["tag_people"]["found"] is True
    assert controls["edit_media"]["found"] is True
    assert controls["edit_media"]["aria_label"] == "Edit media"
    assert "Add description" in str(controls["add_description"]["outer_html"])
    assert "Add description" in str(controls["add_description"]["parent_html"])

    assert media.alt_editor is not None
    assert media.alt_editor.opened is True
    assert media.alt_editor.closed is True
    assert media.alt_editor.heading == "Add description"
    assert media.alt_editor.screenshot is not None
    assert media.alt_editor.screenshot.exists()
    assert media.alt_editor.screenshot.name == "phase-d-alt.png"
    assert media.alt_intro is None
    assert media.alt_back_control is not None
    assert media.alt_back_control.strategy == "exact Back"
    assert media.alt_back_control.clicked is True

    assert media.edit_editor is not None
    assert media.edit_editor.opened is True
    assert media.edit_editor.closed is True
    assert media.edit_editor.heading in {"Image description", "Edit media"}
    assert media.edit_editor.html is not None
    assert media.edit_editor.html.exists()
    assert media.edit_editor.screenshot is not None
    assert media.edit_editor.screenshot.exists()
    assert media.edit_editor.screenshot.name == "phase-d-edit.png"
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_dom_report_phase_d_dismisses_intro_sheet_then_captures_alt_editor(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.dom_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=_alt_intro_hook(fake),
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=0,
        cleanup_timeout_s=0.5,
        cleanup_poll_s=0.05,
        with_media=True,
        media_timeout_s=10.0,
        media_poll_s=0.05,
    )

    media = report.media
    assert media is not None
    assert media.alt_intro is not None
    assert media.alt_intro.opened is True
    assert media.alt_intro.dismissed is True
    assert media.alt_intro.note == ""
    assert media.alt_intro.html is not None and media.alt_intro.html.exists()
    assert media.alt_intro.html.name == "media-alt-intro.html"
    assert media.alt_intro.html.stat().st_mode & 0o777 == 0o600
    intro_html = media.alt_intro.html.read_text(encoding="utf-8")
    assert "Add descriptions" in intro_html
    assert "No, thanks" in intro_html
    assert 'data-testid="sheetDialog"' in intro_html
    intro_buttons = {button.text for button in media.alt_intro.buttons}
    assert {"Sure", "No, thanks"} <= intro_buttons
    assert "No, thanks" in intro_buttons
    assert "Image description" not in intro_html

    assert media.alt_editor is not None
    assert media.alt_editor.opened is True
    assert media.alt_editor.closed is True
    assert media.alt_editor.heading == "Image description"
    assert media.alt_editor.composer_image_visible is True
    assert media.alt_editor.html is not None and media.alt_editor.html.exists()
    editor_html = media.alt_editor.html.read_text(encoding="utf-8")
    assert "Image description" in editor_html
    assert 'data-testid="sheetDialog"' not in editor_html

    assert media.alt_back_control is not None
    assert media.alt_back_control.found is True
    assert media.alt_back_control.strategy == "aria-label Back"
    assert media.alt_back_control.aria_label == "Back"
    assert media.alt_back_control.clicked is True
    assert media.alt_back_control.note == ""

    payload = json.loads(report.report_json.read_text(encoding="utf-8"))
    assert payload["media"]["alt_intro"]["dismissed"] is True
    assert payload["media"]["alt_intro"]["html"] == "media-alt-intro.html"
    assert payload["media"]["alt_back_control"]["strategy"] == "aria-label Back"
    assert payload["media"]["alt_closed"] is True
    assert payload["media"]["alt_editor"]["composer_image_visible"] is True

    text = "\n".join(diagnose.format_dom_report(report))
    assert "Alt intro sheet: opened=yes; dismissed=yes" in text
    assert "Alt intro HTML:" in text
    assert "Alt back control: found=yes; strategy='aria-label Back'" in text
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_dom_report_phase_d_tolerates_missing_media_input(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.dom_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=_no_media_input_hook(fake),
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=0,
        cleanup_timeout_s=0.5,
        cleanup_poll_s=0.05,
        with_media=True,
        media_timeout_s=0.3,
        media_poll_s=0.05,
    )

    assert report.media is not None
    assert report.media.attached is False
    assert "file input not found" in report.media.note
    assert report.report_json is not None
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_dom_report_phase_d_tolerates_missing_preview(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.dom_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=_no_preview_hook(fake),
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=0,
        cleanup_timeout_s=0.5,
        cleanup_poll_s=0.05,
        with_media=True,
        media_timeout_s=0.3,
        media_poll_s=0.05,
    )

    assert report.media is not None
    assert report.media.attached is True
    assert report.media.ready is False
    assert "did not settle" in report.media.note
    assert report.media.removed is False
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_dom_report_clicks_only_discard_on_save_sheet(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")
    hook, events = _discard_sheet_hook(fake)

    report = await diagnose.dom_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=hook,
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=0,
        cleanup_timeout_s=0.5,
        cleanup_poll_s=0.05,
    )

    assert report.cleanup_ok is True
    assert report.dialog_closed is True
    assert report.discard_action == "discard"
    assert events == {"discard": True}
    assert report.discard_sheet
    assert any(button.text == "Discard" for button in report.discard_sheet)
    assert any(button.text == "Save" for button in report.discard_sheet)
    assert report.after_close
    assert any("Save post?" in str(item.get("text", "")) for item in report.after_close)
    assert report.after_close_screenshot is not None
    assert report.after_close_screenshot.exists()
    assert report.after_close_html is not None
    assert report.after_close_html.exists()
    after_close_html = report.after_close_html.read_text(encoding="utf-8")
    assert "after-close-canary" not in after_close_html
    assert report.after_close_html.stat().st_mode & 0o777 == 0o600
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_drafts_report_lists_leftovers_without_modifying(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.drafts_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=_drafts_ui_hook(fake),
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=0,
        drafts_wait_s=2.0,
        drafts_poll_s=0.05,
    )

    text = "\n".join(diagnose.format_drafts_report(report))
    assert report.rendered is True
    assert report.drafts_control_found is True
    assert report.panel_opened is True
    assert "xposter calibration two" in report.drafts
    assert "older report draft" in report.drafts
    assert all("background timeline" not in draft for draft in report.drafts)
    assert "Drafts found: 2" in text
    assert "LEFTOVER FROM CALIBRATION" in text
    assert "Click Edit" in text
    assert "Delete" in text
    assert report.screenshot is not None and report.screenshot.exists()
    assert report.dialog_html is not None and report.dialog_html.exists()
    assert report.report_json is not None and report.report_json.exists()
    payload = json.loads(report.report_json.read_text(encoding="utf-8"))
    assert payload["drafts"] == report.drafts
    assert payload["panel_opened"] is True
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_drafts_report_does_not_use_a_control_under_the_mask(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.drafts_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=_drafts_under_mask_hook(fake),
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=0,
    )

    text = "\n".join(diagnose.format_drafts_report(report))
    assert report.rendered is True
    assert report.drafts_control_found is False
    assert report.panel_opened is False
    assert report.drafts == []
    assert "Drafts control not found" in text
    assert "not clickable" in text
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_drafts_report_never_reads_background_timeline_articles(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.drafts_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=_drafts_no_panel_hook(fake),
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=0,
        drafts_wait_s=0.3,
        drafts_poll_s=0.05,
    )

    text = "\n".join(diagnose.format_drafts_report(report))
    assert report.rendered is True
    assert report.drafts_control_found is True
    assert report.panel_opened is False
    assert report.drafts == []
    assert "Drafts panel did not open" in text
    assert "background timeline" not in text
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_drafts_report_tolerates_missing_control(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.drafts_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=fake.install,
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        networkidle_timeout_ms=0,
    )

    text = "\n".join(diagnose.format_drafts_report(report))
    assert report.rendered is True
    assert report.drafts_control_found is False
    assert report.drafts == []
    assert "Drafts control not found" in text
    assert report.screenshot is not None and report.screenshot.exists()
    assert fake.tweets == []
    assert fake.create_requests == []


@pytest.mark.browser
async def test_drafts_report_never_rendered(tmp_home: XPosterConfig, fake_x_factory: Any) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")

    report = await diagnose.drafts_report(
        tmp_home,
        SessionStore(tmp_home),
        context_hook=_blank_compose_hook(fake),
        display=Display.HEADLESS,
        wait_timeout_s=0.3,
        wait_interval_s=0.05,
        networkidle_timeout_ms=0,
    )

    text = "\n".join(diagnose.format_drafts_report(report))
    assert report.rendered is False
    assert "composer never rendered" in text.lower()
    assert report.screenshot is not None and report.screenshot.exists()
    assert fake.tweets == []
    assert fake.create_requests == []


def test_format_drafts_report_flags_leftovers_and_manual_steps() -> None:
    report = diagnose.DraftsReport(
        rendered=True,
        drafts=["xposter calibration two", "older report draft"],
        artifacts=Path("/tmp/artifacts/drafts"),
        panel_opened=True,
    )

    text = "\n".join(diagnose.format_drafts_report(report))

    assert "Drafts found: 2" in text
    assert "LEFTOVER FROM CALIBRATION" in text
    assert "Open the composer and click Drafts" in text
    assert "Click Edit" in text
    assert "Delete" in text
    assert "Artifacts: /tmp/artifacts/drafts" in text


def test_format_dom_report_warns_when_cleanup_failed() -> None:
    report = diagnose.DomReport(
        rows=(),
        artifacts=Path("/tmp/artifacts/dom"),
        cleanup_ok=False,
        dialog_closed=False,
        close_attempts=2,
        close_path="failed",
        close_click={
            "candidate": '[data-testid="app-bar-close"]',
            "dispatched": True,
            "box": {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0},
            "hit": {"tag": "button", "data_testid": "app-bar-close"},
            "error": "",
        },
        after_close=(
            {
                "tag": "div",
                "role": "dialog",
                "data_testid": "sheetDialog",
                "aria_label": "Save post?",
                "text": "Save post?",
                "buttons": [
                    {
                        "text": "Discard",
                        "aria_label": "",
                        "data_testid": "confirmationSheetCancel",
                        "disabled": False,
                    }
                ],
            },
        ),
    )

    text = "\n".join(diagnose.format_dom_report(report))

    assert "CLEANUP FAILED" in text
    assert "The composer is still open" in text
    assert "xposter doctor --drafts" in text
    assert "After Close overlays (1)" in text
    assert "Save post?" in text
    assert "Close click:" in text
    assert "dispatched=True" in text
    assert "dialog closed: NO" in text


async def test_tells_report_uses_the_live_page(
    config: XPosterConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = {
        "webdriver": True,
        "userAgent": "Mozilla/5.0 HeadlessChrome/156.0.0.0",
        "headlessToken": True,
        "uaDataBrands": ["Chromium/156"],
        "plugins": 0,
        "languages": ["en-US"],
        "platform": "MacIntel",
        "hardwareConcurrency": 8,
        "chromeObject": True,
        "outerWidth": 1440,
        "outerHeight": 900,
        "innerWidth": 1440,
        "innerHeight": 900,
        "screenWidth": 1440,
        "screenHeight": 900,
        "devicePixelRatio": 1,
        "timezone": "Asia/Jerusalem",
        "timezoneOffsetMinutes": -120,
        "notificationPermission": "default",
        "webglRenderer": "ANGLE (Google, Vulkan)",
        "webglSoftware": False,
        "visibilityState": "visible",
        "hasFocus": True,
    }

    class FakeTellsPage:
        async def evaluate(self, script: str) -> dict[str, Any]:
            return raw

    @asynccontextmanager
    async def fake_authed(item: XPosterConfig, store: SessionStore, **kw: Any) -> Any:
        yield SimpleNamespace(page=FakeTellsPage(), handle="olsa", display=Display.HEADLESS)

    monkeypatch.setattr(diagnose, "authenticated_page", fake_authed)

    tells = await diagnose.tells_report(config, SessionStore(config))
    by_name = {tell.name: tell for tell in tells}

    assert by_name["navigator.webdriver"].risk == "warn"
    assert by_name["navigator.webdriver"].value == "true"
    assert by_name["userAgent"].risk == "warn"


async def test_tells_report_passes_live_defaults(
    config: XPosterConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    class FakeTellsPage:
        async def evaluate(self, script: str) -> dict[str, Any]:
            return {}

    @asynccontextmanager
    async def fake_authed(item: XPosterConfig, store: SessionStore, **kw: Any) -> Any:
        seen.update(kw)
        yield SimpleNamespace(page=FakeTellsPage(), handle="", display=Display.HEADLESS)

    monkeypatch.setattr(diagnose, "authenticated_page", fake_authed)

    await diagnose.tells_report(config, SessionStore(config))

    assert seen["check_login"] is False
    assert seen["goto"] == "/home"


def test_parse_status_target_builds_an_own_account_url_from_a_bare_id(
    tmp_home: XPosterConfig,
) -> None:
    config = tmp_home.with_overrides(handle="olsa")

    target = diagnose.parse_status_target("123", config)

    assert target.tweet_id == "123"
    assert target.handle == "olsa"
    assert target.url == "https://x.com/olsa/status/123"
    assert target.path == "/olsa/status/123"


def test_parse_status_target_accepts_the_configured_handle_case_insensitively(
    tmp_home: XPosterConfig,
) -> None:
    config = tmp_home.with_overrides(handle="olsa")

    target = diagnose.parse_status_target("https://x.com/OLSA/status/123?ref=timeline", config)

    assert target.tweet_id == "123"
    assert target.path == "/olsa/status/123"


def test_parse_status_target_refuses_another_handle(tmp_home: XPosterConfig) -> None:
    config = tmp_home.with_overrides(handle="olsa")

    with pytest.raises(ConfigurationError, match="only types under your own tweet"):
        diagnose.parse_status_target("https://x.com/intruder/status/123", config)


def test_parse_status_target_requires_a_configured_handle(tmp_home: XPosterConfig) -> None:
    with pytest.raises(ConfigurationError, match="set --handle"):
        diagnose.parse_status_target("123", tmp_home)


def test_parse_status_target_rejects_non_status_urls(tmp_home: XPosterConfig) -> None:
    config = tmp_home.with_overrides(handle="olsa")

    for value in (
        "https://twitter.com/olsa/status/123",
        "https://x.com/olsa/status/not-a-number",
        "not-a-url",
        "",
    ):
        with pytest.raises(ConfigurationError):
            diagnose.parse_status_target(value, config)


async def test_reply_report_refuses_another_handle_before_the_browser(
    tmp_home: XPosterConfig,
) -> None:
    config = tmp_home.with_overrides(handle="olsa")
    opened: list[object] = []

    async def hook(context: Any) -> None:
        opened.append(context)

    with pytest.raises(ConfigurationError, match="belongs to @intruder"):
        await diagnose.reply_report(
            config,
            SessionStore(config),
            "https://x.com/intruder/status/123",
            context_hook=hook,
        )

    assert opened == []


def test_assert_safe_click_phase_r_allows_only_the_focal_reply_icon() -> None:
    diagnose.assert_safe_click(
        text="Reply",
        aria_label="Reply",
        data_testid="reply",
        phase="R",
        in_focal_article=True,
    )
    diagnose.assert_safe_click(text="", aria_label="Reply", phase="R", in_focal_article=True)

    refused = (
        {"text": "Reply", "aria_label": "Reply", "data_testid": "reply", "phase": "R"},
        {"text": "Reply", "aria_label": "Reply", "data_testid": "reply"},
        {"text": "Reply", "phase": "R", "in_focal_article": True},
        {
            "text": "Reply",
            "data_testid": "tweetButtonInline",
            "phase": "R",
            "in_focal_article": True,
        },
        {
            "text": "Reply",
            "data_testid": "tweetButton",
            "phase": "R",
            "in_focal_article": True,
        },
        {
            "text": "Post",
            "data_testid": "reply",
            "phase": "R",
            "in_focal_article": True,
        },
        {
            "text": "Schedule",
            "data_testid": "reply",
            "phase": "R",
            "in_focal_article": True,
        },
        {
            "text": "Save",
            "data_testid": "reply",
            "phase": "R",
            "in_focal_article": True,
        },
        {
            "text": "Delete",
            "data_testid": "reply",
            "phase": "R",
            "in_focal_article": True,
        },
        {
            "text": "Edit",
            "data_testid": "reply",
            "phase": "R",
            "in_focal_article": True,
        },
    )
    for kwargs in refused:
        with pytest.raises(diagnose.UnsafeClickError):
            diagnose.assert_safe_click(**kwargs)


def test_format_reply_report_warns_when_cleanup_failed() -> None:
    target = diagnose.StatusTarget(
        tweet_id="111",
        handle="olsa",
        url="https://x.com/olsa/status/111",
        path="/olsa/status/111",
    )
    report = diagnose.ReplyReport(
        target=target,
        artifacts=Path("/tmp/artifacts/reply"),
        rows=(diagnose.DomRow("composer_root", "[role=dialog]", 1, False, phase_counts=(0, 1)),),
        cleanup_ok=False,
        dialog_closed=False,
        close_path="failed",
    )

    text = "\n".join(diagnose.format_reply_report(report))

    assert "REPLY CLEANUP FAILED" in text
    assert "The composer is still open and may hold unsent text" in text
    assert "Next: compare reply-composer.html" in text


@pytest.mark.browser
async def test_reply_report_modal_composer_captures_types_and_discards(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")
    path = _reply_fixture(fake)
    config = tmp_home.with_overrides(handle=fake.handle)

    report = await diagnose.reply_report(
        config,
        SessionStore(config),
        "111",
        context_hook=_status_route_hook(fake, path, extra_body=_DECOY_REPLY_SCRIPT),
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        composer_timeout_s=5.0,
        composer_poll_s=0.05,
        networkidle_timeout_ms=0,
        cleanup_timeout_s=1.0,
        cleanup_poll_s=0.05,
    )

    rows = {row.name: row for row in report.rows}
    assert report.rendered is True
    assert report.composer_kind == "modal"
    assert report.composer_found is True
    assert report.note == ""
    assert rows["tweet_article"].phase_counts[0] == 2  # decoy + focal
    assert rows["composer_root"].phase_counts == (0, 1)
    assert rows["composer_textarea_0"].phase_counts == (0, 1)
    assert rows["composer_post_button"].phase_counts == (0, 1)
    assert report.submit_control is not None
    assert report.submit_control["data_testid"] == "tweetButtonInline"
    assert report.submit_control["disabled"] is True
    assert report.submit_enabled_after_typing is True
    assert report.draftjs is True
    assert any(button.data_testid == "tweetButtonInline" for button in report.composer_buttons)
    assert any(
        button.data_testid == "tweetButtonInline" and not button.disabled
        for button in report.composer_buttons_after
    )

    assert report.composer_html is not None and report.composer_html.exists()
    assert report.composer_html.name == "reply-composer.html"
    assert report.composer_html.stat().st_mode & 0o777 == 0o600
    assert "tweetTextarea_0" in report.composer_html.read_text(encoding="utf-8")
    assert report.page_region_html is not None and report.page_region_html.exists()
    assert report.page_region_html.name == "reply-page-region.html"
    assert report.page_region_html.stat().st_mode & 0o777 == 0o600
    assert "focal tweet" in report.page_region_html.read_text(encoding="utf-8")

    names = [path.name for path in report.screenshots]
    assert names == ["reply-a.png", "reply-b.png", "reply-c.png"]
    for screenshot in report.screenshots:
        assert screenshot.exists()
        assert screenshot.stat().st_mode & 0o777 == 0o600

    assert report.cleanup_ok is True
    assert report.dialog_closed is True
    assert report.close_path == "close"
    assert report.discard_action == "discard"
    assert any(button.text == "Discard" for button in report.discard_sheet)
    assert report.inline_cleared is None

    payload = json.loads(report.report_json.read_text(encoding="utf-8"))
    assert report.report_json.stat().st_mode & 0o777 == 0o600
    assert payload["phases"] == ["R", "R2"]
    assert payload["composer_kind"] == "modal"
    assert payload["composer_found"] is True
    assert payload["submit_enabled_after_typing"] is True
    assert payload["draftjs"] is True
    assert payload["cleanup_ok"] is True
    assert payload["dialog_closed"] is True
    assert payload["draft_saved"] is False
    assert payload["inline_cleared"] is None
    assert payload["next_step"] == diagnose._REPLY_NEXT_STEP

    text = "\n".join(diagnose.format_reply_report(report))
    assert "composer=modal" in text
    assert "R2" in text
    assert "Reply composer HTML:" in text
    assert "Artifacts:" in text
    assert "Next: compare reply-composer.html" in text

    assert len(fake.tweets) == 1
    assert fake.tweets[0].id == "111"
    assert fake.create_requests == []


@pytest.mark.browser
async def test_reply_report_inline_composer_clears_text_and_navigates_away(
    tmp_home: XPosterConfig, fake_x_factory: Any
) -> None:
    _skip_without_chrome(tmp_home)
    fake = fake_x_factory(dom="legacy")
    path = _reply_fixture(fake)
    config = tmp_home.with_overrides(handle=fake.handle)

    report = await diagnose.reply_report(
        config,
        SessionStore(config),
        "111",
        context_hook=_status_route_hook(fake, path, extra_body=_INLINE_REPLY_SCRIPT),
        display=Display.HEADLESS,
        wait_timeout_s=10.0,
        wait_interval_s=0.1,
        composer_timeout_s=5.0,
        composer_poll_s=0.05,
        networkidle_timeout_ms=0,
        cleanup_timeout_s=1.0,
        cleanup_poll_s=0.05,
    )

    assert report.rendered is True
    assert report.composer_kind == "inline"
    assert report.composer_found is True
    assert report.note == ""
    assert report.submit_control is not None
    assert report.submit_control["data_testid"] == "tweetButtonInline"
    assert report.submit_enabled_after_typing is True
    assert report.draftjs is True
    assert report.inline_cleared is True
    assert report.cleanup_ok is True
    assert report.dialog_closed is True
    assert report.close_path == "inline-clear"
    assert report.discard_action == "cleared-and-navigated"

    assert report.composer_html is not None
    assert "fx-inline-reply" in report.composer_html.read_text(encoding="utf-8")
    assert [path.name for path in report.screenshots] == [
        "reply-a.png",
        "reply-b.png",
        "reply-c.png",
    ]

    payload = json.loads(report.report_json.read_text(encoding="utf-8"))
    assert payload["composer_kind"] == "inline"
    assert payload["inline_cleared"] is True
    assert payload["cleanup_ok"] is True
    assert payload["dialog_closed"] is True
    assert payload["draft_saved"] is False

    assert len(fake.tweets) == 1
    assert fake.create_requests == []
