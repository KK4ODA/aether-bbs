"""
core/paths.py

Where Aether BBS finds its own files and keeps the station's.

Run from source (``python main.py``), everything is the source folder, as
before: messages/, files/ and logs/ sit next to main.py.

Run from an installer build (PyInstaller, ``sys.frozen``), the program folder
may be read-only (Program Files, /Applications), so the station's data goes in
the same per-user folder as the settings — %APPDATA%\\aether-bbs on Windows,
~/Library/Application Support/aether-bbs on macOS, ~/.config/aether-bbs on
Linux — and the program's own files are read from the bundle.
"""

import shutil
import sys
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    """True when running from an installer build."""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Folder holding VERSION, files/ templates, LICENSE."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return _SOURCE_ROOT


def install_dir() -> Path:
    """The folder an update replaces: the source tree, or the built app's folder."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return _SOURCE_ROOT


def data_dir() -> Path:
    """Where messages/, files/, logs/ and crash.log live."""
    if is_frozen():
        from core.config import get_config_dir
        return get_config_dir()
    return _SOURCE_ROOT


def prepare_data_dir() -> Path:
    """For an installer build: create the data folder, copy in the shipped
    file-area templates that are missing, and make it the working folder so
    the relative paths in the settings (messages, logs) land there."""
    import os
    d = data_dir()
    if not is_frozen():
        return d
    d.mkdir(parents=True, exist_ok=True)
    shipped = resource_dir() / "files"
    if shipped.is_dir():
        for src in shipped.rglob("*"):
            dst = d / "files" / src.relative_to(shipped)
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            elif not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    os.chdir(d)
    return d
