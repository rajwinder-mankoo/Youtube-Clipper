"""Shared duration limits for generated and edited Shorts."""

from __future__ import annotations

import math


# Keep a full second of headroom below one minute. This avoids a nominally
# 60-second render crossing the boundary because of frame or container timing.
MAX_SHORT_DURATION_SECONDS = 59.0


def cap_intervals(intervals, max_duration=MAX_SHORT_DURATION_SECONDS):
    """Trim ordered source intervals to at most ``max_duration`` seconds."""
    try:
        limit = float(max_duration)
    except (TypeError, ValueError) as exc:
        raise ValueError("The Short duration limit must be a number.") from exc
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("The Short duration limit must be positive and finite.")

    capped = []
    remaining = limit
    for raw_start, raw_end in intervals:
        start, end = float(raw_start), float(raw_end)
        if not all(math.isfinite(value) for value in (start, end)) or end <= start:
            raise ValueError("Short pacing intervals must be finite and ordered.")
        if remaining <= 0:
            break
        kept_end = min(end, start + remaining)
        capped.append((start, kept_end))
        remaining -= kept_end - start

    return capped
