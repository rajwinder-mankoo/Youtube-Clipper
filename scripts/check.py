"""Run the repository's portable regression and syntax checks."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(label: str, command: list[str]) -> None:
    print(f"\n== {label} ==", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    python = sys.executable
    run("Compile Python", [python, "-m", "compileall", "-q", "youtube_clipper", "tests"])
    run("Metadata and framing regressions", [python, "test_isolation.py"])
    run(
        "Unit tests",
        [
            python,
            "-m",
            "unittest",
            "tests.test_publishing_safety",
            "tests.test_storage_cleanup",
            "tests.test_captions",
            "tests.test_framing_paths",
            "tests.test_short_duration",
            "tests.test_trimming",
            "tests.test_caption_style",
            "tests.test_accounts_analytics",
            "tests.test_setup",
            "-v",
        ],
    )

    node = shutil.which("node")
    if node:
        run("Dashboard JavaScript", [node, "--check", "dashboard/app.js"])
        run("Caption editor JavaScript", [node, "--check", "dashboard/captions.js"])
        run("Timeline editor JavaScript", [node, "--check", "dashboard/trim.js"])
        run("Crop editor transitions", [node, "tests/test_crop_editor.cjs"])
    else:
        print("\nNode.js not found; skipped browser JavaScript checks.")

    print("\nAll available checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
