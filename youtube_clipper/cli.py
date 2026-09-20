"""Run the full YT Auto Bot pipeline: edit -> source intelligence -> upload."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def run(script: str, env: dict[str, str]) -> int:
    print("\n" + "=" * 70)
    print(f"RUNNING {script}")
    print("=" * 70)
    result = subprocess.run([PYTHON, str(PROJECT_DIR / script)], cwd=PROJECT_DIR, env=env)
    return result.returncode


def main() -> int:
    env = os.environ.copy()
    env.setdefault("YT_AUTO_BOT_PAUSE", "0")

    args = sys.argv[1:]
    dry_run = any(arg.lower() == "--dry-run" for arg in args)
    args = [arg for arg in args if arg.lower() != "--dry-run"]
    if len(args) != 1:
        mode = ""
    else:
        mode = args[0]
    normalized_mode = mode.lower()
    if normalized_mode in {"local", "--local"}:
        env["YT_AUTO_BOT_SOURCE_MODE"] = "local"
        env["YT_AUTO_BOT_AUTO_LOCAL"] = "1"
    elif normalized_mode.startswith("http://") or normalized_mode.startswith("https://"):
        env["YT_AUTO_BOT_SOURCE_MODE"] = "youtube"
        env["YT_AUTO_BOT_YOUTUBE_URL"] = mode
    elif normalized_mode in {"youtube", "--youtube"}:
        url = input("YouTube URL: ").strip()
        env["YT_AUTO_BOT_SOURCE_MODE"] = "youtube"
        env["YT_AUTO_BOT_YOUTUBE_URL"] = url
    else:
        print("Usage:")
        print("  python run_all.py local [--dry-run]")
        print("  python run_all.py <YouTube URL> [--dry-run]")
        print("  python run_all.py youtube [--dry-run]")
        print("\nThe last form prompts for a URL.")
        return 2

    if dry_run:
        env["YT_AUTO_BOT_DRY_RUN"] = "1"

    # youtube_automator.py is interactive by default. os.environ.copy() above
    # preserves an explicit override for either unattended or confirmed mode.

    code = run("main.py", env)
    if code != 0:
        print(f"main.py failed with exit code {code}.")
        return code

    code = run("youtube_automator.py", env)
    if code != 0:
        print(f"youtube_automator.py failed with exit code {code}.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
