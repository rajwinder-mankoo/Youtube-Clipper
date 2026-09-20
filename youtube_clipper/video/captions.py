"""Edit rendered captions without cropping again or burning over old text."""

import hashlib
import json
import math
import os
import re
import subprocess
import uuid
from copy import deepcopy
from pathlib import Path

from youtube_clipper import config
from youtube_clipper.video.limits import MAX_SHORT_DURATION_SECONDS


CAPTION_POSITIONS = {"bottom": 2, "middle": 5, "top": 8}


def normalize_caption_style(raw):
    raw = raw if isinstance(raw, dict) else {}
    font_name = str(raw.get("font_name", "Arial")).strip()
    if not font_name or len(font_name) > 80 or re.search(r"[{}\\\x00-\x1f]", font_name):
        raise ValueError("Choose a valid caption font name.")

    def number(name, default, minimum, maximum):
        try:
            value = float(raw.get(name, default))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Caption {name.replace('_', ' ')} must be a number.") from exc
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(
                f"Caption {name.replace('_', ' ')} must be between {minimum:g} and {maximum:g}."
            )
        return value

    def color(name, default):
        value = str(raw.get(name, default)).strip().upper()
        if not re.fullmatch(r"#[0-9A-F]{6}", value):
            raise ValueError(f"Caption {name.replace('_', ' ')} must be a six-digit color.")
        return value

    position = str(raw.get("position", "bottom")).lower()
    if position not in CAPTION_POSITIONS:
        raise ValueError("Caption position must be top, middle, or bottom.")
    return {
        "font_name": font_name,
        "font_size": round(number("font_size", 38, 20, 120), 1),
        "text_color": color("text_color", "#FFFFFF"),
        "outline_color": color("outline_color", "#000000"),
        "outline": round(number("outline", 3, 0, 10), 1),
        "shadow": round(number("shadow", 1, 0, 10), 1),
        "position": position,
        "margin": round(number("margin", 150, 20, 600)),
    }


def _hex_to_color(value, pysubs2):
    value = value.lstrip("#")
    return pysubs2.Color(int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _color_to_hex(value):
    return f"#{value.r:02X}{value.g:02X}{value.b:02X}"


def caption_style(subtitles):
    style = subtitles.styles.get("Default") or next(iter(subtitles.styles.values()))
    position = next((name for name, alignment in CAPTION_POSITIONS.items() if alignment == style.alignment), "bottom")
    font_name = str(style.fontname or "").strip()
    if not font_name or len(font_name) > 80 or re.search(r"[{}\\\x00-\x1f]", font_name):
        font_name = "Arial"

    def legacy_number(value, default, minimum, maximum):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) and minimum <= value <= maximum else default

    return normalize_caption_style({
        "font_name": font_name,
        "font_size": legacy_number(style.fontsize, 38, 20, 120),
        "text_color": _color_to_hex(style.primarycolor),
        "outline_color": _color_to_hex(style.outlinecolor),
        "outline": legacy_number(style.outline, 3, 0, 10),
        "shadow": legacy_number(style.shadow, 1, 0, 10),
        "position": position,
        "margin": legacy_number(style.marginv, 150, 20, 600),
    })


def apply_caption_style(subtitles, raw_style, pysubs2):
    values = normalize_caption_style(raw_style)
    style = subtitles.styles.get("Default") or next(iter(subtitles.styles.values()))
    style.fontname = values["font_name"]
    style.fontsize = values["font_size"]
    style.primarycolor = _hex_to_color(values["text_color"], pysubs2)
    style.outlinecolor = _hex_to_color(values["outline_color"], pysubs2)
    style.outline = values["outline"]
    style.shadow = values["shadow"]
    style.alignment = CAPTION_POSITIONS[values["position"]]
    style.marginv = values["margin"]
    # Generated karaoke captions contain an explicit white reset color. Keep
    # the active-word highlight but make the resting words follow this style.
    rgb = values["text_color"].lstrip("#")
    ass_bgr = rgb[4:6] + rgb[2:4] + rgb[0:2]
    for event in subtitles.events:
        event.text = re.sub(r"\\c&HFFFFFF&", rf"\\c&H{ass_bgr}&", event.text, flags=re.I)
    return values


def retain_clean_video(raw_video, audio_video, destination):
    destination = Path(destination)
    temporary = destination.with_name(f".clean_{uuid.uuid4().hex}.mp4")
    result = subprocess.run([
        config.FFMPEG, "-y", "-i", str(raw_video), "-i", str(audio_video),
        "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "19", "-c:a", "copy", "-t", str(MAX_SHORT_DURATION_SECONDS),
        "-shortest", "-movflags", "+faststart", str(temporary),
    ], capture_output=True, text=True)
    if result.returncode or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise RuntimeError((result.stderr or "Could not retain the caption editing video.")[-1200:])
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def caption_paths(video):
    video = Path(video).resolve()
    video.relative_to(config.OUTPUT_DIR.resolve())
    manifest = json.loads(video.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    caption = Path(manifest["caption_file"]).resolve()
    caption.relative_to(config.CACHE_DIR.resolve())
    if caption.suffix.lower() != ".ass" or not caption.is_file():
        raise ValueError("The caption layout is missing. Regenerate this Short.")
    clean = video.with_suffix(".clean.mp4")
    if not clean.is_file():
        raise ValueError("This Short needs a clean editing copy. Save its crop again or regenerate it first.")
    clean.resolve().relative_to(config.OUTPUT_DIR.resolve())
    return video, caption, clean


def revision(paths):
    return hashlib.sha256("|".join(
        f"{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in paths
    ).encode()).hexdigest()


def caption_info(video):
    import pysubs2
    paths = caption_paths(video)
    subs = pysubs2.load(str(paths[1]), encoding="utf-8")
    return {"revision": revision(paths), "preview_path": str(paths[2]), "style": caption_style(subs), "cues": [
        {"start": event.start / 1000, "end": event.end / 1000, "text": event.plaintext}
        for event in subs if not event.is_comment
    ]}


def validate_cues(cues, duration):
    if not isinstance(cues, list) or not 1 <= len(cues) <= 1000:
        raise ValueError("Use between 1 and 1000 caption lines.")
    normalized = []
    previous_end = 0
    for index, cue in enumerate(cues, 1):
        if not isinstance(cue, dict):
            raise ValueError(f"Caption {index} is invalid.")
        try:
            start, end = float(cue["start"]), float(cue["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Caption {index}: enter start and end times in seconds.") from exc
        text = cue.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ValueError(f"Caption {index}: use 1–500 characters.")
        if any(char in text for char in ("{", "}", "\\", "\x00")):
            raise ValueError(f"Caption {index}: braces and backslashes are not supported.")
        if not all(math.isfinite(value) for value in (start, end)):
            raise ValueError(f"Caption {index}: times must be finite.")
        start, end = round(start * 100) * 10, round(end * 100) * 10
        if start < previous_end or end <= start or end > round(duration * 1000):
            raise ValueError(f"Caption {index}: times must be ordered, non-overlapping, and within the video.")
        normalized.append({"start": start, "end": end, "text": text.strip()})
        previous_end = end
    return normalized


def save_captions(video, cues, expected_revision, style=None):
    import pysubs2
    paths = caption_paths(video)
    video, caption, clean = paths
    if revision(paths) != expected_revision:
        raise ValueError("This Short changed while you were editing. Close and reopen the editor.")
    # Probe the actual compressed video, not the original source duration.
    import cv2
    capture = cv2.VideoCapture(str(clean))
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        duration = capture.get(cv2.CAP_PROP_FRAME_COUNT) / fps if fps > 0 else 0
    finally:
        capture.release()
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Could not read the editing video's duration.")
    render_duration = min(duration, MAX_SHORT_DURATION_SECONDS)
    cues = validate_cues(cues, render_duration)
    subs = pysubs2.load(str(caption), encoding="utf-8")
    normalized_style = apply_caption_style(subs, style or caption_style(subs), pysubs2)
    old_events = [event for event in subs if not event.is_comment]
    subs.events = []
    for index, cue in enumerate(cues):
        old = old_events[index] if index < len(old_events) else None
        if old and old.plaintext == cue["text"] and old.start == cue["start"] and old.end == cue["end"]:
            event = deepcopy(old)
        else:
            event = pysubs2.SSAEvent(start=cue["start"], end=cue["end"], style=old.style if old else "Default")
            event.plaintext = cue["text"]
        subs.append(event)
    token = uuid.uuid4().hex
    temporary_caption = config.CACHE_DIR / f"caption_edit_{token}.ass"
    temporary_video = video.with_name(f".caption_edit_{token}.mp4")
    original_caption = caption.read_bytes()
    try:
        subs.save(str(temporary_caption), encoding="utf-8")
        relative = temporary_caption.relative_to(config.PROJECT_ROOT).as_posix()
        result = subprocess.run([
            config.FFMPEG, "-y", "-i", str(clean), "-vf", f"ass={relative}",
            "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "medium",
            "-crf", "19", "-c:a", "copy", "-t", str(render_duration),
            "-movflags", "+faststart", str(temporary_video),
        ], cwd=config.PROJECT_ROOT, capture_output=True, text=True)
        if result.returncode or not temporary_video.is_file():
            raise RuntimeError((result.stderr or "Caption rendering failed.")[-1200:])
        if revision(paths) != expected_revision:
            raise ValueError("The Short changed during rendering. Reopen the editor.")
        os.replace(temporary_caption, caption)
        try:
            os.replace(temporary_video, video)
        except OSError:
            caption.write_bytes(original_caption)
            raise
    finally:
        temporary_caption.unlink(missing_ok=True)
        temporary_video.unlink(missing_ok=True)
    result = caption_info(video)
    result["style"] = normalized_style
    return result
