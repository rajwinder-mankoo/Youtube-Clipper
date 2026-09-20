"""Safely shorten a rendered Short while keeping its editing assets aligned."""

from __future__ import annotations

import json
import math
import os
import subprocess
import uuid
from pathlib import Path

from youtube_clipper import config
from youtube_clipper.video.framing import interpolate_framing
from youtube_clipper.video.limits import MAX_SHORT_DURATION_SECONDS


MIN_TRIM_DURATION_SECONDS = 1.0


def normalize_trim(start, end, duration):
    try:
        start, end, duration = float(start), float(end), float(duration)
    except (TypeError, ValueError) as exc:
        raise ValueError("Trim times must be numbers.") from exc
    if not all(math.isfinite(value) for value in (start, end, duration)) or duration <= 0:
        raise ValueError("The video duration and trim times must be finite.")
    start = max(0.0, min(start, duration))
    end = max(0.0, min(end, duration))
    if end - start < MIN_TRIM_DURATION_SECONDS:
        raise ValueError("Keep at least one second of video.")
    if end - start > MAX_SHORT_DURATION_SECONDS + 0.001:
        raise ValueError(f"A Short can be at most {MAX_SHORT_DURATION_SECONDS:g} seconds.")
    return round(start, 3), round(end, 3)


def trim_timeline(timeline, start, end):
    """Trim a compressed-output timeline while retaining original source times."""
    trimmed = []
    for item in timeline or []:
        output_start = float(item["output_start"])
        source_start = float(item["source_start"])
        source_end = float(item["source_end"])
        output_end = output_start + source_end - source_start
        kept_start = max(start, output_start)
        kept_end = min(end, output_end)
        if kept_end <= kept_start:
            continue
        trimmed.append({
            "source_start": round(source_start + kept_start - output_start, 3),
            "source_end": round(source_start + kept_end - output_start, 3),
            "output_start": round(kept_start - start, 3),
        })
    return trimmed


def trim_keyframes(keyframes, start, end):
    if not keyframes:
        return []
    first = interpolate_framing(keyframes, start)
    result = [{
        "time": 0.0,
        "center_x": round(first[0], 5),
        "center_y": round(first[1], 5),
        "zoom": round(first[2], 3),
    }]
    for frame in keyframes:
        timestamp = float(frame.get("time", 0))
        if start < timestamp < end:
            result.append({**frame, "time": round(timestamp - start, 3)})
    return result


def video_duration(path):
    import cv2
    capture = cv2.VideoCapture(str(path))
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
    finally:
        capture.release()
    duration = frames / fps if fps > 0 and frames > 0 else 0
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Could not read the Short duration.")
    return duration


def trim_info(video_path):
    video = Path(video_path).resolve()
    video.relative_to(config.OUTPUT_DIR.resolve())
    manifest = video.with_suffix(".manifest.json")
    if not video.is_file() or not manifest.is_file():
        raise ValueError("This Short has no editable manifest. Regenerate it first.")
    return {
        "video_path": str(video),
        "duration": round(video_duration(video), 3),
        "max_duration": MAX_SHORT_DURATION_SECONDS,
    }


def _render_trim(source, destination, start, duration):
    result = subprocess.run([
        config.FFMPEG, "-y", "-ss", str(start), "-i", str(source),
        "-t", str(duration), "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        str(destination),
    ], cwd=config.PROJECT_ROOT, capture_output=True, text=True)
    if result.returncode or not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError((result.stderr or "Video trimming failed.")[-1200:])


def _trim_subtitles(source, destination, start, end):
    import pysubs2
    subtitles = pysubs2.load(str(source), encoding="utf-8")
    start_ms, end_ms = round(start * 1000), round(end * 1000)
    events = []
    for event in subtitles.events:
        if event.is_comment or event.end <= start_ms or event.start >= end_ms:
            continue
        event.start = max(event.start, start_ms) - start_ms
        event.end = min(event.end, end_ms) - start_ms
        if event.end > event.start:
            events.append(event)
    subtitles.events = events
    subtitles.save(str(destination), encoding="utf-8")


def trim_short(video_path, start, end):
    """Trim a Short and its editable derivatives as one recoverable operation."""
    video = Path(video_path).resolve()
    video.relative_to(config.OUTPUT_DIR.resolve())
    manifest_path = video.with_suffix(".manifest.json")
    if not video.is_file() or not manifest_path.is_file():
        raise ValueError("The selected Short is not editable.")
    duration = video_duration(video)
    start, end = normalize_trim(start, end, duration)
    kept_duration = round(end - start, 3)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    media = [video]
    for raw in (manifest.get("source_master_file"), video.with_suffix(".clean.mp4")):
        if not raw:
            continue
        path = Path(raw).resolve()
        path.relative_to(config.OUTPUT_DIR.resolve())
        if path.is_file() and path not in media:
            media.append(path)

    caption = Path(manifest.get("caption_file", "")).resolve()
    caption.relative_to(config.CACHE_DIR.resolve())
    if not caption.is_file() or caption.suffix.lower() != ".ass":
        raise ValueError("The caption layout is missing. Regenerate this Short.")

    clip = manifest.get("clip") if isinstance(manifest.get("clip"), dict) else {}
    timeline = trim_timeline(clip.get("timeline"), start, end)
    if timeline:
        clip["timeline"] = timeline
        clip["start"] = timeline[0]["source_start"]
        clip["end"] = timeline[-1]["source_end"]
    clip["duration"] = kept_duration
    framing = manifest.get("framing") if isinstance(manifest.get("framing"), dict) else {}
    if isinstance(framing.get("keyframes"), list):
        framing["keyframes"] = trim_keyframes(framing["keyframes"], start, end)
    manifest["clip"] = clip
    manifest["framing"] = framing
    manifest["trim"] = {"start": start, "end": end, "duration": kept_duration}
    manifest["schema_version"] = max(4, int(manifest.get("schema_version", 1)))

    token = uuid.uuid4().hex
    replacements = []
    backups = []
    try:
        for source in media:
            temporary = source.with_name(f".{source.stem}.{token}.trim.mp4")
            _render_trim(source, temporary, start, kept_duration)
            replacements.append((temporary, source))
        caption_temp = caption.with_name(f".{caption.stem}.{token}.trim.ass")
        _trim_subtitles(caption, caption_temp, start, end)
        replacements.append((caption_temp, caption))
        manifest_temp = manifest_path.with_name(f".{manifest_path.stem}.{token}.trim.json")
        manifest_temp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        replacements.append((manifest_temp, manifest_path))

        for temporary, destination in replacements:
            backup = destination.with_name(f".{destination.name}.{token}.bak")
            os.replace(destination, backup)
            backups.append((backup, destination))
            os.replace(temporary, destination)
    except Exception:
        for backup, destination in reversed(backups):
            if backup.exists():
                destination.unlink(missing_ok=True)
                os.replace(backup, destination)
        raise
    finally:
        for temporary, _ in replacements:
            temporary.unlink(missing_ok=True)
        for backup, _ in backups:
            backup.unlink(missing_ok=True)

    return {"duration": kept_duration, "start": start, "end": end}
