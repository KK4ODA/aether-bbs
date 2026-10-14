"""
core/updater.py

Self-updater.  Asks GitHub for the latest Aether BBS release, compares it
with the VERSION file, and (when the sysop agrees) installs it:

* a git clone is fast-forwarded to the release tag (``git merge --ff-only``);
* any other install (the release ZIP unpacked into a folder) is updated by
  downloading the release ZIP and copying it over the install folder.  Files
  it replaces are backed up under ``backups/`` first and put back if the copy
  fails part-way.  Messages, files, logs and backups are never overwritten;
* an installed copy (an installer build, ``sys.frozen``) downloads the
  release's installer for its platform: on Windows the setup runs silently
  once the BBS has quit and starts the new version; on Linux the new app
  folder replaces the old one; on macOS the disk image is opened for the
  sysop to drag the app into Applications.

On the "latest" channel a git clone follows every commit on its branch
instead of releases only.

Aether HF is a separate application with its own signed updater, so it is
not updated from here.

Works with or without an EventBus (terminal mode passes ``None``).
"""

import json
import logging
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from core.events import EventBus, EventType
from core.paths import is_frozen
from core.version import get_version

log = logging.getLogger(__name__)

DEFAULT_REPO = "KK4ODA/aether-bbs"

# Folders in the install directory that hold the station's own data.  A ZIP
# update only adds files that are missing there; it never replaces any.
PRESERVED_DIRS = frozenset({"messages", "files", "logs", "backups", ".git"})

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
HTTP_TIMEOUT = 30

# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class CommitSummary:
    hash: str
    author: str
    date: str
    subject: str


@dataclass
class ReleaseInfo:
    version: str                # "1.3.0" (tag without the leading "v")
    tag: str                    # "v1.3.0"
    name: str = ""
    notes: str = ""             # release body, Markdown
    url: str = ""               # release page
    zip_url: str = ""
    published_at: str = ""
    assets: dict = field(default_factory=dict)   # file name → download URL


@dataclass
class UpdateCheckResult:
    update_available: bool = False
    current_version: str = ""
    latest_version: str = ""
    install_kind: str = ""      # "git" or "zip"
    release: Optional[ReleaseInfo] = None
    current_hash: str = ""
    remote_hash: str = ""
    commits: list[CommitSummary] = field(default_factory=list)
    manual: bool = False        # the sysop pressed "Check for Updates"
    error: Optional[str] = None


@dataclass
class UpdateApplyResult:
    success: bool = False
    old_hash: str = ""          # old version / commit
    new_hash: str = ""          # new version / commit
    warning: Optional[str] = None
    relaunch: bool = True       # False: an installer starts the new version
    manual_step: Optional[str] = None   # the sysop finishes it (macOS)
    error: Optional[str] = None


class GitError(Exception):
    """Raised when a git command fails."""


class UpdateError(Exception):
    """Raised when a release cannot be fetched or installed."""


# ── Version helpers ─────────────────────────────────────────────────────────

_VER_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+.]?(.*))?$")


def parse_version(text: str) -> tuple:
    """Turn '1.2.0', 'v1.3.0-beta.2' into a comparable tuple.

    A pre-release sorts below the release with the same numbers.
    """
    m = _VER_RE.match((text or "").strip())
    if not m:
        return (0, 0, 0, 0, "")
    major, minor, patch, pre = m.groups()
    nums = (int(major), int(minor or 0), int(patch or 0))
    if pre:
        return nums + (0, pre)
    return nums + (1, "")


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


# ── Installer builds ────────────────────────────────────────────────────────

def installer_suffix(platform: Optional[str] = None, machine: Optional[str] = None) -> str:
    """The end of this platform's installer file name on a release."""
    platform = platform or sys.platform
    if platform == "win32":
        return "-windows-setup.exe"
    if platform == "darwin":
        # "-macos.dmg" is the Apple-silicon build (the name 1.3.2 looks for)
        machine = (machine or _platform.machine()).lower()
        return "-macos-intel.dmg" if machine in ("x86_64", "amd64", "i386") else "-macos.dmg"
    return "-linux-x86_64.tar.gz"


def installer_asset(rel: "ReleaseInfo", platform: Optional[str] = None,
                    machine: Optional[str] = None):
    """(name, url) of the release's installer for this computer, or None."""
    want = f"AetherBBS-{rel.version}{installer_suffix(platform, machine)}"
    for name, url in rel.assets.items():
        if name == want:
            return name, url
    return None


# ── Updater ─────────────────────────────────────────────────────────────────

class Updater:
    """Check for and apply Aether BBS updates."""

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

    @property
    def repo_dir(self) -> Path:
        return self._repo

    def update_config(self, config):
        old_hours = getattr(self._config, "auto_check_interval_hours", 0)
        self._config = config
        if config.auto_check_interval_hours != old_hours:
            # a new check interval: replace the periodic loop (0 = none)
            self._stop_event.set()
            self._thread = None
            self.start_periodic_check()

    def local_version(self) -> str:
        """The VERSION of the copy in repo_dir."""
        try:
            return (self._repo / "VERSION").read_text(encoding="utf-8").strip() or get_version()
        except OSError:
            return get_version()

    def _cfg(self, name: str, default):
        return getattr(self._config, name, default) or default

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
        out = self._run_git("status", "--porcelain", "--untracked-files=no")
        return len(out) > 0

    def _parse_log(self, range_spec: str, cwd: Optional[Path] = None) -> list[CommitSummary]:
        """Parse ``git log`` for the given range into CommitSummary list."""
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

    # ── HTTP helpers ────────────────────────────────────────────────────

    def _http_get(self, url: str, accept: str = "application/vnd.github+json") -> bytes:
        req = urllib.request.Request(url, headers={
            "Accept": accept,
            "User-Agent": f"Aether-BBS/{get_version()}",
        })
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                data = resp.read(MAX_DOWNLOAD_BYTES + 1)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise UpdateError("No release has been published yet")
            if e.code == 403:
                raise UpdateError("GitHub is rate-limiting update checks; try again later")
            raise UpdateError(f"GitHub returned HTTP {e.code}")
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise UpdateError(f"Cannot reach GitHub ({reason})")
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise UpdateError("Download is larger than expected; refusing it")
        return data

    def fetch_latest_release(self) -> ReleaseInfo:
        repo = self._cfg("repo", DEFAULT_REPO)
        raw = self._http_get(f"https://api.github.com/repos/{repo}/releases/latest")
        try:
            data = json.loads(raw.decode("utf-8"))
            tag = str(data["tag_name"])
        except (ValueError, KeyError, TypeError):
            raise UpdateError("GitHub sent an unreadable release description")
        return ReleaseInfo(
            version=tag[1:] if tag[:1] in ("v", "V") else tag,
            tag=tag,
            name=str(data.get("name") or tag),
            notes=str(data.get("body") or ""),
            url=str(data.get("html_url") or ""),
            zip_url=str(data.get("zipball_url")
                        or f"https://api.github.com/repos/{repo}/zipball/{tag}"),
            published_at=str(data.get("published_at") or ""),
            assets={str(a.get("name")): str(a.get("browser_download_url"))
                    for a in (data.get("assets") or [])
                    if a.get("name") and a.get("browser_download_url")},
        )

    # ── emit helper ─────────────────────────────────────────────────────

    def _emit(self, event_type: EventType, data=None):
        if self._bus:
            self._bus.emit(event_type, source="updater", data=data)

    # ── check ───────────────────────────────────────────────────────────

    def check_for_updates(self, manual: bool = False) -> UpdateCheckResult:
        """Look for a newer release (or newer commits on the "latest" channel).

        Safe to call from any thread.  Never raises — errors are captured
        in the returned result.
        """
        result = UpdateCheckResult(current_version=self.local_version(), manual=manual)

        try:
            self._emit(EventType.UPDATE_CHECK_STARTED)
            if is_frozen():
                result.install_kind = "installer"
            else:
                result.install_kind = "git" if self._is_git_repo() else "zip"

            if result.install_kind == "git" and self._cfg("channel", "release") == "latest":
                self._check_git_branch(result)
            else:
                rel = self.fetch_latest_release()
                result.release = rel
                result.latest_version = rel.version
                result.update_available = is_newer(rel.version, result.current_version)
                if (result.update_available and result.install_kind == "installer"
                        and not installer_asset(rel)):
                    result.update_available = False
                    result.error = (f"Aether BBS {rel.version} is out, but its installer "
                                    "for this computer is not on the release yet.")

            if result.update_available:
                self._emit(EventType.UPDATE_AVAILABLE, result)
                log.info("Update available: %s -> %s",
                         result.current_version,
                         result.latest_version or result.remote_hash)
            else:
                log.info("Aether BBS is up to date (v%s)", result.current_version)

        except (GitError, UpdateError) as e:
            result.error = str(e)
            self._emit(EventType.UPDATE_FAILED, result.error)
            log.warning("Update check failed: %s", e)
        except Exception as e:
            result.error = f"Unexpected error: {e}"
            self._emit(EventType.UPDATE_FAILED, result.error)
            log.error("Update check error", exc_info=True)

        self._last_check = result
        return result

    def _check_git_branch(self, result: UpdateCheckResult):
        branch = self._cfg("branch", "master")
        self._run_git("fetch", "origin", branch)
        result.current_hash = self._run_git("rev-parse", "--short", "HEAD")
        result.remote_hash = self._run_git("rev-parse", "--short", f"origin/{branch}")
        if result.current_hash != result.remote_hash:
            result.commits = self._parse_log(f"HEAD..origin/{branch}")
            result.update_available = bool(result.commits)
        try:
            result.latest_version = self._run_git(
                "show", f"origin/{branch}:VERSION").strip()
        except GitError:
            pass

    # ── apply ───────────────────────────────────────────────────────────

    def apply_update(
        self,
        check: Optional[UpdateCheckResult] = None,
        progress: Optional[Callable[[str], None]] = None,
    ) -> UpdateApplyResult:
        """Install the update found by *check* (default: the last check).

        Returns a result — never raises.
        """
        check = check or self._last_check
        result = UpdateApplyResult()
        say = progress or (lambda _msg: None)

        try:
            if check is None or not check.update_available:
                raise UpdateError("No update to install; check for updates first")
            self._emit(EventType.UPDATE_STARTED)
            if check.install_kind == "git":
                self._apply_git(check, result, say)
            elif check.install_kind == "installer":
                self._apply_installer(check, result, say)
            else:
                self._apply_zip(check, result, say)
            result.success = True
            self._emit(EventType.UPDATE_COMPLETE, result)
            log.info("Updated %s → %s", result.old_hash, result.new_hash)
        except (GitError, UpdateError) as e:
            result.error = str(e)
            self._emit(EventType.UPDATE_FAILED, result.error)
            log.error("Update failed: %s", e)
        except Exception as e:
            result.error = f"Unexpected error: {e}"
            self._emit(EventType.UPDATE_FAILED, result.error)
            log.error("Update error", exc_info=True)

        return result

    def _apply_git(self, check: UpdateCheckResult, result: UpdateApplyResult, say):
        if self._is_dirty():
            raise UpdateError(
                "This folder has changes of your own to tracked files.  "
                "Commit or stash them before updating."
            )
        result.old_hash = self._run_git("rev-parse", "--short", "HEAD")
        old_reqs = self._read(self._repo / "requirements.txt")

        if check.release and not check.commits:
            tag = check.release.tag
            say(f"Fetching {tag} ...")
            self._run_git("fetch", "origin", "tag", tag, "--no-tags")
            say(f"Moving to {tag} ...")
            try:
                self._run_git("merge", "--ff-only", tag)
            except GitError:
                raise UpdateError(
                    f"Cannot fast-forward to {tag}: this copy has commits of its "
                    "own or is on another branch.  Update it with git by hand."
                )
        else:
            branch = self._cfg("branch", "master")
            say(f"Pulling {branch} ...")
            self._run_git("pull", "--ff-only", "origin", branch)

        result.new_hash = self._run_git("rev-parse", "--short", "HEAD")
        if self._read(self._repo / "requirements.txt") != old_reqs:
            result.warning = self._install_requirements(say)

    def _apply_zip(self, check: UpdateCheckResult, result: UpdateApplyResult, say):
        rel = check.release
        if rel is None:
            raise UpdateError("No release to install")
        result.old_hash = f"v{check.current_version}"
        result.new_hash = f"v{rel.version}"

        with tempfile.TemporaryDirectory(prefix="aether-bbs-update-") as tmp:
            tmp_path = Path(tmp)
            say(f"Downloading Aether BBS {rel.version} ...")
            archive = tmp_path / "release.zip"
            archive.write_bytes(self._http_get(rel.zip_url, accept="*/*"))

            say("Unpacking ...")
            new_root = self._extract_release(archive, tmp_path / "unpacked")
            got = self._read(new_root / "VERSION").decode("utf-8", "replace").strip()
            if not (new_root / "main.py").is_file() or not got:
                raise UpdateError("The download is not an Aether BBS release")
            if parse_version(got) != parse_version(rel.version):
                raise UpdateError(
                    f"The download says version {got}, expected {rel.version}")

            say("Installing ...")
            reqs_changed = self._install_tree(new_root, check.current_version)

        if reqs_changed:
            result.warning = self._install_requirements(say)

    def _apply_installer(self, check: UpdateCheckResult, result: UpdateApplyResult, say):
        rel = check.release
        asset = installer_asset(rel) if rel else None
        if not asset:
            raise UpdateError("This release has no installer for this computer")
        name, url = asset
        result.old_hash = f"v{check.current_version}"
        result.new_hash = f"v{rel.version}"

        say(f"Downloading {name} ...")
        folder = Path(tempfile.gettempdir()) / "aether-bbs-update"
        folder.mkdir(parents=True, exist_ok=True)
        package = folder / name
        package.write_bytes(self._http_get(url, accept="*/*"))

        if sys.platform == "win32":
            say("Starting the installer ...")
            self._run_windows_setup(package, folder)
            result.relaunch = False
        elif sys.platform == "darwin":
            dest = Path.home() / "Downloads" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(package, dest)
            subprocess.Popen(["open", str(dest)])
            result.manual_step = (
                f"The Aether BBS {rel.version} disk image is open.  Quit Aether BBS, "
                "drag the new Aether BBS into Applications (replace the old one), "
                "then start it again.")
        else:
            say("Installing ...")
            self._replace_linux_app(package)

    def _run_windows_setup(self, setup: Path, folder: Path):
        """Run the setup silently after this process exits, then start the
        new version.  A small batch file waits for the BBS to quit so the
        installer can replace its files."""
        exe = Path(sys.executable)
        script = folder / "run-update.cmd"
        script.write_text(
            "@echo off\r\n"
            ":wait\r\n"
            f'tasklist /FI "PID eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul '
            # ping, not timeout: timeout fails without a console
            "&& (ping -n 2 127.0.0.1 >nul & goto wait)\r\n"
            f'"{setup}" /SILENT /SUPPRESSMSGBOXES /NORESTART /NOCANCEL\r\n'
            f'start "" "{exe}"\r\n',
            encoding="utf-8")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags,
                         close_fds=True, cwd=str(folder))

    def _replace_linux_app(self, archive: Path):
        """Swap the files of the app folder for the release's (Linux)."""
        import tarfile
        target = self._repo
        if not os.access(target, os.W_OK):
            raise UpdateError(
                f"{target} is not writable by you.  Unpack the new release there "
                "by hand, or install Aether BBS in a folder you own.")
        with tempfile.TemporaryDirectory(prefix="aether-bbs-update-") as tmp:
            with tarfile.open(archive) as tf:
                for m in tf.getmembers():
                    p = PurePosixPath(m.name)
                    if p.is_absolute() or ".." in p.parts or m.issym() and (
                            PurePosixPath(m.linkname).is_absolute()
                            or ".." in PurePosixPath(m.linkname).parts):
                        raise UpdateError("The download contains an unsafe path")
                if hasattr(tarfile, "data_filter"):
                    tf.extractall(tmp, filter="data")
                else:
                    tf.extractall(tmp)
            roots = [d for d in Path(tmp).iterdir()]
            if len(roots) != 1 or not (roots[0] / "AetherBBS").is_file():
                raise UpdateError("The download is not an Aether BBS Linux build")
            new_root = roots[0]
            for src in sorted(new_root.rglob("*")):
                if src.is_dir() and not src.is_symlink():
                    continue
                dst = target / src.relative_to(new_root)
                dst.parent.mkdir(parents=True, exist_ok=True)
                staged = dst.with_name(dst.name + ".new")
                if src.is_symlink():
                    if staged.exists() or staged.is_symlink():
                        staged.unlink()
                    os.symlink(os.readlink(src), staged)
                else:
                    shutil.copy2(src, staged)
                # rename over the old file: works even for the running program
                os.replace(staged, dst)

    @staticmethod
    def _read(path: Path) -> bytes:
        try:
            return path.read_bytes()
        except OSError:
            return b""

    @staticmethod
    def _extract_release(archive: Path, dest: Path) -> Path:
        """Unpack a GitHub zipball safely and return its top-level folder."""
        try:
            zf = zipfile.ZipFile(archive)
        except zipfile.BadZipFile:
            raise UpdateError("The download is damaged (not a ZIP file)")
        with zf:
            roots = set()
            for info in zf.infolist():
                p = PurePosixPath(info.filename.replace("\\", "/"))
                if p.is_absolute() or ".." in p.parts or ":" in info.filename:
                    raise UpdateError("The download contains an unsafe path")
                if p.parts:
                    roots.add(p.parts[0])
            if len(roots) != 1:
                raise UpdateError("The download has an unexpected layout")
            zf.extractall(dest)
        return dest / roots.pop()

    def _install_tree(self, new_root: Path, old_version: str) -> bool:
        """Copy *new_root* over the install folder.  Returns True when
        requirements.txt changed.  Rolls back on failure."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self._repo / "backups" / f"update-v{old_version}-{stamp}"
        replaced: list[Path] = []      # relative paths backed up then overwritten
        created: list[Path] = []       # relative paths that did not exist before
        reqs_changed = False

        files = sorted(p for p in new_root.rglob("*") if p.is_file())
        # Write VERSION last, so an interrupted copy still reads as the old version.
        files.sort(key=lambda p: p.relative_to(new_root).as_posix() == "VERSION")

        try:
            for src in files:
                rel = src.relative_to(new_root)
                dst = self._repo / rel
                preserved = rel.parts[0] in PRESERVED_DIRS
                if dst.exists():
                    if preserved or dst.is_dir():
                        continue
                    if dst.read_bytes() == src.read_bytes():
                        continue
                    if rel.as_posix() == "requirements.txt":
                        # Line endings alone (CRLF on Windows) are not a change.
                        reqs_changed = (dst.read_bytes().splitlines()
                                        != src.read_bytes().splitlines())
                    (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst, backup / rel)
                    replaced.append(rel)
                else:
                    created.append(rel)
                    if rel.as_posix() == "requirements.txt":
                        reqs_changed = True
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        except Exception as e:
            log.error("Update copy failed, rolling back: %s", e)
            for rel in replaced:
                try:
                    shutil.copy2(backup / rel, self._repo / rel)
                except OSError:
                    log.error("Rollback could not restore %s", rel)
            for rel in created:
                try:
                    (self._repo / rel).unlink()
                except OSError:
                    pass
            raise UpdateError(
                f"Could not write the new files ({e}).  The old version was put back."
            )

        if replaced:
            log.info("Update replaced %d file(s); old copies in %s", len(replaced), backup)
        return reqs_changed

    def _install_requirements(self, say) -> Optional[str]:
        """pip-install requirements.txt.  Returns a warning on failure."""
        say("Installing new Python packages ...")
        cmd = [sys.executable, "-m", "pip", "install", "-r",
               str(self._repo / "requirements.txt")]
        err = None
        try:
            r = subprocess.run(cmd, cwd=self._repo, capture_output=True,
                               text=True, timeout=900)
            if r.returncode != 0:
                lines = (r.stderr or r.stdout or "").strip().splitlines()
                err = lines[-1] if lines else f"pip exited with code {r.returncode}"
        except (OSError, subprocess.TimeoutExpired) as e:
            err = str(e)
        if err:
            log.warning("pip install failed: %s", err)
            return ("The new version needs Python packages that could not be "
                    f"installed ({err}).  Run: python -m pip install -r requirements.txt")
        return None

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
        # each loop has its own stop event, so a loop being replaced (it
        # may be mid-check) cannot carry on beside its successor
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._periodic_loop,
            args=(interval * 3600, self._stop_event),
            name="UpdatePeriodic",
            daemon=True,
        )
        self._thread.start()
        log.info("Periodic update check every %dh", interval)

    def _periodic_loop(self, interval_sec: float, stop: threading.Event):
        while not stop.is_set():
            if stop.wait(timeout=interval_sec):
                break  # stop requested
            self.check_for_updates()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
