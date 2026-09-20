"""Pure, testable publishing-policy helpers for YouTube uploads."""

from __future__ import annotations


PUBLISHING_MODES = {"scheduled", "private", "public"}


def normalize_publishing_mode(value: object) -> str:
    mode = str(value or "scheduled").strip().lower()
    if mode not in PUBLISHING_MODES:
        allowed = ", ".join(sorted(PUBLISHING_MODES))
        raise ValueError(f"privacy_status must be one of: {allowed}.")
    return mode


def unattended_upload_enabled(env_value: str | None, default: bool = False) -> bool:
    """Environment value wins; absence falls back to the configured default."""
    if env_value is None:
        return bool(default)
    return env_value.strip().lower() in {"1", "true", "yes", "on"}


def publishing_action(mode: str, position: int, total: int) -> tuple[str, bool]:
    """Return (privacy, needs_schedule_slot) for one item in an ordered batch."""
    mode = normalize_publishing_mode(mode)
    if total < 1 or not 0 <= position < total:
        raise ValueError("position must identify an item in the upload batch.")
    if mode == "public":
        return "public", False
    if mode == "private":
        return "private", False
    if position == total - 1:
        return "public", False
    return "private", True
