# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point of the packaged command line (``openhowi``), next to the app (``open_HOWI``)."""

import sys

from openhowi.cli import main

if __name__ == "__main__":
    sys.exit(main())
