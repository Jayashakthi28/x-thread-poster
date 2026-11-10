"""Tests for the session store: permissions, atomic writes, locking and summaries."""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from xposter import ParkedError, SessionBusyError, SessionSummary, XPosterConfig
from xposter.store import ParkInfo, SessionStore, sanitise_html

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)

_CHILD_LOCK_SCRIPT = """
import sys
from pathlib import Path

from xposter import XPosterConfig
from xposter.store import SessionStore

store = SessionStore(XPosterConfig(home=Path(sys.argv[1])))
store.ensure_dirs()
with store.lock():
    print("locked", flush=True)
    sys.stdin.read()
"""


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _cookie(
    name: str,
    value: str,
    *,
    domain: str = ".x.com",
    expires: float = -1,
) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": expires,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }


def _state(*cookies: dict[str, Any]) -> dict[str, Any]:
    return {"cookies": list(cookies), "origins": []}


def _epoch(moment: datetime) -> float:
    return moment.timestamp()


@pytest.fixture
def store(tmp_home: XPosterConfig) -> SessionStore:
    return SessionStore(tmp_home)


def test_ensure_dirs_is_private_and_fixes_existing_modes(
    store: SessionStore, tmp_home: XPosterConfig
) -> None:
    os.chmod(tmp_home.home, 0o755)

    store.ensure_dirs()

    assert _mode(tmp_home.home) == 0o700
    assert _mode(tmp_home.profile_dir) == 0o700

    os.chmod(tmp_home.home, 0o777)
    os.chmod(tmp_home.profile_dir, 0o777)

    store.ensure_dirs()

    assert _mode(tmp_home.home) == 0o700
    assert _mode(tmp_home.profile_dir) == 0o700


def test_ensure_dirs_creates_nested_home(tmp_path: Path) -> None:
    config = XPosterConfig(home=tmp_path / "one" / "two")
    store = SessionStore(config)

    store.ensure_dirs()

    assert config.home.is_dir()
    assert _mode(config.home) == 0o700
    assert config.profile_dir.is_dir()
    assert _mode(config.profile_dir) == 0o700


def test_artifacts_and_wipe_create_private_home_on_fresh_config(tmp_path: Path) -> None:
    config = XPosterConfig(home=tmp_path / "fresh" / "x")
    store = SessionStore(config)

    store.new_artifacts_dir("run")
    assert _mode(config.home) == 0o700

    store.wipe_session()

    assert _mode(config.home) == 0o700
    assert config.profile_dir.is_dir()
    assert _mode(config.profile_dir) == 0o700


def test_save_state_writes_private_json(store: SessionStore, tmp_home: XPosterConfig) -> None:
    state = _state(_cookie("auth_token", "token", expires=_epoch(NOW) + 86400))

    store.save_state(state)

    assert _mode(tmp_home.storage_state_path) == 0o600
    assert json.loads(tmp_home.storage_state_path.read_text()) == state
    assert store.load_state() == state
    assert store.has_state() is True


def test_load_state_missing_returns_none(store: SessionStore) -> None:
    assert store.load_state() is None
    assert store.has_state() is False


@pytest.mark.parametrize(
    "state",
    [
        pytest.param({"cookies": [], "origins": []}, id="empty-cookies"),
        pytest.param({"origins": []}, id="missing-cookies"),
        pytest.param({"cookies": "nope"}, id="wrong-cookie-type"),
    ],
)
def test_save_state_refuses_states_without_cookies(
    store: SessionStore, tmp_home: XPosterConfig, state: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="cookies"):
        store.save_state(state)

    assert not tmp_home.storage_state_path.exists()


def test_save_state_keeps_previous_file_when_new_state_is_empty(
    store: SessionStore, tmp_home: XPosterConfig
) -> None:
    good = _state(_cookie("auth_token", "keep-me"))
    store.save_state(good)

    with pytest.raises(ValueError, match="cookies"):
        store.save_state({"cookies": [], "origins": []})

    assert store.load_state() == good


def test_save_state_rotates_previous_good_file_to_bak(
    store: SessionStore, tmp_home: XPosterConfig
) -> None:
    first = _state(_cookie("auth_token", "first"))
    second = _state(_cookie("auth_token", "second"))
    backup = tmp_home.home / "storage_state.json.bak"

    store.save_state(first)
    store.save_state(second)

    assert _mode(backup) == 0o600
    assert json.loads(backup.read_text()) == first
    assert store.load_state() == second


def test_load_state_falls_back_to_bak_when_main_is_corrupt(
    store: SessionStore, tmp_home: XPosterConfig
) -> None:
    first = _state(_cookie("auth_token", "first"))
    second = _state(_cookie("auth_token", "second"))
    store.save_state(first)
    store.save_state(second)

    tmp_home.storage_state_path.write_text('{"cookies": [')

    assert store.load_state() == first
    assert store.has_state() is True


def test_load_state_never_raises_on_garbage(store: SessionStore, tmp_home: XPosterConfig) -> None:
    tmp_home.storage_state_path.write_bytes(b"\x00\xffnot json")

    assert store.load_state() is None


def test_failed_replace_leaves_no_partial_file(
    store: SessionStore, tmp_home: XPosterConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _state(_cookie("auth_token", "good"))
    store.save_state(good)
    before = set(tmp_home.home.iterdir())

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("xposter.store.os.replace", fail)

    with pytest.raises(OSError, match="simulated replace failure"):
        store.save_state(_state(_cookie("auth_token", "new")))

    assert json.loads(tmp_home.storage_state_path.read_text()) == good
    assert set(tmp_home.home.iterdir()) == before


def test_save_state_error_does_not_leak_values(
    store: SessionStore, tmp_home: XPosterConfig
) -> None:
    class Leaky:
        def __repr__(self) -> str:
            return "CANARY-LEAK"

    state: dict[str, Any] = {
        "cookies": [{"name": "auth_token", "value": Leaky(), "domain": ".x.com"}],
        "origins": [],
    }

    with pytest.raises(ValueError) as excinfo:
        store.save_state(state)

    assert "CANARY-LEAK" not in str(excinfo.value)
    assert not tmp_home.storage_state_path.exists()


def test_lock_writes_holder_pid_and_releases(store: SessionStore, tmp_home: XPosterConfig) -> None:
    with store.lock():
        assert tmp_home.lock_path.read_text().strip() == str(os.getpid())
        assert _mode(tmp_home.lock_path) == 0o600

    with store.lock():
        pass


def test_lock_releases_after_exception(store: SessionStore) -> None:
    with pytest.raises(RuntimeError, match="boom"), store.lock():
        raise RuntimeError("boom")

    with store.lock():
        pass


def test_lock_contention_names_holder_pid(store: SessionStore, tmp_home: XPosterConfig) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_LOCK_SCRIPT, str(tmp_home.home)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(SessionBusyError, match=f"pid {child.pid}"), store.lock():
            pass
    finally:
        with contextlib.suppress(OSError):
            if child.stdin is not None:
                child.stdin.close()
        child.wait(timeout=10)
        with contextlib.suppress(OSError):
            if child.stdout is not None:
                child.stdout.close()

    with store.lock():
        pass


def test_summarize_without_any_session(store: SessionStore) -> None:
    assert store.summarize(now=NOW) == SessionSummary(
        present=False,
        has_auth_token=False,
        has_ct0=False,
        auth_expires_at=None,
        days_left=None,
        expired=True,
        handle=None,
        last_verified_at=None,
        parked=False,
    )


def test_summarize_session_cookie_has_no_expiry(store: SessionStore) -> None:
    store.save_state(_state(_cookie("auth_token", "tok"), _cookie("ct0", "csrf")))

    summary = store.summarize(now=NOW)

    assert summary.present is True
    assert summary.has_auth_token is True
    assert summary.has_ct0 is True
    assert summary.auth_expires_at is None
    assert summary.days_left is None
    assert summary.expired is False


def test_summarize_future_expiry_counts_whole_days(store: SessionStore) -> None:
    expires = _epoch(NOW) + 6 * 86400 + 3600
    store.save_state(_state(_cookie("auth_token", "tok", expires=expires)))

    summary = store.summarize(now=NOW)

    assert summary.auth_expires_at == "2026-11-15T13:00:00Z"
    assert summary.days_left == 6
    assert summary.expired is False


def test_summarize_past_expiry_is_expired(store: SessionStore) -> None:
    expires = _epoch(NOW) - 3600
    store.save_state(_state(_cookie("auth_token", "tok", expires=expires)))

    summary = store.summarize(now=NOW)

    assert summary.auth_expires_at == "2026-11-09T11:00:00Z"
    assert summary.days_left == -1
    assert summary.expired is True


def test_summarize_missing_auth_token_is_expired(store: SessionStore) -> None:
    store.save_state(_state(_cookie("ct0", "csrf", expires=_epoch(NOW) + 86400)))

    summary = store.summarize(now=NOW)

    assert summary.present is True
    assert summary.has_auth_token is False
    assert summary.has_ct0 is True
    assert summary.auth_expires_at is None
    assert summary.days_left is None
    assert summary.expired is True


def test_summarize_ignores_non_x_domains(store: SessionStore) -> None:
    store.save_state(
        _state(
            _cookie("auth_token", "elsewhere", domain="example.com", expires=_epoch(NOW) + 86400),
            _cookie("ct0", "elsewhere", domain="www.x.com", expires=_epoch(NOW) + 86400),
        )
    )

    summary = store.summarize(now=NOW)

    assert summary.has_auth_token is False
    assert summary.has_ct0 is False
    assert summary.expired is True


def test_summarize_reads_handle_and_verification_from_meta(store: SessionStore) -> None:
    store.update_meta(handle="olsa", last_verified_at="2026-11-09T11:30:00Z")

    summary = store.summarize(now=NOW)

    assert summary.handle == "olsa"
    assert summary.last_verified_at == "2026-11-09T11:30:00Z"


def test_summarize_reports_parked(store: SessionStore) -> None:
    store.park("Challenge shown")

    assert store.summarize(now=NOW).parked is True


def test_summarize_profile_only_is_present_but_not_expired(
    store: SessionStore, tmp_home: XPosterConfig
) -> None:
    store.ensure_dirs()
    (tmp_home.profile_dir / "Cookies").write_text("chrome data")

    summary = store.summarize(now=NOW)

    assert summary.present is True
    assert summary.has_auth_token is False
    assert summary.auth_expires_at is None
    assert summary.days_left is None
    assert summary.expired is False


def test_summarize_never_raises_on_garbage(store: SessionStore, tmp_home: XPosterConfig) -> None:
    tmp_home.storage_state_path.write_bytes(b"\xff\xfe{")
    tmp_home.meta_path.write_text("not json")
    tmp_home.parked_path.write_text("{")

    summary = store.summarize(now=NOW)

    assert isinstance(summary, SessionSummary)
    assert summary.present is True
    assert summary.has_auth_token is False
    assert summary.expired is True
    assert summary.handle is None
    assert summary.parked is False


def test_summarize_never_contains_cookie_values(store: SessionStore) -> None:
    canary = "CANARY-4d9f2c-cookie-value"
    store.save_state(
        _state(
            _cookie("auth_token", canary, expires=_epoch(NOW) + 86400),
            _cookie("ct0", canary),
        )
    )

    summary = store.summarize(now=NOW)

    assert canary not in repr(summary)
    assert canary not in str(summary)
    assert canary not in json.dumps(summary.to_json())


def test_meta_merge_and_delete(store: SessionStore, tmp_home: XPosterConfig) -> None:
    assert store.read_meta() == {}

    merged = store.update_meta(handle="olsa", user_id="42")

    assert merged == {"handle": "olsa", "user_id": "42"}
    assert _mode(tmp_home.meta_path) == 0o600

    merged = store.update_meta(last_verified_at="2026-11-09T12:00:00Z")

    assert merged == {
        "handle": "olsa",
        "user_id": "42",
        "last_verified_at": "2026-11-09T12:00:00Z",
    }

    merged = store.update_meta(handle=None, missing=None)

    assert "handle" not in merged
    assert store.read_meta() == {
        "user_id": "42",
        "last_verified_at": "2026-11-09T12:00:00Z",
    }


def test_read_meta_corrupt_returns_empty(store: SessionStore, tmp_home: XPosterConfig) -> None:
    tmp_home.meta_path.write_text("[]")
    assert store.read_meta() == {}

    tmp_home.meta_path.write_text("not json")
    assert store.read_meta() == {}


def test_park_writes_private_marker_with_iso_since(
    store: SessionStore, tmp_home: XPosterConfig
) -> None:
    info = store.park("Challenge shown", kind="challenge", detail="captcha")

    assert isinstance(info, ParkInfo)
    assert info.reason == "Challenge shown"
    assert info.kind == "challenge"
    assert info.detail == "captcha"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", info.since)
    assert _mode(tmp_home.parked_path) == 0o600
    assert store.parked() == info


def test_park_keeps_first_reason_and_appends_detail(store: SessionStore) -> None:
    first = store.park("Challenge shown", kind="challenge", detail="captcha")
    second = store.park("Rate limited", kind="rate_limit", detail="retry after 1h")

    assert second.reason == first.reason
    assert second.kind == first.kind
    assert second.since == first.since
    assert "captcha" in second.detail
    assert "retry after 1h" in second.detail
    assert store.parked() == second


def test_unpark(store: SessionStore) -> None:
    assert store.unpark() is False

    store.park("Challenge shown")

    assert store.unpark() is True
    assert store.parked() is None
    assert store.unpark() is False


def test_raise_if_parked(store: SessionStore) -> None:
    assert store.raise_if_parked() is None

    info = store.park("Challenge shown", detail="captcha")

    with pytest.raises(ParkedError) as excinfo:
        store.raise_if_parked()

    assert excinfo.value.reason == "Challenge shown"
    assert excinfo.value.since == info.since


def test_parked_corrupt_returns_none(store: SessionStore, tmp_home: XPosterConfig) -> None:
    tmp_home.parked_path.write_text("not json")

    assert store.parked() is None


def test_parked_accepts_minimal_marker(store: SessionStore, tmp_home: XPosterConfig) -> None:
    tmp_home.parked_path.write_text('{"reason": "manual"}')

    info = store.parked()

    assert info == ParkInfo(reason="manual", kind="challenge", since="", detail="")
    assert store.summarize(now=NOW).parked is True


def test_new_artifacts_dir_is_private_and_sanitised(
    store: SessionStore, tmp_home: XPosterConfig
) -> None:
    path = store.new_artifacts_dir("Login failed!! ../../etc/passwd")

    assert path.is_dir()
    assert path.parent == tmp_home.artifacts_dir
    assert _mode(path) == 0o700
    assert _mode(tmp_home.artifacts_dir) == 0o700
    assert "/" not in path.name
    assert ".." not in path.name
    assert "Login-failed" in path.name


def test_new_artifacts_dir_empty_label_falls_back(store: SessionStore) -> None:
    path = store.new_artifacts_dir("   ")

    assert path.name.endswith("-run")


def test_new_artifacts_dir_names_are_unique(store: SessionStore) -> None:
    first = store.new_artifacts_dir("same")
    second = store.new_artifacts_dir("same")

    assert first != second
    assert first.is_dir()
    assert second.is_dir()


def test_artifacts_pruning_keeps_newest(tmp_home: XPosterConfig) -> None:
    config = tmp_home.with_overrides(keep_artifacts=3, artifact_max_age_days=36500)
    store = SessionStore(config)
    created: list[Path] = []
    base = 1_600_000_000

    for index in range(5):
        path = store.new_artifacts_dir(f"run-{index}")
        stamp = (base + index) * 1_000_000_000
        os.utime(path, ns=(stamp, stamp))
        created.append(path)

    remaining = [path.name for path in tmp_home.artifacts_dir.iterdir()]

    assert len(remaining) == 3
    assert created[0].name not in remaining
    assert created[1].name not in remaining
    for path in created[2:]:
        assert path.is_dir()


def test_wipe_session_keeps_parked_ledger_and_artifacts(
    store: SessionStore, tmp_home: XPosterConfig
) -> None:
    store.ensure_dirs()
    store.save_state(_state(_cookie("auth_token", "first")))
    store.save_state(_state(_cookie("auth_token", "second")))
    store.update_meta(handle="olsa")
    store.park("Challenge shown")
    artifacts = store.new_artifacts_dir("run")
    tmp_home.ledger_path.write_text("{}\n")
    (tmp_home.profile_dir / "Cookies").write_text("chrome data")
    deep = tmp_home.profile_dir / "Default" / "Local Storage"
    deep.mkdir(parents=True)
    (deep / "leveldb").write_text("data")

    store.wipe_session()

    assert tmp_home.profile_dir.is_dir()
    assert list(tmp_home.profile_dir.iterdir()) == []
    assert _mode(tmp_home.profile_dir) == 0o700
    assert not tmp_home.storage_state_path.exists()
    assert not (tmp_home.home / "storage_state.json.bak").exists()
    assert not tmp_home.meta_path.exists()
    assert tmp_home.parked_path.exists()
    assert tmp_home.ledger_path.exists()
    assert artifacts.is_dir()
    assert store.load_state() is None
    assert store.parked() is not None


def test_summarize_naive_now_is_treated_as_utc(store: SessionStore) -> None:
    expires = NOW.timestamp() + 2 * 86400
    store.save_state(_state(_cookie("auth_token", "tok", expires=expires)))

    aware = store.summarize(now=NOW)
    naive = store.summarize(now=NOW.replace(tzinfo=None))

    assert aware.auth_expires_at == naive.auth_expires_at
    assert aware.days_left == naive.days_left == 2


def test_summarize_accepts_non_utc_now(store: SessionStore) -> None:
    expires = NOW.timestamp() + 3 * 86400
    store.save_state(_state(_cookie("auth_token", "tok", expires=expires)))
    shifted = NOW.astimezone(timezone(timedelta(hours=5)))

    assert store.summarize(now=shifted).days_left == 3


def test_artifact_max_age_defaults_to_14(tmp_path: Path) -> None:
    assert XPosterConfig(home=tmp_path / "x").artifact_max_age_days == 14


@pytest.mark.parametrize(
    "html",
    [
        pytest.param('<input type="password" name="pw" value="PWSECRET1">', id="quoted"),
        pytest.param("<INPUT VALUE='PWSECRET2' TYPE=password NAME=pw2>", id="reversed-single"),
        pytest.param(
            '<input name="pw3" TYPE="Password" value=PWSECRET3 autocomplete="current-password">',
            id="unquoted",
        ),
        pytest.param("<input type=password value=PWSECRET4 />", id="self-closing"),
    ],
)
def test_sanitise_html_redacts_password_values(html: str) -> None:
    cleaned = sanitise_html(html)

    assert "PWSECRET" not in cleaned
    assert 'value="[REDACTED]"' in cleaned


@pytest.mark.parametrize(
    "html",
    [
        pytest.param(
            '<input type="hidden" name="authenticity_token" value="HIDDENSECRET1">', id="quoted"
        ),
        pytest.param("<input value='HIDDENSECRET2' TYPE=Hidden name=csrf>", id="reversed-single"),
        pytest.param('<input name="x" type=hidden value=HIDDENSECRET3>', id="unquoted"),
    ],
)
def test_sanitise_html_redacts_hidden_input_values(html: str) -> None:
    cleaned = sanitise_html(html)

    assert "HIDDENSECRET" not in cleaned
    assert 'value="[REDACTED]"' in cleaned


@pytest.mark.parametrize(
    ("html", "secret"),
    [
        pytest.param(
            '<meta name="csrf-token" content="METASECRET1">', "METASECRET1", id="csrf-token"
        ),
        pytest.param(
            "<meta content='METASECRET2' name='x-csrf-token'>", "METASECRET2", id="reversed"
        ),
        pytest.param(
            '<meta name="authenticity_token" content=METASECRET3>', "METASECRET3", id="token"
        ),
        pytest.param('<meta name="foo_csrf_bar" content="METASECRET4">', "METASECRET4", id="csrf"),
        pytest.param('<meta NAME="CSRF" CONTENT="METASECRET5">', "METASECRET5", id="upper"),
    ],
)
def test_sanitise_html_redacts_token_meta_content(html: str, secret: str) -> None:
    cleaned = sanitise_html(html)

    assert secret not in cleaned
    assert 'content="[REDACTED]"' in cleaned


def test_sanitise_html_keeps_unrelated_meta_and_text_inputs() -> None:
    html = (
        '<meta name="viewport" content="width=device-width">'
        '<input type="text" name="q" value="keep">'
    )

    assert sanitise_html(html) == html


def test_sanitise_html_ignores_token_names_inside_hyphenated_attributes() -> None:
    html = '<div data-att="keep" data-ct0="keep-too">text</div>'

    assert sanitise_html(html) == html


@pytest.mark.parametrize(
    ("html", "secret"),
    [
        pytest.param('{"ct0":"CT0SECRET"}', "CT0SECRET", id="ct0-json"),
        pytest.param('{"auth_token":"AUTHSECRET"}', "AUTHSECRET", id="auth-token-json"),
        pytest.param("{'guest_id': 'GUESTSECRET'}", "GUESTSECRET", id="single-quoted-json"),
        pytest.param('"kdt":"KDTSECRET"', "KDTSECRET", id="kdt-json"),
        pytest.param('"att":"ATTSECRET"', "ATTSECRET", id="att-json"),
        pytest.param("Cookie: ct0=COOKIESECRET; other=keep", "COOKIESECRET", id="cookie"),
        pytest.param("auth_token=BARESECRET&x=1", "BARESECRET", id="assignment"),
        pytest.param("guest_id = BARESECRET2", "BARESECRET2", id="spaced-assignment"),
        pytest.param("session ct0: COLONSECRET", "COLONSECRET", id="bare-colon"),
    ],
)
def test_sanitise_html_redacts_inline_tokens(html: str, secret: str) -> None:
    cleaned = sanitise_html(html)

    assert secret not in cleaned
    assert "[REDACTED]" in cleaned


@pytest.mark.parametrize(
    ("html", "secret"),
    [
        pytest.param("Authorization: Bearer BEARERSECRET", "BEARERSECRET", id="header-bearer"),
        pytest.param("X-Authorization: Bearer PREFIXSECRET", "PREFIXSECRET", id="prefixed"),
        pytest.param('"authorization":"Basic AUTHBASIC"', "AUTHBASIC", id="json-basic"),
        pytest.param("'authorization': 'Token AUTHTOKEN'", "AUTHTOKEN", id="single-token"),
        pytest.param("standalone Bearer LONESECRET", "LONESECRET", id="bearer"),
        pytest.param(
            "headers = {authorization: 'Bearer HEADERSECRET'}", "HEADERSECRET", id="bare-header"
        ),
    ],
)
def test_sanitise_html_redacts_authorization_headers(html: str, secret: str) -> None:
    cleaned = sanitise_html(html)

    assert secret not in cleaned
    assert "[REDACTED]" in cleaned


def test_sanitise_html_is_idempotent_and_keeps_safe_content() -> None:
    html = (
        '<input type="text" name="q" value="keep">'
        '<input type="password" name="pw" value="PWSECRET">'
        '<input type="hidden" name="authenticity_token" value="HIDDENSECRET">'
        '<meta name="csrf-token" content="CSRFMETA">'
        '<meta name="viewport" content="width=device-width">'
        '<script>window.x={"ct0":"CT0SECRET","guest_id":"GUESTSECRET"};</script>'
        "<script>document.cookie='auth_token=AUTHSECRET; path=/';</script>"
        "<script>fetch('/x',{headers:{Authorization:'Bearer BEARERSECRET'}});</script>"
    )

    once = sanitise_html(html)

    assert sanitise_html(once) == once
    for secret in (
        "PWSECRET",
        "HIDDENSECRET",
        "CSRFMETA",
        "CT0SECRET",
        "GUESTSECRET",
        "AUTHSECRET",
        "BEARERSECRET",
    ):
        assert secret not in once
    assert 'value="keep"' in once
    assert 'name="viewport"' in once
    assert once.count("[REDACTED]") >= 6


def test_sanitise_html_redacts_truncated_tags() -> None:
    password = sanitise_html('<input type=password value="PWSECRET')
    hidden = sanitise_html("<input type=hidden value='HIDDENSECRET")
    meta = sanitise_html("<meta name='csrf-token' content='METASECRET")
    inline = sanitise_html('{"ct0":"CT0SECRET')

    for cleaned in (password, hidden, meta, inline):
        assert "[REDACTED]" in cleaned
    assert "PWSECRET" not in password
    assert "HIDDENSECRET" not in hidden
    assert "METASECRET" not in meta
    assert "CT0SECRET" not in inline


@pytest.mark.parametrize(
    "html",
    ["", "<", "<<<>>>", "<input", "<meta", "{{{{", "Bearer", "ct0="],
)
def test_sanitise_html_never_raises_on_malformed_html(html: str) -> None:
    assert isinstance(sanitise_html(html), str)


def test_write_artifact_writes_utf8_with_private_modes(store: SessionStore) -> None:
    directory = store.new_artifacts_dir("run")

    path = store.write_artifact(directory, "page.html", "héllo")

    assert path == directory / "page.html"
    assert path.read_bytes() == "héllo".encode()
    assert _mode(path) == 0o600
    assert _mode(directory) == 0o700


def test_write_artifact_writes_bytes_unchanged(store: SessionStore) -> None:
    directory = store.new_artifacts_dir("run")
    payload = b"\x89PNG\r\n\x1a\n\x00"

    path = store.write_artifact(directory, "shot.png", payload)

    assert path.read_bytes() == payload


def test_write_artifact_sanitises_str_when_asked(store: SessionStore) -> None:
    directory = store.new_artifacts_dir("run")
    dirty = (
        '<input type="hidden" name="_csrf" value="HIDDENSECRET">'
        '<meta name="csrf-token" content="METASECRET">'
    )

    path = store.write_artifact(directory, "dump.html", dirty, sanitise=True)
    text = path.read_text()

    assert "HIDDENSECRET" not in text
    assert "METASECRET" not in text
    assert "[REDACTED]" in text
    raw = store.write_artifact(directory, "raw.html", dirty)
    assert "HIDDENSECRET" in raw.read_text()


def test_write_artifact_sanitise_leaves_bytes_alone(store: SessionStore) -> None:
    directory = store.new_artifacts_dir("run")
    payload = b'<input type="password" value="BYTESECRET">'

    path = store.write_artifact(directory, "trace.zip", payload, sanitise=True)

    assert path.read_bytes() == payload


def test_write_artifact_refuses_paths_outside_the_artifact_root(
    store: SessionStore, tmp_home: XPosterConfig, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="under"):
        store.write_artifact(outside, "page.html", "x")

    with pytest.raises(ValueError, match="under"):
        store.write_artifact(tmp_home.home, "page.html", "x")


def test_write_artifact_refuses_symlinked_directory_outside(
    store: SessionStore, tmp_home: XPosterConfig, tmp_path: Path
) -> None:
    store.ensure_dirs()
    root = tmp_home.artifacts_dir
    root.mkdir(mode=0o700, exist_ok=True)
    link = root / "link"
    link.symlink_to(tmp_path)

    with pytest.raises(ValueError, match="under"):
        store.write_artifact(link, "page.html", "x")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("page.html", "page.html", id="plain"),
        pytest.param("../../evil.html", "evil.html", id="traversal"),
        pytest.param("/etc/passwd", "passwd", id="absolute"),
        pytest.param("a/b\\c", "c", id="separators"),
        pytest.param("nested/page.html", "page.html", id="nested"),
        pytest.param("", "artifact", id="empty"),
        pytest.param("..", "artifact", id="dots"),
    ],
)
def test_write_artifact_sanitises_names(store: SessionStore, name: str, expected: str) -> None:
    directory = store.new_artifacts_dir("run")

    path = store.write_artifact(directory, name, "x")

    assert path.parent == directory
    assert path.name == expected


def test_write_artifact_creates_missing_subdirectory(
    store: SessionStore, tmp_home: XPosterConfig
) -> None:
    directory = tmp_home.artifacts_dir / "nested" / "run"

    path = store.write_artifact(directory, "page.html", "x")

    assert path == directory / "page.html"
    assert directory.is_dir()
    assert _mode(directory) == 0o700


def test_write_artifact_failed_replace_leaves_no_partial_file(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = store.new_artifacts_dir("run")
    path = store.write_artifact(directory, "page.html", "first")
    before = sorted(entry.name for entry in directory.iterdir())

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("simulated write_artifact failure")

    monkeypatch.setattr("xposter.store.os.replace", fail)

    with pytest.raises(OSError, match="simulated write_artifact failure"):
        store.write_artifact(directory, "page.html", "second")

    assert path.read_text() == "first"
    assert sorted(entry.name for entry in directory.iterdir()) == before


def test_protect_file_restricts_a_library_written_file(
    store: SessionStore, tmp_home: XPosterConfig
) -> None:
    store.ensure_dirs()
    shot = tmp_home.profile_dir / "shot.png"
    shot.write_bytes(b"png")
    os.chmod(shot, 0o644)

    store.protect_file(shot)

    assert _mode(shot) == 0o600


def test_protect_file_refuses_outside_home(store: SessionStore, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    os.chmod(outside, 0o644)

    with pytest.raises(ValueError, match="outside"):
        store.protect_file(outside)

    assert _mode(outside) == 0o644


def test_protect_file_missing_raises(store: SessionStore, tmp_home: XPosterConfig) -> None:
    with pytest.raises(FileNotFoundError):
        store.protect_file(tmp_home.home / "missing.txt")


def test_protect_file_refuses_symlink_escaping_home(
    store: SessionStore, tmp_home: XPosterConfig, tmp_path: Path
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("x")
    os.chmod(target, 0o644)
    store.ensure_dirs()
    link = tmp_home.home / "link"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="outside"):
        store.protect_file(link)

    assert _mode(target) == 0o644


def test_protect_file_refuses_a_directory(store: SessionStore, tmp_home: XPosterConfig) -> None:
    store.ensure_dirs()

    with pytest.raises(ValueError, match="non-file"):
        store.protect_file(tmp_home.profile_dir)


def test_audit_permissions_clean_tree(store: SessionStore, tmp_home: XPosterConfig) -> None:
    store.ensure_dirs()
    store.save_state(_state(_cookie("auth_token", "first")))
    store.save_state(_state(_cookie("auth_token", "second")))
    store.update_meta(handle="olsa")
    store.park("Challenge shown")
    tmp_home.ledger_path.write_text("{}\n")
    os.chmod(tmp_home.ledger_path, 0o600)
    with store.lock():
        pass
    directory = store.new_artifacts_dir("run")
    store.write_artifact(directory, "page.html", "<html></html>")

    assert store.audit_permissions() == []


def test_audit_permissions_finds_a_loose_artifact_file(
    store: SessionStore,
) -> None:
    directory = store.new_artifacts_dir("run")
    path = store.write_artifact(directory, "page.html", "x")
    os.chmod(path, 0o644)

    problems = store.audit_permissions()

    matching = [problem for item, problem in problems if item == str(path)]
    assert matching
    assert "0644" in matching[0]
    assert "0600" in matching[0]


def test_audit_permissions_finds_a_loose_directory(
    store: SessionStore, tmp_home: XPosterConfig
) -> None:
    store.ensure_dirs()
    loose = tmp_home.profile_dir / "Cache"
    loose.mkdir()
    os.chmod(loose, 0o755)

    problems = store.audit_permissions()

    matching = [problem for item, problem in problems if item == str(loose)]
    assert matching
    assert "0755" in matching[0]
    assert "0700" in matching[0]


def test_audit_permissions_finds_a_symlink(store: SessionStore, tmp_home: XPosterConfig) -> None:
    store.ensure_dirs()
    target = tmp_home.profile_dir / "target"
    target.write_text("x")
    link = tmp_home.home / "link"
    link.symlink_to(target)

    problems = store.audit_permissions()

    matching = [problem for item, problem in problems if item == str(link)]
    assert matching
    assert "symlink" in matching[0]


def test_audit_permissions_finds_a_loose_credential_file(
    store: SessionStore, tmp_home: XPosterConfig
) -> None:
    store.ensure_dirs()
    store.update_meta(handle="olsa")
    os.chmod(tmp_home.meta_path, 0o644)

    problems = store.audit_permissions()

    matching = [problem for item, problem in problems if item == str(tmp_home.meta_path)]
    assert matching
    assert "0644" in matching[0]
    assert "0600" in matching[0]


def test_audit_permissions_missing_home_is_clean(tmp_path: Path) -> None:
    config = XPosterConfig(home=tmp_path / "absent")

    assert SessionStore(config).audit_permissions() == []


def test_artifacts_age_pruning_removes_old_directories(tmp_home: XPosterConfig) -> None:
    config = tmp_home.with_overrides(keep_artifacts=10, artifact_max_age_days=7)
    store = SessionStore(config)
    root = tmp_home.artifacts_dir
    root.mkdir(mode=0o700, parents=True)
    now = datetime.now(UTC).timestamp()
    old = root / "old"
    stale = root / "stale"
    recent = root / "recent"
    for path, age_days in ((old, 30), (stale, 8), (recent, 2)):
        path.mkdir()
        stamp = now - age_days * 86400
        os.utime(path, (stamp, stamp))

    newest = store.new_artifacts_dir("trigger")

    assert not old.exists()
    assert not stale.exists()
    assert recent.exists()
    assert newest.exists()


def test_artifacts_pruning_always_keeps_the_newest(tmp_home: XPosterConfig) -> None:
    config = tmp_home.with_overrides(keep_artifacts=0, artifact_max_age_days=0)
    store = SessionStore(config)
    root = tmp_home.artifacts_dir
    root.mkdir(mode=0o700, parents=True)
    now = datetime.now(UTC).timestamp()
    newest = root / "newest"
    older = root / "older"
    for path, age_days in ((newest, 100), (older, 200)):
        path.mkdir()
        stamp = now - age_days * 86400
        os.utime(path, (stamp, stamp))

    store._prune_artifacts(root)

    assert newest.exists()
    assert not older.exists()
