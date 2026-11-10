# SPDX-License-Identifier: GPL-3.0-or-later
"""Check a packaged app works: the command line, the airfoil database, slicing, and the browser app.

python packaging/smoke_test.py dist/open_HOWI/open_HOWI
"""

import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(exe: str, *args: str) -> str:
    done = subprocess.run([exe, *args], capture_output=True, text=True, timeout=180, check=False)
    if done.returncode:
        raise SystemExit(f"{' '.join(args)} failed ({done.returncode}):\n{done.stdout}\n{done.stderr}")
    return done.stdout


def main(exe: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        run(exe, "airfoils", "--export", "sd7037", "-o", str(out / "sd7037.dat"))
        assert (out / "sd7037.dat").read_text().strip(), "airfoil database missing"
        run(exe, "slice", str(ROOT / "examples" / "glider_block.howi.json"), "-o", str(out / "block.gcode"))
        assert "G93" in (out / "block.gcode").read_text(), "no G-code"
        print("command line: ok")

        server = subprocess.Popen([exe, "gui", "--port", "8799", "--no-browser"], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)  # fmt: skip
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            for _ in range(60):
                try:
                    page = opener.open("http://127.0.0.1:8799/", timeout=2).read().decode()
                    break
                except OSError:
                    time.sleep(0.5)
            else:
                raise SystemExit("the app did not start")
            assert "open_HOWI" in page, "index.html missing"
            for asset in ("app.js", "viewer.js", "app.css", "vendor/three/three.module.min.js"):
                assert opener.open(f"http://127.0.0.1:8799/{asset}", timeout=5).status == 200, asset
            info = json.load(opener.open("http://127.0.0.1:8799/api/info", timeout=5))
            print(f"browser app: ok (version {info['version']})")
        finally:
            server.terminate()
            server.wait(timeout=20)


if __name__ == "__main__":
    main(sys.argv[1])
