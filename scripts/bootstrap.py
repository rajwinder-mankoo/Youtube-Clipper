"""Create a local development environment and verify system dependencies."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
RUNTIME_DIRS = ("input", "output", "cache", "logs", "music")
SYSTEM_TOOLS = ("ffmpeg", "ffprobe", "deno", "tesseract")


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def run(command: list[str]) -> None:
    print("+", " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Create configuration and directories without installing Python packages.",
    )
    args = parser.parse_args()

    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer is required.")

    for name in RUNTIME_DIRS:
        (ROOT / name).mkdir(exist_ok=True)

    accounts = ROOT / "config" / "accounts.json"
    example = ROOT / "config" / "accounts.example.json"
    if not accounts.exists() and example.exists():
        shutil.copy2(example, accounts)
        print("Created config/accounts.json from the safe example.")

    if not venv_python().is_file():
        if VENV.exists():
            print(f"The existing environment at {VENV} is incomplete; repairing it.")
        else:
            print(f"Creating virtual environment at {VENV}")
        venv.EnvBuilder(with_pip=True).create(VENV)

    if not args.skip_install:
        python = str(venv_python())
        run([python, "-m", "pip", "install", "--upgrade", "pip"])
        run([python, "-m", "pip", "install", "-r", "requirements.txt"])

    missing = [tool for tool in SYSTEM_TOOLS if shutil.which(tool) is None]
    print("\nSetup complete.")
    if missing:
        print("Install these system tools before running the full pipeline:")
        for tool in missing:
            print(f"  - {tool}")
    else:
        print("FFmpeg, FFprobe, Deno, and Tesseract are available.")

    activate = (
        r".\.venv\Scripts\Activate.ps1"
        if sys.platform == "win32"
        else "source .venv/bin/activate"
    )
    launch = (
        r".\.venv\Scripts\python.exe"
        if sys.platform == "win32"
        else ".venv/bin/python"
    )
    print(f"\nActivate: {activate}")
    print(f"Start:    {launch} dashboard.py")
    print(f"Check:    {launch} scripts/check.py")
    print(f"Doctor:   {launch} scripts/doctor.py")
    print("Using the command above works even when virtual-environment activation is unavailable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
