"""
core/updater.py

Git-based auto-updater.  Fetches from origin, compares HEAD with the
remote tracking branch, and (optionally) pulls + restarts.

Works with or without an EventBus (terminal mode passes ``None``).
"""

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.events import EventBus, EventType

log = logging.getLogger(__name__)

# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class CommitSummary:
    hash: str
    author: str
    date: str
    subject: str


@dataclass
class UpdateCheckResult:
    update_available: bool = False
    current_hash: str = ""
    remote_hash: str = ""
    commits: list[CommitSummary] = field(default_factory=list)
    aether_update_available: bool = False
    aether_commits: list[CommitSummary] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class UpdateApplyResult:
    success: bool = False
    old_hash: str = ""
    new_hash: str = ""
    aether_updated: bool = False
    error: Optional[str] = None


class GitError(Exception):
    """Raised when a git command fails."""


# ── Updater ─────────────────────────────────────────────────────────────────

class Updater:
    """Check for and apply updates via git pull."""

    def __init__(
        self,
        repo_dir: Path,
        event_bus: Optional[EventBus],
        config,                           # UpdaterConfig (avoid circular import)
    ):
        self._repo = Path(repo_dir)
        self._bus = event_bus
        self._config = config
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_check: Optional[UpdateCheckResult] = None

    # ── public properties ───────────────────────────────────────────────

    @property
    def last_check(self) -> Optional[UpdateCheckResult]:
        return self._last_check

    # ── git helpers ─────────────────────────────────────────────────────

    def _run_git(self, *args: str, cwd: Optional[Path] = None) -> str:
        """Run a git command and return stdout.  Raises GitError on failure."""
        cmd = ["git"] + list(args)
        try:
            r = subprocess.run(
                cmd,
                cwd=cwd or self._repo,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except FileNotFoundError:
            raise GitError("git is not installed or not on PATH")
        except subprocess.TimeoutExpired:
            raise GitError(f"git command timed out: {' '.join(cmd)}")
        if r.returncode != 0:
            raise GitError(r.stderr.strip() or f"git exited with code {r.returncode}")
        return r.stdout.strip()

    def _is_git_repo(self) -> bool:
        if not (self._repo / ".git").exists():
            return False
        try:
            self._run_git("status", "--porcelain")
            return True
        except GitError:
            return False

    def _is_dirty(self) -> bool:
        out = self._run_git("status", "--porcelain")
        return len(out) > 0

    def _parse_log(self, range_spec: str, cwd: Optional[Path] = None) -> list[CommitSummary]:
        """Parse ``git log --oneline`` for the given range into CommitSummary list."""
        try:
            raw = self._run_git(
                "log", "--format=%h|%an|%ai|%s", range_spec, cwd=cwd,
            )
        except GitError:
            return []
        commits: list[CommitSummary] = []
        for line in raw.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append(CommitSummary(*parts))
        return commits

    # ── emit helper ─────────────────────────────────────────────────────

    def _emit(self, event_type: EventType, data=None):
        if self._bus:
            self._bus.emit(event_type, source="updater", data=data)

    # ── check ───────────────────────────────────────────────────────────

    def check_for_updates(self) -> UpdateCheckResult:
        """Fetch from origin and compare HEAD with the remote branch.

        Safe to call from any thread.  Never raises — errors are captured
        in the returned result.
        """
        result = UpdateCheckResult()
        branch = self._config.branch

        try:
            if not self._is_git_repo():
                result.error = "Not a git repository — updates unavailable"
                self._emit(EventType.UPDATE_FAILED, result.error)
                self._last_check = result
                return result

            self._emit(EventType.UPDATE_CHECK_STARTED)

            # Fetch latest from origin
            self._run_git("fetch", "origin", branch)

            # Compare hashes
            result.current_hash = self._run_git("rev-parse", "--short", "HEAD")
            result.remote_hash = self._run_git("rev-parse", "--short", f"origin/{branch}")

            if result.current_hash != result.remote_hash:
                result.update_available = True
                result.commits = self._parse_log(f"HEAD..origin/{branch}")

            # Optionally check AETHER submodule
            if self._config.include_aether:
                aether_dir = self._repo / "aether_hf_modem"
                if (aether_dir / ".git").exists() or (self._repo / ".gitmodules").exists():
                    try:
                        self._run_git("fetch", "origin", cwd=aether_dir)
                        local = self._run_git("rev-parse", "--short", "HEAD", cwd=aether_dir)
                        remote = self._run_git("rev-parse", "--short", "origin/master", cwd=aether_dir)
                        if local != remote:
                            result.aether_update_available = True
                            result.aether_commits = self._parse_log(
                                f"HEAD..origin/master", cwd=aether_dir,
                            )
                    except GitError as e:
                        log.warning("AETHER submodule check failed: %s", e)

            if result.update_available or result.aether_update_available:
                self._emit(EventType.UPDATE_AVAILABLE, result)
                log.info(
                    "Update available: %d new commit(s)%s",
                    len(result.commits),
                    f", AETHER: {len(result.aether_commits)} commit(s)"
                    if result.aether_update_available else "",
                )
            else:
                log.info("Software is up to date (%s)", result.current_hash)

        except GitError as e:
            result.error = str(e)
            self._emit(EventType.UPDATE_FAILED, result.error)
            log.warning("Update check failed: %s", e)
        except Exception as e:
            result.error = f"Unexpected error: {e}"
            self._emit(EventType.UPDATE_FAILED, result.error)
            log.error("Update check error", exc_info=True)

        self._last_check = result
        return result

    # ── apply ───────────────────────────────────────────────────────────

    def apply_update(self, include_aether: bool = False) -> UpdateApplyResult:
        """Pull the latest commits.  Returns result — never raises."""
        result = UpdateApplyResult()
        branch = self._config.branch

        try:
            if self._is_dirty():
                result.error = (
                    "Working tree has uncommitted changes.  "
                    "Please commit or stash them before updating."
                )
                self._emit(EventType.UPDATE_FAILED, result.error)
                return result

            self._emit(EventType.UPDATE_STARTED)
            result.old_hash = self._run_git("rev-parse", "--short", "HEAD")

            # Fast-forward pull
            self._run_git("pull", "--ff-only", "origin", branch)
            result.new_hash = self._run_git("rev-parse", "--short", "HEAD")
            result.success = True

            # AETHER submodule
            if include_aether:
                aether_dir = self._repo / "aether_hf_modem"
                if aether_dir.exists():
                    try:
                        self._run_git(
                            "submodule", "update", "--remote", "aether_hf_modem",
                        )
                        result.aether_updated = True
                        log.info("AETHER HF submodule updated")
                    except GitError as e:
                        log.warning("AETHER submodule update failed: %s", e)

            self._emit(EventType.UPDATE_COMPLETE, result)
            log.info("Updated %s → %s", result.old_hash, result.new_hash)

        except GitError as e:
            result.error = str(e)
            self._emit(EventType.UPDATE_FAILED, result.error)
            log.error("Update failed: %s", e)
        except Exception as e:
            result.error = f"Unexpected error: {e}"
            self._emit(EventType.UPDATE_FAILED, result.error)
            log.error("Update error", exc_info=True)

        return result

    # ── async / background ──────────────────────────────────────────────

    def check_once_async(self):
        """Run a single update check on a background thread."""
        t = threading.Thread(
            target=self.check_for_updates,
            name="UpdateCheck",
            daemon=True,
        )
        t.start()

    def start_periodic_check(self):
        """Start a daemon thread that checks periodically."""
        interval = self._config.auto_check_interval_hours
        if interval <= 0:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._periodic_loop,
            args=(interval * 3600,),
            name="UpdatePeriodic",
            daemon=True,
        )
        self._thread.start()
        log.info("Periodic update check every %dh", interval)

    def _periodic_loop(self, interval_sec: float):
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=interval_sec):
                break  # stop requested
            self.check_for_updates()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
