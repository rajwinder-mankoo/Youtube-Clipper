import subprocess
import os
import json
import re
import shutil
import sys
import hashlib
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pysubs2
from faster_whisper import WhisperModel

from youtube_clipper.metadata.source_detector import detect_video_source
from youtube_clipper import config as bot_config
from youtube_clipper.video.limits import (
    MAX_SHORT_DURATION_SECONDS,
    cap_intervals,
)

# ============================================================
# CONFIG
#
# Paths, the ffmpeg location and logging now come from config.py, which
# derives everything from the project folder itself instead of a hardcoded
# "E:\YT Auto Bot" drive/path. Variable names below are kept the same as
# before so the rest of this file does not need to change.
# ============================================================

PROJECT_DIR = bot_config.PROJECT_DIR

INPUT_DIR = bot_config.INPUT_DIR
OUTPUT_DIR = bot_config.OUTPUT_DIR
TEMP_DIR = bot_config.CACHE_DIR  # "cache" on disk; kept as TEMP_DIR in code below.

# Resolved by config.py: env override -> config/settings.json -> PATH -> configured default.
FFMPEG = bot_config.FFMPEG

bot_config.setup_logging("main")

# Whisper
MODEL_SIZE = os.environ.get(
    "YT_AUTO_BOT_WHISPER_MODEL",
    str(bot_config.setting("whisper_model", "small")),
)
WHISPER_PROGRESS_SECONDS = float(os.environ.get("YT_AUTO_BOT_WHISPER_PROGRESS_SECONDS", "30"))

# Shorts
TARGET_MIN_SHORTS = 5
TARGET_MAX_SHORTS = 10
MIN_CLIP_LENGTH = 20
# Product limit only; never a preferred selection duration. Keeping generated
# videos at 59 seconds leaves headroom below the requested one-minute ceiling.
MAX_UPLOAD_SHORT_DURATION = MAX_SHORT_DURATION_SECONDS
QUALITY_THRESHOLD = 28.0
NATURAL_PAUSE_SECONDS = 1.20
STRONG_SCENE_PAUSE_SECONDS = 2.20

# Output
WIDTH = 1080
HEIGHT = 1920

# Captions
FONT_NAME = "Arial"
FONT_SIZE = 38
CAPTION_MARGIN_BOTTOM = 150

# Face / speaker analysis
FACE_SAMPLE_FPS = 5
SPEAKER_SAMPLE_INTERVAL = 0.35
SPEAKER_CONFIRMATIONS = 2
MIN_SPEAKER_SHOT = 1.20
SPEAKER_SWITCH_DISTANCE = 0.16
FACE_LOST_GRACE = 1.00
CONTINUITY_BONUS = 12.0
MOUTH_MOTION_WEIGHT = 32.0
FACE_SIZE_WEIGHT = 220.0
CENTER_WEIGHT = 2.0

# Editing / pacing
MIN_SPEECH_GAP = 0.55
REMOVE_GAP_THRESHOLD = 2.0
KEEP_GAP_THRESHOLD = 0.65
MAX_SILENCE_REMOVALS_PER_SHORT = 4
HOOK_WINDOW_SECONDS = 7.0
OPENING_HOOK_WEIGHT = 2.0
PACING_WEIGHT = 10.0
DIVERSITY_PENALTY = 18.0
CAPTION_WORDS_PER_GROUP = 4
CAPTION_MAX_CHARS = 28
CAPTION_MARGIN_BOTTOM = 150

# Set YT_AUTO_BOT_PAUSE=1 if you want the old "Press Enter" behavior.
PAUSE_AT_END = os.environ.get("YT_AUTO_BOT_PAUSE", "0") == "1"

# Every processing run gets its own output directory, so reruns never overwrite
# previously rendered Shorts.
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

# Visual quality
DEFAULT_ZOOM = 1.00
# Keep the face/speaker crop at the native portrait crop width. The previous
# 1.10 close-up made faces feel excessively zoomed/cut off.
CLOSEUP_ZOOM = 1.00
# Pull the framing slightly toward the original frame center so the crop
# preserves more surrounding context instead of locking directly on the face.
FACE_CENTER_BLEND = 0.70


# ============================================================
# CHECK FFMPEG
# ============================================================

if not Path(FFMPEG).exists():

    print("=" * 70)
    print("ERROR: FFmpeg not found")
    print("=" * 70)

    print()
    print("Expected:")
    print(FFMPEG)
    print()
    print("Fix this by either:")
    print("  - installing ffmpeg and making sure it is on PATH, or")
    print(f"  - editing \"ffmpeg_path\" in {bot_config.SETTINGS_PATH}, or")
    print("  - setting the YT_AUTO_BOT_FFMPEG environment variable.")

    if os.environ.get("YT_AUTO_BOT_SOURCE_MODE", "").strip().lower() not in {"local", "youtube"}:
        input("\nPress Enter to exit...")
    sys.exit(1)


# ============================================================
# DIRECTORIES
#
# input/, cache/, output/, logs/ and config/ are already created by
# config.py. These calls are kept as a harmless safety net.
# ============================================================

INPUT_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)


# ============================================================
# YOUTUBE DOWNLOAD
# ============================================================

def download_youtube(url=None):
    """Download one YouTube source using a reliability-first yt-dlp strategy.

    YouTube can expose different formats to different player clients. We first
    download an exact known-good combined MP4 when available, then fall back to
    other MP4 selectors. Metadata is collected after the download so a metadata
    probe cannot block an otherwise successful download.
    """
    print()
    print("=" * 70)
    print("YOUTUBE SOURCE")
    print("=" * 70)

    if url is None:
        url = input("\nPaste YouTube URL: ").strip()
    if not url:
        print("No URL entered.")
        sys.exit(1)

    source_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    youtube_dir = TEMP_DIR / "youtube_source" / source_id
    youtube_dir.mkdir(parents=True, exist_ok=True)

    # Remove stale source files from an interrupted/old attempt.
    for old in youtube_dir.glob("source.*"):
        if old.is_file():
            try:
                old.unlink()
            except OSError:
                pass

    output_template = str(youtube_dir / "source.%(ext)s")

    common = [
        sys.executable, "-m", "yt_dlp",
        "--js-runtimes", "deno",
        "--no-playlist",
        "--ffmpeg-location", FFMPEG,
        "--force-overwrites",
        "--no-continue",
        "-o", output_template,
    ]

    # Exact combined format 18 is deliberately first: on some YouTube videos
    # it is available even when adaptive/SABR format selection is problematic.
    attempts = [
        ("combined MP4 format 18", ["-f", "18"]),
        # OpenCV's bundled decoder is not guaranteed to decode AV1 even when
        # the system FFmpeg can. Prefer a CPU-friendly H.264 source capped at
        # 1080p before falling back to arbitrary MP4 codecs.
        ("H.264 MP4 video + M4A audio", [
            "-f",
            "bv*[vcodec^=avc1][height<=1080][ext=mp4]+ba[ext=m4a]/"
            "b[vcodec^=avc1][height<=1080][ext=mp4]/18",
        ]),
        ("combined H.264 MP4", [
            "-f", "b[vcodec^=avc1][height<=1080][ext=mp4]/18",
        ]),
        ("combined MP4 over HTTPS", ["-f", "b[ext=mp4][protocol=https]/b[ext=mp4]/b"]),
        ("MP4 video + M4A audio", ["-f", "bv*[ext=mp4][protocol=https]+ba[ext=m4a][protocol=https]/bv*[ext=mp4]+ba[ext=m4a]"]),
        ("combined MP4 with TV/Web/iOS clients", [
            "--extractor-args", "youtube:player-client=tv,web,ios",
            "-f", "18/b[ext=mp4]/b",
        ]),
    ]

    downloaded = None
    for label, extra in attempts:
        print()
        print(f"Trying YouTube download: {label}")
        command = common + extra + [url]
        result = subprocess.run(command, cwd=str(PROJECT_DIR))
        if result.returncode == 0:
            candidates = [
                p for p in youtube_dir.glob("source.*")
                if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
            ]
            if candidates:
                downloaded = max(candidates, key=lambda p: p.stat().st_mtime)
                break

    if downloaded is None:
        print()
        print("=" * 70)
        print("YouTube download failed.")
        print("=" * 70)
        print("Possible causes: unavailable/restricted video, network issue, or")
        print("a YouTube-side player/client change.")
        sys.exit(1)

    source = downloaded
    print()
    print("Downloaded:")
    print(source)

    # Metadata is intentionally collected AFTER the successful download.
    metadata_command = [
        sys.executable, "-m", "yt_dlp",
        "--js-runtimes", "deno",
        "--dump-single-json",
        "--skip-download",
        "--no-playlist",
        url,
    ]
    metadata_result = subprocess.run(
        metadata_command,
        cwd=str(PROJECT_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    source_metadata = {}
    if metadata_result.returncode == 0 and metadata_result.stdout.strip():
        try:
            source_metadata = json.loads(metadata_result.stdout)
        except json.JSONDecodeError:
            source_metadata = {}

    compact_metadata = {
        "webpage_url": source_metadata.get("webpage_url") or url,
        "original_url": url,
        "id": source_metadata.get("id", ""),
        "title": source_metadata.get("title", ""),
        "channel": source_metadata.get("channel", ""),
        "uploader": source_metadata.get("uploader", ""),
        "description": source_metadata.get("description", ""),
        "tags": source_metadata.get("tags", []) or [],
        "upload_date": source_metadata.get("upload_date", ""),
        "duration": source_metadata.get("duration"),
        "thumbnail": source_metadata.get("thumbnail", ""),
    }

    # Use the real ID when metadata succeeded; otherwise retain the stable URL ID.
    real_id = re.sub(r"[^A-Za-z0-9._-]+", "_", str(compact_metadata.get("id") or source_id))[:80]
    if real_id != source_id:
        real_dir = TEMP_DIR / "youtube_source" / real_id
        if not real_dir.exists():
            try:
                youtube_dir.rename(real_dir)
                youtube_dir = real_dir
                source = youtube_dir / downloaded.name
            except OSError:
                pass

    source_metadata_path = youtube_dir / "source_metadata.json"
    with open(source_metadata_path, "w", encoding="utf-8") as f:
        json.dump(compact_metadata, f, indent=2, ensure_ascii=False)

    # NOTE: earlier versions also wrote this same dict to a single global,
    # unscoped file (cache/youtube_source_metadata.json) and had
    # source_detector.py read it back unconditionally on every run -- local
    # video runs included. That meant a local video processed after a
    # YouTube video would silently inherit the PREVIOUS YouTube video's
    # title/description/tags as if they belonged to the new source. This is
    # the root cause of the "every series identified as 'hour'" bug: once a
    # compilation-style YouTube title (e.g. containing "1 Hour") leaked in
    # this way, every subsequent run's search queries were biased toward
    # that stale, irrelevant title. Fixed by returning compact_metadata
    # directly to the caller instead of round-tripping it through a shared
    # file. source_metadata_path above is kept for internal
    # debugging/traceability (per-source, not shared) but is no longer read
    # back into detection.

    return source, (compact_metadata.get("webpage_url") or url), compact_metadata


# ============================================================
# LOCAL VIDEO
# ============================================================

def choose_local_video():

    extensions = [

        "*.mp4",
        "*.mkv",
        "*.mov",
        "*.avi",
        "*.webm"
    ]


    videos = []


    for extension in extensions:

        videos.extend(
            INPUT_DIR.glob(extension)
        )


    if not videos:

        print()
        print("No videos found in input.")

        print()
        print(
            "Put a video inside:"
        )

        print(INPUT_DIR)

        sys.exit(1)


    print()
    print("Videos found:")


    for index, file in enumerate(
        videos,
        1
    ):

        print(
            f"{index}. {file.name}"
        )


    auto_local = os.environ.get("YT_AUTO_BOT_AUTO_LOCAL", "0") == "1"
    if auto_local:
        # Use the most recently modified input video for unattended runs.
        video = max(videos, key=lambda p: p.stat().st_mtime)
        print("Automatic local selection:", video.name)
        return video

    choice = input(
        "\nChoose video number: "
    ).strip()

    try:
        choice = int(choice)
        video = videos[choice - 1]
    except (ValueError, IndexError):
        print("Invalid selection.")
        sys.exit(1)

    return video


# ============================================================
# SOURCE MENU
# ============================================================

print()
print("=" * 70)
print("YOUTUBE SHORTS BOT")
print("=" * 70)

print()
print("1. Process local video")
print("2. Download from YouTube URL")

auto_mode = os.environ.get("YT_AUTO_BOT_SOURCE_MODE", "").strip().lower()

# Dashboard/automation runs must never block on stdin.
if auto_mode in {"local", "youtube"}:
    source_choice = ""
else:
    source_choice = input("\nChoose 1 or 2: ").strip()

source_url = ""
# Explicit per-run metadata dict, threaded directly to detect_video_source()
# below instead of round-tripping through a shared file. Empty for local
# videos and for any path that never called download_youtube() -- never
# inherited from a previous run.
downloaded_source_metadata: dict = {}
if auto_mode == "local":
    video = choose_local_video()
elif auto_mode == "youtube":
    forced_url = os.environ.get("YT_AUTO_BOT_YOUTUBE_URL", "").strip()
    if not forced_url:
        print("YT_AUTO_BOT_SOURCE_MODE=youtube requires YT_AUTO_BOT_YOUTUBE_URL.")
        sys.exit(1)
    video, source_url, downloaded_source_metadata = download_youtube(forced_url)
elif source_choice == "1":
    video = choose_local_video()
    sidecar = video.with_suffix(video.suffix + ".url.txt")
    if sidecar.exists():
        source_url = sidecar.read_text(encoding="utf-8", errors="ignore").strip()
elif source_choice == "2":
    video, source_url, downloaded_source_metadata = download_youtube()
else:
    print("Invalid choice.")
    sys.exit(1)


print()
print("Source:")
print(video)


# ============================================================
# SOURCE ID
#
# Each video gets its own transcript cache.
# ============================================================

# Build a source identity from the ACTUAL media, not just the filename.
# YouTube downloads are stored in a folder named after the YouTube video ID;
# local files additionally include size + modification time so replacing a file
# with new content cannot accidentally reuse an old transcript.
is_youtube_source = (
    bool(source_url)
    and video.parent.name != ""
    and video.parent.parent.name == "youtube_source"
)

if is_youtube_source:
    raw_source_id = video.parent.name
else:
    stat = video.stat()
    raw_source_id = (
        f"{video.stem}_{stat.st_size}_{stat.st_mtime_ns}"
    )

source_id = re.sub(
    r"[^a-zA-Z0-9_-]",
    "_",
    raw_source_id
)

# Keep IDs manageable and collision-resistant.
if len(source_id) > 80:
    source_id = source_id[:60] + "_" + hashlib.sha256(
        raw_source_id.encode("utf-8", errors="ignore")
    ).hexdigest()[:16]

transcript_path = TEMP_DIR / f"transcript_{source_id}.json"
source_context_path = TEMP_DIR / f"source_context_{source_id}.json"
SOURCE_OUTPUT_DIR = OUTPUT_DIR / source_id / RUN_ID
SOURCE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RENDER_TEMP_DIR = TEMP_DIR / "render" / source_id / RUN_ID
RENDER_TEMP_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# WHISPER
# ============================================================

print()
print("=" * 70)
print("LOADING WHISPER")
print("=" * 70)

print(f"Whisper model: {MODEL_SIZE} (CPU / int8)")

model = WhisperModel(
    MODEL_SIZE,
    device="cpu",
    compute_type="int8"
)

print("Whisper loaded.")


# ============================================================
# TRANSCRIPTION
# ============================================================

need_transcription = True


if transcript_path.exists():

    try:

        with open(
            transcript_path,
            "r",
            encoding="utf-8"
        ) as f:

            cached = json.load(f)


        # Make sure it contains word timestamps.
        has_words = any(

            len(
                segment.get(
                    "words",
                    []
                )
            ) > 0

            for segment in cached
        )


        if has_words:

            transcript_data = cached

            need_transcription = False

            print()
            print(
                "Existing word-level transcript found."
            )

            print(
                "Using cached transcript."
            )

    except Exception:

        need_transcription = True


if need_transcription:

    print()
    print("=" * 70)
    print("TRANSCRIBING")
    print("=" * 70)

    print()
    print(
        "Whisper will analyze the complete source once."
    )

    print(
        "The result will be cached for future runs."
    )

    print()


    segments, info = model.transcribe(

        str(video),

        language="en",

        beam_size=5,

        vad_filter=True,

        word_timestamps=True
    )


    transcript_data = []
    last_progress_report = -WHISPER_PROGRESS_SECONDS
    last_segment_end = 0.0


    for segment in segments:

        words = []


        if segment.words:

            for word in segment.words:

                text = word.word.strip()


                if not text:

                    continue


                words.append({

                    "start": float(
                        word.start
                    ),

                    "end": float(
                        word.end
                    ),

                    "text": text
                })


        segment_start = float(segment.start)
        segment_end = float(segment.end)
        last_segment_end = segment_end

        transcript_data.append({

            "start": segment_start,

            "end": segment_end,

            "text": segment.text.strip(),

            "words": words
        })

        if segment_end - last_progress_report >= WHISPER_PROGRESS_SECONDS:
            elapsed_minutes = segment_end / 60.0
            print(
                f"Whisper progress: processed about {elapsed_minutes:.1f} minutes "
                f"of source audio...",
                flush=True,
            )
            last_progress_report = segment_end


    if transcript_data:
        print(
            f"Whisper finished audio at about {last_segment_end / 60.0:.1f} minutes.",
            flush=True,
        )


    with open(
        transcript_path,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            transcript_data,
            f,
            indent=2,
            ensure_ascii=False
        )


    print()
    print("Transcription complete.")

    print(
        "Detected language:",
        info.language
    )


# ============================================================
# WORD LIST
# ============================================================

all_words = []


for segment in transcript_data:

    all_words.extend(
        segment.get(
            "words",
            []
        )
    )


print()
print(
    "Total words:",
    len(all_words)
)


if not all_words:

    print()
    print(
        "ERROR: No word timestamps found."
    )

    sys.exit(1)


# ============================================================
# REAL-WORLD SOURCE / SCENE DETECTION
# ============================================================

print()
print("=" * 70)
print("IDENTIFYING REAL-WORLD VIDEO SOURCE")
print("=" * 70)

try:
    source_context = detect_video_source(
        video_path=video,
        transcript_data=transcript_data,
        source_url=source_url,
        source_metadata=downloaded_source_metadata,
        output_path=source_context_path,
    )
    print()
    print("Source type:", source_context.get("source_type", "unknown"))
    print("Detected source:", source_context.get("title") or "Unknown")
    print("Confidence:", f"{source_context.get('confidence', 0.0) * 100:.0f}%")
except Exception as exc:
    print()
    print("Source detection warning:", exc)
    source_context = {
        "source_type": "unknown",
        "title": "",
        "confidence": 0.0,
        "evidence": [],
        "characters": [],
        "scene_summary": "",
        "keywords": [],
    }


# ============================================================
# VIDEO INFO
# ============================================================

cap = cv2.VideoCapture(
    str(video)
)


if not cap.isOpened():

    print(
        "Could not open video."
    )

    sys.exit(1)


fps = cap.get(
    cv2.CAP_PROP_FPS
)

frame_count = cap.get(
    cv2.CAP_PROP_FRAME_COUNT
)

source_width = int(
    cap.get(
        cv2.CAP_PROP_FRAME_WIDTH
    )
)

source_height = int(
    cap.get(
        cv2.CAP_PROP_FRAME_HEIGHT
    )
)


cap.release()


if fps <= 0:

    fps = 30


video_duration = (
    frame_count / fps
)


print()
print(
    f"Video duration: "
    f"{video_duration / 60:.2f} minutes"
)

print(
    f"Resolution: "
    f"{source_width}x{source_height}"
)


# ============================================================
# CATCHINESS / HOOK / PACING SCORING
# ============================================================

HOOK_WORDS = [
    "listen", "look", "wait", "stop", "please", "why", "how", "what if",
    "you know", "the truth", "let me tell you", "i need to tell you",
    "do you understand", "remember", "here's the thing", "the problem is",
    "the point is", "but", "because", "actually", "never", "nobody", "everyone"
]

EMOTIONAL_WORDS = [
    "love", "hate", "fear", "afraid", "sorry", "regret", "death", "die",
    "dead", "life", "dream", "hope", "pain", "hurt", "miss", "alone", "lost",
    "happy", "sad", "angry", "beautiful", "terrified", "shame", "betray", "truth"
]

CONFLICT_WORDS = [
    "never", "always", "wrong", "right", "leave", "fight", "believe", "don't",
    "can't", "won't", "should", "shouldn't", "must", "why did you",
    "what are you doing", "you lied", "i disagree", "no", "yes", "but you"
]

POWER_WORDS = [
    "world", "change", "future", "freedom", "choice", "choose", "different",
    "success", "failure", "important", "meaning", "purpose", "remember", "imagine",
    "power", "money", "fear", "risk", "secret", "prove", "win", "lose", "life"
]

FILLER_WORDS = {
    "uh", "um", "erm", "hmm", "like", "you know", "sort of", "kind of",
    "basically", "actually", "right", "okay"
}


def normalize(text):
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def count_terms(text, terms):
    return sum(1 for term in terms if term in text)


def sentence_like_break(text):
    return bool(re.search(r"[.!?][\"']?$", text.strip()))


def score_candidate(text, words=None, candidate_start=None, candidate_end=None):
    text = normalize(text)
    score = 0.0

    score += count_terms(text, HOOK_WORDS) * 6
    score += count_terms(text, EMOTIONAL_WORDS) * 4
    score += count_terms(text, CONFLICT_WORDS) * 4
    score += count_terms(text, POWER_WORDS) * 3
    score += min(text.count("?"), 4) * 8
    score += min(text.count("!"), 5) * 4

    word_count = len(text.split())
    if 55 <= word_count <= 150:
        score += 8
    if word_count >= 90:
        score += 5

    # Prefer openings that immediately contain a hook rather than a setup.
    if words and candidate_start is not None:
        opening = [w for w in words if w["start"] < candidate_start + HOOK_WINDOW_SECONDS]
        opening_text = normalize(" ".join(w["text"] for w in opening))
        opening_score = (
            count_terms(opening_text, HOOK_WORDS) * 8
            + count_terms(opening_text, EMOTIONAL_WORDS) * 4
            + count_terms(opening_text, CONFLICT_WORDS) * 4
            + opening_text.count("?") * 6
        )
        score += opening_score * OPENING_HOOK_WEIGHT

        # Fast speech usually indicates energy; very slow speech is penalized.
        duration = max((candidate_end or words[-1]["end"]) - candidate_start, 1)
        wps = len(words) / duration
        if wps > 2.2:
            score += 10
        elif wps > 1.5:
            score += 8
        elif wps > 1.0:
            score += 4
        elif wps < 0.65:
            score -= 8

        # Filler density hurts the score.
        filler_count = sum(1 for w in words if normalize(w["text"]).strip(".,?!") in FILLER_WORDS)
        score -= min(filler_count * 1.5, 15)

    return round(score, 2)


def trim_to_sentence_boundaries(words, desired_start, desired_end):
    """Choose clip boundaries that preserve conversational context.

    Avoid starting in the middle of a sentence. When no nearby sentence
    boundary exists, include a small lead-in of preceding speech instead of
    dropping the setup that makes the selected moment understandable.
    """
    if not words:
        return desired_start, desired_end

    relevant = [w for w in words if w["end"] > desired_start and w["start"] < desired_end]
    if not relevant:
        return desired_start, desired_end

    first = relevant[0]
    last = relevant[-1]
    start = first["start"]
    end = last["end"]

    # Prefer the beginning of the sentence containing the selected moment.
    first_index = all_words.index(first) if first in all_words else 0
    boundary_found = False
    for i in range(first_index - 1, -1, -1):
        previous = all_words[i]
        if first["start"] - previous["end"] > 5.0:
            break
        if sentence_like_break(previous["text"]):
            if i + 1 < len(all_words):
                candidate_start = all_words[i + 1]["start"]
                if candidate_start <= first["start"]:
                    start = candidate_start
                    boundary_found = True
            break

    # If the sentence boundary is farther away or Whisper punctuation is weak,
    # retain up to two seconds of preceding spoken context.
    if not boundary_found:
        lead_in_start = max(desired_start, first["start"] - 2.0)
        context_words = [
            w for w in all_words
            if lead_in_start <= w["start"] < first["start"]
        ]
        if context_words:
            start = context_words[0]["start"]

    # Prefer a natural sentence ending shortly after the requested endpoint.
    for w in all_words:
        if w["end"] < desired_end:
            continue
        if w["end"] > desired_end + 4.0:
            break
        if sentence_like_break(w["text"]):
            end = w["end"]
            break

    # Otherwise end on a spoken word, never halfway through one.
    if end < desired_end:
        following = [
            w for w in all_words
            if w["start"] >= desired_end and w["end"] <= desired_end + 2.0
        ]
        if following:
            end = following[0]["end"]

    return start, end

# ============================================================
# CONTENT-DRIVEN CANDIDATE DISCOVERY
# ============================================================
#
# The old selector generated a time grid (10-second starts x
# [32, 40, 48, 55, 60]-second windows) and then trimmed those windows.
# The selector below reverses that relationship:
#
#   transcript -> semantic units -> natural boundaries -> moments
#             -> quality score -> overlap filtering -> selection
#
# A 180-second limit exists only because it is the current technical
# maximum for a square/vertical YouTube Short. It is never used as a
# preferred clip duration and the selector never pads a moment toward it.

def _word_is_sentence_end(word):
    return bool(re.search(r"[.!?]$", word.get("text", "").strip()))


def build_semantic_units(words):
    """Build transcript units around sentence/pause boundaries."""
    if not words:
        return []

    units = []
    current = []
    for word in words:
        if not current:
            current = [word]
            continue

        previous = current[-1]
        gap = max(0.0, float(word["start"]) - float(previous["end"]))
        sentence_end = _word_is_sentence_end(previous)
        pause_boundary = gap >= NATURAL_PAUSE_SECONDS

        # A pause belongs between units. Never pull the first word of the
        # next scene/topic backward into the previous unit.
        if pause_boundary or sentence_end:
            units.append({
                "start": float(current[0]["start"]),
                "end": float(current[-1]["end"]),
                "text": " ".join(w["text"] for w in current).strip(),
                "words": list(current),
            })
            current = [word]
            continue

        current.append(word)

        safety_boundary = (
            float(word["end"]) - float(current[0]["start"]) >= 14.0
        )
        if safety_boundary:
            units.append({
                "start": float(current[0]["start"]),
                "end": float(current[-1]["end"]),
                "text": " ".join(w["text"] for w in current).strip(),
                "words": list(current),
            })
            current = []

    if current:
        units.append({
            "start": float(current[0]["start"]),
            "end": float(current[-1]["end"]),
            "text": " ".join(w["text"] for w in current).strip(),
            "words": list(current),
        })

    return units


def _token_set(text):
    stop = {
        "the", "and", "that", "this", "with", "from", "your", "you",
        "are", "was", "were", "have", "has", "had", "for", "but",
        "not", "they", "them", "then", "than", "just", "what", "why",
        "how", "who", "when", "where",
    }
    return {
        token for token in re.findall(r"[a-zA-Z']+", normalize(text))
        if len(token) >= 3 and token not in stop
    }


def semantic_boundary_strength(left, right):
    """Estimate whether adjacent transcript units cross a scene/topic boundary."""
    gap = max(0.0, right["start"] - left["end"])
    score = 0.0

    if gap >= STRONG_SCENE_PAUSE_SECONDS:
        score += 5.0
    elif gap >= NATURAL_PAUSE_SECONDS:
        score += 2.5

    if _word_is_sentence_end(left["words"][-1]):
        score += 1.5

    left_tokens = _token_set(left["text"])
    right_tokens = _token_set(right["text"])
    if left_tokens and right_tokens:
        overlap = len(left_tokens & right_tokens) / max(
            1, len(left_tokens | right_tokens)
        )
        if overlap < 0.08:
            score += 2.0
        elif overlap < 0.16:
            score += 1.0

    return score


def _natural_boundary_before(units, index):
    return index <= 0 or semantic_boundary_strength(
        units[index - 1], units[index]
    ) >= 4.0


def _natural_boundary_after(units, index):
    return index >= len(units) - 1 or semantic_boundary_strength(
        units[index], units[index + 1]
    ) >= 4.0


def _moment_anchor_score(units, index):
    """Score a semantic unit as a possible centre/anchor."""
    unit = units[index]
    text = unit["text"]
    score = score_candidate(
        text,
        words=unit["words"],
        candidate_start=unit["start"],
        candidate_end=unit["end"],
    )

    # Setup -> response -> payoff signals.
    if "?" in text:
        score += 5
    if "!" in text:
        score += 3
    if index > 0 and "?" in units[index - 1]["text"]:
        score += 5

    if index > 0 and index + 1 < len(units):
        previous = units[index - 1]["text"]
        following = units[index + 1]["text"]
        if "?" in previous and len(following.split()) >= 3:
            score += 6

    memorable = (
        "i can't believe", "no way", "are you serious", "what the",
        "oh my god", "that's why", "you have to", "i told you",
    )
    if any(term in normalize(text) for term in memorable):
        score += 5

    if len(unit["words"]) >= 8:
        score += 3
    elif len(unit["words"]) <= 3:
        score -= 4

    return round(score, 2)


def _expand_moment(units, anchor_index):
    """Expand an anchor to the smallest coherent natural moment."""
    left = anchor_index
    right = anchor_index
    technical_limit_hit = False

    # Include setup until a real boundary.
    while left > 0:
        proposed_duration = units[right]["end"] - units[left - 1]["start"]
        if proposed_duration > MAX_UPLOAD_SHORT_DURATION:
            technical_limit_hit = True
            break
        if _natural_boundary_before(units, left):
            break
        left -= 1

    # Include response/reaction/payoff until the moment concludes.
    while right < len(units) - 1:
        proposed_duration = units[right + 1]["end"] - units[left]["start"]
        if proposed_duration > MAX_UPLOAD_SHORT_DURATION:
            technical_limit_hit = True
            break
        if _natural_boundary_after(units, right):
            break
        right += 1

    # If the semantic moment itself continues beyond YouTube's technical
    # Shorts limit, reject it instead of manufacturing an artificial ending.
    if technical_limit_hit:
        return None

    # At an isolated anchor, include an immediate question/setup or payoff
    # when doing so still produces one coherent exchange.
    if left == right:
        if anchor_index > 0 and "?" in units[anchor_index - 1]["text"]:
            if (
                units[anchor_index]["start"] - units[anchor_index - 1]["start"] <= 12
                and units[anchor_index]["end"] - units[anchor_index - 1]["start"]
                <= MAX_UPLOAD_SHORT_DURATION
            ):
                left -= 1
        if right + 1 < len(units) and (
            "!" in units[anchor_index]["text"]
            or "?" in units[anchor_index]["text"]
        ):
            if units[right + 1]["end"] - units[left]["start"] <= MAX_UPLOAD_SHORT_DURATION:
                right += 1

    selected_words = [
        word
        for unit in units[left:right + 1]
        for word in unit["words"]
    ]
    if not selected_words:
        return None

    return {
        "start": units[left]["start"],
        "end": units[right]["end"],
        "duration": units[right]["end"] - units[left]["start"],
        "text": " ".join(w["text"] for w in selected_words).strip(),
        "words": selected_words,
        "unit_start": left,
        "unit_end": right,
    }


def _quality_components(moment, anchor_score, units):
    words = moment["words"]
    text = normalize(moment["text"])
    duration = moment["duration"]

    natural_start = (
        moment["unit_start"] == 0
        or _natural_boundary_before(units, moment["unit_start"])
    )
    natural_end = (
        moment["unit_end"] >= len(units) - 1
        or _natural_boundary_after(units, moment["unit_end"])
    )

    completeness = 0.0
    if "?" in text:
        completeness += 4.0
    if "!" in text:
        completeness += 3.0
    if len(words) >= 30:
        completeness += 3.0
    if natural_start:
        completeness += 2.0
    if natural_end:
        completeness += 2.0

    payoff = min(
        10.0,
        count_terms(text, EMOTIONAL_WORDS) * 1.2
        + count_terms(text, CONFLICT_WORDS) * 1.5
        + min(text.count("!"), 3) * 2.0
        + min(text.count("?"), 2) * 1.5,
    )

    gaps = [
        max(0.0, b["start"] - a["end"])
        for a, b in zip(words, words[1:])
    ]
    long_silence = sum(g for g in gaps if g >= REMOVE_GAP_THRESHOLD)
    silence_penalty = min(long_silence * 3.0, 12.0)

    word_density = len(words) / max(duration, 1.0)
    coherence = 8.0 if word_density >= 1.0 else 4.0 if word_density >= 0.65 else 0.0

    # Duration is only a sanity check, never a target.
    duration_sanity = 2.0 if 20 <= duration <= 120 else 0.5

    total = (
        anchor_score
        + completeness
        + payoff
        + coherence
        + duration_sanity
        - silence_penalty
    )

    return {
        "context_completeness": round(completeness, 2),
        "dialogue_coherence": round(coherence, 2),
        "payoff": round(payoff, 2),
        "silence_penalty": round(silence_penalty, 2),
        "anchor_score": round(anchor_score, 2),
        "quality_score": round(total, 2),
    }


def _candidate_is_duplicate(candidate, existing):
    """Reject candidates that represent substantially the same moment."""
    if candidate["start"] < existing["end"] and existing["start"] < candidate["end"]:
        overlap_start = max(candidate["start"], existing["start"])
        overlap_end = min(candidate["end"], existing["end"])
        overlap = max(0.0, overlap_end - overlap_start)
        shorter = min(candidate["duration"], existing["duration"])
        if shorter > 0 and overlap / shorter >= 0.45:
            return True

    a = _token_set(candidate["text"])
    b = _token_set(existing["text"])
    temporal_gap = min(
        abs(candidate["start"] - existing["end"]),
        abs(existing["start"] - candidate["end"]),
    )
    if a and b and temporal_gap < 10:
        similarity = len(a & b) / max(1, len(a | b))
        if similarity >= 0.96:
            return True

    return False


def discover_content_candidates(all_words):
    """Find strong semantic moments without a fixed timestamp grid."""
    units = build_semantic_units(all_words)
    if not units:
        return []

    anchors = []
    for index in range(len(units)):
        score = _moment_anchor_score(units, index)
        if score >= 8.0:
            anchors.append((score, index))

    # Weak-hook transcripts still get content-driven inspection; there is
    # deliberately no return to fixed 10-second timestamp sampling.
    if not anchors:
        anchors = [
            (_moment_anchor_score(units, i), i)
            for i, unit in enumerate(units)
            if len(unit["words"]) >= 8
        ]

    raw_candidates = []
    for anchor_score, index in anchors:
        moment = _expand_moment(units, index)
        if not moment:
            continue
        if moment["duration"] < MIN_CLIP_LENGTH:
            continue
        if moment["duration"] > MAX_UPLOAD_SHORT_DURATION:
            continue

        components = _quality_components(moment, anchor_score, units)
        if components["quality_score"] < QUALITY_THRESHOLD:
            continue

        raw_candidates.append({
            "start": round(moment["start"], 3),
            "end": round(moment["end"], 3),
            "duration": round(moment["duration"], 3),
            "score": components["quality_score"],
            "selection_score": components["quality_score"],
            "quality": components,
            "text": moment["text"],
        })

    # Collapse duplicate anchors that expanded into the same conversation.
    raw_candidates.sort(key=lambda x: x["score"], reverse=True)
    candidates = []
    for candidate in raw_candidates:
        if any(_candidate_is_duplicate(candidate, existing) for existing in candidates):
            continue
        candidates.append(candidate)

    return candidates


print()
print("=" * 70)
print("FINDING CONTENT-DRIVEN MOMENTS")
print("=" * 70)

candidates = discover_content_candidates(all_words)

print()
print("Semantic candidates found:", len(candidates))

# ============================================================
# QUALITY + DIVERSITY-AWARE SELECTION
# ============================================================

selected = []

while candidates and len(selected) < TARGET_MAX_SHORTS:
    best = None
    best_score = -10**9

    for candidate in candidates:
        if any(_candidate_is_duplicate(candidate, chosen) for chosen in selected):
            continue

        score = candidate["score"]

        # Penalize nearby/redundant moments, but do not trim a strong moment
        # merely to make timestamps look non-overlapping.
        for chosen in selected:
            distance = min(
                abs(candidate["start"] - chosen["start"]),
                abs(candidate["end"] - chosen["end"]),
            )
            if distance < 30:
                score -= DIVERSITY_PENALTY
            elif distance < 60:
                score -= DIVERSITY_PENALTY * 0.5

        if score > best_score:
            best_score = score
            best = candidate

    if best is None or best_score < QUALITY_THRESHOLD:
        break

    best["selection_score"] = round(best_score, 2)
    selected.append(best)
    candidates.remove(best)

print()
print(
    f"Selected {len(selected)} high-quality moments "
    f"(target range {TARGET_MIN_SHORTS}-{TARGET_MAX_SHORTS}; not a quota)."
)

# ============================================================
# FINAL PRE-RENDER VALIDATION
# ============================================================

validated_selected = []
for candidate in selected:
    duration = candidate["end"] - candidate["start"]
    checks = {
        "duration_ge_20": duration >= MIN_CLIP_LENGTH,
        "duration_within_youtube_limit": duration <= MAX_UPLOAD_SHORT_DURATION,
        "coherent_context": bool(candidate.get("text", "").strip()),
        "natural_start": candidate["start"] >= 0,
        "natural_end": candidate["end"] > candidate["start"],
        "not_duplicate": not any(
            _candidate_is_duplicate(candidate, other)
            for other in validated_selected
        ),
    }
    candidate["validation"] = checks

    if all(checks.values()):
        validated_selected.append(candidate)
    else:
        print(
            "Rejected before rendering:",
            f"{candidate['start']:.1f}s → {candidate['end']:.1f}s",
            checks,
        )

selected = validated_selected

# ============================================================
# DISPLAY SELECTION
# ============================================================

print()
print("=" * 70)
print("SELECTED SHORTS")
print("=" * 70)

for index, clip in enumerate(selected, 1):
    print()
    print(
        f"#{index}"
        f" | {clip['start']:.1f}s"
        f" → {clip['end']:.1f}s"
        f" | Duration: {clip['end'] - clip['start']:.1f}s"
        f" | Quality: {clip['score']}"
    )
    print("Quality components:", clip.get("quality", {}))
    print(clip["text"][:300])

# ============================================================
# FACE DETECTOR
# ============================================================

print()
print("Loading face detector...")


face_detector = cv2.CascadeClassifier(

    cv2.data.haarcascades
    +
    "haarcascade_frontalface_default.xml"
)


# ============================================================
# FACE DETECTION
# ============================================================

def detect_faces(frame):

    gray = cv2.cvtColor(

        frame,

        cv2.COLOR_BGR2GRAY
    )


    gray = cv2.equalizeHist(
        gray
    )


    return face_detector.detectMultiScale(

        gray,

        scaleFactor=1.1,

        minNeighbors=5,

        minSize=(60, 60)
    )


# ============================================================
# ACTIVE SPEAKER ESTIMATION
# ============================================================

def face_center(face):
    x, y, w, h = face
    return x + w / 2, y + h / 2


def face_motion_score(previous_frame, current_frame, face):
    if previous_frame is None:
        return 0.0
    try:
        previous_gray = cv2.cvtColor(previous_frame, cv2.COLOR_BGR2GRAY)
        current_gray = cv2.cvtColor(current_frame, cv2.COLOR_BGR2GRAY)
        x, y, w, h = [int(v) for v in face]
        y1 = max(0, int(y + h * 0.52))
        y2 = min(current_gray.shape[0], int(y + h * 0.92))
        x1 = max(0, x)
        x2 = min(current_gray.shape[1], x + w)
        if x2 <= x1 or y2 <= y1:
            return 0.0
        a = current_gray[y1:y2, x1:x2]
        b = previous_gray[y1:y2, x1:x2]
        if a.shape != b.shape or a.size == 0:
            return 0.0
        return float(np.mean(cv2.absdiff(a, b)) / 255.0)
    except Exception:
        return 0.0


def choose_speaker_face(faces, previous_frame, current_frame, width, height, previous_center=None):
    if len(faces) == 0:
        return None

    best_face = None
    best_score = -999999.0
    max_distance = np.sqrt((width / 2) ** 2 + (height / 2) ** 2)

    for face in faces:
        x, y, w, h = face
        cx, cy = face_center(face)
        size_score = (w * h) / max(width * height, 1)
        distance = np.sqrt((cx - width / 2) ** 2 + (cy - height / 2) ** 2)
        center_score = 1 - distance / max_distance
        mouth_motion = face_motion_score(previous_frame, current_frame, face)

        score = (
            size_score * FACE_SIZE_WEIGHT
            + center_score * CENTER_WEIGHT
            + mouth_motion * MOUTH_MOTION_WEIGHT
        )

        # Strong continuity preference: don't jump to another face unless it
        # has a meaningful advantage. This is what keeps the shot locked.
        if previous_center is not None:
            normalized_distance = abs(cx - previous_center) / max(width, 1)
            score -= normalized_distance * 80
            if normalized_distance < SPEAKER_SWITCH_DISTANCE:
                score += CONTINUITY_BONUS

        if score > best_score:
            best_score = score
            best_face = face

    return best_face


# ============================================================
# CREATE STABLE SPEAKER SHOTS
# ============================================================

def create_speaker_shots(clip_start, clip_end):
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_MSEC, clip_start * 1000)

    shots = []
    previous_frame = None
    current_center = source_width / 2
    current_face = None
    shot_start = clip_start
    pending_center = None
    pending_count = 0
    last_seen = clip_start
    next_sample = clip_start

    while True:
        current_time = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
        if current_time >= clip_end:
            break
        ret, frame = cap.read()
        if not ret:
            break
        if current_time < next_sample:
            continue
        next_sample += SPEAKER_SAMPLE_INTERVAL

        faces = detect_faces(frame)
        candidate = choose_speaker_face(
            faces,
            previous_frame,
            frame,
            source_width,
            source_height,
            current_center,
        )

        if candidate is not None:
            candidate_center = face_center(candidate)[0]
            last_seen = current_time

            if current_face is None:
                current_face = candidate
                current_center = candidate_center
                shot_start = current_time
                pending_center = None
                pending_count = 0
            else:
                distance = abs(candidate_center - current_center) / max(source_width, 1)
                if distance >= SPEAKER_SWITCH_DISTANCE:
                    if pending_center is not None and abs(candidate_center - pending_center) / max(source_width, 1) < 0.08:
                        pending_count += 1
                    else:
                        pending_center = candidate_center
                        pending_count = 1

                    if pending_count >= SPEAKER_CONFIRMATIONS and current_time - shot_start >= MIN_SPEAKER_SHOT:
                        shots.append({
                            "start": shot_start,
                            "end": current_time,
                            "center_x": current_center,
                        })
                        current_center = candidate_center
                        current_face = candidate
                        shot_start = current_time
                        pending_center = None
                        pending_count = 0
                else:
                    pending_center = None
                    pending_count = 0
        else:
            # Hold the current shot during brief detector failures.
            if current_time - last_seen > FACE_LOST_GRACE and current_face is not None:
                current_face = None

        previous_frame = frame.copy()

    cap.release()

    if clip_end > shot_start:
        shots.append({
            "start": shot_start,
            "end": clip_end,
            "center_x": current_center,
        })

    # Merge accidental micro-shots and clamp boundaries.
    merged = []
    for shot in shots:
        shot["start"] = max(clip_start, shot["start"])
        shot["end"] = min(clip_end, shot["end"])
        if shot["end"] <= shot["start"]:
            continue
        if merged and shot["start"] - merged[-1]["start"] < MIN_SPEAKER_SHOT:
            merged[-1]["end"] = shot["end"]
        else:
            merged.append(shot)

    if not merged:
        merged = [{"start": clip_start, "end": clip_end, "center_x": source_width / 2}]

    return merged


def get_shot_center(shots, current_time):
    if not shots:
        return source_width / 2
    for shot in shots:
        if shot["start"] <= current_time < shot["end"]:
            return shot["center_x"]
    return shots[-1]["center_x"]


# ============================================================
# SILENCE / PACING MAP
# ============================================================

def build_keep_intervals(clip_start, clip_end):
    """Conservatively remove only obvious dead air.

    The previous implementation removed almost every gap above 0.85s and
    retained only 0.25s. That is too aggressive for dialogue: dramatic pauses,
    reactions and sentence breaks can easily exceed that length and removing
    them makes the Short feel out of context.

    We now: 
      * remove only gaps >= 2.0s;
      * preserve 0.65s of natural pause;
      * never remove a pause immediately after sentence-ending punctuation;
      * limit removals to four per Short.
    """
    words = [
        w for w in all_words
        if w["start"] < clip_end and w["end"] > clip_start
    ]
    if not words:
        return [(clip_start, clip_end)]

    intervals = []
    cursor = clip_start
    removal_count = 0

    for i, word in enumerate(words):
        ws = max(clip_start, word["start"])
        we = min(clip_end, word["end"])
        if we <= ws:
            continue

        gap = ws - cursor
        previous_word = words[i - 1] if i else None
        previous_text = str(previous_word.get("text", "")) if previous_word else ""
        sentence_break = bool(re.search(r"[.!?][\"']?$", previous_text.strip()))

        should_remove = (
            gap >= REMOVE_GAP_THRESHOLD
            and removal_count < MAX_SILENCE_REMOVALS_PER_SHORT
            and not sentence_break
        )

        if should_remove:
            # Preserve a substantial natural pause so dialogue remains coherent.
            keep_end = min(ws, cursor + KEEP_GAP_THRESHOLD)
            if keep_end > cursor:
                intervals.append((cursor, keep_end))
            removal_count += 1
        else:
            intervals.append((cursor, ws))

        intervals.append((ws, we))
        cursor = we

    if cursor < clip_end:
        # Never aggressively trim the tail; the ending may contain a reaction.
        intervals.append((cursor, clip_end))

    # Merge overlaps and remove only truly microscopic intervals.
    cleaned = []
    for a, b in sorted(intervals):
        if b - a < 0.03:
            continue
        if cleaned and a <= cleaned[-1][1] + 0.03:
            cleaned[-1] = (cleaned[-1][0], max(cleaned[-1][1], b))
        else:
            cleaned.append((a, b))

    return cleaned or [(clip_start, clip_end)]


def build_timeline_map(intervals):
    """Map original source times to the compressed Short timeline."""
    mapped = []
    compressed = 0.0
    for a, b in intervals:
        mapped.append({"source_start": a, "source_end": b, "output_start": compressed})
        compressed += b - a
    return mapped, compressed


def map_source_time(t, timeline):
    if not timeline:
        return t
    for item in timeline:
        if item["source_start"] <= t <= item["source_end"]:
            return item["output_start"] + (t - item["source_start"])
    if t < timeline[0]["source_start"]:
        return 0.0
    return timeline[-1]["output_start"] + (timeline[-1]["source_end"] - timeline[-1]["source_start"])

# ============================================================
# CREATE WORD-SYNCED / HIGHLIGHTED CAPTIONS
# ============================================================

def create_captions(clip_start, clip_end, index, timeline=None):
    words = [
        word for word in all_words
        if word["start"] < clip_end and word["end"] > clip_start
    ]

    subs = pysubs2.SSAFile()
    subs.info["PlayResX"] = WIDTH
    subs.info["PlayResY"] = HEIGHT

    style = subs.styles["Default"]
    style.fontname = FONT_NAME
    style.fontsize = FONT_SIZE
    style.primarycolor = pysubs2.Color(255, 255, 255)
    style.outlinecolor = pysubs2.Color(0, 0, 0)
    style.outline = 3
    style.shadow = 1
    style.alignment = 2
    style.marginv = CAPTION_MARGIN_BOTTOM

    # Highlight the currently spoken word using ASS override tags.
    highlight = r"{\c&H00FFFF&}"
    reset = r"{\c&HFFFFFF&}"

    group = []
    group_chars = 0

    def mapped_time(source_t):
        if timeline is None:
            return source_t - clip_start
        return map_source_time(source_t, timeline)

    def add_group(group_words):
        if not group_words:
            return
        start = mapped_time(group_words[0]["start"])
        end = mapped_time(group_words[-1]["end"])
        if end <= start:
            return

        parts = []
        for word in group_words:
            raw = re.sub(r"\s+", " ", word["text"]).strip()
            if not raw:
                continue
            # A separate event per word would flicker. Instead use karaoke-style timing.
            duration_cs = max(1, int((word["end"] - word["start"]) * 100))
            parts.append(r"{\k%d}%s" % (duration_cs, raw))

        text = " ".join(parts)
        if not text:
            return

        event = pysubs2.SSAEvent(
            start=int(max(0, start) * 1000),
            end=int(max(start + 0.05, end) * 1000),
            text=highlight + text + reset,
        )
        subs.append(event)

    for word in words:
        raw = word["text"].strip()
        if not raw:
            continue
        group.append(word)
        group_chars += len(raw) + 1
        punctuation = raw.endswith((".", "?", "!", ",", ";", ":"))
        if len(group) >= CAPTION_WORDS_PER_GROUP or group_chars >= CAPTION_MAX_CHARS or punctuation:
            add_group(group)
            group = []
            group_chars = 0

    add_group(group)

    ass_path = RENDER_TEMP_DIR / f"captions_{index}.ass"
    subs.save(str(ass_path))
    return ass_path


# ============================================================
# RENDER ONE SHORT
# ============================================================


def render_short(
    clip,
    index
):
    start = clip["start"]
    end = clip["end"]
    original_duration = end - start

    print()
    print("=" * 70)
    print(f"SHORT {index}")
    print(f"{start:.1f}s → {end:.1f}s")
    print("=" * 70)

    # --------------------------------------------------------
    # SPEAKER SHOTS
    # --------------------------------------------------------
    print("\nDetecting stable speaker shots...")
    speaker_shots = create_speaker_shots(start, end)
    print("Speaker shots:", len(speaker_shots))
    for shot in speaker_shots:
        print(
            f"  {shot['start']:.1f}s → {shot['end']:.1f}s | "
            f"center={shot['center_x']:.0f}"
        )

    # --------------------------------------------------------
    # REMOVE LONG DEAD AIR
    # --------------------------------------------------------
    print("\nOptimizing pacing / removing long pauses...")
    keep_intervals = cap_intervals(build_keep_intervals(start, end))
    timeline, compressed_duration = build_timeline_map(keep_intervals)
    removed = original_duration - compressed_duration
    print(f"Original duration: {original_duration:.2f}s")
    print(f"Output duration:   {compressed_duration:.2f}s")
    print(f"Silence removed:   {max(0, removed):.2f}s")

    # The final rendered duration is what matters. A candidate that only
    # meets the 20-second floor before pacing must not become a sub-20s Short
    # because dead-air removal shortened it.
    if compressed_duration < MIN_CLIP_LENGTH:
        print(
            f"REJECTED before render: paced duration {compressed_duration:.2f}s "
            f"is below the {MIN_CLIP_LENGTH}s minimum."
        )
        return False

    # --------------------------------------------------------
    # CREATE CROPPED VIDEO
    # --------------------------------------------------------
    temp_video = RENDER_TEMP_DIR / f"cropped_{index}.mp4"
    # Keep a clean, full-frame copy of the paced clip beside the final Short.
    # The dashboard framing editor uses this master so manual adjustments are
    # always made from the original picture instead of re-cropping a 9:16 file.
    source_master = SOURCE_OUTPUT_DIR / f"short_{index:02d}.source.mp4"
    source_master_temp = RENDER_TEMP_DIR / f"fullframe_{index}.mp4"
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(temp_video), fourcc, fps, (WIDTH, HEIGHT))
    source_writer = cv2.VideoWriter(
        str(source_master_temp), fourcc, fps, (source_width, source_height)
    )

    if not writer.isOpened() or not source_writer.isOpened():
        print("ERROR: Could not open the Short or full-frame video writer.")
        cap.release()
        writer.release()
        source_writer.release()
        source_master_temp.unlink(missing_ok=True)
        return False

    crop_width_base = int(source_height * 9 / 16)
    crop_width_base = min(crop_width_base, source_width)
    frame_number = 0
    written_frames = 0
    interval_index = 0

    while True:
        current_time = start + frame_number / fps
        if current_time >= end:
            break

        ret, frame = cap.read()
        if not ret:
            break

        # Only emit frames that survive the pacing pass.
        while interval_index < len(keep_intervals) and current_time >= keep_intervals[interval_index][1]:
            interval_index += 1
        if interval_index >= len(keep_intervals):
            frame_number += 1
            continue
        if current_time < keep_intervals[interval_index][0]:
            frame_number += 1
            continue

        center_x = get_shot_center(speaker_shots, current_time)
        # Reduce aggressive face-following: keep most of the portrait crop near
        # the original frame center while still following the active speaker.
        center_x = (
            center_x * FACE_CENTER_BLEND
            + (source_width / 2) * (1.0 - FACE_CENTER_BLEND)
        )

        # Mild punch-in when a face exists in the shot. Keep this at 1.00 by
        # default so the speaker is not unnecessarily cropped.
        shot_zoom = DEFAULT_ZOOM
        shot = next((s for s in speaker_shots if s["start"] <= current_time < s["end"]), None)
        if shot is not None:
            # Short speaker shots no longer trigger a 10% punch-in.
            shot_width = max(1, shot["end"] - shot["start"])
            if shot_width < 2.0:
                shot_zoom = CLOSEUP_ZOOM

        crop_width = int(crop_width_base / shot_zoom)
        crop_width = max(2, min(crop_width, source_width))

        left = int(center_x - crop_width / 2)
        left = max(0, min(source_width - crop_width, left))

        cropped = frame[:, left:left + crop_width]
        cropped = cv2.resize(cropped, (WIDTH, HEIGHT), interpolation=cv2.INTER_AREA)
        writer.write(cropped)
        if frame.shape[1] != source_width or frame.shape[0] != source_height:
            frame = cv2.resize(
                frame, (source_width, source_height), interpolation=cv2.INTER_AREA
            )
        source_writer.write(frame)
        written_frames += 1
        frame_number += 1

    cap.release()
    writer.release()
    source_writer.release()

    if written_frames == 0:
        print(
            "ERROR: OpenCV could not decode any frames for this Short. "
            "The source codec may not be supported by OpenCV."
        )
        source_master_temp.unlink(missing_ok=True)
        return False

    # --------------------------------------------------------
    # CAPTIONS
    # --------------------------------------------------------
    print("Creating word-synchronized highlighted captions...")
    ass_path = create_captions(start, end, index, timeline=timeline)

    # --------------------------------------------------------
    # FINAL RENDER + COMPRESSED AUDIO
    # --------------------------------------------------------
    output_file = SOURCE_OUTPUT_DIR / f"short_{index:02d}.mp4"
    print("Adding captions and synchronized audio...")

    relative_ass = ass_path.relative_to(PROJECT_DIR).as_posix()

    # Build an audio concat filter that mirrors the video pacing map.
    audio_parts = []
    for i, (a, b) in enumerate(keep_intervals):
        rel_a = max(0.0, a - start)
        rel_b = max(rel_a + 0.01, b - start)
        audio_parts.append(
            f"[1:a]atrim=start={rel_a:.3f}:end={rel_b:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
    audio_concat_inputs = "".join(f"[a{i}]" for i in range(len(keep_intervals)))
    audio_filter = ";".join(audio_parts)
    if len(keep_intervals) > 1:
        audio_filter += f";{audio_concat_inputs}concat=n={len(keep_intervals)}:v=0:a=1[aout]"
    else:
        audio_filter += ";[a0]anull[aout]"

    command = [
        FFMPEG,
        "-y",
        "-i", str(temp_video),
        "-ss", str(start),
        "-i", str(video),
        "-filter_complex", audio_filter,
        "-vf", f"ass={relative_ass}",
        "-map", "0:v:0",
        "-map", "[aout]",
        "-t", str(compressed_duration),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "19",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_file),
    ]

    result = subprocess.run(command, cwd=str(PROJECT_DIR))
    if result.returncode != 0:
        print()
        print(f"ERROR rendering Short {index}")
        source_master_temp.unlink(missing_ok=True)
        return False

    # Convert OpenCV's editing master to browser-friendly H.264 and attach the
    # already-synchronized audio from the completed Short. It remains free of
    # captions and cropping, which makes it safe to reframe repeatedly.
    master_command = [
        FFMPEG,
        "-y",
        "-i", str(source_master_temp),
        "-i", str(output_file),
        "-map", "0:v:0",
        "-map", "1:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "22",
        "-c:a", "copy",
        "-shortest",
        "-movflags", "+faststart",
        str(source_master),
    ]
    master_result = subprocess.run(master_command, cwd=str(PROJECT_DIR))
    source_master_temp.unlink(missing_ok=True)
    if master_result.returncode != 0:
        print(f"ERROR creating full-frame editing master for Short {index}")
        source_master.unlink(missing_ok=True)
        output_file.unlink(missing_ok=True)
        return False

    print()
    print("SUCCESS:")
    print(output_file)
    from youtube_clipper.video.captions import retain_clean_video
    retain_clean_video(temp_video, output_file, output_file.with_suffix(".clean.mp4"))
    clip["timeline"] = timeline
    return True


# ============================================================
# RENDER SELECTED SHORTS
# ============================================================

# Attach source intelligence to every selected clip. clip_id is deterministic
# and stable for this clip's entire lifecycle (rendering, captions, metadata,
# upload) -- it is the same value used to derive every per-clip cache
# filename downstream, so nothing from a different clip/run can collide with it.
for clip_index, clip in enumerate(selected, 1):
    clip["clip_id"] = f"{source_id}_clip_{clip_index:02d}"
    clip["source_context_file"] = str(source_context_path)
    clip["source_id"] = source_id
    clip["run_id"] = RUN_ID
    clip["output_directory"] = str(SOURCE_OUTPUT_DIR)
    clip["transcript_file"] = str(transcript_path)
    clip["source_title"] = source_context.get("title", "")
    clip["source_type"] = source_context.get("source_type", "unknown")
    clip["source_confidence"] = source_context.get("confidence", 0.0)

print()
print("=" * 70)
print("RENDERING SELECTED SHORTS")
print("=" * 70)

successful = 0


for index, clip in enumerate(

    selected,

    1

):

    if render_short(

        clip,

        index

    ):

        # Persist an exact per-Short manifest next to the rendered MP4.
        # The uploader uses this to bind this exact file to its exact
        # clip/transcript instead of guessing from the latest run.
        manifest_path = SOURCE_OUTPUT_DIR / f"short_{index:02d}.manifest.json"
        manifest = {
            "schema_version": 2,
            "clip_id": clip.get("clip_id", ""),
            "source_id": source_id,
            "run_id": RUN_ID,
            "source_title": clip.get("source_title", ""),
            "source_type": clip.get("source_type", "unknown"),
            "output_file": str(SOURCE_OUTPUT_DIR / f"short_{index:02d}.mp4"),
            "source_master_file": str(
                SOURCE_OUTPUT_DIR / f"short_{index:02d}.source.mp4"
            ),
            "caption_file": str(RENDER_TEMP_DIR / f"captions_{index}.ass"),
            "framing": {
                "mode": "auto_face_tracking",
                "center_x": 0.5,
                "center_y": 0.5,
                "zoom": 1.0,
            },
            "transcript_file": str(transcript_path),
            "clip": clip,
        }
        with open(manifest_path, "w", encoding="utf-8") as mf:
            json.dump(manifest, mf, indent=2, ensure_ascii=False)
        successful += 1


# ============================================================
# SAVE SELECTION
# ============================================================

selection_path = (
    TEMP_DIR
    /
    f"selected_{source_id}_{RUN_ID}.json"
)


with open(

    selection_path,

    "w",

    encoding="utf-8"

) as f:

    json.dump(

        selected,

        f,

        indent=2,

        ensure_ascii=False
    )


# ============================================================
# COMPLETE
# ============================================================

print()
print("=" * 70)
print("BOT COMPLETE")
print("=" * 70)

print()
print(
    f"Created {successful} Shorts."
)

print()
print(
    "Output:"
)

print(
    SOURCE_OUTPUT_DIR
)

print()

failed = len(selected) - successful
if failed:
    print(f"ERROR: {failed} of {len(selected)} selected Shorts failed to render.")
    sys.exit(1)

if os.environ.get("YT_AUTO_BOT_SOURCE_MODE", "").strip().lower() not in {"local", "youtube"}:
    input("Press Enter to exit...")
