"""Mechanical guard against browser fingerprint spoofing and script injection.

The package deliberately limits ``page.evaluate`` to read-only diagnostics and
hit-testing (see ``READ_ONLY_PAGE_EVALUATE``). This test makes that policy
enforceable: a future edit cannot quietly add an init script, extra headers, a
user-agent/locale/timezone override, a proxy, request interception, geolocation,
permission emulation or a runtime viewport change.
"""

from __future__ import annotations

import re
from pathlib import Path

from xposter.browser import READ_ONLY_PAGE_EVALUATE

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "xposter"

# Intentionally empty: no product module is grandfathered into using these
# APIs. browser.py's only viewport handling is the launch kwarg (a layout
# choice); it never calls set_viewport_size on a live page.
_ALLOWED: frozenset[tuple[str, str]] = frozenset()

_FORBIDDEN: dict[str, re.Pattern[str]] = {
    "add_init_script": re.compile(r"add_init_script"),
    "add_script_tag": re.compile(r"add_script_tag"),
    "set_extra_http_headers": re.compile(r"set_extra_http_headers"),
    "extra_http_headers": re.compile(r"extra_http_headers"),
    "user_agent=": re.compile(r"user_agent\s*="),
    "locale=": re.compile(r"locale\s*="),
    "timezone_id=": re.compile(r"timezone_id\s*="),
    "geolocation": re.compile(r"geolocation"),
    "proxy=": re.compile(r"proxy\s*="),
    ".route(": re.compile(r"\.route\s*\("),
    "fetch(": re.compile(r"\bfetch\s*\("),
    "override_permissions": re.compile(r"override_permissions"),
    "emulate_media": re.compile(r"emulate_media"),
    "set_viewport_size": re.compile(r"set_viewport_size"),
}


def _source_files(root: Path) -> list[Path]:
    """Every text candidate under ``root``; caches and binaries excluded."""
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    )


def _violations(root: Path) -> list[str]:
    """Return ``path:line: pattern`` for every forbidden API under ``root``."""
    violations: list[str] = []
    for path in _source_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for name, pattern in _FORBIDDEN.items():
            if (relative, name) in _ALLOWED:
                continue
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                violations.append(f"{relative}:{line}: {name}")
    return violations


def test_read_only_evaluate_policy_is_declared() -> None:
    assert READ_ONLY_PAGE_EVALUATE is True


def test_source_uses_no_injection_or_override_apis() -> None:
    violations = _violations(_SRC_ROOT)

    assert not violations, (
        "The browser layer must stay read-only; found forbidden injection/override "
        "API usage:\n  " + "\n  ".join(violations)
    )


def test_scanner_actually_catches_a_violation(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("await page.add_init_script('x')\n", encoding="utf-8")

    assert _violations(tmp_path) == ["bad.py:1: add_init_script"]
