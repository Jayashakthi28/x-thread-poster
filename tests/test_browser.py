"""Pure tests for the browser layer: discovery, launch flags, login and tells.

Nothing here starts Chrome or touches the network; the real-browser checks live
in ``test_browser_integration.py``. Fake executables and a fake ``Popen`` keep
the login and Xvfb behaviour deterministic.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from xposter import browser
from xposter.browser import (
    PlainLogin,
    Tell,
    XvfbServer,
    assess_tells,
    build_launch_kwargs,
    chrome_version,
    collect_tells,
    find_chrome,
    new_page,
    open_context,
    require_chrome,
    resolve_display,
)
from xposter.config import Display, XPosterConfig
from xposter.errors import BrowserUnavailableError, ConfigurationError

REAL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
HEADLESS_UA = REAL_UA.replace("Chrome/156", "HeadlessChrome/156")


def _executable(path: Path, script: str = "#!/bin/sh\nexit 0\n") -> Path:
    """Write an executable stub, the smallest thing find_chrome accepts."""
    path.write_text(script)
    path.chmod(0o755)
    return path


def _by_name(tells: list[Tell], name: str) -> Tell:
    matches = [tell for tell in tells if tell.name == name]
    assert len(matches) == 1, f"expected one {name!r} tell, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# find_chrome / require_chrome / chrome_version
# ---------------------------------------------------------------------------


def test_find_chrome_prefers_an_explicit_path(tmp_home: XPosterConfig, tmp_path: Path) -> None:
    chrome = _executable(tmp_path / "chrome")
    config = tmp_home.with_overrides(chrome_path=str(chrome))

    assert find_chrome(config) == str(chrome)


def test_find_chrome_rejects_a_bad_explicit_path(tmp_home: XPosterConfig, tmp_path: Path) -> None:
    missing = tmp_path / "not-here"
    config = tmp_home.with_overrides(chrome_path=str(missing))

    with pytest.raises(ConfigurationError, match="chrome_path"):
        find_chrome(config)


def test_find_chrome_rejects_a_non_executable_file(tmp_home: XPosterConfig, tmp_path: Path) -> None:
    plain = tmp_path / "chrome"
    plain.write_text("not a program")
    plain.chmod(0o644)
    config = tmp_home.with_overrides(chrome_path=str(plain))

    if os.access(plain, os.X_OK):
        pytest.skip("test host ignores file modes for executable checks")
    with pytest.raises(ConfigurationError, match="not an executable"):
        find_chrome(config)


def test_find_chrome_uses_macos_default_paths(
    tmp_home: XPosterConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chrome = _executable(tmp_path / "Google Chrome")
    monkeypatch.setattr(browser, "_platform_name", lambda: "darwin")
    monkeypatch.setattr(browser, "_MAC_CHROME_PATHS", (str(chrome),))

    assert find_chrome(tmp_home) == str(chrome)


def test_find_chrome_uses_linux_which(
    tmp_home: XPosterConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chrome = _executable(tmp_path / "google-chrome-stable")
    monkeypatch.setattr(browser, "_platform_name", lambda: "linux")
    monkeypatch.setattr(browser, "_LINUX_CHROME_PATHS", ())
    monkeypatch.setattr(
        browser,
        "_which",
        lambda name: str(chrome) if name == "google-chrome-stable" else None,
    )

    assert find_chrome(tmp_home) == str(chrome)


def test_find_chrome_uses_linux_absolute_fallback(
    tmp_home: XPosterConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chrome = _executable(tmp_path / "chrome")
    monkeypatch.setattr(browser, "_platform_name", lambda: "linux")
    monkeypatch.setattr(browser, "_which", lambda name: None)
    monkeypatch.setattr(browser, "_LINUX_CHROME_PATHS", (str(chrome),))

    assert find_chrome(tmp_home) == str(chrome)


def test_find_chrome_uses_windows_paths(
    tmp_home: XPosterConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chrome = _executable(tmp_path / "chrome.exe")
    monkeypatch.setattr(browser, "_platform_name", lambda: "win32")
    monkeypatch.setattr(browser, "_WINDOWS_CHROME_PATHS", (str(chrome),))

    assert find_chrome(tmp_home) == str(chrome)


def test_find_chrome_returns_none_instead_of_bundled_chromium(
    tmp_home: XPosterConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser, "_platform_name", lambda: "linux")
    monkeypatch.setattr(browser, "_which", lambda name: None)
    monkeypatch.setattr(browser, "_LINUX_CHROME_PATHS", ())

    assert find_chrome(tmp_home) is None


def test_require_chrome_returns_a_path(tmp_home: XPosterConfig, tmp_path: Path) -> None:
    chrome = _executable(tmp_path / "chrome")
    config = tmp_home.with_overrides(chrome_path=str(chrome))

    assert require_chrome(config) == str(chrome)


def test_require_chrome_explains_how_to_install(
    tmp_home: XPosterConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser, "find_chrome", lambda config: None)

    with pytest.raises(BrowserUnavailableError, match="Install Google Chrome"):
        require_chrome(tmp_home)


def test_chrome_version_parses_the_version(tmp_path: Path) -> None:
    chrome = _executable(tmp_path / "chrome", "#!/bin/sh\necho 'Google Chrome 156.0.8078.80'\n")

    assert chrome_version(str(chrome)) == "156.0.8078.80"


def test_chrome_version_accepts_chromium_output(tmp_path: Path) -> None:
    chrome = _executable(tmp_path / "chrome", "#!/bin/sh\necho 'Chromium 131.0.6778.85'\n")

    assert chrome_version(str(chrome)) == "131.0.6778.85"


def test_chrome_version_returns_none_for_missing_binary(tmp_path: Path) -> None:
    assert chrome_version(str(tmp_path / "missing")) is None


def test_chrome_version_returns_none_for_unparseable_output(tmp_path: Path) -> None:
    chrome = _executable(tmp_path / "chrome", "#!/bin/sh\necho 'hello'\n")

    assert chrome_version(str(chrome)) is None


def test_chrome_version_returns_none_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("chrome", 10.0)

    monkeypatch.setattr(browser.subprocess, "run", timeout)

    assert chrome_version(str(tmp_path / "chrome")) is None


# ---------------------------------------------------------------------------
# resolve_display
# ---------------------------------------------------------------------------


def _which_xvfb(present: bool) -> Any:
    def which(name: str) -> str | None:
        return "/usr/bin/Xvfb" if present and name == "Xvfb" else None

    return which


@pytest.mark.parametrize(
    ("requested", "platform", "xvfb", "env", "expected"),
    [
        pytest.param(Display.AUTO, "linux", True, {}, Display.XVFB, id="linux-auto-xvfb"),
        pytest.param(Display.AUTO, "linux", False, {}, Display.HEADLESS, id="linux-auto-no-xvfb"),
        pytest.param(Display.AUTO, "darwin", True, {}, Display.HEADLESS, id="mac-auto"),
        pytest.param(Display.AUTO, "win32", True, {}, Display.HEADLESS, id="win-auto"),
        pytest.param(Display.HEADLESS, "linux", True, {}, Display.HEADLESS, id="forced-headless"),
        pytest.param(Display.XVFB, "linux", True, {}, Display.XVFB, id="forced-xvfb"),
        pytest.param(
            Display.VISIBLE, "linux", True, {"DISPLAY": ":0"}, Display.VISIBLE, id="linux-x11"
        ),
        pytest.param(
            Display.VISIBLE,
            "linux",
            False,
            {"WAYLAND_DISPLAY": "wayland-0"},
            Display.VISIBLE,
            id="linux-wayland",
        ),
        pytest.param(Display.VISIBLE, "darwin", False, {}, Display.VISIBLE, id="mac-visible"),
        pytest.param(Display.VISIBLE, "win32", False, {}, Display.VISIBLE, id="win-visible"),
    ],
)
def test_resolve_display_matrix(
    tmp_home: XPosterConfig,
    requested: Display,
    platform: str,
    xvfb: bool,
    env: dict[str, str],
    expected: Display,
) -> None:
    config = tmp_home.with_overrides(display=requested)

    resolved = resolve_display(config, platform=platform, env=env, which=_which_xvfb(xvfb))

    assert resolved == expected


def test_resolve_display_xvfb_without_binary_raises(tmp_home: XPosterConfig) -> None:
    config = tmp_home.with_overrides(display=Display.XVFB)

    with pytest.raises(BrowserUnavailableError, match="Xvfb is not on PATH"):
        resolve_display(config, platform="linux", env={}, which=_which_xvfb(False))


def test_resolve_display_visible_without_display_raises(tmp_home: XPosterConfig) -> None:
    config = tmp_home.with_overrides(display=Display.VISIBLE)

    with pytest.raises(BrowserUnavailableError, match="DISPLAY"):
        resolve_display(config, platform="linux", env={}, which=_which_xvfb(True))


def test_resolve_display_visible_ignores_blank_display(tmp_home: XPosterConfig) -> None:
    config = tmp_home.with_overrides(display=Display.VISIBLE)

    with pytest.raises(BrowserUnavailableError, match="WAYLAND_DISPLAY"):
        resolve_display(
            config,
            platform="linux",
            env={"DISPLAY": "   ", "WAYLAND_DISPLAY": ""},
            which=_which_xvfb(True),
        )


def test_resolve_display_uses_the_real_platform_when_omitted(
    tmp_home: XPosterConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser, "_platform_name", lambda: "darwin")

    assert resolve_display(tmp_home, env={}, which=_which_xvfb(True)) == Display.HEADLESS


def test_resolve_display_uses_process_env_when_omitted(
    tmp_home: XPosterConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    config = tmp_home.with_overrides(display=Display.VISIBLE)

    assert resolve_display(config, platform="linux", which=_which_xvfb(True)) == Display.VISIBLE


@pytest.mark.parametrize("name", ["Darwin", "macOS", "LINUX", "Linux", "Windows", "win32"])
def test_resolve_display_normalises_platform_names(tmp_home: XPosterConfig, name: str) -> None:
    expected = Display.XVFB if name.lower().startswith("linux") else Display.HEADLESS

    assert resolve_display(tmp_home, platform=name, env={}, which=_which_xvfb(True)) == expected


# ---------------------------------------------------------------------------
# build_launch_kwargs
# ---------------------------------------------------------------------------


def test_build_launch_kwargs_headless(tmp_home: XPosterConfig) -> None:
    kwargs = build_launch_kwargs(tmp_home, Display.HEADLESS, "/fake/chrome")

    assert kwargs["executable_path"] == "/fake/chrome"
    assert kwargs["headless"] is True
    assert kwargs["ignore_default_args"] == ["--enable-automation"]
    assert kwargs["viewport"] == {"width": 1440, "height": 900}
    assert "no_viewport" not in kwargs
    assert kwargs["accept_downloads"] is False
    assert kwargs["timeout"] == tmp_home.default_timeout_ms
    assert not {"user_agent", "locale", "timezone_id"} & kwargs.keys()
    assert kwargs["args"] == [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1440,900",
    ]


@pytest.mark.parametrize("display", [Display.XVFB, Display.VISIBLE])
def test_build_launch_kwargs_headed_uses_no_viewport(
    tmp_home: XPosterConfig, display: Display, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser, "_running_as_root", lambda: False)

    kwargs = build_launch_kwargs(tmp_home, display, "/fake/chrome")

    assert kwargs["headless"] is False
    assert kwargs["no_viewport"] is True
    assert "viewport" not in kwargs
    assert "--window-size=1440,900" in kwargs["args"]


def test_build_launch_kwargs_keeps_playwright_keychain_defaults(
    tmp_home: XPosterConfig,
) -> None:
    kwargs = build_launch_kwargs(tmp_home, Display.HEADLESS, "/fake/chrome")

    ignored = kwargs["ignore_default_args"]
    assert isinstance(ignored, list)
    assert "--use-mock-keychain" not in ignored
    assert "--password-store=basic" not in ignored
    joined = " ".join(kwargs["args"])
    assert "--headless" not in joined
    assert "--remote-debugging" not in joined
    assert "--enable-automation" not in joined


def test_build_launch_kwargs_does_not_override_identity(
    tmp_home: XPosterConfig,
) -> None:
    kwargs = build_launch_kwargs(tmp_home, Display.HEADLESS, "/fake/chrome")

    for key in ("user_agent", "locale", "timezone_id", "screen", "device_scale_factor"):
        assert key not in kwargs


def test_build_launch_kwargs_adds_no_sandbox_only_for_root_linux(
    tmp_home: XPosterConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser, "_platform_name", lambda: "linux")
    monkeypatch.setattr(browser, "_running_as_root", lambda: True)

    kwargs = build_launch_kwargs(tmp_home, Display.HEADLESS, "/fake/chrome")

    assert "--no-sandbox" in kwargs["args"]


@pytest.mark.parametrize(
    ("platform", "root", "expected"),
    [
        pytest.param("linux", False, False, id="linux-user"),
        pytest.param("darwin", True, False, id="mac-root"),
        pytest.param("win32", True, False, id="windows-root"),
    ],
)
def test_build_launch_kwargs_never_adds_no_sandbox_elsewhere(
    tmp_home: XPosterConfig,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    root: bool,
    expected: bool,
) -> None:
    monkeypatch.setattr(browser, "_platform_name", lambda: platform)
    monkeypatch.setattr(browser, "_running_as_root", lambda: root)

    kwargs = build_launch_kwargs(tmp_home, Display.HEADLESS, "/fake/chrome")

    assert ("--no-sandbox" in kwargs["args"]) is expected


def test_build_launch_kwargs_treats_unresolved_auto_as_headless(
    tmp_home: XPosterConfig,
) -> None:
    kwargs = build_launch_kwargs(tmp_home, Display.AUTO, "/fake/chrome")

    assert kwargs["headless"] is True
    assert kwargs["viewport"] == {"width": 1440, "height": 900}


# ---------------------------------------------------------------------------
# XvfbServer
# ---------------------------------------------------------------------------


def _fake_xvfb(tmp_path: Path) -> tuple[Path, Path]:
    """Create a stub Xvfb that publishes the socket and cleans it up on TERM."""
    socket_dir = tmp_path / "X11-unix"
    socket_dir.mkdir()
    script = tmp_path / "fake-xvfb"
    script.write_text(
        f"""#!{sys.executable}
import signal
import sys
import time
from pathlib import Path

number = sys.argv[1].lstrip(":")
socket = Path({str(socket_dir)!r}) / f"X{{number}}"
socket.touch()

def stop(*args):
    socket.unlink(missing_ok=True)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
while True:
    time.sleep(0.02)
"""
    )
    script.chmod(0o755)
    return script, socket_dir


def test_xvfb_server_starts_and_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script, socket_dir = _fake_xvfb(tmp_path)
    monkeypatch.setattr(browser, "_X11_SOCKET_DIR", socket_dir)

    with XvfbServer(executable=str(script), timeout_s=5.0) as server:
        process = server._process
        assert server.display.startswith(":")
        assert process is not None
        assert process.poll() is None
        assert (socket_dir / f"X{server.display[1:]}").exists()

    assert process is not None
    assert process.poll() is not None
    assert not (socket_dir / f"X{server.display[1:]}").exists()


def test_xvfb_server_reports_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser, "_X11_SOCKET_DIR", tmp_path)
    monkeypatch.setattr(browser, "_which", lambda name: None)

    with pytest.raises(BrowserUnavailableError, match="Xvfb was not found"), XvfbServer():
        pass


def test_xvfb_server_reports_unexecutable_binary(tmp_path: Path) -> None:
    with (
        pytest.raises(BrowserUnavailableError, match="could not be started"),
        XvfbServer(executable=str(tmp_path / "missing"), timeout_s=1.0),
    ):
        pass


def test_xvfb_server_reports_immediate_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = _executable(tmp_path / "dead-xvfb", "#!/bin/sh\nexit 3\n")
    monkeypatch.setattr(browser, "_X11_SOCKET_DIR", tmp_path / "X11-unix")
    (tmp_path / "X11-unix").mkdir()

    with (
        pytest.raises(BrowserUnavailableError, match="exited immediately"),
        XvfbServer(executable=str(dead), timeout_s=2.0),
    ):
        pass


def test_xvfb_server_reports_a_socket_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    silent = tmp_path / "silent-xvfb"
    silent.write_text(f"#!{sys.executable}\nimport time\nwhile True: time.sleep(0.02)\n")
    silent.chmod(0o755)
    monkeypatch.setattr(browser, "_X11_SOCKET_DIR", tmp_path / "X11-unix")
    (tmp_path / "X11-unix").mkdir()

    with (
        pytest.raises(BrowserUnavailableError, match="did not create"),
        XvfbServer(executable=str(silent), timeout_s=0.2),
    ):
        pass


# ---------------------------------------------------------------------------
# Stale SingletonLock pid checks (E4)
# ---------------------------------------------------------------------------


def _write_chrome_lock(profile_dir: Path, pid: int) -> None:
    """Write the ``<hostname>-<pid>`` symlink Chrome uses as SingletonLock."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "SingletonLock").symlink_to(f"{socket.gethostname()}-{pid}")


def test_profile_in_use_accepts_a_live_chrome_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_dir = tmp_path / "profile"
    _write_chrome_lock(profile_dir, os.getpid())
    seen: dict[str, Any] = {}

    def fake_ps(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["command"] = command
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
                "--user-data-dir=/profile"
            ),
            stderr="",
        )

    monkeypatch.setattr(browser.subprocess, "run", fake_ps)

    assert browser._profile_in_use(profile_dir) is True
    assert seen["command"] == ["ps", "-p", str(os.getpid()), "-o", "comm=,args="]
    assert seen["timeout"] == 3.0


def test_profile_in_use_rejects_a_live_non_chrome_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_dir = tmp_path / "profile"
    _write_chrome_lock(profile_dir, os.getpid())

    monkeypatch.setattr(
        browser.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout="/bin/zsh -c pytest", stderr=""
        ),
    )

    assert browser._profile_in_use(profile_dir) is False


def test_profile_in_use_ignores_a_dead_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    profile_dir = tmp_path / "profile"
    _write_chrome_lock(profile_dir, dead.pid)

    def unexpected_ps(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("ps must not run for a dead pid")

    monkeypatch.setattr(browser.subprocess, "run", unexpected_ps)

    assert browser._profile_in_use(profile_dir) is False


@pytest.mark.parametrize("failure", ["missing", "timeout", "nonzero"])
def test_profile_in_use_is_conservative_when_ps_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    profile_dir = tmp_path / "profile"
    _write_chrome_lock(profile_dir, os.getpid())

    def fake_ps(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if failure == "missing":
            raise OSError("ps not found")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("ps", 3.0)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="ps failed")

    monkeypatch.setattr(browser.subprocess, "run", fake_ps)

    assert browser._profile_in_use(profile_dir) is True


# ---------------------------------------------------------------------------
# new_page
# ---------------------------------------------------------------------------


class _FakeContext:
    """Just enough BrowserContext for new_page tests."""

    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages
        self.created = 0

    async def new_page(self) -> Any:
        self.created += 1
        page = object()
        self.pages.append(page)
        return page


async def test_new_page_reuses_the_first_page() -> None:
    first = object()
    context = _FakeContext([first])

    page = await new_page(context)  # type: ignore[arg-type]

    assert page is first
    assert context.created == 0


async def test_new_page_creates_one_when_the_context_is_empty() -> None:
    context = _FakeContext([])

    page = await new_page(context)  # type: ignore[arg-type]

    assert page is context.pages[0]
    assert context.created == 1


# ---------------------------------------------------------------------------
# open_context
# ---------------------------------------------------------------------------


class _FakeBrowserContext:
    """Minimal BrowserContext for open_context tests."""

    def __init__(self) -> None:
        self.pages: list[Any] = []
        self.default_timeout: int | None = None
        self.closed = False

    def set_default_timeout(self, value: int) -> None:
        self.default_timeout = value

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    """Records launch options and returns (or raises) on demand."""

    def __init__(
        self, *, context: _FakeBrowserContext | None = None, error: Exception | None = None
    ) -> None:
        self.context = context
        self.error = error
        self.user_data_dir: str | None = None
        self.kwargs: dict[str, Any] | None = None

    async def launch_persistent_context(
        self, user_data_dir: str, **kwargs: Any
    ) -> _FakeBrowserContext:
        self.user_data_dir = user_data_dir
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        assert self.context is not None
        return self.context


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium


class _FakeXvfb:
    """Stand-in for XvfbServer so no X server is needed in unit tests."""

    def __init__(self) -> None:
        self.display = ":99"
        self.entered = False
        self.exited = False

    def __enter__(self) -> _FakeXvfb:
        self.entered = True
        return self

    def __exit__(self, *args: object) -> None:
        self.exited = True


async def test_open_context_launches_and_always_closes(
    tmp_home: XPosterConfig, tmp_path: Path
) -> None:
    config = _login_config(tmp_home, tmp_path)
    context = _FakeBrowserContext()
    chromium = _FakeChromium(context=context)

    async with open_context(_FakePlaywright(chromium), config) as opened:  # type: ignore[arg-type]
        assert opened is context
        assert context.default_timeout == config.default_timeout_ms

    assert context.closed is True
    assert chromium.user_data_dir == str(config.profile_dir)
    assert chromium.kwargs is not None
    assert chromium.kwargs["executable_path"] == str(config.chrome_path)
    assert chromium.kwargs["headless"] is True


async def test_open_context_wraps_a_profile_lock_error(
    tmp_home: XPosterConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _login_config(tmp_home, tmp_path)
    error = PlaywrightError("ProcessSingleton: profile appears to be in use")
    version_calls: list[str] = []
    monkeypatch.setattr(
        browser,
        "chrome_version",
        lambda path: version_calls.append(path) or "156.0.8078.80",
    )

    with pytest.raises(
        BrowserUnavailableError, match="another Chrome is using the profile"
    ) as exc_info:
        async with open_context(  # type: ignore[arg-type]
            _FakePlaywright(_FakeChromium(error=error)), config
        ):
            pass

    message = str(exc_info.value)
    assert "pip install -U playwright" not in message
    assert "Detected Chrome" not in message
    assert version_calls == []


async def test_open_context_wraps_a_missing_binary_error(
    tmp_home: XPosterConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _login_config(tmp_home, tmp_path)
    error = PlaywrightError("Executable doesn't exist at /missing/chrome")
    monkeypatch.setattr(browser, "chrome_version", lambda path: "156.0.8078.80")

    with pytest.raises(BrowserUnavailableError) as exc_info:
        async with open_context(  # type: ignore[arg-type]
            _FakePlaywright(_FakeChromium(error=error)), config
        ):
            pass

    message = str(exc_info.value)
    assert "Install Google Chrome" in message
    assert "Detected Chrome 156.0.8078.80" in message
    assert "pip install -U playwright" in message
    assert "otherwise close other Chrome windows using this profile" in message


async def test_open_context_launch_error_names_version_and_update_hint(
    tmp_home: XPosterConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _login_config(tmp_home, tmp_path)
    error = PlaywrightError("Failed to launch the browser process: crashed")
    monkeypatch.setattr(browser, "chrome_version", lambda path: "156.0.8078.80")

    with pytest.raises(BrowserUnavailableError) as exc_info:
        async with open_context(  # type: ignore[arg-type]
            _FakePlaywright(_FakeChromium(error=error)), config
        ):
            pass

    message = str(exc_info.value)
    assert "Detected Chrome 156.0.8078.80" in message
    assert (
        "If Chrome auto-updated, update Playwright (`pip install -U playwright`); "
        "otherwise close other Chrome windows using this profile." in message
    )


async def test_open_context_launch_error_omits_an_unknown_version(
    tmp_home: XPosterConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _login_config(tmp_home, tmp_path)
    error = PlaywrightError("Failed to launch the browser process: crashed")
    monkeypatch.setattr(browser, "chrome_version", lambda path: None)

    with pytest.raises(BrowserUnavailableError) as exc_info:
        async with open_context(  # type: ignore[arg-type]
            _FakePlaywright(_FakeChromium(error=error)), config
        ):
            pass

    message = str(exc_info.value)
    assert "Detected Chrome" not in message
    assert "pip install -U playwright" in message


async def test_open_context_sets_display_for_xvfb(
    tmp_home: XPosterConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _login_config(tmp_home, tmp_path)
    context = _FakeBrowserContext()
    chromium = _FakeChromium(context=context)
    xvfb = _FakeXvfb()
    monkeypatch.setattr(browser, "XvfbServer", lambda: xvfb)

    async with open_context(  # type: ignore[arg-type]
        _FakePlaywright(chromium), config, display=Display.XVFB
    ):
        pass

    assert xvfb.entered is True
    assert xvfb.exited is True
    assert context.closed is True
    assert chromium.kwargs is not None
    assert chromium.kwargs["env"]["DISPLAY"] == ":99"
    assert chromium.kwargs["headless"] is False


# ---------------------------------------------------------------------------
# PlainLogin
# ---------------------------------------------------------------------------


def _login_config(tmp_home: XPosterConfig, tmp_path: Path) -> XPosterConfig:
    chrome = _executable(tmp_path / "chrome")
    return tmp_home.with_overrides(chrome_path=str(chrome))


def test_plain_login_command_is_an_ordinary_chrome(tmp_home: XPosterConfig, tmp_path: Path) -> None:
    config = _login_config(tmp_home, tmp_path)

    command = PlainLogin(config).command()

    assert command == [
        str(config.chrome_path),
        f"--user-data-dir={config.profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--use-mock-keychain",
        "--password-store=basic",
        "--window-size=1100,900",
        "https://x.com/i/flow/login",
    ]
    joined = " ".join(command)
    assert "--remote-debugging" not in joined
    assert "--enable-automation" not in joined
    assert "--headless" not in joined
    assert "--disable-blink-features" not in joined
    assert "--user-agent" not in joined


def test_plain_login_command_accepts_a_custom_url(tmp_home: XPosterConfig, tmp_path: Path) -> None:
    config = _login_config(tmp_home, tmp_path)

    command = PlainLogin(config, url="https://x.com/home").command()

    assert command[-1] == "https://x.com/home"


class _ExitingProcess:
    """Fake Chrome that exits by itself."""

    def __init__(self, code: int = 0) -> None:
        self.returncode = code
        self.terminated = False
        self.killed = False
        self.wait_timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class _GracefulOnTerminate:
    """Fake Chrome that ignores the first wait and exits after SIGTERM."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.returncode is not None:
            return self.returncode
        raise subprocess.TimeoutExpired("chrome", timeout)

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _StubbornProcess:
    """Fake Chrome that only exits after SIGKILL."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.returncode is not None:
            return self.returncode
        raise subprocess.TimeoutExpired("chrome", timeout)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_plain_login_run_returns_the_process_exit_code(
    tmp_home: XPosterConfig, tmp_path: Path
) -> None:
    config = _login_config(tmp_home, tmp_path)
    process = _ExitingProcess(code=0)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_popen(args: list[str], **kwargs: Any) -> _ExitingProcess:
        calls.append((args, kwargs))
        return process

    statuses: list[str] = []
    code = PlainLogin(config).run(timeout_s=5.0, on_status=statuses.append, popen=fake_popen)

    assert code == 0
    assert calls == [
        (
            PlainLogin(config).command(),
            {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL},
        )
    ]
    assert any("QUIT" in status for status in statuses)
    assert stat.S_IMODE(config.profile_dir.stat().st_mode) == 0o700


def test_plain_login_run_terminates_on_timeout(tmp_home: XPosterConfig, tmp_path: Path) -> None:
    config = _login_config(tmp_home, tmp_path)
    process = _GracefulOnTerminate()
    statuses: list[str] = []

    code = PlainLogin(config).run(
        timeout_s=0.01,
        on_status=statuses.append,
        popen=lambda *args, **kwargs: process,
    )

    assert code == 0
    assert process.terminated is True
    assert process.killed is False
    assert process.wait_timeouts == [0.01, 20.0]
    assert any("Still waiting" in status for status in statuses)


def test_plain_login_run_kills_a_process_that_ignores_sigterm(
    tmp_home: XPosterConfig, tmp_path: Path
) -> None:
    config = _login_config(tmp_home, tmp_path)
    process = _StubbornProcess()

    code = PlainLogin(config).run(
        timeout_s=0.01,
        popen=lambda *args, **kwargs: process,
    )

    assert code == -9
    assert process.terminated is True
    assert process.killed is True
    assert process.wait_timeouts == [0.01, 20.0, 10.0]


def test_plain_login_refuses_a_live_profile_lock(
    tmp_home: XPosterConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _login_config(tmp_home, tmp_path)
    _write_chrome_lock(config.profile_dir, os.getpid())
    monkeypatch.setattr(
        browser.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout="Google Chrome --user-data-dir=/profile", stderr=""
        ),
    )
    spawned = False

    def fake_popen(*args: Any, **kwargs: Any) -> _ExitingProcess:
        nonlocal spawned
        spawned = True
        return _ExitingProcess()

    with pytest.raises(BrowserUnavailableError, match="another Chrome is using the profile"):
        PlainLogin(config).run(popen=fake_popen)

    assert spawned is False


def test_plain_login_ignores_a_stale_profile_lock(
    tmp_home: XPosterConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _login_config(tmp_home, tmp_path)
    config.profile_dir.mkdir(parents=True)
    (config.profile_dir / "SingletonLock").symlink_to(f"{socket.gethostname()}-999999")
    monkeypatch.setattr(browser, "_pid_alive", lambda pid: False)

    code = PlainLogin(config).run(timeout_s=5.0, popen=lambda *args, **kwargs: _ExitingProcess())

    assert code == 0


def test_plain_login_ignores_a_malformed_lock(tmp_home: XPosterConfig, tmp_path: Path) -> None:
    config = _login_config(tmp_home, tmp_path)
    config.profile_dir.mkdir(parents=True)
    (config.profile_dir / "SingletonLock").write_text("not a symlink")

    code = PlainLogin(config).run(timeout_s=5.0, popen=lambda *args, **kwargs: _ExitingProcess())

    assert code == 0


def test_plain_login_wraps_spawn_failures(tmp_home: XPosterConfig, tmp_path: Path) -> None:
    config = _login_config(tmp_home, tmp_path)

    def broken_popen(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("no such executable")

    with pytest.raises(BrowserUnavailableError, match="could not be started"):
        PlainLogin(config).run(popen=broken_popen)


# ---------------------------------------------------------------------------
# collect_tells / assess_tells
# ---------------------------------------------------------------------------


async def test_collect_tells_returns_the_evaluated_mapping() -> None:
    payload = {"webdriver": False, "userAgent": REAL_UA}

    class FakePage:
        async def evaluate(self, script: str) -> dict[str, Any]:
            assert "navigator.webdriver" in script
            assert "WEBGL_debug_renderer_info" in script
            return payload

    assert await collect_tells(FakePage()) == payload  # type: ignore[arg-type]


def _clean_tells(**overrides: Any) -> dict[str, Any]:
    """A full, healthy tell set matching collect_tells' shape."""
    raw: dict[str, Any] = {
        "webdriver": False,
        "userAgent": REAL_UA,
        "headlessToken": False,
        "uaDataBrands": ["Chromium/156", "Google Chrome/156", "Not_A Brand/24"],
        "plugins": 5,
        "languages": ["en-GB", "en-US", "en"],
        "platform": "MacIntel",
        "hardwareConcurrency": 18,
        "chromeObject": True,
        "outerWidth": 1440,
        "outerHeight": 900,
        "innerWidth": 1440,
        "innerHeight": 900,
        "screenWidth": 1440,
        "screenHeight": 900,
        "devicePixelRatio": 1,
        "timezone": "Asia/Calcutta",
        "timezoneOffsetMinutes": -330,
        "notificationPermission": "default",
        "webglRenderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M5 Pro, Unspecified Version)",
        "webglSoftware": False,
        "visibilityState": "visible",
        "hasFocus": True,
    }
    raw.update(overrides)
    return raw


def test_assess_tells_clean_browser_is_all_ok() -> None:
    tells = assess_tells(_clean_tells(), Display.XVFB)

    assert tells
    assert {tell.risk for tell in tells} == {"ok"}
    assert all(isinstance(tell, Tell) for tell in tells)
    assert all(isinstance(tell.value, str) for tell in tells)
    assert len({tell.name for tell in tells}) == len(tells)


def test_assess_tells_flags_webdriver() -> None:
    tells = assess_tells(_clean_tells(webdriver=True), Display.XVFB)

    tell = _by_name(tells, "navigator.webdriver")
    assert tell.risk == "warn"
    assert "automation" in tell.explanation


@pytest.mark.parametrize(
    ("display", "expected_risk"),
    [
        pytest.param(Display.HEADLESS, "warn", id="headless"),
        pytest.param(Display.AUTO, "warn", id="auto"),
        pytest.param(Display.XVFB, "ok", id="xvfb"),
        pytest.param(Display.VISIBLE, "ok", id="visible"),
    ],
)
def test_assess_tells_headless_token_depends_on_display(
    display: Display, expected_risk: str
) -> None:
    tells = assess_tells(_clean_tells(headlessToken=True), display)

    tell = _by_name(tells, "userAgent")
    assert tell.risk == expected_risk
    if expected_risk == "warn":
        assert tell.explanation == (
            "truthful, but X's web client explicitly classifies HeadlessChrome user agents "
            "as bot-like (see docs/research/web-client.md); prefer `--display visible` for "
            "the first runs and Xvfb on Linux"
        )


def test_assess_tells_detects_headless_token_without_the_boolean() -> None:
    tells = assess_tells(_clean_tells(headlessToken=False, userAgent=HEADLESS_UA), Display.HEADLESS)

    assert _by_name(tells, "userAgent").risk == "warn"


@pytest.mark.parametrize("renderer", ["Google SwiftShader", "llvmpipe (LLVM 15.0.7, 256 bits)"])
def test_assess_tells_notes_software_webgl(renderer: str) -> None:
    tells = assess_tells(_clean_tells(webglRenderer=renderer), Display.VISIBLE)

    tell = _by_name(tells, "WebGL unmasked renderer")
    assert tell.risk == "note"
    assert tell.value == renderer


def test_assess_tells_honours_the_software_webgl_flag() -> None:
    tells = assess_tells(_clean_tells(webglRenderer="", webglSoftware=True), Display.HEADLESS)

    assert _by_name(tells, "WebGL unmasked renderer").risk == "note"


def test_assess_tells_formats_the_timezone_offset() -> None:
    tells = assess_tells(
        _clean_tells(timezone="Asia/Calcutta", timezoneOffsetMinutes=-330), Display.VISIBLE
    )

    assert _by_name(tells, "timezone").value == "Asia/Calcutta (UTC+05:30)"


def test_assess_tells_notes_a_tiny_screen() -> None:
    tells = assess_tells(_clean_tells(screenWidth=800, screenHeight=600), Display.HEADLESS)

    tell = _by_name(tells, "screen")
    assert tell.risk == "note"
    assert tell.value.startswith("800x600")


@pytest.mark.parametrize("languages", [[], None])
def test_assess_tells_warns_about_missing_languages(languages: Any) -> None:
    tells = assess_tells(_clean_tells(languages=languages), Display.VISIBLE)

    assert _by_name(tells, "navigator.languages").risk == "warn"


def test_assess_tells_warns_about_a_hidden_page() -> None:
    tells = assess_tells(_clean_tells(visibilityState="hidden"), Display.VISIBLE)

    assert _by_name(tells, "document.visibilityState").risk == "warn"


def test_assess_tells_marks_only_the_failing_checks() -> None:
    raw = _clean_tells(
        webdriver=True,
        languages=[],
        visibilityState="hidden",
        screenWidth=640,
        screenHeight=480,
    )

    tells = assess_tells(raw, Display.HEADLESS)
    non_ok = {tell.name for tell in tells if tell.risk != "ok"}

    assert non_ok == {
        "navigator.webdriver",
        "navigator.languages",
        "document.visibilityState",
        "screen",
    }


def test_assess_tells_is_pure() -> None:
    raw = _clean_tells(webdriver=True)
    before = dict(raw)

    assess_tells(raw, Display.HEADLESS)

    assert raw == before
