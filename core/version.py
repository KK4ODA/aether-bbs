"""
core/version.py

Centralised version information.  Reads the VERSION file at the repo root
and optionally enriches it with the current git short-hash.
"""

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

from core.paths import resource_dir

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VERSION_FILE = resource_dir() / "VERSION"


def get_version() -> str:
    """Return the semver string from the VERSION file (e.g. '1.1.0')."""
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "0.0.0"


def get_git_hash() -> str | None:
    """Return the short git commit hash, or *None* if unavailable."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def get_version_display() -> str:
    """Return a human-friendly version string, e.g. 'v1.1.0 (abc1234)'."""
    ver = get_version()
    h = get_git_hash()
    if h:
        return f"v{ver} ({h})"
    return f"v{ver}"
