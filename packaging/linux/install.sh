#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Installs the open_HOWI folder this script sits in for the current user: the program in
# ~/.local/share/open_HOWI, the commands open_HOWI (the app) and openhowi (the command line) in
# ~/.local/bin, and an entry in the applications menu.
# Run ./install.sh --uninstall to remove it again.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
dest="${XDG_DATA_HOME:-$HOME/.local/share}/open_HOWI"
bin="$HOME/.local/bin"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
icons="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"

if [ "${1:-}" = "--uninstall" ]; then
  rm -rf "$dest" "$bin/open_HOWI" "$bin/openhowi" "$apps/open_HOWI.desktop" "$icons/open_HOWI.png"
  echo "open_HOWI removed. Your presets stay in ~/.config/openhowi."
  exit 0
fi

mkdir -p "$dest" "$bin" "$apps" "$icons"
rm -rf "$dest/app"
cp -R "$here/open_HOWI" "$dest/app"
cp "$here/open_HOWI.png" "$icons/open_HOWI.png"
ln -sf "$dest/app/open_HOWI" "$bin/open_HOWI"
ln -sf "$dest/app/openhowi" "$bin/openhowi"
cat > "$apps/open_HOWI.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=open_HOWI
Comment=Hot-wire foam cutter slicer and machine control
Exec=$dest/app/open_HOWI
Icon=open_HOWI
Terminal=false
StartupWMClass=open_HOWI
Categories=Graphics;Engineering;
DESKTOP
echo "open_HOWI installed: start it from your applications menu, or run: open_HOWI"
case ":$PATH:" in *":$bin:"*) ;; *) echo "(add $bin to your PATH to run it from a terminal)";; esac
echo "For the USB port: sudo usermod -aG dialout \$USER  (then log out and in again)"
