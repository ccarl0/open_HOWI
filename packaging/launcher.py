# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point of the packaged app (PyInstaller): starts open_HOWI in the browser."""

import sys

from openhowi.desktop import main

if __name__ == "__main__":
    sys.exit(main())
