"""Smoke-test an installed Hoard: start it, check it answers, quit it.

    python scripts/smoke_installed.py /path/to/hoard[.exe]

Uses a throwaway data folder and port, and no keychain.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = "8765"
BASE = f"http://127.0.0.1:{PORT}"


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=3) as resp:
        return resp.status, resp.geturl(), resp.read().decode()


def main() -> None:
    hoard = sys.argv[1]
    home = tempfile.mkdtemp(prefix="hoard-smoke-")
    env = {
        **os.environ,
        "HOARD_HOME": home,
        "APP_PORT": PORT,
        "HOARD_NO_BROWSER": "1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
    }
    proc = subprocess.Popen([hoard], env=env, cwd=home)
    try:
        for _ in range(60):
            try:
                status, _, body = get("/api/health")
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise SystemExit("Hoard didn't start within 30s")
        health = json.loads(body)
        assert health["app"] == "hoard", health
        print("health:", health)

        status, url, body = get("/")
        assert url.endswith("/setup") and "Welcome to Hoard" in body, (status, url)
        print("first run redirects to setup: ok")

        req = urllib.request.Request(
            BASE + "/api/shutdown", method="POST", headers={"Origin": BASE}
        )
        urllib.request.urlopen(req, timeout=3).read()
        proc.wait(timeout=20)
        print("quit: ok (exit code", proc.returncode, ")")
    finally:
        if proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    main()
