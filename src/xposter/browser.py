"""Chrome discovery, launch options and the one-time human login.

X is sensitive to automation fingerprints, so this module deliberately does the
opposite of stealth tooling: it launches the user's real Google Chrome (never
Playwright's bundled Chromium), removes only the one flag Playwright adds that
makes ``navigator.webdriver`` true, and changes nothing else about the browser.
A real Chrome with a real profile is both more truthful and less fragile than a
patched one.

The second job is the one-time login. The most scrutinised moment is a human
signing in, so that moment must not involve a CDP-controlled browser at all:
:class:`PlainLogin` starts an ordinary, headed Chrome, the user signs in and
quits it, and only afterwards do we reopen the same profile through Playwright.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import ExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

from .config import Display, XPosterConfig
from .errors import BrowserUnavailableError, ConfigurationError

# Policy marker for reviewers and the mechanical test in tests/test_no_injection.py:
# this package only calls page/locator.evaluate for read-only diagnostics and
# hit-testing; it never injects scripts, init scripts, headers or overrides.
READ_ONLY_PAGE_EVALUATE = True

# Chrome locations are ordered best-first. Tests replace these tuples so they
# can exercise the lookup without depending on what is installed on the host.
_MAC_CHROME_PATHS: tuple[str, ...] = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
_LINUX_CHROME_NAMES: tuple[str, ...] = ("google-chrome-stable", "google-chrome", "chrome")
_LINUX_CHROME_PATHS: tuple[str, ...] = ("/opt/google/chrome/chrome",)
_WINDOWS_CHROME_PATHS: tuple[str, ...] = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)

_X11_SOCKET_DIR = Path("/tmp/.X11-unix")
_INSTALL_HINT = (
    "Install Google Chrome from https://www.google.com/chrome/, or point "
    "XPOSTER_CHROME_PATH at the Google Chrome executable. xposter never falls "
    "back to Playwright's bundled Chromium: its fingerprint is not a real Chrome."
)
_PROFILE_BUSY = "another Chrome is using the profile; close it or wait for the other xposter run"
_PLAYWRIGHT_UPDATE_HINT = (
    "If Chrome auto-updated, update Playwright (`pip install -U playwright`); "
    "otherwise close other Chrome windows using this profile."
)

# Headless Chrome reports a small default screen and is still a useful fallback,
# but a normal desktop window keeps the window/screen numbers internally
# consistent with a real desktop browser. The headed path keeps the user's real
# window size instead, so no viewport override is passed there.
_HEADLESS_VIEWPORT = {"width": 1440, "height": 900}
_WINDOW_ARGS = ("--no-first-run", "--no-default-browser-check")
_BLINK_ARG = "--disable-blink-features=AutomationControlled"
_WINDOW_SIZE_ARG = "--window-size=1440,900"


def _platform_name() -> str:
    """Return the running platform; isolated so tests can substitute one."""
    return sys.platform


def _which(name: str) -> str | None:
    """Resolve an executable name; isolated so tests need not touch PATH."""
    return shutil.which(name)


def _normalise_platform(raw: str) -> str:
    """Collapse platform spellings to ``darwin``, ``windows`` or ``linux``."""
    lowered = raw.strip().lower()
    if lowered.startswith("darwin") or lowered.startswith("mac"):
        return "darwin"
    if lowered.startswith("win"):
        return "windows"
    if lowered.startswith("linux"):
        return "linux"
    return lowered


def _is_executable(path: Path) -> bool:
    """True only for a real file the current user may execute."""
    return path.is_file() and os.access(path, os.X_OK)


def _find_chrome_in(system: str, which: Callable[[str], str | None]) -> str | None:
    """Search the platform's usual locations; ``None`` means no real Chrome."""
    if system == "darwin":
        for raw in _MAC_CHROME_PATHS:
            candidate = Path(raw).expanduser()
            if _is_executable(candidate):
                return str(candidate)
        return None

    if system == "linux":
        for name in _LINUX_CHROME_NAMES:
            found = which(name)
            if found and _is_executable(Path(found)):
                return found
        for raw in _LINUX_CHROME_PATHS:
            candidate = Path(raw)
            if _is_executable(candidate):
                return str(candidate)
        return None

    if system == "windows":
        for raw in _WINDOWS_CHROME_PATHS:
            candidate = Path(os.path.expandvars(raw)).expanduser()
            if _is_executable(candidate):
                return str(candidate)
        return None

    return None


def find_chrome(config: XPosterConfig) -> str | None:
    """Locate a real Google Chrome executable, or return ``None``.

    An explicit ``chrome_path`` is a promise, so a bad value is a configuration
    error rather than a silent fallback; an empty result lets the CLI explain
    how to install Chrome.
    """
    if config.chrome_path is not None:
        candidate = Path(config.chrome_path).expanduser()
        if not _is_executable(candidate):
            raise ConfigurationError(
                f"chrome_path {config.chrome_path!r} is not an executable file; "
                f"fix XPOSTER_CHROME_PATH or chrome_path. {_INSTALL_HINT}"
            )
        return str(candidate)

    return _find_chrome_in(_normalise_platform(_platform_name()), _which)


def require_chrome(config: XPosterConfig) -> str:
    """Return a Chrome path or fail with an actionable install message."""
    path = find_chrome(config)
    if path is None:
        raise BrowserUnavailableError(f"Google Chrome was not found. {_INSTALL_HINT}")
    return path


_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+(?:\.\d+)?)\b")


def chrome_version(path: str) -> str | None:
    """Parse ``chrome --version``; ``None`` when the binary cannot answer.

    The version is diagnostic only, so every failure is downgraded instead of
    raising: a working login with a missing version report is still useful.
    """
    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VERSION_RE.search(f"{completed.stdout}\n{completed.stderr}")
    return match.group(1) if match else None


def _has_linux_display(env: Mapping[str, str]) -> bool:
    """True when a Linux desktop (X11 or Wayland) can show a window."""
    return bool(env.get("DISPLAY", "").strip() or env.get("WAYLAND_DISPLAY", "").strip())


def resolve_display(
    config: XPosterConfig,
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Display:
    """Pick the display mode, validating what the host can actually support.

    ``AUTO`` prefers Xvfb on Linux because Xvfb renders a real headed Chrome
    into a virtual display: no headless marker at all. macOS and Windows have
    no equivalent, so headless is the unattended default there.
    """
    system = _normalise_platform(_platform_name() if platform is None else platform)
    environment: Mapping[str, str] = os.environ if env is None else env
    requested = config.display

    if requested == Display.HEADLESS:
        return Display.HEADLESS

    if requested == Display.XVFB:
        if which("Xvfb") is None:
            raise BrowserUnavailableError(
                "XPOSTER_DISPLAY=xvfb was requested but Xvfb is not on PATH; "
                "install it (for example `sudo apt install xvfb`) or choose "
                "XPOSTER_DISPLAY=headless."
            )
        return Display.XVFB

    if requested == Display.VISIBLE:
        if system == "linux" and not _has_linux_display(environment):
            raise BrowserUnavailableError(
                "XPOSTER_DISPLAY=visible needs a desktop session, but neither "
                "DISPLAY nor WAYLAND_DISPLAY is set on this Linux host; run "
                "xposter inside the desktop session or use XPOSTER_DISPLAY=xvfb."
            )
        return Display.VISIBLE

    if system == "linux":
        return Display.XVFB if which("Xvfb") is not None else Display.HEADLESS
    return Display.HEADLESS


def _running_as_root() -> bool:
    """True for uid 0; isolated so tests can simulate a root Linux container."""
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid is not None and geteuid() == 0)


def build_launch_kwargs(
    config: XPosterConfig, display: Display, chrome_path: str
) -> dict[str, Any]:
    """Build the keyword arguments for ``launch_persistent_context``.

    This function is pure and contains every launch decision, so the honest-vs-
    detectable trade-offs are reviewable in one place. Playwright's useful
    defaults (notably ``--use-mock-keychain`` and ``--password-store=basic``)
    are intentionally left in place; only the automation flag is removed.
    """
    # AUTO should have been resolved before launching; treating it as headless
    # is the safe unattended interpretation if a caller forgets.
    headless = display in (Display.HEADLESS, Display.AUTO)

    args = [*_WINDOW_ARGS, _BLINK_ARG, _WINDOW_SIZE_ARG]
    if _normalise_platform(_platform_name()) == "linux" and _running_as_root():
        # Chrome refuses to start without a sandbox as root; a container running
        # as root has no usable sandbox, so the flag is required rather than a
        # convenience.
        args.append("--no-sandbox")

    kwargs: dict[str, Any] = {
        "executable_path": chrome_path,
        "headless": headless,
        "ignore_default_args": ["--enable-automation"],
        "args": args,
        "accept_downloads": False,
        "timeout": config.default_timeout_ms,
    }
    if headless:
        kwargs["viewport"] = dict(_HEADLESS_VIEWPORT)
    else:
        kwargs["no_viewport"] = True
    return kwargs


def _display_number_free(number: int) -> bool:
    """True when no X server owns ``:number`` (socket or lock file)."""
    return (
        not (_X11_SOCKET_DIR / f"X{number}").exists() and not Path(f"/tmp/.X{number}-lock").exists()
    )


def _free_display_number() -> int:
    """Find an unused display number, starting above the usual physical ones."""
    for number in range(99, 199):
        if _display_number_free(number):
            return number
    raise BrowserUnavailableError(
        "no free X display number between :99 and :198; stop stray Xvfb servers "
        "or use XPOSTER_DISPLAY=headless."
    )


class XvfbServer:
    """Start a virtual X display for real, headed Chrome on Linux.

    Xvfb is used synchronously from async code; startup is short and the only
    blocking wait is bounded by ``timeout_s``.
    """

    def __init__(
        self,
        *,
        executable: str | None = None,
        timeout_s: float = 10.0,
        screen: str = "1920x1080x24",
    ) -> None:
        self._executable = executable
        self._timeout_s = timeout_s
        self._screen = screen
        self._process: subprocess.Popen[bytes] | None = None
        self.display = ""

    def __enter__(self) -> XvfbServer:
        executable = self._executable or _which("Xvfb")
        if executable is None:
            raise BrowserUnavailableError(
                "Xvfb was not found; install it (for example `sudo apt install "
                "xvfb`) or use XPOSTER_DISPLAY=headless."
            )

        number = _free_display_number()
        self.display = f":{number}"
        command = [
            executable,
            self.display,
            "-screen",
            "0",
            self._screen,
            "-nolisten",
            "tcp",
        ]
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise BrowserUnavailableError(
                f"Xvfb at {executable!r} could not be started: {exc}. Install "
                "Xvfb or use XPOSTER_DISPLAY=headless."
            ) from exc

        socket = _X11_SOCKET_DIR / f"X{number}"
        deadline = time.monotonic() + self._timeout_s
        while not socket.exists():
            if self._process.poll() is not None:
                code = self._process.returncode
                self._process = None
                raise BrowserUnavailableError(
                    f"Xvfb {self.display} exited immediately (code {code}); check "
                    "the Xvfb installation or use XPOSTER_DISPLAY=headless."
                )
            if time.monotonic() > deadline:
                self._stop()
                raise BrowserUnavailableError(
                    f"Xvfb {self.display} did not create {socket} within "
                    f"{self._timeout_s:.0f}s; stop stray servers or use "
                    "XPOSTER_DISPLAY=headless."
                )
            time.sleep(0.02)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stop()

    def _stop(self) -> None:
        """Terminate Xvfb, escalating only when it ignores SIGTERM."""
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5.0)


def _singleton_pid(profile_dir: Path) -> int | None:
    """Read the pid encoded in Chrome's ``SingletonLock`` symlink, if any.

    Chrome writes the link target as ``<hostname>-<pid>``; anything unreadable
    or malformed is treated as stale and left for Chrome's own recovery.
    """
    lock = profile_dir / "SingletonLock"
    try:
        target = os.readlink(lock)
    except OSError:
        return None
    _, separator, raw_pid = target.rpartition("-")
    if not separator:
        return None
    try:
        return int(raw_pid)
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    """True when a process with ``pid`` exists (permission means it exists)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_looks_like_chrome(pid: int) -> bool:
    """Verify that a live lock pid really belongs to Chrome.

    A stale ``SingletonLock`` can point at a recycled pid, which would make an
    otherwise free profile look busy forever. ``ps`` is the cheapest portable
    check; when it is unavailable or fails we stay conservative (busy) rather
    than risk two Chrome processes sharing one profile.
    """
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm=,args="],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if completed.returncode != 0:
        return True
    haystack = f"{completed.stdout}\n{completed.stderr}".lower()
    return "chrome" in haystack or "chromium" in haystack


def _profile_in_use(profile_dir: Path) -> bool:
    """True when a live Chrome owns the profile's process singleton."""
    pid = _singleton_pid(profile_dir)
    if pid is None or not _pid_alive(pid):
        return False
    return _pid_looks_like_chrome(pid)


def _profile_busy_error(profile_dir: Path) -> BrowserUnavailableError:
    """Operator-facing explanation of a locked Chrome profile."""
    return BrowserUnavailableError(f"{_PROFILE_BUSY} ({profile_dir}).")


def _launch_error(
    exc: BaseException, config: XPosterConfig, chrome_path: str
) -> BrowserUnavailableError:
    """Translate Playwright launch failures into actionable browser errors."""
    text = str(exc)
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in (
            "singleton",
            "profile appears to be in use",
            "already in use",
            "cannot launch using this profile",
        )
    ):
        return _profile_busy_error(config.profile_dir)

    # Outside the profile-busy case the common cause is a Chrome/Playwright
    # mismatch after an auto-update, so name the installed version when the
    # binary can still answer `--version`.
    version = chrome_version(chrome_path)
    version_note = f"Detected Chrome {version}. " if version else ""

    if (
        isinstance(exc, FileNotFoundError)
        or "enoent" in lowered
        or ("executable" in lowered and "exist" in lowered)
    ):
        return BrowserUnavailableError(
            f"Chrome at {chrome_path} could not be started: {text}. "
            f"{_INSTALL_HINT} {version_note}{_PLAYWRIGHT_UPDATE_HINT}"
        )
    return BrowserUnavailableError(
        f"Chrome at {chrome_path} failed to start with profile "
        f"{config.profile_dir}: {text}. {version_note}{_PLAYWRIGHT_UPDATE_HINT}"
    )


@asynccontextmanager
async def open_context(
    playwright: Playwright,
    config: XPosterConfig,
    *,
    display: Display | None = None,
) -> AsyncIterator[BrowserContext]:
    """Open the persistent Chrome profile and always shut it down again.

    Closing gracefully matters beyond hygiene: Chrome flushes cookies to disk
    on quit, so the saved login only survives if this context (and the plain
    login window) exit normally.
    """
    resolved = resolve_display(config) if display is None else display
    chrome_path = require_chrome(config)

    profile_dir = config.profile_dir
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with suppress(OSError):
        profile_dir.chmod(0o700)

    if _profile_in_use(profile_dir):
        raise _profile_busy_error(profile_dir)

    kwargs = build_launch_kwargs(config, resolved, chrome_path)

    with ExitStack() as stack:
        if resolved == Display.XVFB:
            server = stack.enter_context(XvfbServer())
            # Playwright's env replaces the browser environment, so start from
            # the current one and add the virtual display.
            kwargs["env"] = {**os.environ, "DISPLAY": server.display}

        try:
            context = await playwright.chromium.launch_persistent_context(
                str(profile_dir), **kwargs
            )
        except (PlaywrightError, OSError) as exc:
            raise _launch_error(exc, config, chrome_path) from exc

        try:
            context.set_default_timeout(config.default_timeout_ms)
            yield context
        finally:
            # A browser crash makes close() fail; that must not hide the real
            # error path that led here.
            with suppress(PlaywrightError):
                await context.close()


async def new_page(context: BrowserContext) -> Page:
    """Return the context's first page, creating one only when none exists.

    A persistent Chrome starts with an ``about:blank`` page; reusing it avoids
    an extra tab that looks unusual and switches focus away from the run.
    """
    pages = context.pages
    if pages:
        return pages[0]
    return await context.new_page()


class PlainLogin:
    """Run the one-time, human-driven sign-in in an ordinary Chrome.

    No Playwright, no CDP and no automation flags touch this window: during
    login X sees exactly what it would see for any user starting Chrome. The
    process must exit normally (the user quits Chrome) so cookies reach disk
    before Playwright reopens the profile.
    """

    def __init__(self, config: XPosterConfig, *, url: str = "https://x.com/i/flow/login") -> None:
        self._config = config
        self._url = url

    def command(self) -> list[str]:
        """The exact Chrome argv for the plain login window."""
        chrome = require_chrome(self._config)
        return [
            chrome,
            f"--user-data-dir={self._config.profile_dir.expanduser()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--use-mock-keychain",
            "--password-store=basic",
            "--window-size=1100,900",
            self._url,
        ]

    def run(
        self,
        *,
        timeout_s: float = 900.0,
        on_status: Callable[[str], None] | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> int:
        """Spawn Chrome, wait for the user to quit it, and return its exit code.

        The timeout is deliberately generous because a human may need a 2FA
        code. On timeout Chrome is asked to quit normally first: SIGKILL would
        discard the cookie jar we are trying to save.
        """
        profile_dir = self._config.profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            profile_dir.chmod(0o700)

        if _profile_in_use(profile_dir):
            raise _profile_busy_error(profile_dir)

        def status(message: str) -> None:
            if on_status is not None:
                on_status(message)

        status(
            "Sign in to X in the Chrome window that just opened. When you are "
            "signed in, QUIT Chrome completely (Cmd+Q on macOS, or close every "
            "Chrome window) so the login cookies are flushed to disk; do not "
            "just close the tab."
        )

        try:
            process = popen(
                self.command(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise BrowserUnavailableError(
                f"Chrome could not be started for the login: {exc}. {_INSTALL_HINT}"
            ) from exc

        try:
            return int(process.wait(timeout=timeout_s))
        except subprocess.TimeoutExpired:
            status(
                f"Still waiting after {timeout_s:.0f}s; asking Chrome to quit so "
                "cookies are flushed, then stopping it if it does not."
            )
            process.terminate()
            try:
                return int(process.wait(timeout=20.0))
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    return int(process.wait(timeout=10.0))
                except subprocess.TimeoutExpired:
                    return -1


@dataclass(frozen=True, slots=True)
class Tell:
    """One browser property a website can observe, with a risk assessment."""

    name: str
    value: str
    risk: str
    explanation: str


_TELLS_SCRIPT = """
() => {
  const uaData = (window.isSecureContext && navigator.userAgentData)
    ? navigator.userAgentData
    : null;
  let webgl = null;
  try {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
    if (gl) {
      const ext = gl.getExtension("WEBGL_debug_renderer_info");
      webgl = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    }
  } catch (err) {
    webgl = null;
  }
  return {
    webdriver: navigator.webdriver === true,
    userAgent: navigator.userAgent,
    headlessToken: /HeadlessChrome/.test(navigator.userAgent),
    uaDataBrands: uaData
      ? Array.from(uaData.brands).map((brand) => `${brand.brand}/${brand.version}`)
      : null,
    plugins: navigator.plugins ? navigator.plugins.length : 0,
    languages: Array.from(navigator.languages || []),
    platform: navigator.platform || "",
    hardwareConcurrency: navigator.hardwareConcurrency || 0,
    chromeObject: typeof window.chrome === "object" && window.chrome !== null,
    outerWidth: window.outerWidth,
    outerHeight: window.outerHeight,
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    screenWidth: window.screen ? window.screen.width : 0,
    screenHeight: window.screen ? window.screen.height : 0,
    devicePixelRatio: window.devicePixelRatio || 0,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    timezoneOffsetMinutes: new Date().getTimezoneOffset(),
    notificationPermission: (typeof Notification !== "undefined")
      ? Notification.permission
      : "unsupported",
    webglRenderer: webgl,
    webglSoftware: /swiftshader|llvmpipe/i.test(webgl || ""),
    visibilityState: document.visibilityState,
    hasFocus: document.hasFocus(),
  };
}
"""


async def collect_tells(page: Page) -> dict[str, Any]:
    """Read what the current page can observe about the browser.

    Everything here is a single read-only evaluate: no patching, no property
    overrides. The point is to measure the honest browser we actually ship.
    """
    raw = await page.evaluate(_TELLS_SCRIPT)
    return cast(dict[str, Any], raw)


def _show(value: object) -> str:
    """Render a collected value for display in diagnostic tables."""
    if value is None:
        return "unavailable"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) if value else "(empty)"
    return str(value)


def _as_int(value: object) -> int:
    """Best-effort integer for values that may be missing or stringly typed."""
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return 0


def _utc_offset(minutes: object) -> str:
    """Render JavaScript's ``getTimezoneOffset`` as a familiar UTC offset."""
    offset = _as_int(minutes)
    # JavaScript reports UTC minus local time, so UTC+05:30 arrives as -330.
    sign = "+" if offset <= 0 else "-"
    absolute = abs(offset)
    return f"UTC{sign}{absolute // 60:02d}:{absolute % 60:02d}"


_HEADLESS_UA_EXPLANATION = (
    "truthful, but X's web client explicitly classifies HeadlessChrome user agents "
    "as bot-like (see docs/research/web-client.md); prefer `--display visible` for "
    "the first runs and Xvfb on Linux"
)


def _headless_explanation(display: Display) -> tuple[str, str]:
    """Risk and wording for a browser that reports the HeadlessChrome marker."""
    if display in (Display.HEADLESS, Display.AUTO):
        return ("warn", _HEADLESS_UA_EXPLANATION)
    return (
        "ok",
        "a HeadlessChrome token is not produced by this display mode; no action needed",
    )


def assess_tells(raw: Mapping[str, Any], display: Display) -> list[Tell]:
    """Turn collected browser facts into an operator-readable risk table.

    This is pure and never suggests spoofing: the fix for a warning is a
    different launch mode (headed or Xvfb), not a JavaScript patch. Risks are
    ``ok``, ``note`` (expected/explainable) and ``warn`` (fix before posting).
    """
    tells: list[Tell] = []

    webdriver = raw.get("webdriver") is True
    tells.append(
        Tell(
            "navigator.webdriver",
            _show(raw.get("webdriver")),
            "warn" if webdriver else "ok",
            "true is the classic automation flag a site checks first"
            if webdriver
            else "false: Chrome is not advertising automation",
        )
    )

    ua = str(raw.get("userAgent") or "")
    headless = raw.get("headlessToken") is True or "HeadlessChrome" in ua
    if headless:
        risk, explanation = _headless_explanation(display)
    else:
        risk, explanation = "ok", "no HeadlessChrome token; consistent with a real Chrome"
    tells.append(Tell("userAgent", ua or "unavailable", risk, explanation))

    brands = raw.get("uaDataBrands")
    if brands is None:
        tells.append(
            Tell(
                "userAgentData.brands",
                "unavailable (needs a secure context)",
                "ok",
                "UA-CH brands appear on https pages; absence is expected elsewhere",
            )
        )
    else:
        tells.append(
            Tell(
                "userAgentData.brands",
                _show(brands),
                "ok",
                "UA-CH brands; real Chrome reports its own brand set",
            )
        )

    tells.append(
        Tell(
            "navigator.plugins.length",
            _show(raw.get("plugins")),
            "ok",
            "Chrome ships a small fixed plugin list; zero is unusual but not decisive",
        )
    )

    languages = raw.get("languages")
    if isinstance(languages, (list, tuple)) and languages:
        tells.append(
            Tell(
                "navigator.languages",
                _show(languages),
                "ok",
                "non-empty language preferences look like a configured browser",
            )
        )
    else:
        tells.append(
            Tell(
                "navigator.languages",
                _show(languages),
                "warn",
                "an empty language list is not what a normal desktop Chrome reports",
            )
        )

    tells.append(
        Tell(
            "navigator.platform",
            _show(raw.get("platform")),
            "ok",
            "platform string; real Chrome reports the host platform",
        )
    )
    tells.append(
        Tell(
            "navigator.hardwareConcurrency",
            _show(raw.get("hardwareConcurrency")),
            "ok",
            "CPU count; real Chrome reports the host's cores",
        )
    )
    tells.append(
        Tell(
            "window.chrome",
            "present" if raw.get("chromeObject") else "absent",
            "ok",
            "Chrome exposes window.chrome; other browsers do not",
        )
    )

    outer = f"{_show(raw.get('outerWidth'))}x{_show(raw.get('outerHeight'))}"
    inner = f"{_show(raw.get('innerWidth'))}x{_show(raw.get('innerHeight'))}"
    tells.append(
        Tell(
            "window.outer/inner",
            f"{outer} / {inner}",
            "ok",
            "both sizes are reported; equality is expected in headless, where "
            "there is no window chrome to add",
        )
    )

    screen_width = _as_int(raw.get("screenWidth"))
    screen_height = _as_int(raw.get("screenHeight"))
    tiny_screen = screen_width < 1024 or screen_height < 768
    tells.append(
        Tell(
            "screen",
            f"{screen_width}x{screen_height} @{_show(raw.get('devicePixelRatio'))}",
            "note" if tiny_screen else "ok",
            "a tiny virtual screen is typical of headless defaults and unusual on a desktop"
            if tiny_screen
            else "screen size is plausible for a desktop browser",
        )
    )

    offset = raw.get("timezoneOffsetMinutes")
    tells.append(
        Tell(
            "timezone",
            f"{_show(raw.get('timezone'))} ({_utc_offset(offset)})",
            "ok",
            "timezone comes from the host, consistent with the real machine",
        )
    )
    tells.append(
        Tell(
            "Notification.permission",
            _show(raw.get("notificationPermission")),
            "ok",
            "permission state is normal in every value, including default",
        )
    )

    renderer = str(raw.get("webglRenderer") or "")
    renderer_lower = renderer.lower()
    software = raw.get("webglSoftware") is True or (
        "swiftshader" in renderer_lower or "llvmpipe" in renderer_lower
    )
    tells.append(
        Tell(
            "WebGL unmasked renderer",
            renderer or "unavailable",
            "note" if software else "ok",
            "software rasterisation (SwiftShader/llvmpipe) is expected in VMs "
            "and headless, but unusual on desktop hardware"
            if software
            else "hardware renderer string is consistent with the host",
        )
    )

    visibility = str(raw.get("visibilityState") or "unknown")
    tells.append(
        Tell(
            "document.visibilityState",
            visibility,
            "warn" if visibility == "hidden" else "ok",
            "a hidden page is throttled and is not what a foreground browser shows"
            if visibility == "hidden"
            else "visible page; normal for an active browser",
        )
    )
    tells.append(
        Tell(
            "document.hasFocus",
            _show(raw.get("hasFocus")),
            "ok",
            "focus changes with window activation and is not a fingerprint by itself",
        )
    )

    return tells
