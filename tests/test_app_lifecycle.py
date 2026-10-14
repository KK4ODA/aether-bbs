"""
tests/test_app_lifecycle.py

Start the whole application (offscreen) and stop it the way a service
manager would.  It must shut down cleanly: exit code 0, every subsystem
stopped, and no GUI timer touching the database after it closes (that
used to raise and abort the process with SIGABRT).
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is POSIX")
def test_sigterm_shuts_down_cleanly(tmp_path):
    home = tmp_path / "home"
    cfg_dir = home / ".config" / "aether-bbs"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "aether_bbs.toml").write_text('[logging]\nlevel = "INFO"\n')
    work = tmp_path / "work"
    work.mkdir()
    env = dict(os.environ, HOME=str(home), QT_QPA_PLATFORM="offscreen")
    proc = subprocess.Popen([sys.executable, str(ROOT / "main.py")], cwd=work, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        time.sleep(8)                     # let every subsystem start
        assert proc.poll() is None, proc.stdout.read().decode(errors="replace")
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError("did not exit after SIGTERM")
    text = out.decode(errors="replace")
    assert proc.returncode == 0, text[-2000:]
    assert "Shutdown complete" in text
    assert "Traceback" not in text
