#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Wraps dist/open_HOWI.app in a disk image with a shortcut to Applications: make_dmg.sh <output.dmg>
set -eu
stage=$(mktemp -d)
cp -R dist/open_HOWI.app "$stage/"
ln -s /Applications "$stage/Applications"
hdiutil create -volname "open_HOWI" -srcfolder "$stage" -ov -format UDZO "$1"
rm -rf "$stage"
