"""Set up the project when needed, run preflight checks, and start the dashboard."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
IMPORT_PROBE = "import faster_whisper, pysubs2, cv2, yt_dlp, ddgs, pytesseract, googleapiclient, google_auth_oauthlib, google_auth_httplib2"


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, cwd=ROOT, check=check)


def environment_ready() -> bool:
    if not VENV_PYTHON.is_file():
        return False
    return subprocess.run(
        [str(VENV_PYTHON), "-c", IMPORT_PROBE],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def dashboard_port_status() -> str:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/state", timeout=0.5) as response:
            if response.status == 200:
                return "running"
    except (OSError, urllib.error.URLError):
        pass
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=0.25):
            return "occupied"
    except OSError:
        return "available"


def main() -> int:
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is required.", file=sys.stderr)
        return 1
    try:
        if not environment_ready():
            print("Preparing the project virtual environment. This may take several minutes.")
            run([sys.executable, "scripts/bootstrap.py"])
            if not environment_ready():
                print(
                    "The virtual environment was created, but one or more Python packages "
                    "still cannot be imported. Run its doctor command for details.",
                    file=sys.stderr,
                )
                return 1

        doctor = run([str(VENV_PYTHON), "scripts/doctor.py"], check=False)
        if doctor.returncode:
            return doctor.returncode

        port_status = dashboard_port_status()
        if port_status == "running":
            print("\nYouTube Clipper is already running at http://127.0.0.1:8765")
            return 0
        if port_status == "occupied":
            print(
                "\nPort 8765 is being used by another application. Stop that application "
                "or change the dashboard port before starting YouTube Clipper.",
                file=sys.stderr,
            )
            return 1

        print("\nStarting YouTube Clipper at http://127.0.0.1:8765")
        return run([str(VENV_PYTHON), "dashboard.py"], check=False).returncode
    except subprocess.CalledProcessError as exc:
        print(
            f"\nSetup stopped because a command failed (exit code {exc.returncode}). "
            "Review the message above, then run this starter again.",
            file=sys.stderr,
        )
        return exc.returncode or 1
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
