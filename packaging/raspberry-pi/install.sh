#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Sets up open_HOWI on a Raspberry Pi (or any Debian-based Linux with systemd) next to the cutter:
# it starts by itself when the Pi boots, drives the machine over USB, and you use it from a laptop,
# tablet or phone on the same network. On the Pi:
#
#   curl -fsSL https://raw.githubusercontent.com/ccarl0/open_HOWI/main/packaging/raspberry-pi/install.sh | bash
#
# Options (after `bash -s --` when piped):
#   --password PASS   the password other computers are asked for (otherwise you are asked; empty: none)
#   --no-password     no password (only on a network you trust: anyone on it can move the machine)
#   --kiosk           also show open_HOWI full screen on the Pi's own display when it starts
#   --port N          port to listen on (default 8765)
#   --source SPEC     what to install with pip (default: the latest open_HOWI from GitHub)
#   --prefix DIR      where to install it (default /opt/open-howi)
#   --no-service      install the program only; start it yourself with: <prefix>/bin/openhowi gui --host 0.0.0.0
#   --uninstall       remove the service, the program and the kiosk start-up
#   --dry-run         show what would be done, change nothing
set -euo pipefail

PREFIX=/opt/open-howi
PORT=8765
SOURCE="git+https://github.com/ccarl0/open_HOWI.git"
PASSWORD=""
ASK_PASSWORD=1
KIOSK=0
SERVICE=1
UNINSTALL=0
DRY_RUN=0
UNIT=/etc/systemd/system/open-howi.service
ENVFILE=/etc/open-howi.env
USER_NAME=${SUDO_USER:-$(id -un)}
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
AUTOSTART="$USER_HOME/.config/autostart/open-howi-kiosk.desktop"

while [ $# -gt 0 ]; do
  case "$1" in
    --password) PASSWORD="$2"; ASK_PASSWORD=0; shift ;;
    --no-password) PASSWORD=""; ASK_PASSWORD=0 ;;
    --kiosk) KIOSK=1 ;;
    --port) PORT="$2"; shift ;;
    --source) SOURCE="$2"; shift ;;
    --prefix) PREFIX="$2"; shift ;;
    --no-service) SERVICE=0 ;;
    --uninstall) UNINSTALL=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '3,24p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }
run() {  # run a command (as root where needed), or just show it with --dry-run
  if [ "$DRY_RUN" = 1 ]; then echo "  would run: $*"; return 0; fi
  if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi
}
write_root() {  # write stdin to a root-owned file
  if [ "$DRY_RUN" = 1 ]; then echo "  would write $1:"; sed 's/^/    /' | grep -v 'PASSWORD=' || true; return 0; fi
  if [ "$(id -u)" = 0 ]; then cat > "$1"; else sudo tee "$1" > /dev/null; fi
}

if [ "$UNINSTALL" = 1 ]; then
  say "Removing open_HOWI"
  if [ -f "$UNIT" ]; then
    run systemctl disable --now open-howi.service || true
    run rm -f "$UNIT"
    run systemctl daemon-reload
  fi
  run rm -f "$ENVFILE" "$AUTOSTART"
  run rm -rf "$PREFIX"
  say "Done. Your presets stay in $USER_HOME/.config/openhowi."
  exit 0
fi

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
  echo "open_HOWI needs Python 3.11 or newer (Raspberry Pi OS Bookworm or later)." >&2
  exit 1
fi

say "1/5 Installing what open_HOWI needs (Python venv, git)"
if command -v apt-get > /dev/null; then
  run apt-get update -qq
  run apt-get install -y -qq python3-venv python3-pip git
fi

say "2/5 Installing open_HOWI in $PREFIX"
run mkdir -p "$PREFIX"
run chown "$USER_NAME" "$PREFIX"
if [ "$DRY_RUN" = 1 ]; then
  echo "  would create a Python environment in $PREFIX and pip install $SOURCE"
else
  python3 -m venv "$PREFIX"
  "$PREFIX/bin/pip" install -q --upgrade pip
  "$PREFIX/bin/pip" install -q --upgrade "$SOURCE"
fi

say "3/5 Letting $USER_NAME use the USB port (group dialout)"
run usermod -aG dialout "$USER_NAME"

if [ "$ASK_PASSWORD" = 1 ] && [ "$SERVICE" = 1 ] && [ -r /dev/tty ] && [ "$DRY_RUN" = 0 ]; then
  echo
  echo "Other computers on the network can be asked for a password before they can use the machine."
  while true; do
    read -r -s -p "Password (leave empty for none): " PASSWORD < /dev/tty; echo
    [ -z "$PASSWORD" ] && break
    read -r -s -p "Once more: " again < /dev/tty; echo
    [ "$PASSWORD" = "$again" ] && break
    echo "They differ; try again."
  done
fi

if [ "$SERVICE" = 1 ]; then
  say "4/5 Starting open_HOWI when the Pi boots (systemd service open-howi)"
  escaped=$(printf '%s' "$PASSWORD" | sed 's/[\\"]/\\&/g')
  printf 'OPENHOWI_PASSWORD="%s"\n' "$escaped" | write_root "$ENVFILE"
  run chmod 600 "$ENVFILE"
  write_root "$UNIT" <<UNIT
[Unit]
Description=open_HOWI, hot-wire foam cutter slicer and machine control
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
SupplementaryGroups=dialout
EnvironmentFile=-$ENVFILE
ExecStart=$PREFIX/bin/openhowi gui --host 0.0.0.0 --port $PORT --no-browser
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
  run systemctl daemon-reload
  run systemctl enable --now open-howi.service
else
  say "4/5 No service: start it with  $PREFIX/bin/openhowi gui --host 0.0.0.0 --port $PORT"
fi

if [ "$KIOSK" = 1 ]; then
  say "5/5 Showing it full screen on the Pi's display at start-up"
  browser=$(command -v chromium || command -v chromium-browser || true)
  if [ -z "$browser" ]; then
    run apt-get install -y -qq chromium || run apt-get install -y -qq chromium-browser
    browser=$(command -v chromium || command -v chromium-browser || echo chromium)
  fi
  if [ "$DRY_RUN" = 1 ]; then
    echo "  would write $AUTOSTART to start $browser --kiosk http://localhost:$PORT/"
  else
    mkdir -p "$(dirname "$AUTOSTART")"
    cat > "$AUTOSTART" <<DESKTOP
[Desktop Entry]
Type=Application
Name=open_HOWI (full screen)
Exec=sh -c 'sleep 8; $browser --kiosk --noerrdialogs --disable-infobars --no-first-run http://localhost:$PORT/'
X-GNOME-Autostart-enabled=true
DESKTOP
    chown "$USER_NAME" "$AUTOSTART"
  fi
else
  say "5/5 No kiosk (add --kiosk to show it full screen on a screen attached to the Pi)"
fi

echo
say "open_HOWI is set up."
host=$(hostname)
echo "Open it from a computer, tablet or phone on the same network:"
echo "  http://$host.local:$PORT/"
for ip in $(hostname -I 2>/dev/null || true); do
  case "$ip" in *:*) ;; *) echo "  http://$ip:$PORT/" ;; esac
done
if [ -n "$PASSWORD" ]; then
  echo "It asks for the password you chose (any user name)."
elif [ "$SERVICE" = 1 ]; then
  echo "No password: anyone on this network can use the machine. Rerun with --password to set one."
fi
echo "Plug the cutter's controller into the Pi's USB port; it shows up in the Device tab."
echo "If $USER_NAME was just added to the dialout group, reboot once: sudo reboot"
