"""Report whether this checkout is ready to run YouTube Clipper."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
PYTHON_PACKAGES = {
    "faster_whisper": "faster-whisper",
    "pysubs2": "pysubs2",
    "cv2": "opencv-python",
    "yt_dlp": "yt-dlp",
    "ddgs": "ddgs",
    "pytesseract": "pytesseract",
    "googleapiclient": "google-api-python-client",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "google_auth_httplib2": "google-auth-httplib2",
}
RUNTIME_DIRS = ("input", "output", "cache", "logs", "music")


@dataclass(frozen=True)
class Result:
    level: str
    label: str
    detail: str


def expected_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def missing_python_packages() -> list[str]:
    return [
        package
        for module, package in PYTHON_PACKAGES.items()
        if importlib.util.find_spec(module) is None
    ]


def read_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "file is missing"
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(value, dict):
        return None, "top-level value must be a JSON object"
    return value, None


def account_destinations(value: dict | None) -> tuple[list[dict] | None, str | None]:
    destinations = (value or {}).get("accounts")
    if not isinstance(destinations, list):
        return None, "config/accounts.json must contain an accounts list"
    if not all(
        isinstance(item, dict) and item.get("id") and item.get("platform")
        for item in destinations
    ):
        return None, "every account needs non-empty id and platform values"
    return destinations, None


def collect_results() -> list[Result]:
    results: list[Result] = []
    if sys.version_info >= (3, 10):
        results.append(Result("pass", "Python", sys.version.split()[0]))
    else:
        results.append(Result("fail", "Python", "Python 3.10 or newer is required"))

    expected = expected_python()
    try:
        using_venv = Path(sys.executable).resolve() == expected.resolve()
    except OSError:
        using_venv = False
    results.append(Result(
        "pass" if using_venv else "warn",
        "Environment",
        "project virtual environment" if using_venv else f"run this check with {expected}",
    ))

    missing = missing_python_packages()
    results.append(Result(
        "fail" if missing else "pass",
        "Python packages",
        f"missing: {', '.join(missing)}" if missing else "all required packages are importable",
    ))

    settings, settings_error = read_json(ROOT / "config" / "settings.json")
    accounts, accounts_error = read_json(ROOT / "config" / "accounts.json")
    results.append(Result(
        "fail" if settings_error else "pass",
        "Settings",
        settings_error or "config/settings.json is valid",
    ))
    destinations, schema_error = account_destinations(accounts) if not accounts_error else (None, None)
    account_problem = accounts_error or schema_error
    results.append(Result(
        "fail" if account_problem else "pass",
        "Accounts",
        account_problem or f"{len(destinations)} destination(s) configured",
    ))

    missing_dirs = [name for name in RUNTIME_DIRS if not (ROOT / name).is_dir()]
    unwritable_dirs = [
        name for name in RUNTIME_DIRS
        if (ROOT / name).is_dir() and not os.access(ROOT / name, os.W_OK)
    ]
    results.append(Result(
        "fail" if missing_dirs or unwritable_dirs else "pass",
        "Runtime folders",
        f"missing: {', '.join(missing_dirs)}" if missing_dirs
        else f"not writable: {', '.join(unwritable_dirs)}" if unwritable_dirs
        else "all runtime folders exist and are writable",
    ))

    configured_ffmpeg = str((settings or {}).get("ffmpeg_path") or "").strip()
    ffmpeg_ready = bool(shutil.which("ffmpeg") or (configured_ffmpeg and Path(configured_ffmpeg).is_file()))
    results.append(Result(
        "pass" if ffmpeg_ready else "warn",
        "FFmpeg",
        "available" if ffmpeg_ready else "not found; generation and editing will not work",
    ))
    for tool, purpose in (
        ("ffprobe", "video inspection"),
        ("deno", "reliable YouTube downloads"),
        ("tesseract", "improved source detection (optional)"),
    ):
        found = shutil.which(tool)
        results.append(Result(
            "pass" if found else "warn",
            tool,
            "available" if found else f"not found; needed for {purpose}",
        ))

    oauth = ROOT / "oauth_web_client.json"
    if not oauth.exists():
        results.append(Result(
            "info",
            "YouTube OAuth",
            "not configured; add oauth_web_client.json only when publishing is needed",
        ))
    else:
        oauth_data, oauth_error = read_json(oauth)
        web = (oauth_data or {}).get("web")
        valid_web = isinstance(web, dict) and all(
            web.get(key) for key in ("client_id", "client_secret", "auth_uri", "token_uri")
        )
        results.append(Result(
            "pass" if not oauth_error and valid_web else "warn",
            "YouTube OAuth",
            "web application credentials found" if not oauth_error and valid_web
            else "oauth_web_client.json is not a valid Google Web application credential",
        ))
    return results


def main() -> int:
    print("YouTube Clipper readiness\n")
    results = collect_results()
    icons = {"pass": "PASS", "warn": "WARN", "info": "INFO", "fail": "FAIL"}
    for result in results:
        print(f"[{icons[result.level]}] {result.label}: {result.detail}")
    failures = [result for result in results if result.level == "fail"]
    warnings = [result for result in results if result.level == "warn"]
    print()
    if failures:
        print("Setup is incomplete. Run: python scripts/bootstrap.py")
        return 1
    if warnings:
        print("Dashboard ready. Resolve warnings before generating Shorts.")
    else:
        print("Ready to generate Shorts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
