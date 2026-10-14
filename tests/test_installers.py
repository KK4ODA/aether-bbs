"""
tests/test_installers.py

Installer builds: where an installed copy keeps its data, how it updates
itself from the release's installer on each platform, and the --self-test the
build workflow runs.  The builds themselves are made and started by
.github/workflows/installers.yml.
"""

import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import core.paths as paths
import core.updater as updater_mod
from core.config import UpdaterConfig
from core.updater import ReleaseInfo, Updater, installer_asset, installer_suffix

ROOT = Path(__file__).parent.parent


# ── paths ──────────────────────────────────────────────────────────────────

def test_source_run_keeps_everything_in_the_source_folder():
    assert not paths.is_frozen()
    assert paths.data_dir() == ROOT
    assert paths.resource_dir() == ROOT
    assert paths.install_dir() == ROOT


def test_installed_copy_keeps_data_in_the_profile(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    (bundle / "files" / "GENERAL").mkdir(parents=True)
    (bundle / "files" / "GENERAL" / "template.txt").write_text("shipped")
    (bundle / "files" / "GENERAL" / "edited.txt").write_text("shipped")
    profile = tmp_path / "profile" / "aether-bbs"
    (profile / "files" / "GENERAL").mkdir(parents=True)
    (profile / "files" / "GENERAL" / "edited.txt").write_text("sysop's")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "AetherBBS"))
    monkeypatch.setattr("core.config.get_config_dir", lambda: profile)
    monkeypatch.chdir(tmp_path)

    assert paths.resource_dir() == bundle
    assert paths.install_dir() == (tmp_path / "app").resolve()
    assert paths.prepare_data_dir() == profile
    assert Path.cwd() == profile
    assert (profile / "files" / "GENERAL" / "template.txt").read_text() == "shipped"
    assert (profile / "files" / "GENERAL" / "edited.txt").read_text() == "sysop's"


# ── installer assets ───────────────────────────────────────────────────────

def _release(version="1.4.0", kinds=("windows-setup.exe", "macos.dmg", "macos-intel.dmg",
                                     "linux-x86_64.tar.gz")):
    return ReleaseInfo(version=version, tag=f"v{version}", assets={
        f"AetherBBS-{version}-{k}": f"https://example.invalid/AetherBBS-{version}-{k}"
        for k in kinds})


def test_installer_asset_per_platform():
    rel = _release()
    assert installer_asset(rel, "win32")[0] == "AetherBBS-1.4.0-windows-setup.exe"
    assert installer_asset(rel, "darwin", "arm64")[0] == "AetherBBS-1.4.0-macos.dmg"
    assert installer_asset(rel, "darwin", "x86_64")[0] == "AetherBBS-1.4.0-macos-intel.dmg"
    # a release from before the Intel build: an Intel Mac is not offered the ARM image
    assert installer_asset(_release(kinds=("macos.dmg",)), "darwin", "x86_64") is None
    assert installer_asset(rel, "linux")[0] == "AetherBBS-1.4.0-linux-x86_64.tar.gz"
    assert installer_asset(_release(kinds=("macos.dmg",)), "win32") is None
    assert installer_suffix("win32") == "-windows-setup.exe"


def _frozen_updater(tmp_path, monkeypatch, release, payload=b""):
    monkeypatch.setattr(updater_mod, "is_frozen", lambda: True)
    install = tmp_path / "AetherBBS"
    install.mkdir(exist_ok=True)
    (install / "VERSION").write_text("1.3.2")
    up = Updater(install, None, UpdaterConfig())
    monkeypatch.setattr(up, "fetch_latest_release", lambda: release)
    monkeypatch.setattr(up, "_http_get", lambda url, accept="": payload)
    monkeypatch.setattr(updater_mod.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    return up


def test_installed_copy_without_its_installer_is_not_offered(tmp_path, monkeypatch):
    rel = _release(kinds=())
    up = _frozen_updater(tmp_path, monkeypatch, rel)
    r = up.check_for_updates()
    assert r.install_kind == "installer"
    assert not r.update_available
    assert "installer for this computer is not on the release yet" in r.error


def test_installed_copy_is_offered_its_installer(tmp_path, monkeypatch):
    up = _frozen_updater(tmp_path, monkeypatch, _release())
    r = up.check_for_updates()
    assert r.install_kind == "installer" and r.update_available and r.error is None


def test_windows_runs_setup_after_the_bbs_quits(tmp_path, monkeypatch):
    up = _frozen_updater(tmp_path, monkeypatch, _release(), b"MZ setup")
    check = up.check_for_updates()
    monkeypatch.setattr(updater_mod.sys, "platform", "win32")
    monkeypatch.setattr(updater_mod.sys, "executable", r"C:\Apps\Aether BBS\AetherBBS.exe")
    monkeypatch.setattr(Updater, "_can_write", staticmethod(lambda folder: True))
    started = []
    monkeypatch.setattr(updater_mod.subprocess, "Popen",
                        lambda args, **kw: started.append(args))

    r = up.apply_update(check)
    assert r.success, r.error
    assert r.relaunch is False                       # the script starts it again
    setup = tmp_path / "tmp" / "aether-bbs-update" / "AetherBBS-1.4.0-windows-setup.exe"
    assert setup.read_bytes() == b"MZ setup"
    script = (tmp_path / "tmp" / "aether-bbs-update" / "run-update.cmd").read_text()
    assert f'PID eq {os.getpid()}' in script
    assert f'"{setup}" /SILENT' in script
    assert r'start "" "C:\Apps\Aether BBS\AetherBBS.exe"' in script
    assert started and started[0][:2] == ["cmd", "/c"]
    raw = (tmp_path / "tmp" / "aether-bbs-update" / "run-update.cmd").read_bytes()
    assert b"\r\r\n" not in raw and raw.count(b"\r\n") == raw.count(b"\n")
    assert "update.log" in script and "setup.log" in script
    assert "/CURRENTUSER" in script and "runas" not in script


def test_windows_all_users_install_asks_for_permission_first(tmp_path, monkeypatch):
    """Installed for all users (Program Files), the setup needs an
    administrator.  Asked for from the background after the BBS had quit,
    Windows' prompt only blinked in the taskbar and the update never ran;
    now it is asked for while the BBS is still in front."""
    up = _frozen_updater(tmp_path, monkeypatch, _release(), b"MZ setup")
    check = up.check_for_updates()
    monkeypatch.setattr(updater_mod.sys, "platform", "win32")
    monkeypatch.setattr(updater_mod.sys, "executable",
                        r"C:\Program Files\Aether BBS\AetherBBS.exe")
    monkeypatch.setattr(Updater, "_can_write", staticmethod(lambda folder: False))
    elevated = []
    monkeypatch.setattr(Updater, "_run_elevated",
                        staticmethod(lambda script, folder: elevated.append(script)))
    monkeypatch.setattr(updater_mod.subprocess, "Popen",
                        lambda *a, **kw: pytest.fail("started without permission"))

    r = up.apply_update(check)
    assert r.success and r.relaunch is False, r.error
    script = elevated[0].read_text(encoding="utf-8")
    assert "/ALLUSERS" in script
    # started with the sysop's own rights, not the administrator's
    assert r'explorer.exe "C:\Program Files\Aether BBS\AetherBBS.exe"' in script
    assert 'start ""' not in script


def test_windows_update_is_called_off_when_permission_is_refused(tmp_path, monkeypatch):
    up = _frozen_updater(tmp_path, monkeypatch, _release(), b"MZ setup")
    check = up.check_for_updates()
    monkeypatch.setattr(updater_mod.sys, "platform", "win32")
    monkeypatch.setattr(Updater, "_can_write", staticmethod(lambda folder: False))

    def refuse(script, folder):
        raise updater_mod.UpdateError("Windows did not give permission")
    monkeypatch.setattr(Updater, "_run_elevated", staticmethod(refuse))
    r = up.apply_update(check)
    assert not r.success and "permission" in r.error   # the BBS keeps running


def test_can_write_tries_the_folder(tmp_path):
    assert Updater._can_write(tmp_path)
    assert not Updater._can_write(tmp_path / "missing")


WINDOWS_BBS = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[4])
import core.updater as u
setup, folder, app = (Path(a) for a in sys.argv[1:4])
sys.executable = str(app)
u.Updater._run_windows_setup(None, setup, folder)
time.sleep(3)                       # the BBS shutting down
(folder / "bbs-quit.txt").write_text("quit")
"""


@pytest.mark.skipif(sys.platform != "win32", reason="runs the real update script")
def test_windows_update_script_runs_setup_after_the_bbs_quits(tmp_path):
    """The script the updater leaves behind, run for real: it waits for the
    BBS to quit, runs the setup, then starts the BBS again.  (The 1.3.6
    script, run like this, never got as far as the setup.)"""
    import subprocess
    import time
    folder = tmp_path / "update folder"
    folder.mkdir()
    setup = folder / "fake-setup.cmd"
    setup.write_text('@if exist "%~dp0bbs-quit.txt" (echo after>"%~dp0setup-ran.txt")'
                     ' else (echo before>"%~dp0setup-ran.txt")\r\n'
                     '@echo %*>>"%~dp0setup-ran.txt"\r\n')
    app = folder / "fake-bbs.cmd"
    app.write_text('@echo started > "%~dp0bbs-started.txt"\r\n@exit\r\n')
    subprocess.run([sys.executable, "-c", WINDOWS_BBS, str(setup), str(folder),
                    str(app), str(ROOT)], check=True, timeout=30)

    started = folder / "bbs-started.txt"
    deadline = time.time() + 30
    while time.time() < deadline and not started.exists():
        time.sleep(0.2)
    log_path = folder / "update.log"
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    assert started.exists(), log
    ran = (folder / "setup-ran.txt").read_text()
    assert ran.startswith("after"), ran          # only once the BBS had quit
    assert "/SILENT" in ran and "setup.log" in ran
    assert "Setup finished with code 0" in log and "Started" in log


def test_macos_opens_the_disk_image(tmp_path, monkeypatch):
    up = _frozen_updater(tmp_path, monkeypatch, _release(), b"dmg")
    check = up.check_for_updates()
    monkeypatch.setattr(updater_mod.sys, "platform", "darwin")
    monkeypatch.setattr(updater_mod._platform, "machine", lambda: "arm64")
    monkeypatch.setattr(updater_mod.Path, "home", lambda: tmp_path / "home")
    opened = []
    monkeypatch.setattr(updater_mod.subprocess, "Popen", lambda args, **kw: opened.append(args))

    r = up.apply_update(check)
    assert r.success and "drag the new Aether BBS into Applications" in r.manual_step
    dmg = tmp_path / "home" / "Downloads" / "AetherBBS-1.4.0-macos.dmg"
    assert dmg.read_bytes() == b"dmg" and opened == [["open", str(dmg)]]


def _tarball(files: dict[str, bytes], root="AetherBBS") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{root}/{name}" if root else name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.skipif(sys.platform == "win32", reason="Linux update path")
def test_linux_swaps_in_the_new_app_folder(tmp_path, monkeypatch):
    payload = _tarball({"AetherBBS": b"new program", "_internal/lib.so": b"new lib",
                        "_internal/VERSION": b"1.4.0"})
    up = _frozen_updater(tmp_path, monkeypatch, _release(), payload)
    install = tmp_path / "AetherBBS"
    (install / "AetherBBS").write_bytes(b"old program")
    (install / "_internal").mkdir()
    (install / "_internal" / "lib.so").write_bytes(b"old lib")
    check = up.check_for_updates()
    monkeypatch.setattr(updater_mod.sys, "platform", "linux")

    r = up.apply_update(check)
    assert r.success, r.error
    assert r.relaunch is True
    assert (install / "AetherBBS").read_bytes() == b"new program"
    assert (install / "_internal" / "lib.so").read_bytes() == b"new lib"
    assert os.access(install / "AetherBBS", os.X_OK)
    assert not list(install.rglob("*.new"))


@pytest.mark.skipif(sys.platform == "win32", reason="Linux update path")
@pytest.mark.parametrize("files,root,why", [
    ({"../evil": b"x"}, "AetherBBS", "unsafe path"),
    ({"something-else": b"x"}, "AetherBBS", "not an Aether BBS Linux build"),
])
def test_linux_rejects_bad_archives(tmp_path, monkeypatch, files, root, why):
    up = _frozen_updater(tmp_path, monkeypatch, _release(), _tarball(files, root))
    (tmp_path / "AetherBBS" / "AetherBBS").write_bytes(b"old program")
    check = up.check_for_updates()
    monkeypatch.setattr(updater_mod.sys, "platform", "linux")
    r = up.apply_update(check)
    assert not r.success and why in r.error
    assert (tmp_path / "AetherBBS" / "AetherBBS").read_bytes() == b"old program"
    assert not (tmp_path / "evil").exists()


# ── self-test ──────────────────────────────────────────────────────────────

def test_self_test_covers_every_module():
    """main.self_test() imports a fixed list (a frozen build can't list its
    own modules); this keeps that list in step with the source tree."""
    import ast
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "self_test")
    packages = next(ast.literal_eval(n.value) for n in ast.walk(fn)
                    if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "packages")
    listed = {f"{p}.{m}" for p, names in packages.items() for m in names.split()}
    on_disk = {f"{f.parent.name}.{f.stem}" for p in packages
               for f in (ROOT / p).glob("*.py") if f.stem != "__init__"}
    assert listed == on_disk


def test_self_test_runs_from_source():
    import subprocess
    r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--self-test"],
                       capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert "self-test OK" in r.stdout


# ── update dialog ──────────────────────────────────────────────────────────

QtWidgets = pytest.importorskip("PyQt6.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _dialog(restarts):
    from core.updater import UpdateCheckResult
    from gui.update_dialog import UpdateDialog
    r = UpdateCheckResult(update_available=True, current_version="1.3.2",
                          latest_version="1.4.0", install_kind="installer",
                          release=_release())
    return UpdateDialog(r, object(), lambda **kw: restarts.append(kw))


def test_dialog_lets_the_installer_restart_the_bbs(qapp, monkeypatch):
    from core.updater import UpdateApplyResult
    from PyQt6.QtCore import QTimer
    restarts = []
    dlg = _dialog(restarts)
    monkeypatch.setattr(QTimer, "singleShot", lambda ms, fn: fn())
    dlg._on_apply_done(UpdateApplyResult(success=True, old_hash="v1.3.2",
                                         new_hash="v1.4.0", relaunch=False))
    assert restarts == [{"relaunch": False}]
    assert "installer finishes" in dlg._status.text()


def test_dialog_shows_the_macos_step_and_does_not_restart(qapp):
    from core.updater import UpdateApplyResult
    restarts = []
    dlg = _dialog(restarts)
    dlg._on_apply_done(UpdateApplyResult(success=True, manual_step="Drag it."))
    assert restarts == [] and dlg._status.text() == "Drag it."
    assert dlg._later_btn.text() == "Close" and dlg._later_btn.isEnabled()
