"""
Centralized configuration for YT Auto Bot.

Single source of truth for filesystem paths (input / cache / output / logs /
config) and for a few settings that used to be hardcoded and duplicated
across main.py, youtube_automator.py and source_detector.py.

Everything is derived from PROJECT_ROOT, the folder this file lives in --
NOT from a hardcoded drive letter. If the project is ever moved or copied to
a new machine/drive, nothing here needs to be edited.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# ============================================================
# PROJECT ROOT
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Kept as an alias: every other module previously imported "PROJECT_DIR".
PROJECT_DIR = PROJECT_ROOT

INPUT_DIR = PROJECT_ROOT / "input"
CACHE_DIR = PROJECT_ROOT / "cache"
OUTPUT_DIR = PROJECT_ROOT / "output"
LOGS_DIR = PROJECT_ROOT / "logs"
CONFIG_DIR = PROJECT_ROOT / "config"

for _directory in (INPUT_DIR, CACHE_DIR, OUTPUT_DIR, LOGS_DIR, CONFIG_DIR):
    _directory.mkdir(parents=True, exist_ok=True)

# Cache subfolders, named here once so call sites read clearly.
RENDER_CACHE_DIR = CACHE_DIR / "render"
YOUTUBE_SOURCE_CACHE_DIR = CACHE_DIR / "youtube_source"

# ------------------------------------------------------------------
# One-time migration: earlier builds stored everything (transcripts,
# source-context files, render scratch files, the upload log, etc.) in a
# folder named "temp" instead of "cache". If that folder exists from before
# the computer reset, move its contents into cache/ once so nothing already
# generated is silently lost or orphaned. Never touches input/ or output/.
# ------------------------------------------------------------------
_LEGACY_TEMP_DIR = PROJECT_ROOT / "temp"
if _LEGACY_TEMP_DIR.exists() and _LEGACY_TEMP_DIR.is_dir():
    for _item in list(_LEGACY_TEMP_DIR.iterdir()):
        _target = CACHE_DIR / _item.name
        if not _target.exists():
            try:
                shutil.move(str(_item), str(_target))
            except OSError:
                pass

# ============================================================
# USER-EDITABLE SETTINGS (config/settings.json)
# ============================================================

SETTINGS_PATH = CONFIG_DIR / "settings.json"

_DEFAULT_SETTINGS = {
    # Only used if YT_AUTO_BOT_FFMPEG is not set and ffmpeg is not on PATH.
    "ffmpeg_path": "",
    "whisper_model": "small",
    "schedule_interval_hours": 12,
    "start_delay_minutes": 10,
    "privacy_status": "scheduled",
    # Safe default: command-line uploads require confirmation. The dashboard
    # opts into unattended mode explicitly after the user clicks Upload.
    "auto_upload_default": False,
    "channel_keywords": [],
    "default_channel_keywords": ["shorts", "youtube shorts"],
}


def _load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = dict(_DEFAULT_SETTINGS)
                merged.update(data)
                return merged
        except Exception:
            pass
    # First run on this machine: write the defaults out so there's a real
    # file to edit instead of values buried in source code.
    try:
        SETTINGS_PATH.write_text(
            json.dumps(_DEFAULT_SETTINGS, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return dict(_DEFAULT_SETTINGS)


SETTINGS = _load_settings()


def setting(name: str, default=None):
    """Return a setting while keeping callers independent of JSON loading."""
    return SETTINGS.get(name, default)


def string_list_setting(name: str, default=None) -> list[str]:
    """Return a normalized list of non-empty strings from a JSON setting."""
    value = SETTINGS.get(name, default or [])
    if not isinstance(value, list):
        return list(default or [])
    return [str(item).strip() for item in value if str(item).strip()]


def bool_setting(name: str, default: bool = False) -> bool:
    """Return a boolean setting without treating the string "false" as true."""
    value = SETTINGS.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(default)

# ============================================================
# FFMPEG
# ============================================================


def _resolve_ffmpeg() -> str:
    """Resolve the ffmpeg executable path without a hardcoded, reset-fragile path.

    Order of precedence:
      1. YT_AUTO_BOT_FFMPEG environment variable (explicit override).
      2. "ffmpeg_path" in config/settings.json, if that file actually exists.
      3. ffmpeg found on PATH (works if ffmpeg was installed globally).
      4. The configured path anyway (so the existing "FFmpeg not found"
         check in main.py still prints a real, useful path).
    """
    env_override = os.environ.get("YT_AUTO_BOT_FFMPEG", "").strip()
    if env_override:
        return env_override

    configured = str(SETTINGS.get("ffmpeg_path") or "").strip()
    if configured and Path(configured).exists():
        return configured

    found = shutil.which("ffmpeg")
    if found:
        return found

    return configured or "ffmpeg"


FFMPEG = _resolve_ffmpeg()

# ============================================================
# LOGGING
# ============================================================

_RUN_STAMP = datetime.now().strftime("%Y%m%d_%H%M%S_%f")


class _Tee:
    """Mirrors writes to an existing stream into a log file too.

    Every existing print() call keeps behaving exactly as before (same
    console output); this only adds a persistent copy under logs/.
    """

    def __init__(self, stream, log_file):
        self._stream = stream
        self._log_file = log_file

    def write(self, data):
        # Always preserve the original Unicode text in the UTF-8 log file.
        # Console/pipe output is best-effort: some Windows launchers expose
        # stdout as cp1252 and will raise UnicodeEncodeError for characters
        # such as the right arrow (\u2192). In that case, encode using the
        # actual stream encoding with replacement instead of attempting to
        # write the original Unicode string again.
        try:
            self._stream.write(data)
        except UnicodeEncodeError:
            try:
                encoding = getattr(self._stream, "encoding", None) or "utf-8"
                safe_data = data.encode(encoding, errors="replace").decode(encoding, errors="replace")
                self._stream.write(safe_data)
            except Exception:
                # Console output must never be allowed to terminate the bot.
                pass
        except (BrokenPipeError, OSError, ValueError):
            # The frontend/process pipe may disappear during shutdown.
            pass

        try:
            self._log_file.write(data)
            self._log_file.flush()
        except Exception:
            pass

    def flush(self):
        self._stream.flush()
        try:
            self._log_file.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        # Delegate anything else (isatty, encoding, etc.) to the real stream.
        return getattr(self._stream, name)


def setup_logging(script_name: str) -> Path:
    """Start teeing stdout/stderr into logs/<script>_<run>.log. Call once per process."""
    log_path = LOGS_DIR / f"{script_name}_{_RUN_STAMP}.log"
    log_file = open(log_path, "a", encoding="utf-8")

    # Prefer UTF-8 for Windows console/pipe output so Unicode technical
    # markers (for example, arrows) remain visible instead of crashing a
    # run with UnicodeEncodeError under cp1252.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)
    return log_path
