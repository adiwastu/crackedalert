"""Cracked Alert v2 -- Telegram trading bot for cTrader Open API."""

import os
import subprocess

__version__ = "2.0.25"

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_cached_version = None


def version() -> str:
    """Runtime version: v2.<commit count>, derived from git.

    Falls back to the static __version__ when git is unavailable
    (e.g. a wheel install with no checkout on disk).
    """
    global _cached_version
    if _cached_version is not None:
        return _cached_version
    try:
        count = subprocess.run(
            ["git", "-C", _REPO_ROOT, "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.strip()
        if count:
            _cached_version = "v2.%s" % count
            return _cached_version
    except (subprocess.SubprocessError, OSError):
        pass
    _cached_version = __version__
    return _cached_version
