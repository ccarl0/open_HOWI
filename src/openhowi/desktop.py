# SPDX-License-Identifier: GPL-3.0-or-later
"""Starting the app from a desktop shortcut or an installer: one copy runs, in the browser.

Double-clicking again while it runs just opens it in the browser again. With arguments
(``open_HOWI slice wing.howi.json``) the packaged app works as the ``openhowi`` command.
"""

from __future__ import annotations

import json
import sys
import urllib.request
import webbrowser

DEFAULT_PORT = 8765


def running_instance(port: int = DEFAULT_PORT) -> bool:
    """True when open_HOWI already answers on this computer at ``port``."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a proxy
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/info", timeout=1.5) as response:
            info = json.load(response)
    except (OSError, ValueError):
        return False
    return isinstance(info, dict) and "default_project" in info


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        from .cli import main as cli

        return cli(argv)
    url = f"http://localhost:{DEFAULT_PORT}/"
    if running_instance():
        print(f"open_HOWI is already running: opening {url}")
        webbrowser.open(url)
        return 0
    from .server import serve

    try:
        serve("127.0.0.1", DEFAULT_PORT, desktop=True)
    except OSError:  # the usual port is taken by another program: any free one will do
        serve("127.0.0.1", 0, desktop=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
