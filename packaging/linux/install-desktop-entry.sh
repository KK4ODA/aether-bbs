#!/bin/sh
# Adds Aether BBS to your desktop's application menu, pointing at this folder.
# Run it again if you move the folder.  Remove with:
#   rm ~/.local/share/applications/aether-bbs.desktop ~/.local/share/icons/hicolor/256x256/apps/aether-bbs.png
set -e
here="$(cd "$(dirname "$0")" && pwd)"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
icons="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"
mkdir -p "$apps" "$icons"
cp "$here/aether-bbs.png" "$icons/aether-bbs.png"
cat > "$apps/aether-bbs.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Aether BBS
Comment=Amateur radio BBS for VARA FM/HF, Aether HF and AX.25 packet
Exec="$here/AetherBBS"
Icon=aether-bbs
Terminal=false
Categories=Network;HamRadio;
DESKTOP
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$apps" || true
echo "Aether BBS added to the application menu."
