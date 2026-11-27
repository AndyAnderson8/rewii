"""ReWii - all-in-one toolkit for softmodded Wiis."""
from __future__ import annotations


def _version() -> str:
    """The installed version. ``pyproject.toml`` is the only place it is written down:
    it reaches here through the package metadata (which the packaged app bundles too,
    see packaging/rewii.spec)."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:              # pragma: no cover - always there on supported Pythons
        return "0+unknown"
    try:
        return version("rewii")
    except PackageNotFoundError:     # running from a source tree that was never installed
        return "0+unknown"


__version__ = _version()
