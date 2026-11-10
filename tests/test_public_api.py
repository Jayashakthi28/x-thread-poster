"""Contract tests for the package's public surface.

The public API is what the future OLSA adapter and any other caller import, so
it is pinned here: every name in ``__all__`` must resolve, the version must match
``pyproject.toml``, and importing the package must not drag Playwright in. The
last rule is checked in a subprocess because the test process itself imports
browser-backed names elsewhere and would hide an eager import.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

import xposter

#: Names the package promises to load lazily (their modules import Playwright).
LAZY_NAMES = frozenset({"XPoster", "SessionInfo", "RefreshResult"})

#: Everything else must be importable without Playwright.
EAGER_NAMES = frozenset(xposter.__all__) - LAZY_NAMES


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Run one snippet in a fresh interpreter with the package importable."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_dunder_all_is_a_unique_list_of_strings() -> None:
    assert isinstance(xposter.__all__, list)
    assert xposter.__all__
    assert all(isinstance(name, str) and name for name in xposter.__all__)
    assert len(xposter.__all__) == len(set(xposter.__all__))


def test_every_public_name_resolves() -> None:
    for name in xposter.__all__:
        value = getattr(xposter, name)
        assert value is not None, name


def test_lazy_names_are_promised_public_names() -> None:
    assert set(xposter.__all__) >= LAZY_NAMES
    assert EAGER_NAMES.isdisjoint(LAZY_NAMES)


def test_every_eager_name_resolves_without_playwright() -> None:
    result = run_python(
        """
        import sys

        import xposter

        for name in xposter.__all__:
            if name in {"XPoster", "SessionInfo", "RefreshResult"}:
                continue
            getattr(xposter, name)

        assert "playwright" not in sys.modules, sorted(
            name for name in sys.modules if name.startswith("playwright")
        )
        assert "xposter.poster" not in sys.modules
        assert "xposter.sessions" not in sys.modules
        print("ok")
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_importing_xposter_does_not_import_playwright() -> None:
    result = run_python(
        """
        import sys

        import xposter

        assert xposter.__version__
        assert "playwright" not in sys.modules, sorted(
            name for name in sys.modules if name.startswith("playwright")
        )
        assert "xposter.poster" not in sys.modules
        assert "xposter.sessions" not in sys.modules
        print("ok")
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_browser_backed_names_load_on_first_access() -> None:
    result = run_python(
        """
        import sys

        import xposter

        assert "playwright" not in sys.modules
        assert xposter.XPoster.__name__ == "XPoster"
        assert "playwright" in sys.modules
        assert "xposter.poster" in sys.modules
        assert xposter.SessionInfo.__name__ == "SessionInfo"
        assert xposter.RefreshResult.__name__ == "RefreshResult"
        assert "xposter.sessions" in sys.modules
        print("ok")
        """
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_lazy_attribute_is_cached() -> None:
    first = xposter.XPoster
    second = xposter.XPoster

    assert first is second
    assert xposter.__dict__["XPoster"] is first


def test_unknown_attribute_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="has no attribute 'not_public'"):
        _ = xposter.not_public


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert data["project"]["version"] == xposter.__version__


def test_package_source_does_not_import_the_olsa_pipeline() -> None:
    """The package must stay standalone; no production module may import olsa."""
    source_root = Path(__file__).resolve().parents[1] / "src" / "xposter"
    pattern = re.compile(r"^\s*(?:from|import)\s+olsa\b", re.MULTILINE)

    offenders = [
        path.name
        for path in source_root.glob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_public_helpers_are_the_documented_objects() -> None:
    assert xposter.split_thread.__module__ == "xposter.splitter"
    assert xposter.split_with_first.__module__ == "xposter.splitter"
    assert xposter.pack_segments.__module__ == "xposter.splitter"
    assert xposter.weighted_length.__module__ == "xposter.splitter"
    assert xposter.validate_tweet.__module__ == "xposter.splitter"
    assert xposter.ParkInfo.__module__ == "xposter.store"
    assert xposter.Decision.__module__ == "xposter.ledger"
    assert xposter.LedgerStatus.__module__ == "xposter.ledger"
    assert xposter.PostRejectedError.__module__ == "xposter.errors"
