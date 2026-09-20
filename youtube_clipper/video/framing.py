"""Re-render a generated Short from its retained full-frame editing master."""

from __future__ import annotations

import json
import math
import os
import subprocess
import uuid
from pathlib import Path

from youtube_clipper import config as bot_config
from youtube_clipper.video.limits import MAX_SHORT_DURATION_SECONDS


OUTPUT = bot_config.OUTPUT_DIR.resolve()
WIDTH = 1080
HEIGHT = 1920


def _inside_output(raw_path, *, suffix=None):
    path = Path(raw_path).resolve()
    path.relative_to(OUTPUT)
    if suffix and path.suffix.lower() != suffix:
        raise ValueError(f"Expected a {suffix} file.")
    return path


def _caption_file_and_filter_path(raw_path):
    """Validate a caption under the configured cache, including a linked cache."""
    caption = Path(raw_path).resolve()
    cache_root = bot_config.CACHE_DIR.resolve()
    try:
        relative = caption.relative_to(cache_root)
    except ValueError as exc:
        raise ValueError("The caption file is outside the configured cache folder.") from exc
    if caption.suffix.lower() != ".ass" or not caption.is_file():
        raise ValueError("The caption layout is missing. Regenerate this Short.")

    # Prefer the application-facing path (for example cache/... through the
    # Proxmox symlink) so FFmpeg can run from PROJECT_ROOT as before.
    configured_path = bot_config.CACHE_DIR / relative
    try:
        filter_path = configured_path.relative_to(bot_config.PROJECT_ROOT).as_posix()
    except ValueError:
        filter_path = caption.as_posix()
    # Escape characters meaningful to FFmpeg's filter parser. subprocess
    # already handles shell quoting; these escapes are for the ass filter.
    for character in ("\\", ":", "'", ",", "[", "]"):
        filter_path = filter_path.replace(character, f"\\{character}")
    return caption, filter_path


def crop_geometry(source_width, source_height, center_x, center_y, zoom):
    """Return a clamped 9:16 crop rectangle in source pixels."""
    if source_width < 2 or source_height < 2:
        raise ValueError("The editing master has invalid dimensions.")
    center_x, center_y, zoom = normalize_framing(center_x, center_y, zoom)

    crop_height = source_height / zoom
    crop_width = crop_height * 9 / 16
    if crop_width > source_width:
        crop_width = source_width / zoom
        crop_height = crop_width * 16 / 9

    crop_width = max(2, min(source_width, int(round(crop_width))))
    crop_height = max(2, min(source_height, int(round(crop_height))))
    left = int(round(source_width * center_x - crop_width / 2))
    top = int(round(source_height * center_y - crop_height / 2))
    left = max(0, min(source_width - crop_width, left))
    top = max(0, min(source_height - crop_height, top))
    return left, top, crop_width, crop_height


def normalize_framing(center_x, center_y, zoom):
    try:
        values = [float(center_x), float(center_y), float(zoom)]
    except (TypeError, ValueError) as exc:
        raise ValueError("Framing values must be numbers.") from exc
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Framing values must be finite numbers.")
    return (
        min(1.0, max(0.0, values[0])),
        min(1.0, max(0.0, values[1])),
        min(3.0, max(1.0, values[2])),
    )


def normalize_keyframes(keyframes, duration=None):
    """Validate, sort and deduplicate manual framing keyframes."""
    if not isinstance(keyframes, list):
        raise ValueError("Framing keyframes must be a list.")
    if len(keyframes) > 100:
        raise ValueError("A Short can have at most 100 framing keyframes.")

    normalized = {}
    for item in keyframes:
        if not isinstance(item, dict):
            raise ValueError("Each framing keyframe must be an object.")
        try:
            timestamp = float(item.get("time", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("Keyframe times must be numbers.") from exc
        if not math.isfinite(timestamp):
            raise ValueError("Keyframe times must be finite numbers.")
        timestamp = max(0.0, timestamp)
        if duration is not None:
            timestamp = min(float(duration), timestamp)
        center_x, center_y, zoom = normalize_framing(
            item.get("center_x", 0.5),
            item.get("center_y", 0.5),
            item.get("zoom", 1.0),
        )
        key = round(timestamp, 3)
        normalized[key] = {
            "time": key,
            "center_x": round(center_x, 5),
            "center_y": round(center_y, 5),
            "zoom": round(zoom, 3),
        }

    ordered = [normalized[key] for key in sorted(normalized)]
    if not ordered:
        raise ValueError("At least one framing keyframe is required.")
    if ordered[0]["time"] > 0:
        ordered.insert(0, {**ordered[0], "time": 0.0})
    return ordered


def _interpolate_normalized(frames, timestamp):
    try:
        timestamp = float(timestamp)
    except (TypeError, ValueError) as exc:
        raise ValueError("The framing timestamp must be a number.") from exc
    if not math.isfinite(timestamp):
        raise ValueError("The framing timestamp must be finite.")
    if timestamp <= frames[0]["time"]:
        return frames[0]["center_x"], frames[0]["center_y"], frames[0]["zoom"]
    if timestamp >= frames[-1]["time"]:
        return frames[-1]["center_x"], frames[-1]["center_y"], frames[-1]["zoom"]

    for left, right in zip(frames, frames[1:]):
        if left["time"] <= timestamp <= right["time"]:
            span = right["time"] - left["time"]
            ratio = 0.0 if span <= 0 else (timestamp - left["time"]) / span
            return tuple(
                left[field] + (right[field] - left[field]) * ratio
                for field in ("center_x", "center_y", "zoom")
            )
    return frames[-1]["center_x"], frames[-1]["center_y"], frames[-1]["zoom"]


def interpolate_framing(keyframes, timestamp):
    """Linearly interpolate framing values at a point on the clip timeline."""
    return _interpolate_normalized(normalize_keyframes(keyframes), timestamp)


def reframe_short(video_path, center_x=0.5, center_y=0.5, zoom=1.0, keyframes=None):
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for manual framing. Install the project requirements first."
        ) from exc

    center_x, center_y, zoom = normalize_framing(center_x, center_y, zoom)
    output_file = _inside_output(video_path, suffix=".mp4")
    if not output_file.is_file() or not output_file.name.lower().endswith(".mp4"):
        raise ValueError("The selected Short no longer exists.")

    manifest_path = output_file.with_suffix(".manifest.json")
    if not manifest_path.is_file():
        raise ValueError("This Short has no editing manifest. Regenerate it first.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_master = _inside_output(manifest.get("source_master_file", ""), suffix=".mp4")
    if not source_master.is_file():
        raise ValueError("The full-frame editing master is missing. Regenerate this Short.")

    caption_file, caption_filter_path = _caption_file_and_filter_path(
        manifest.get("caption_file", "")
    )

    cap = cv2.VideoCapture(str(source_master))
    source_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    source_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = frame_count / fps if frame_count > 0 and fps > 0 else None
    if not cap.isOpened() or source_width < 2 or source_height < 2:
        cap.release()
        raise ValueError("The full-frame editing master could not be opened.")

    render_duration = min(duration, MAX_SHORT_DURATION_SECONDS) if duration else MAX_SHORT_DURATION_SECONDS
    framing_keyframes = normalize_keyframes(
        keyframes if keyframes is not None else [{
            "time": 0, "center_x": center_x, "center_y": center_y, "zoom": zoom,
        }],
        duration=render_duration,
    )
    first = framing_keyframes[0]
    left, top, crop_width, crop_height = crop_geometry(
        source_width, source_height, first["center_x"], first["center_y"], first["zoom"]
    )
    token = uuid.uuid4().hex[:10]
    cropped_temp = output_file.with_name(f".{output_file.stem}.{token}.video.mp4")
    final_temp = output_file.with_name(f".{output_file.stem}.{token}.final.mp4")
    writer = cv2.VideoWriter(
        str(cropped_temp), cv2.VideoWriter_fourcc(*"mp4v"), fps, (WIDTH, HEIGHT)
    )
    if not writer.isOpened():
        cap.release()
        raise ValueError("Could not create the manually framed video.")

    frames = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            current_time = frames / fps
            if current_time >= render_duration:
                break
            frame_center_x, frame_center_y, frame_zoom = _interpolate_normalized(
                framing_keyframes, current_time
            )
            left, top, crop_width, crop_height = crop_geometry(
                source_width, source_height, frame_center_x, frame_center_y, frame_zoom
            )
            cropped = frame[top:top + crop_height, left:left + crop_width]
            resized = cv2.resize(cropped, (WIDTH, HEIGHT), interpolation=cv2.INTER_AREA)
            writer.write(resized)
            frames += 1
    finally:
        cap.release()
        writer.release()

    if frames == 0:
        cropped_temp.unlink(missing_ok=True)
        raise ValueError("No frames could be decoded from the editing master.")

    try:
        command = [
            bot_config.FFMPEG,
            "-y",
            "-i", str(cropped_temp),
            "-i", str(output_file),
            "-vf", f"ass={caption_filter_path}",
            "-map", "0:v:0",
            "-map", "1:a:0?",
            "-t", str(render_duration),
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "19",
            "-c:a", "copy",
            "-shortest",
            "-movflags", "+faststart",
            str(final_temp),
        ]
        result = subprocess.run(
            command,
            cwd=str(bot_config.PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not final_temp.is_file():
            detail = (result.stderr or result.stdout or "FFmpeg failed.")[-1200:]
            raise RuntimeError(detail)
        from youtube_clipper.video.captions import retain_clean_video
        retain_clean_video(cropped_temp, output_file, output_file.with_suffix(".clean.mp4"))
        os.replace(final_temp, output_file)
    finally:
        cropped_temp.unlink(missing_ok=True)
        final_temp.unlink(missing_ok=True)

    manifest["schema_version"] = max(3, int(manifest.get("schema_version", 1)))
    manifest["framing"] = {
        "mode": "manual_keyframes" if len(framing_keyframes) > 1 else "manual",
        "center_x": first["center_x"],
        "center_y": first["center_y"],
        "zoom": first["zoom"],
        "interpolation": "linear",
        "keyframes": framing_keyframes,
        "crop_pixels": {
            "left": left,
            "top": top,
            "width": crop_width,
            "height": crop_height,
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest["framing"]
