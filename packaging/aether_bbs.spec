# PyInstaller spec for Aether BBS.  Build from the repository root:
#     pyinstaller packaging/aether_bbs.spec
# Output: dist/AetherBBS/ (Windows, Linux) or dist/Aether BBS.app (macOS).
# Needs Pillow for the icon conversion.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
ICON = str(ROOT / "packaging" / "aether-bbs.png")

datas = [
    (str(ROOT / "VERSION"), "."),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "files"), "files"),
    (ICON, "packaging"),
]

# Loaded by name at run time, so PyInstaller cannot see them
hiddenimports = collect_submodules("pyttsx3.drivers")
if sys.platform == "win32":
    hiddenimports += ["win32com.client", "pythoncom", "pywintypes"]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "pytestqt", "_pytest", "tkinter", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AetherBBS",
    console=False,
    icon=ICON,
)

coll = COLLECT(exe, a.binaries, a.datas, name="AetherBBS")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Aether BBS.app",
        icon=ICON,
        bundle_identifier="org.kk4oda.aether-bbs",
        version=(ROOT / "VERSION").read_text().strip(),
        info_plist={
            "CFBundleName": "Aether BBS",
            "CFBundleDisplayName": "Aether BBS",
            "NSHighResolutionCapable": True,
            "NSMicrophoneUsageDescription": "Aether BBS does not record audio.",
        },
    )
