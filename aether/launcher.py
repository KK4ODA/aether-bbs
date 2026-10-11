"""
aether/launcher.py

Locating and launching Aether HF.

The desktop application (``aether-hf``) contains the daemon and its panel
and starts the daemon itself; the standalone daemon (``aetherd``) is what a
headless gateway runs.  Either answers on the VARA-compatible host port once
*Host programs* is switched on in Aether's Setup (``[host] enabled = true``).
"""

import os
import shutil
import sys
from pathlib import Path


def _candidates() -> list[Path]:
    paths: list[Path] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            paths.append(Path(local) / "Aether HF" / "aether-hf.exe")
            paths.append(Path(local) / "Aether HF" / "aetherd.exe")
        for pf in (os.environ.get("ProgramFiles", ""),
                   os.environ.get("ProgramFiles(x86)", "")):
            if pf:
                paths.append(Path(pf) / "Aether HF" / "aether-hf.exe")
    elif sys.platform == "darwin":
        paths.append(Path("/Applications/Aether HF.app/Contents/MacOS/aether-hf"))
    else:
        paths.append(Path("/usr/bin/aether-hf"))
        paths.append(Path("/usr/local/bin/aetherd"))
        paths.append(Path("/usr/bin/aetherd"))
    return paths


def find_aether_executable() -> str:
    """Return the path of an installed Aether HF, or "" if none is found."""
    for p in _candidates():
        if p.is_file():
            return str(p)
    for name in ("aether-hf", "aetherd"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def is_daemon(exe_path: str) -> bool:
    """True when ``exe_path`` is the headless daemon rather than the desktop app."""
    return Path(exe_path).stem.lower() == "aetherd"


def build_command(exe_path: str, config_file: str = "") -> list[str]:
    """Command line that starts Aether HF from ``exe_path``.

    ``config_file`` is passed as ``--config`` to the standalone daemon; the
    desktop application keeps its own station.toml and takes no arguments.
    """
    cmd = [exe_path]
    if config_file and is_daemon(exe_path):
        cmd += ["--config", config_file]
    return cmd
