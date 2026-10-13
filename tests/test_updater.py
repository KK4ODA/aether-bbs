"""
tests/test_updater.py

The self-updater: release check against GitHub (faked), installing a release
ZIP over a plain folder, fast-forwarding a git clone to a release tag, and the
startup prompt in the main window.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.config import UpdaterConfig
from core.events import EventBus, EventType
from core.updater import (
    ReleaseInfo, UpdateCheckResult, UpdateError, Updater, is_newer, parse_version,
)


# ── helpers ────────────────────────────────────────────────────────────────

def _release_json(tag="v1.3.0", body="## New\n- things"):
    return json.dumps({
        "tag_name": tag, "name": f"Aether BBS {tag}", "body": body,
        "html_url": f"https://github.com/KK4ODA/aether-bbs/releases/tag/{tag}",
        "zipball_url": f"https://api.github.com/repos/KK4ODA/aether-bbs/zipball/{tag}",
    }).encode()


def _zip(files: dict[str, str], root="KK4ODA-aether-bbs-abc1234") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(f"{root}/{name}" if root else name, text)
    return buf.getvalue()


def _install(tmp_path, version="1.2.0") -> Path:
    d = tmp_path / "install"
    (d / "core").mkdir(parents=True)
    (d / "messages").mkdir()
    (d / "files" / "GENERAL").mkdir(parents=True)
    (d / "VERSION").write_text(version + "\n")
    (d / "main.py").write_text("print('old')\n")
    (d / "core" / "a.py").write_text("A = 1\n")
    (d / "requirements.txt").write_bytes(b"PyQt6\r\n")   # as saved on Windows
    (d / "messages" / "bbs.db").write_text("station data")
    (d / "files" / "GENERAL" / "readme.txt").write_text("sysop edited")
    return d


def _fake_http(monkeypatch, updater, zip_bytes=b"", tag="v1.3.0"):
    calls = []

    def get(url, accept="application/vnd.github+json"):
        calls.append(url)
        if "releases/latest" in url:
            return _release_json(tag)
        return zip_bytes
    monkeypatch.setattr(updater, "_http_get", get)
    return calls


# ── version comparison ─────────────────────────────────────────────────────

def test_version_ordering():
    assert is_newer("1.3.0", "1.2.0")
    assert is_newer("v1.10.0", "1.9.9")
    assert is_newer("1.3.0", "1.3.0-beta.2")
    assert not is_newer("1.2.0", "1.2.0")
    assert not is_newer("1.2.0", "1.3.0")
    assert parse_version("v2") == parse_version("2.0.0")


# ── check ──────────────────────────────────────────────────────────────────

def test_check_finds_newer_release_for_zip_install(tmp_path, monkeypatch):
    bus = EventBus()
    seen = []
    bus.subscribe(EventType.UPDATE_AVAILABLE, lambda e: seen.append(e.data))
    up = Updater(_install(tmp_path), bus, UpdaterConfig())
    _fake_http(monkeypatch, up)

    r = up.check_for_updates()
    assert r.error is None
    assert r.install_kind == "zip"
    assert r.update_available
    assert (r.current_version, r.latest_version) == ("1.2.0", "1.3.0")
    assert r.release.notes.startswith("## New")
    assert seen and seen[0] is r


def test_check_up_to_date(tmp_path, monkeypatch):
    up = Updater(_install(tmp_path, "1.3.0"), None, UpdaterConfig())
    _fake_http(monkeypatch, up)
    r = up.check_for_updates()
    assert not r.update_available and r.error is None


def test_check_offline_reports_error(tmp_path, monkeypatch):
    bus = EventBus()
    failed = []
    bus.subscribe(EventType.UPDATE_FAILED, lambda e: failed.append(e.data))
    up = Updater(_install(tmp_path), bus, UpdaterConfig())

    def offline(*a, **k):
        raise UpdateError("Cannot reach GitHub (no network)")
    monkeypatch.setattr(up, "_http_get", offline)
    r = up.check_for_updates()
    assert not r.update_available
    assert "Cannot reach GitHub" in r.error
    assert failed == [r.error]


# ── ZIP install ────────────────────────────────────────────────────────────

def test_zip_update_installs_and_keeps_station_data(tmp_path, monkeypatch):
    d = _install(tmp_path)
    up = Updater(d, None, UpdaterConfig())
    _fake_http(monkeypatch, up, _zip({
        "VERSION": "1.3.0\n",
        "main.py": "print('new')\n",
        "core/a.py": "A = 1\n",                       # unchanged
        "core/b.py": "B = 2\n",                       # new file
        "requirements.txt": "PyQt6\n",
        "files/GENERAL/readme.txt": "shipped",        # must not overwrite
        "files/GENERAL/template.txt": "new template", # missing → added
        "messages/bbs.db": "EMPTY",                   # must not overwrite
    }))
    pip = []
    monkeypatch.setattr(up, "_install_requirements", lambda say: pip.append(1))

    msgs = []
    r = up.apply_update(up.check_for_updates(), progress=msgs.append)
    assert r.success, r.error
    assert (r.old_hash, r.new_hash) == ("v1.2.0", "v1.3.0")
    assert (d / "VERSION").read_text().strip() == "1.3.0"
    assert (d / "main.py").read_text() == "print('new')\n"
    assert (d / "core" / "b.py").exists()
    assert (d / "messages" / "bbs.db").read_text() == "station data"
    assert (d / "files" / "GENERAL" / "readme.txt").read_text() == "sysop edited"
    assert (d / "files" / "GENERAL" / "template.txt").read_text() == "new template"
    assert not pip                                     # requirements unchanged
    assert any("Downloading" in m for m in msgs)

    backups = list((d / "backups").glob("update-v1.2.0-*"))
    assert len(backups) == 1
    assert (backups[0] / "main.py").read_text() == "print('old')\n"
    assert not (backups[0] / "core" / "a.py").exists()  # unchanged → not backed up


def test_zip_update_runs_pip_when_requirements_change(tmp_path, monkeypatch):
    d = _install(tmp_path)
    up = Updater(d, None, UpdaterConfig())
    _fake_http(monkeypatch, up, _zip({
        "VERSION": "1.3.0", "main.py": "x", "requirements.txt": "PyQt6\nnewdep\n"}))
    monkeypatch.setattr(up, "_install_requirements", lambda say: "pip broke")
    r = up.apply_update(up.check_for_updates())
    assert r.success and r.warning == "pip broke"


@pytest.mark.parametrize("files,root,why", [
    ({"VERSION": "1.3.0"}, "r", "not an Aether BBS release"),
    ({"VERSION": "1.4.0", "main.py": "x"}, "r", "says version 1.4.0"),
    ({"../evil.py": "x", "main.py": "x"}, "r", "unsafe path"),
    ({"VERSION": "1.3.0", "main.py": "x"}, "", "unexpected layout"),
])
def test_zip_update_rejects_bad_downloads(tmp_path, monkeypatch, files, root, why):
    d = _install(tmp_path)
    up = Updater(d, None, UpdaterConfig())
    _fake_http(monkeypatch, up, _zip(files, root=root))
    r = up.apply_update(up.check_for_updates())
    assert not r.success and why in r.error
    assert (d / "main.py").read_text() == "print('old')\n"
    assert not (tmp_path / "evil.py").exists()


def test_zip_update_rolls_back_when_copy_fails(tmp_path, monkeypatch):
    d = _install(tmp_path)
    up = Updater(d, None, UpdaterConfig())
    _fake_http(monkeypatch, up, _zip({
        "VERSION": "1.3.0", "main.py": "print('new')\n",
        "core/a.py": "A = 2\n", "zz_new.py": "x"}))

    real_copy = shutil.copy2

    def flaky(src, dst, *a, **k):
        if str(dst).endswith("zz_new.py"):
            raise PermissionError("file in use")
        return real_copy(src, dst, *a, **k)
    monkeypatch.setattr("core.updater.shutil.copy2", flaky)

    r = up.apply_update(up.check_for_updates())
    assert not r.success and "old version was put back" in r.error
    assert (d / "main.py").read_text() == "print('old')\n"
    assert (d / "core" / "a.py").read_text() == "A = 1\n"
    assert (d / "VERSION").read_text().strip() == "1.2.0"


def test_apply_without_update_fails_cleanly(tmp_path):
    up = Updater(_install(tmp_path), None, UpdaterConfig())
    r = up.apply_update(UpdateCheckResult(update_available=False))
    assert not r.success and "No update" in r.error


# ── git install ────────────────────────────────────────────────────────────

def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "init.defaultBranch=master", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_clone_fast_forwards_to_release_tag(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init")
    (origin / "VERSION").write_text("1.2.0\n")
    (origin / "requirements.txt").write_text("PyQt6\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "1.2.0")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))

    (origin / "VERSION").write_text("1.3.0\n")
    _git(origin, "commit", "-am", "1.3.0")
    _git(origin, "tag", "v1.3.0")
    (origin / "VERSION").write_text("1.4.0-dev\n")
    _git(origin, "commit", "-am", "after the release")

    (clone / "station.db").write_text("untracked data must not block updates")
    up = Updater(clone, None, UpdaterConfig())
    _fake_http(monkeypatch, up)
    check = up.check_for_updates()
    assert check.install_kind == "git" and check.update_available

    r = up.apply_update(check)
    assert r.success, r.error
    assert (clone / "VERSION").read_text().strip() == "1.3.0"   # the tag, not master


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_latest_channel_follows_branch(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init")
    (origin / "VERSION").write_text("1.2.0\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "base")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    (origin / "x.py").write_text("x")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "new feature")

    up = Updater(clone, None, UpdaterConfig(channel="latest"))
    _fake_http(monkeypatch, up)
    check = up.check_for_updates()
    assert check.update_available
    assert [c.subject for c in check.commits] == ["new feature"]
    assert up.apply_update(check).success
    assert (clone / "x.py").exists()


# ── startup prompt ─────────────────────────────────────────────────────────

QtWidgets = pytest.importorskip("PyQt6.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _window(tmp_path, skipped=""):
    """A bare MainWindow with only what the update handlers use."""
    from core.config import ConfigManager
    from gui.mainwindow import MainWindow
    path = tmp_path / "aether_bbs.toml"
    path.write_text(f'[updater]\nskipped_version = "{skipped}"\n')
    cm = ConfigManager(path)
    w = MainWindow.__new__(MainWindow)
    w._cm, w._config = cm, cm.load()
    w._update_label = QtWidgets.QLabel()
    w._prompted_versions = set()
    w._pending_update = None
    shown = []
    w._show_update_dialog = lambda r=None: shown.append(r)
    return w, cm, shown


def _result(**kw):
    return UpdateCheckResult(
        update_available=True, current_version="1.2.0", latest_version="1.3.0",
        install_kind="zip", release=ReleaseInfo(version="1.3.0", tag="v1.3.0"), **kw)


def test_startup_check_prompts_once(qapp, tmp_path):
    w, _cm, shown = _window(tmp_path)
    w._on_update_available(_result())
    w._on_update_available(_result())          # periodic re-check: no 2nd prompt
    qapp.processEvents()
    assert len(shown) == 1
    assert w._update_label.text() == "UPDATE v1.3.0"


def test_skipped_version_does_not_prompt(qapp, tmp_path):
    w, _cm, shown = _window(tmp_path, skipped="1.3.0")
    w._on_update_available(_result())
    qapp.processEvents()
    assert shown == []
    assert w._update_label.text() == "UPDATE v1.3.0"   # still clickable


def test_manual_check_leaves_prompt_to_settings(qapp, tmp_path):
    w, _cm, shown = _window(tmp_path)
    w._on_update_available(_result(manual=True))
    qapp.processEvents()
    assert shown == []


def test_skip_this_version_is_saved(qapp, tmp_path):
    w, cm, _ = _window(tmp_path)
    w._skip_update_version("1.3.0")
    assert cm.reload().updater.skipped_version == "1.3.0"


def test_update_dialog_builds(qapp):
    from gui.update_dialog import UpdateDialog
    r = _result()
    r.release.notes = "## Fixes\n- one"
    dlg = UpdateDialog(r, SimpleNamespace(), lambda: None,
                       on_skip=lambda v: None, active_sessions=2)
    assert "1.3.0" in dlg._headline()
    assert not dlg._skip_btn.isHidden()


def test_settings_save_keeps_skip_and_channel(qapp, tmp_path):
    from core.config import ConfigManager
    from gui.settings import SettingsTab
    path = tmp_path / "aether_bbs.toml"
    path.write_text('[updater]\nskipped_version = "1.3.0"\nchannel = "latest"\n')
    cm = ConfigManager(path)
    tab = SettingsTab(cm, cm.load(), EventBus())
    upd = tab._build_config().updater
    assert (upd.skipped_version, upd.channel) == ("1.3.0", "latest")
    tab._upd_unskip.setChecked(True)
    assert tab._build_config().updater.skipped_version == ""
