"""Filesystem store for the saved X session, parking marker and failure artifacts.

The persistent Chrome profile is the primary session; ``storage_state.json`` is a
backup credential refreshed after each good run. Both are full-account
credentials, so every file written here is 0600 and every directory 0700, and
writes go through a temporary file in the same directory followed by
``os.replace`` so a crash cannot leave a half-written credential for a later run
to load.

Cookie values never appear in a summary, a log line or an exception message
produced by this module: a leaked value is a leaked account.
"""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .config import XPosterConfig
from .errors import ParkedError, SessionBusyError
from .models import SessionSummary

__all__ = ["ParkInfo", "SessionStore", "sanitise_html"]

_X_COOKIE_DOMAINS = frozenset({"x.com", ".x.com", "twitter.com", ".twitter.com"})
_DAY_SECONDS = 86400.0
_LABEL_MAX = 60
_FILENAME_MAX = 120
_REDACTED = "[REDACTED]"

_TOKEN_KEY = r"(?:auth_token|guest_id|ct0|kdt|att)"
_HTML_ATTRS = r"""(?:[^>"']|"[^"]*"|'[^']*')*"""
_HTML_ATTRS_OPEN = r"""(?:[^>"']|"[^"]*"?|'[^']*'?)*"""
_INPUT_TAG_RE = re.compile(rf"<input\b{_HTML_ATTRS}>", re.IGNORECASE)
_INPUT_PREFIX_RE = re.compile(rf"<input\b{_HTML_ATTRS_OPEN}", re.IGNORECASE)
_META_TAG_RE = re.compile(rf"<meta\b{_HTML_ATTRS}>", re.IGNORECASE)
_META_PREFIX_RE = re.compile(rf"<meta\b{_HTML_ATTRS_OPEN}", re.IGNORECASE)
_TYPE_ATTR_RE = re.compile(
    r"""(?<![\w-])type\s*=\s*(?:"([^"]*)"?|'([^']*)'?|([^\s>]*))""", re.IGNORECASE
)
_NAME_ATTR_RE = re.compile(
    r"""(?<![\w-])name\s*=\s*(?:"([^"]*)"?|'([^']*)'?|([^\s>]*))""", re.IGNORECASE
)
_VALUE_ATTR_RE = re.compile(
    r"""(?<![\w-])value\s*=\s*(?:"[^"]*"?|'[^']*'?|[^\s>]*)""", re.IGNORECASE
)
_CONTENT_ATTR_RE = re.compile(
    r"""(?<![\w-])content\s*=\s*(?:"[^"]*"?|'[^']*'?|[^\s>]*)""", re.IGNORECASE
)
_AUTH_KEY = r"(?:[A-Za-z0-9_-]*authorization)"
_AUTH_QUOTED_RE = re.compile(
    rf"""(?P<key>"(?:{_AUTH_KEY})"|'(?:{_AUTH_KEY})')\s*:\s*"""
    r"""(?P<value>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^,}\n<]+)""",
    re.IGNORECASE,
)
_AUTH_BARE_RE = re.compile(
    rf"""(?<![\w-])(?P<key>{_AUTH_KEY})\b\s*[:=]\s*"""
    r"""(?P<value>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\n,;}<]+)""",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\b(Bearer)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_JSON_DOUBLE_RE = re.compile(
    rf'"(?P<key>{_TOKEN_KEY})"\s*:\s*(?P<value>"(?:[^"\\]|\\.)*"|[^,}}\s]+)',
    re.IGNORECASE,
)
_JSON_SINGLE_RE = re.compile(
    rf"'(?P<key>{_TOKEN_KEY})'\s*:\s*(?P<value>'(?:[^'\\]|\\.)*'|[^,}}\s]+)",
    re.IGNORECASE,
)
_BARE_COLON_RE = re.compile(
    rf"""(?<![\w$.-])(?P<key>{_TOKEN_KEY})\s*:\s*"""
    r"""(?P<value>"[^"]*"|'[^']*'|[^\s,;}]+)""",
    re.IGNORECASE,
)
_ASSIGN_RE = re.compile(
    rf"""(?<![\w$.-])(?P<key>{_TOKEN_KEY})\s*=\s*"""
    r"""(?P<value>"[^"]*"|'[^']*'|[^;\s&,}]+)""",
    re.IGNORECASE,
)


def sanitise_html(html: str) -> str:
    """Redact credential material from HTML before it is written as evidence.

    A failure dump of a composed page can contain a CSRF meta tag, values of
    hidden inputs, inline session JSON or an authorization header. Cookie
    values are usually HttpOnly and absent from the DOM, but CSRF tokens are
    not, and an artifact is a plain file on disk. Redaction is deliberately
    regex-based and never raises: evidence collection must not fail because the
    page markup is broken. The output is idempotent so a second pass over an
    already-sanitised file is harmless.
    """
    text = html
    try:
        text = _INPUT_TAG_RE.sub(lambda match: _clean_input_tag(match.group(0)), text)
        text = _INPUT_PREFIX_RE.sub(lambda match: _clean_input_tag(match.group(0)), text)
        text = _META_TAG_RE.sub(lambda match: _clean_meta_tag(match.group(0)), text)
        text = _META_PREFIX_RE.sub(lambda match: _clean_meta_tag(match.group(0)), text)
        text = _AUTH_QUOTED_RE.sub(_redact_auth_quoted, text)
        text = _AUTH_BARE_RE.sub(_redact_auth_bare, text)
        text = _BEARER_RE.sub(_redact_bearer, text)
        text = _JSON_DOUBLE_RE.sub(_redact_json_double, text)
        text = _JSON_SINGLE_RE.sub(_redact_json_single, text)
        text = _BARE_COLON_RE.sub(_redact_bare_colon, text)
        text = _ASSIGN_RE.sub(_redact_assignment, text)
    except Exception:
        return text
    return text


def _attr_value(pattern: re.Pattern[str], tag: str) -> str | None:
    match = pattern.search(tag)
    if match is None:
        return None
    return next((group for group in match.groups() if group is not None), "")


def _clean_input_tag(tag: str) -> str:
    input_type = _attr_value(_TYPE_ATTR_RE, tag)
    if input_type is None:
        return tag
    normalised = input_type.strip().strip("/").lower()
    if normalised not in {"password", "hidden"}:
        return tag
    return _VALUE_ATTR_RE.sub(f'value="{_REDACTED}"', tag)


def _clean_meta_tag(tag: str) -> str:
    name = _attr_value(_NAME_ATTR_RE, tag)
    if name is None:
        return tag
    lowered = name.strip().lower()
    if "token" not in lowered and "csrf" not in lowered:
        return tag
    return _CONTENT_ATTR_RE.sub(f'content="{_REDACTED}"', tag)


def _redact_auth_quoted(match: re.Match[str]) -> str:
    key = match.group("key")
    quote = key[0]
    return f"{key}: {quote}{_REDACTED}{quote}"


def _redact_auth_bare(match: re.Match[str]) -> str:
    return f"{match.group('key')}: {_REDACTED}"


def _redact_bearer(match: re.Match[str]) -> str:
    return f"{match.group(1)} {_REDACTED}"


def _redact_json_double(match: re.Match[str]) -> str:
    return f'"{match.group("key")}":"{_REDACTED}"'


def _redact_json_single(match: re.Match[str]) -> str:
    return f"'{match.group('key')}':'{_REDACTED}'"


def _redact_bare_colon(match: re.Match[str]) -> str:
    return f"{match.group('key')}: {_REDACTED}"


def _redact_assignment(match: re.Match[str]) -> str:
    return f"{match.group('key')}={_REDACTED}"


@dataclass(frozen=True, slots=True)
class ParkInfo:
    """Why automation is parked and since when.

    ``since`` and every other timestamp is an ISO-8601 UTC string such as
    ``2026-11-09T12:00:00Z``. Keeping the original ``since`` on repeated parks
    is what lets an operator see how long the account has been waiting for a
    human instead of watching the clock reset on every retry.
    """

    reason: str
    kind: str
    since: str
    detail: str


class SessionStore:
    """Owns the files under :attr:`XPosterConfig.home`.

    Every path comes from the config properties so a test config never touches
    the real home. The class is deliberately synchronous and stdlib-only:
    browser runs, the CLI and health checks all call it from different places.
    """

    def __init__(self, config: XPosterConfig) -> None:
        self._config = config

    def ensure_dirs(self) -> None:
        """Create the home and Chrome profile directories, forcing mode 0700.

        The profile is often created earlier by a manual login, so the mode is
        applied unconditionally instead of only at creation time.
        """
        self._ensure_home()
        profile = self._config.profile_dir
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(profile, 0o700)

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold the single-process lock that guards the Chrome profile.

        Two Chrome processes on one ``user-data-dir`` corrupt its databases, so
        every browser run happens inside this lock. The lock is non-blocking:
        a second caller gets :class:`SessionBusyError` naming the holder's pid
        rather than silently waiting for a run that may take many minutes.
        """
        self._ensure_home()
        fd = os.open(self._config.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                holder = _read_lock_pid(fd)
                raise SessionBusyError(
                    f"Another xposter run holds the session lock (pid {holder}); "
                    "wait for it to finish."
                ) from None
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
            yield
        finally:
            os.close(fd)

    def save_state(self, state: Mapping[str, Any]) -> None:
        """Atomically refresh the backup credential, keeping the previous good copy.

        A state with zero cookies is almost always a failed capture, so it is
        refused: an empty write would otherwise destroy the only usable backup
        and force the operator through another manual login.
        """
        cookies = state.get("cookies")
        if not isinstance(cookies, list | tuple) or not cookies:
            raise ValueError(
                "Refusing to save a storage_state without cookies; "
                "the previous backup is left untouched."
            )
        payload = _encode_json(dict(state))
        main = self._config.storage_state_path
        previous = _read_bytes(main)
        if previous is not None and _looks_like_good_state(previous):
            self._write_bytes(_backup_path(main), previous)
        self._write_bytes(main, payload)

    def load_state(self) -> dict[str, Any] | None:
        """Return the saved Playwright state, falling back to ``.bak`` if needed.

        A truncated main file is expected after a crash; returning the last
        good backup keeps a run possible without asking for a new login.
        """
        main = self._config.storage_state_path
        for path in (main, _backup_path(main)):
            data = _read_json(path)
            if isinstance(data, dict):
                return cast(dict[str, Any], data)
        return None

    def has_state(self) -> bool:
        """True when a loadable credential backup exists, including ``.bak``."""
        return self.load_state() is not None

    def read_meta(self) -> dict[str, Any]:
        """Return non-secret session metadata; ``{}`` when missing or corrupt."""
        data = _read_json(self._config.meta_path)
        if isinstance(data, dict):
            return cast(dict[str, Any], data)
        return {}

    def update_meta(self, **fields: Any) -> dict[str, Any]:
        """Merge fields into ``session.json`` and return the merged mapping.

        A ``None`` value deletes the key, which lets callers clear a stale
        handle or verification time without rewriting the whole file.
        """
        meta = self.read_meta()
        for key, value in fields.items():
            if value is None:
                meta.pop(key, None)
            else:
                meta[key] = value
        self._write_bytes(self._config.meta_path, _encode_json(meta))
        return meta

    def summarize(self, now: datetime | None = None) -> SessionSummary:
        """Build a cookie-free status view for ``status``, health checks and runs.

        This must never raise: a corrupt file should make the CLI say "log in
        again", not crash the health check. A naive ``now`` is treated as UTC.
        """
        try:
            return self._build_summary(now)
        except Exception:
            return self._fallback_summary()

    def park(self, reason: str, *, kind: str = "challenge", detail: str = "") -> ParkInfo:
        """Write the parking marker and return it.

        After a challenge, lock or rate limit the automation must stop until a
        human clears it. Repeated parks keep the original ``since`` and reason
        so the waiting time and first cause are not lost, appending new detail
        for the operator instead.
        """
        existing = self.parked()
        if existing is None:
            info = ParkInfo(
                reason=reason,
                kind=kind,
                since=_iso(datetime.now(UTC)),
                detail=detail,
            )
        else:
            merged_detail = "\n".join(part for part in (existing.detail, detail) if part)
            info = ParkInfo(
                reason=existing.reason,
                kind=existing.kind,
                since=existing.since,
                detail=merged_detail,
            )
        self._write_bytes(self._config.parked_path, _encode_json(_park_payload(info)))
        return info

    def parked(self) -> ParkInfo | None:
        """Return the parking marker, or ``None`` when not parked or unreadable.

        Any readable JSON object counts as parked, even with missing fields:
        the safe answer when the marker exists is to stop and let a human
        unpark, never to assume the session is usable.
        """
        data = _read_json(self._config.parked_path)
        if not isinstance(data, dict):
            return None
        return ParkInfo(
            reason=_str_or_none(data.get("reason")) or "",
            kind=_str_or_none(data.get("kind")) or "challenge",
            since=_str_or_none(data.get("since")) or "",
            detail=_str_or_none(data.get("detail")) or "",
        )

    def unpark(self) -> bool:
        """Remove the parking marker; True when it existed."""
        try:
            self._config.parked_path.unlink()
        except FileNotFoundError:
            return False
        return True

    def raise_if_parked(self) -> None:
        """Raise :class:`ParkedError` while the marker exists, so runs stop early."""
        info = self.parked()
        if info is not None:
            raise ParkedError(info.reason, info.since)

    def new_artifacts_dir(self, label: str) -> Path:
        """Create a private directory for failure evidence and prune old ones.

        Screenshots, HTML dumps and traces can contain page content, so they
        live under the same 0700 home. Only the newest
        :attr:`XPosterConfig.keep_artifacts` directories and those younger than
        :attr:`XPosterConfig.artifact_max_age_days` are kept; the newest one is
        never pruned so a run always has a directory to write into.
        """
        root = self._config.artifacts_dir
        self._ensure_home()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        slug = _sanitise_label(label)
        candidate = root / f"{stamp}-{slug}"
        suffix = 1
        while candidate.exists():
            suffix += 1
            candidate = root / f"{stamp}-{slug}-{suffix}"
        candidate.mkdir(mode=0o700)
        os.chmod(candidate, 0o700)
        self._prune_artifacts(root)
        return candidate

    def write_artifact(
        self,
        directory: Path,
        name: str,
        data: str | bytes,
        *,
        sanitise: bool = False,
    ) -> Path:
        """Write one failure-evidence file atomically inside ``artifacts/``.

        Artifacts are written through the same 0600 atomic helper as
        credentials because a DOM dump can contain CSRF material. ``name`` is
        reduced to a bare filename and the directory is resolved before use so
        a caller (or a page-supplied label) cannot escape the artifact root.
        """
        root = self._config.artifacts_dir
        if not _is_within(directory, root):
            raise ValueError(f"Artifacts must be written under {root}; refusing {directory}.")
        self._ensure_home()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        path = directory / _sanitise_filename(name)
        if sanitise and isinstance(data, str):
            data = sanitise_html(data)
        payload = data.encode("utf-8") if isinstance(data, str) else data
        self._write_bytes(path, payload)
        return path

    def protect_file(self, path: Path) -> None:
        """Restrict a file created by Playwright to owner read/write only.

        Screenshots and traces are written by the browser library with the
        process umask, so their modes must be tightened after the fact. A path
        outside the private home is refused rather than chmodded.
        """
        home = self._config.home
        target = _resolve_path(path)
        base = _resolve_path(home)
        if target == base or not _is_relative_to(target, base):
            raise ValueError(f"Refusing to protect a file outside {home}: {path}.")
        if not path.exists():
            raise FileNotFoundError(path)
        if not path.is_file():
            raise ValueError(f"Refusing to protect a non-file path: {path}.")
        os.chmod(path, 0o600)

    def audit_permissions(self) -> list[tuple[str, str]]:
        """List paths under home whose mode is looser than the private policy.

        ``doctor`` needs evidence about files as well as directories: an
        artifact written 0644 or a symlink pointing at a credential defeats
        the 0700 home. An empty list means the tree is clean.
        """
        home = self._config.home
        if not home.exists():
            return []
        problems: list[tuple[str, str]] = []
        try:
            home_mode = stat.S_IMODE(home.stat().st_mode)
        except OSError as exc:
            return [(str(home), f"unreadable: {exc}")]
        if home_mode & 0o077:
            problems.append((str(home), f"directory mode {home_mode:04o}; expected 0700"))
        credentials = {
            self._config.storage_state_path,
            _backup_path(self._config.storage_state_path),
            self._config.meta_path,
            self._config.parked_path,
            self._config.ledger_path,
            self._config.lock_path,
        }
        artifacts = self._config.artifacts_dir
        try:
            for raw_root, dirnames, filenames in os.walk(home, followlinks=False):
                root = Path(raw_root)
                for entry in dirnames:
                    path = root / entry
                    if path.is_symlink():
                        problems.append((str(path), "symlink; expected a directory"))
                        continue
                    problem = _mode_problem(path, 0o700, "directory")
                    if problem is not None:
                        problems.append((str(path), problem))
                for entry in filenames:
                    path = root / entry
                    if path.is_symlink():
                        problems.append((str(path), "symlink; expected a regular file"))
                        continue
                    if path not in credentials and not _is_relative_to(path, artifacts):
                        continue
                    problem = _mode_problem(path, 0o600, "file")
                    if problem is not None:
                        problems.append((str(path), problem))
        except OSError as exc:
            problems.append((str(home), f"walk failed: {exc}"))
        problems.sort(key=lambda item: item[0])
        return problems

    def wipe_session(self) -> None:
        """Delete the saved login while keeping parking, ledger and artifacts.

        Logging out should not erase the reason automation is parked, the
        record of what was already posted, or the evidence needed to diagnose
        the logout itself.
        """
        profile = self._config.profile_dir
        self._ensure_home()
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        for child in profile.iterdir():
            _remove_path(child)
        os.chmod(profile, 0o700)
        main = self._config.storage_state_path
        for path in (main, _backup_path(main), self._config.meta_path):
            with suppress(FileNotFoundError):
                path.unlink()

    def _build_summary(self, now: datetime | None) -> SessionSummary:
        current = _as_utc(now)
        now_ts = current.timestamp()
        main = self._config.storage_state_path
        backup = _backup_path(main)
        state = self.load_state()
        meta = self.read_meta()
        parked = self.parked()
        state_present = state is not None or main.exists() or backup.exists()
        profile_present = self._profile_has_content()

        has_auth_token = False
        has_ct0 = False
        auth_expires: float | None = None
        if state is not None:
            cookies = state.get("cookies")
            if isinstance(cookies, list):
                for cookie in cookies:
                    if not isinstance(cookie, Mapping):
                        continue
                    domain = cookie.get("domain")
                    if not isinstance(domain, str) or domain.lower() not in _X_COOKIE_DOMAINS:
                        continue
                    name = cookie.get("name")
                    value = cookie.get("value")
                    has_value = isinstance(value, str) and bool(value)
                    if name == "auth_token":
                        has_auth_token = has_auth_token or has_value
                        expiry = _cookie_expiry(cookie.get("expires"))
                        if expiry is not None:
                            auth_expires = expiry
                    elif name == "ct0":
                        has_ct0 = has_ct0 or has_value

        auth_expires_at = _iso_from_epoch(auth_expires) if auth_expires is not None else None
        days_left = (
            math.floor((auth_expires - now_ts) / _DAY_SECONDS) if auth_expires is not None else None
        )
        if state_present:
            expired = (not has_auth_token) or (auth_expires is not None and auth_expires <= now_ts)
        else:
            expired = not profile_present
        return SessionSummary(
            present=state_present or profile_present,
            has_auth_token=has_auth_token,
            has_ct0=has_ct0,
            auth_expires_at=auth_expires_at,
            days_left=days_left,
            expired=expired,
            handle=_str_or_none(meta.get("handle")),
            last_verified_at=_str_or_none(meta.get("last_verified_at")),
            parked=parked is not None,
        )

    def _fallback_summary(self) -> SessionSummary:
        present = False
        parked = False
        with suppress(OSError):
            present = self._config.storage_state_path.exists() or self._profile_has_content()
        with suppress(Exception):
            parked = self.parked() is not None
        return SessionSummary(
            present=present,
            has_auth_token=False,
            has_ct0=False,
            auth_expires_at=None,
            days_left=None,
            expired=True,
            handle=None,
            last_verified_at=None,
            parked=parked,
        )

    def _profile_has_content(self) -> bool:
        try:
            return any(self._config.profile_dir.iterdir())
        except OSError:
            return False

    def _prune_artifacts(self, root: Path) -> None:
        keep = max(self._config.keep_artifacts, 1)
        cutoff = (
            datetime.now(UTC).timestamp()
            - max(self._config.artifact_max_age_days, 0) * _DAY_SECONDS
        )
        try:
            entries = [path for path in root.iterdir() if path.is_dir() and not path.is_symlink()]
        except OSError:
            return
        entries.sort(key=_artifact_order, reverse=True)
        for index, stale in enumerate(entries):
            if index == 0:
                continue
            try:
                modified = stale.stat().st_mtime
            except OSError:
                continue
            if index >= keep or modified < cutoff:
                shutil.rmtree(stale, ignore_errors=True)

    def _ensure_home(self) -> None:
        home = self._config.home
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(home, 0o700)

    def _write_bytes(self, path: Path, payload: bytes) -> None:
        self._ensure_home()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            with suppress(OSError):
                tmp.unlink()
            raise
        _fsync_dir(path.parent)


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak")


def _park_payload(info: ParkInfo) -> dict[str, str]:
    return {
        "reason": info.reason,
        "kind": info.kind,
        "since": info.since,
        "detail": info.detail,
    }


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_from_epoch(timestamp: float) -> str:
    return _iso(datetime.fromtimestamp(timestamp, tz=UTC))


def _cookie_expiry(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _sanitise_label(label: str) -> str:
    chars = [ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "-" for ch in label.strip()]
    slug = "".join(chars)
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-_")[:_LABEL_MAX].rstrip("-_")
    return slug or "run"


def _sanitise_filename(name: str) -> str:
    tail = name.replace("\\", "/").rsplit("/", 1)[-1]
    chars = [ch if ch.isascii() and (ch.isalnum() or ch in "._-") else "-" for ch in tail.strip()]
    slug = "".join(chars)
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-._")[:_FILENAME_MAX].rstrip("-._")
    return slug or "artifact"


def _resolve_path(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(path))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except ValueError:
        return False


def _is_within(path: Path, root: Path) -> bool:
    target = _resolve_path(path)
    base = _resolve_path(root)
    return target == base or _is_relative_to(target, base)


def _mode_problem(path: Path, expected: int, kind: str) -> str | None:
    try:
        mode = stat.S_IMODE(path.lstat().st_mode)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"unreadable: {exc}"
    if mode & 0o077:
        return f"{kind} mode {mode:04o}; expected {expected:04o}"
    return None


def _artifact_order(path: Path) -> tuple[int, str]:
    try:
        return (path.stat().st_mtime_ns, path.name)
    except OSError:
        return (0, path.name)


def _read_lock_pid(fd: int) -> str:
    try:
        raw = os.read(fd, 64)
    except OSError:
        return "unknown"
    text = raw.decode("ascii", errors="ignore").strip()
    return text if text.isdigit() else "unknown"


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _read_json(path: Path) -> object | None:
    raw = _read_bytes(path)
    if raw is None:
        return None
    try:
        parsed: object = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed


def _looks_like_good_state(raw: bytes) -> bool:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(data, dict) and isinstance(data.get("cookies"), list) and bool(data["cookies"])
    )


def _encode_json(data: object) -> bytes:
    try:
        text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        raise ValueError("Refusing to write session data that is not valid JSON.") from None
    try:
        return text.encode("utf-8") + b"\n"
    except UnicodeEncodeError:
        raise ValueError("Refusing to write session data that is not valid JSON.") from None


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(fd)
    finally:
        with suppress(OSError):
            os.close(fd)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    with suppress(FileNotFoundError):
        path.unlink()
