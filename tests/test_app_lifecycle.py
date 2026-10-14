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


# Every service on, pointed at ports where nothing listens: startup must
# wire them all up without an error, and shutdown stop them all.
ALL_ON = """
[logging]
level = "INFO"
[aprs]
enabled = true
is_server = "127.0.0.1"
is_port = 1
[winlink]
enabled = true
gateway_callsign = "N0CALL"
[outbound_delivery.inbound_email]
enabled = true
host = "127.0.0.1"
port = 1
[bbs.forwarding]
server_enabled = true
server_port = 47301
server_bind = "127.0.0.1"
[[bbs.forwarding_neighbours]]
bbs_callsign = "N0NBR-1"
host = "127.0.0.1"
port = 47302
[[instances]]
name = "FM"
cmd_port = 47310
data_port = 47311
[[instances]]
name = "PKT"
transport = "SOUNDMODEM"
cmd_port = 47320
data_port = 47321
[instances.soundmodem]
port = 47322
"""


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is POSIX")
@pytest.mark.parametrize("config", ['[logging]\nlevel = "INFO"\n', ALL_ON],
                         ids=["defaults", "every-service-on"])
def test_sigterm_shuts_down_cleanly(tmp_path, config):
    home = tmp_path / "home"
    cfg_dir = home / ".config" / "aether-bbs"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "aether_bbs.toml").write_text(config)
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
