
import json
import os
import re
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from datetime import datetime, timedelta, timezone

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    GOOGLE_IMPORT_ERROR = None
except ImportError as exc:  # Dry runs do not require Google client libraries.
    Request = Credentials = InstalledAppFlow = build = MediaFileUpload = None
    GOOGLE_IMPORT_ERROR = exc

    class HttpError(Exception):
        pass

from youtube_clipper.metadata.seo import (
    generate_metadata as generate_contextual_metadata,
    tags_are_near_duplicates,
)
from youtube_clipper.publishing.policy import (
    normalize_publishing_mode,
    publishing_action,
    unattended_upload_enabled,
)
from youtube_clipper.publishing.payload import (
    build_caption_insert_body,
    build_video_insert_body,
)
from youtube_clipper import config as bot_config
from youtube_clipper.publishing.storage import upload_log_lock, atomic_save_json
from youtube_clipper.publishing.scheduler import next_schedule_slots as append_schedule_slots

# ============================================================
# CONFIG
#
# Paths and logging now come from config.py (derived from the project
# folder itself, not a hardcoded "E:\YT Auto Bot" path). Variable names are
# kept the same as before so the rest of this file does not need to change.
# ============================================================

PROJECT_DIR = bot_config.PROJECT_DIR
OUTPUT_DIR = bot_config.OUTPUT_DIR
TEMP_DIR = bot_config.CACHE_DIR  # "cache" on disk; kept as TEMP_DIR in code below.

CLIENT_SECRETS = PROJECT_DIR / "client_secret.json"
TOKEN_FILE = Path(os.environ.get(
    "YT_AUTO_BOT_TOKEN_FILE",
    str(PROJECT_DIR / "youtube_token.json"),
))

bot_config.setup_logging("youtube_automator")

# This scope is required for uploads. It also lets us upload a
# YouTube caption track through captions.insert.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

NUMBER_OF_SHORTS = 5

# Scheduled uploads are spaced this many hours apart.
# PowerShell override: $env:YT_AUTO_BOT_INTERVAL_HOURS="12"
SCHEDULE_INTERVAL_HOURS = float(os.environ.get(
    "YT_AUTO_BOT_INTERVAL_HOURS",
    str(bot_config.setting("schedule_interval_hours", 12)),
))
SCHEDULE_INTERVAL_MINUTES = int(SCHEDULE_INTERVAL_HOURS * 60)
START_DELAY_MINUTES = int(os.environ.get(
    "YT_AUTO_BOT_START_DELAY_MINUTES",
    str(bot_config.setting("start_delay_minutes", 10)),
))

# "private" is strongly recommended for the first test.
# Change to "public" only after you verify the generated metadata.
PRIVACY_STATUS = normalize_publishing_mode(
    bot_config.setting("privacy_status", "scheduled")
)

CATEGORY_ID = "22"  # People & Blogs
DEFAULT_CHANNEL_KEYWORDS = bot_config.string_list_setting(
    "default_channel_keywords", ["shorts", "youtube shorts"]
)

# Add your own permanent channel keywords here.
CHANNEL_KEYWORDS = bot_config.string_list_setting("channel_keywords")

# If True, a .srt subtitle track is uploaded in addition to the
# burned-in captions already present in the rendered video.
UPLOAD_YOUTUBE_CAPTIONS = True

# Interactive by default. Set YT_AUTO_BOT_AUTO_UPLOAD=1 for unattended runs.
_auto_upload_env = os.environ.get("YT_AUTO_BOT_AUTO_UPLOAD")
AUTO_UPLOAD = unattended_upload_enabled(
    _auto_upload_env,
    default=bot_config.bool_setting("auto_upload_default", False),
)
DRY_RUN = (
    "--dry-run" in {arg.lower() for arg in sys.argv[1:]}
    or unattended_upload_enabled(os.environ.get("YT_AUTO_BOT_DRY_RUN"))
)
DRY_RUN_REPORT_DIR = OUTPUT_DIR / "dry-run-reports"
DRY_RUN_REPORT_PATH = os.environ.get("YT_AUTO_BOT_DRY_RUN_REPORT", "").strip()
APPROVED_REPORT_PATH = os.environ.get("YT_AUTO_BOT_APPROVED_REPORT", "").strip()
LIVE_SCHEDULE = unattended_upload_enabled(os.environ.get("YT_AUTO_BOT_LIVE_SCHEDULE"))

# Prevent duplicate uploads by recording uploaded files.
UPLOAD_LOG = Path(os.environ.get(
    "YT_AUTO_BOT_UPLOAD_LOG",
    str(TEMP_DIR / "youtube_upload_log.json"),
))

SELECTED_FILES_ENV = os.environ.get("YT_AUTO_BOT_SELECTED_FILES", "").strip()
SELECTED_FILES = {
    str(Path(p).resolve())
    for p in SELECTED_FILES_ENV.split(os.pathsep)
    if p.strip()
}


# ============================================================
# UTILITIES
# ============================================================

def normalize(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clean_title(text):
    text = normalize(text)
    text = re.sub(r"\s+", " ", text)
    text = text.replace("\n", " ")
    return text[:100].rstrip(" -:;,") or "Interesting Moment"


def clean_tag(tag):
    tag = normalize(tag).strip("#,")
    if not tag:
        return ""
    if re.search(r"https?://\S+|www\.\S+", tag, re.I):
        return ""
    if '"' in tag or "\n" in tag or "\r" in tag:
        return ""
    tag = re.sub(r"\s+", " ", tag).strip(" ,;|")
    if not tag or len(tag) > 60:
        return ""
    if not re.search(r"[A-Za-z0-9]", tag):
        return ""
    return tag


def youtube_tag_cost(tag, include_comma=False):
    tag = normalize(tag)
    cost = len(tag) + (2 if " " in tag else 0)
    return cost + (1 if include_comma else 0)


def youtube_tags_character_count(tags):
    return sum(
        youtube_tag_cost(tag, include_comma=i > 0)
        for i, tag in enumerate(tags)
    )


def validate_and_normalize_tags(tags, max_chars=470, allowed_phrases=None):
    """Final defense before videos.insert: normalize, dedupe and budget tags."""
    out = []
    seen = set()
    total = 0
    allowed = {
        normalize(x).casefold()
        for x in (allowed_phrases or [])
        if normalize(x)
    }

    for raw in tags or []:
        tag = clean_tag(str(raw))
        if not tag:
            continue

        key = tag.casefold()
        if key in seen:
            continue
        if any(tags_are_near_duplicates(tag, existing) for existing in out):
            continue

        # A final payload validator must also reject phrases that did not come
        # from the confirmed source/entity context. This catches stale or
        # accidental transcript phrases even if an upstream generator changes.
        low = tag.casefold()
        supported = (
            low in allowed
            or any(
                phrase in low
                for phrase in allowed
                if len(phrase) >= 4
            )
        )
        generic_allowed = low in {
            "shorts", "youtube shorts", "tv clips", "web series",
            "movie clips", "anime clips", "sports highlights", "news clips",
            "podcast clips", "music videos", "creator clips", "entertainment",
            "clips",
        }

        if len(tag.split()) == 1 and not supported and not generic_allowed:
            continue
        if len(tag.split()) >= 2 and not supported and not generic_allowed:
            continue

        cost = youtube_tag_cost(tag, include_comma=bool(out))
        if total + cost > max_chars:
            continue

        seen.add(key)
        out.append(tag)
        total += cost

    return out


def load_json(path, default=None):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ============================================================
# AUTHENTICATION
# ============================================================

def get_youtube_service():
    if GOOGLE_IMPORT_ERROR is not None:
        raise RuntimeError(
            "YouTube client dependencies are not installed. "
            "Run: python -m pip install -r requirements.txt"
        ) from GOOGLE_IMPORT_ERROR
    if not CLIENT_SECRETS.exists():
        print()
        print("=" * 70)
        print("MISSING YOUTUBE OAUTH CREDENTIALS")
        print("=" * 70)
        print()
        print("Put your downloaded Google OAuth desktop-app JSON here:")
        print(CLIENT_SECRETS)
        print()
        print("Rename it to:")
        print("client_secret.json")
        sys.exit(1)

    credentials = None

    if TOKEN_FILE.exists():
        try:
            credentials = Credentials.from_authorized_user_file(
                str(TOKEN_FILE),
                SCOPES,
            )
        except Exception:
            credentials = None

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CLIENT_SECRETS),
            SCOPES,
        )
        credentials = flow.run_local_server(port=0)

        save_json(
            TOKEN_FILE,
            json.loads(credentials.to_json()),
        )

    return build(
        "youtube",
        "v3",
        credentials=credentials,
    )


# ============================================================
# TRANSCRIPT / CLIP DATA
# ============================================================

def find_latest_selection():
    files = sorted(
        TEMP_DIR.glob("selected_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def find_transcript_for_selection(selection_path, selected=None):
    if not selection_path:
        return None

    # New selection files carry the exact source_id/run_id in each clip.
    if selected:
        transcript_file = selected[0].get("transcript_file")
        if transcript_file:
            path = Path(transcript_file)
            if path.exists():
                return path

        source_id = selected[0].get("source_id", "")
    else:
        source_id = ""

    if not source_id:
        # Legacy selection filename fallback.
        name = selection_path.name
        source_id = name.removeprefix("selected_").removesuffix(".json")

    path = TEMP_DIR / f"transcript_{source_id}.json"
    return path if path.exists() else None


def load_all_clip_data():
    """Load clip metadata for every processing run we can find.

    main.py stores each run in output/<source_id>/<run_id>/ and writes the
    exact output directory into each selected_*.json file.  The old uploader
    only loaded the newest selection, which meant Shorts in older output
    folders were invisible to the uploader.
    """
    selections = {}
    latest_path = find_latest_selection()

    selection_files = sorted(
        TEMP_DIR.glob("selected_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    for selection_path in selection_files:
        selected = load_json(selection_path, [])
        if not selected:
            continue

        transcript_path = find_transcript_for_selection(selection_path, selected)
        transcript = load_json(transcript_path, []) if transcript_path else []

        # A selection file can contain clips from one output directory.
        output_directory = selected[0].get("output_directory")
        if output_directory:
            key = str(Path(output_directory).resolve())
            # Newest selection for a directory wins.
            if key not in selections:
                selections[key] = {
                    "selection_path": selection_path,
                    "selected": selected,
                    "transcript": transcript,
                }

    if not selections:
        raise FileNotFoundError(
            "No usable selected_*.json files found in the temp folder. "
            "Run main.py first."
        )

    # Backward-compatible fallback data for files that do not have matching
    # selection metadata.
    if latest_path:
        latest_selected = load_json(latest_path, [])
        latest_transcript_path = find_transcript_for_selection(
            latest_path, latest_selected
        )
        latest_transcript = (
            load_json(latest_transcript_path, [])
            if latest_transcript_path
            else []
        )
    else:
        latest_selected = []
        latest_transcript = []

    return selections, (latest_path, latest_selected, latest_transcript)


def words_for_clip(transcript, start, end):
    words = []

    for segment in transcript:
        for word in segment.get("words", []):
            if word["start"] < end and word["end"] > start:
                words.append(word)

    return words


# ============================================================
# LOCAL SEO METADATA GENERATOR
# ============================================================

def generate_metadata(clip, index, transcript, project_dir=None, channel_keywords=None, default_channel_keywords=None, privacy_status=None):
    metadata = generate_contextual_metadata(
        clip=clip,
        index=index,
        transcript=transcript,
        project_dir=project_dir or PROJECT_DIR,
        channel_keywords=channel_keywords if channel_keywords is not None else CHANNEL_KEYWORDS,
        default_channel_keywords=default_channel_keywords if default_channel_keywords is not None else DEFAULT_CHANNEL_KEYWORDS,
        privacy_status=privacy_status or PRIVACY_STATUS,
    )
    # Final public-description guard: only the compact Series/#tags format is
    # allowed to reach YouTube. This protects uploads made from old cache data.
    context = metadata.get("source_context") or {}
    raw_title = str(context.get("series_name") or context.get("show_name") or context.get("anime_name") or context.get("title") or clip.get("source_title") or "").strip()
    if "|" in raw_title:
        series = raw_title.split("|")[-1].strip().strip('"')
    elif re.search(r"\s-\s", raw_title):
        series = re.split(r"\s-\s", raw_title)[-1].strip().strip('"')
    else:
        series = raw_title.strip('"')
    if series:
        hashes=[]
        for tag in metadata.get("tags", []):
            h="#"+re.sub(r"[^A-Za-z0-9]", "", str(tag))
            if len(h)>=4 and h.casefold() not in {x.casefold() for x in hashes}: hashes.append(h)
        metadata["description"] = f'Series - "{series}"\n\n#tags -\n' + " ".join(hashes)
    return metadata


# ============================================================
# SRT CAPTIONS
# ============================================================

def format_srt_time(seconds):
    seconds = max(0, float(seconds))
    millis = int(round((seconds - int(seconds)) * 1000))
    whole = int(seconds)

    if millis >= 1000:
        whole += 1
        millis = 0

    hours = whole // 3600
    minutes = (whole % 3600) // 60
    secs = whole % 60

    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def create_srt(clip, transcript, index, clip_key=""):
    # The retained ASS is authoritative: it has render timing and any caption
    # edits. Regenerating from the transcript would discard those edits.
    caption_file = clip.get("rendered_caption_file")
    if caption_file and Path(caption_file).is_file():
        import pysubs2
        safe_key = re.sub(r"[^A-Za-z0-9_-]+", "_", clip_key or "clip")[:80]
        srt_path = TEMP_DIR / f"youtube_caption_{safe_key}_{index:02d}.srt"
        pysubs2.load(str(caption_file), encoding="utf-8").save(str(srt_path), format_="srt", encoding="utf-8")
        return srt_path
    start = float(clip["start"])
    end = float(clip["end"])

    words = words_for_clip(transcript, start, end)

    timeline = clip.get("timeline")
    if timeline:
        mapped_words = []
        for word in words:
            # Intersect words with retained intervals; omitted audio must never
            # acquire a subtitle at an unrelated position in the output.
            for interval in timeline:
                left = max(word["start"], interval["source_start"])
                right = min(word["end"], interval["source_end"])
                if right > left:
                    offset = interval["output_start"] - interval["source_start"]
                    mapped_words.append({**word, "start": left + offset, "end": right + offset})
        words = mapped_words
        start = 0.0
        end = max(item["output_start"] + item["source_end"] - item["source_start"] for item in timeline)

    if not words:
        return None

    groups = []
    group = []
    chars = 0

    for word in words:
        text = normalize(word["text"])
        if not text:
            continue

        group.append(word)
        chars += len(text) + 1

        punctuation = text.endswith((".", "?", "!", ",", ";", ":"))

        if len(group) >= 4 or chars >= 42 or punctuation:
            groups.append(group)
            group = []
            chars = 0

    if group:
        groups.append(group)

    lines = []
    number = 1

    for group in groups:
        gs = max(start, group[0]["start"])
        ge = min(end, group[-1]["end"])

        if ge <= gs:
            continue

        text = " ".join(normalize(w["text"]) for w in group)

        lines.extend([
            str(number),
            f"{format_srt_time(gs - start)} --> {format_srt_time(ge - start)}",
            text,
            "",
        ])
        number += 1

    if not lines:
        return None

    # Filename is scoped by clip_key (the clip's clip_id when available,
    # otherwise the selection file's own name) plus index -- not index alone.
    # Index alone collides: two different runs/sources can each have a
    # "short #1", and previously both wrote to the same
    # cache/youtube_caption_01.srt path.
    safe_key = re.sub(r"[^A-Za-z0-9_-]+", "_", clip_key or "clip")[:80]
    srt_path = TEMP_DIR / f"youtube_caption_{safe_key}_{index:02d}.srt"
    srt_path.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

    return srt_path


# ============================================================
# UPLOAD + SCHEDULING
# ============================================================

def iso_to_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_channel_uploads(youtube):
    channel = youtube.channels().list(
        part="contentDetails",
        mine=True,
    ).execute()
    items = channel.get("items", [])
    if not items:
        raise RuntimeError("OAuth account has no accessible YouTube channel.")

    playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    videos = []
    page = None

    while True:
        data = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=page,
        ).execute()

        ids = [x["contentDetails"]["videoId"] for x in data.get("items", [])]
        if ids:
            for i in range(0, len(ids), 50):
                chunk = ids[i:i + 50]
                videos.extend(
                    youtube.videos().list(
                        part="snippet,status,processingDetails",
                        id=",".join(chunk),
                    ).execute().get("items", [])
                )

        page = data.get("nextPageToken")
        if not page:
            break

    return videos


def reconcile_due_schedules(youtube, videos):
    now = datetime.now(timezone.utc)

    for video in videos:
        status = video.get("status", {})
        publish_at = iso_to_dt(status.get("publishAt"))

        if (
            publish_at
            and publish_at <= now
            and status.get("privacyStatus") == "private"
        ):
            video_id = video["id"]
            print(f"Publishing overdue scheduled video: {video_id}")
            youtube.videos().update(
                part="status",
                body={
                    "id": video_id,
                    "status": {"privacyStatus": "public"},
                },
            ).execute()


def reconcile_upload_log(youtube, upload_log):
    """Reconcile local upload state with YouTube before scheduling.

    The local log is only a duplicate-protection cache; YouTube is the source
    of truth for whether a video still exists. If a user deletes a video in
    YouTube Studio, its video ID is no longer returned by videos.list(), so the
    corresponding local-log entry is removed and the rendered Short becomes
    uploadable again.

    If the API check fails, keep the log untouched rather than risking a
    duplicate upload.
    """
    if not upload_log:
        return upload_log, []

    items = list(upload_log.items())
    video_ids = [
        str(entry.get("video_id") or "").strip()
        for _, entry in items
        if str(entry.get("video_id") or "").strip()
    ]
    if not video_ids:
        return upload_log, []

    live_ids = set()
    try:
        for offset in range(0, len(video_ids), 50):
            chunk = video_ids[offset:offset + 50]
            response = youtube.videos().list(
                part="id,status",
                id=",".join(chunk),
            ).execute()
            live_ids.update(
                str(item.get("id"))
                for item in response.get("items", [])
                if item.get("id")
            )
    except Exception as exc:
        print()
        print("WARNING: Could not reconcile upload log with YouTube.")
        print(f"         Keeping local log unchanged: {exc}")
        return upload_log, []

    cleaned = {}
    deleted = []
    for path_key, entry in items:
        video_id = str(entry.get("video_id") or "").strip()
        if video_id and video_id not in live_ids:
            deleted.append((path_key, video_id, entry))
            continue
        cleaned[path_key] = entry

    if deleted:
        print()
        print("YOUTUBE STATE RECONCILIATION")
        print("=" * 70)
        for path_key, video_id, _entry in deleted:
            print(f"Deleted on YouTube -> reopening local Short: {video_id}")
            print(f"  {path_key}")
        save_upload_log(cleaned)

    return cleaned, deleted


def next_schedule_slots(videos, count):
    """Append new slots after the latest future channel schedule."""
    return append_schedule_slots(
        videos,
        count,
        interval_minutes=SCHEDULE_INTERVAL_MINUTES,
        start_delay_minutes=START_DELAY_MINUTES,
    )


def validate_before_upload(video_path, metadata):
    """Validate the exact metadata that will be sent to videos.insert."""
    problems = []

    if not video_path.exists():
        problems.append(f"Input file does not exist: {video_path}")
    else:
        if video_path.stat().st_size <= 0:
            problems.append(f"Output file is empty (0 bytes): {video_path}")
        if video_path.suffix.lower() != ".mp4":
            problems.append(f"Output file is not an .mp4: {video_path}")

    try:
        video_path.resolve().relative_to(OUTPUT_DIR.resolve())
    except ValueError:
        problems.append(f"Output file is not under the output/ directory: {video_path}")

    if not normalize(metadata.get("title")):
        problems.append("No title was generated.")
    if not normalize(metadata.get("description")):
        problems.append("No description was generated.")

    public_text = " ".join([
        normalize(metadata.get("title")),
        normalize(metadata.get("description")),
        " ".join(metadata.get("tags") or []),
    ])
    if re.search(r"https?://\S+|www\.\S+", public_text, re.I):
        problems.append("A URL was found in public metadata (title/description/tags).")

    # Re-normalize the FINAL metadata immediately before upload. This protects
    # against future SEO changes and guarantees the payload itself is safe.
    original_tags = metadata.get("tags") or []
    context = metadata.get("source_context") or {}
    allowed_phrases = [
        context.get("title", ""),
        *context.get("characters", [])[:8],
        *context.get("actors", [])[:8],
        *context.get("keywords", [])[:20],
        "shorts",
        "youtube shorts",
    ]
    final_tags = validate_and_normalize_tags(
        original_tags,
        max_chars=470,
        allowed_phrases=allowed_phrases,
    )
    metadata["tags"] = final_tags

    tag_chars = youtube_tags_character_count(final_tags)
    metadata["tag_character_count"] = tag_chars

    if not final_tags:
        problems.append("No valid YouTube tags remain after final validation.")
    if tag_chars > 500:
        problems.append(f"YouTube tag budget exceeded: {tag_chars}/500.")
    # YouTube does not require a minimum number of tags. Never reject an upload
    # merely because a clip has fewer than 10 *relevant* tags; doing so forces
    # keyword stuffing and was the reason valid Shorts were being skipped.
    # The generator still produces as many evidence-backed tags as possible.

    # Verify the exact list is internally clean.
    lowered = [t.casefold() for t in final_tags]
    if len(lowered) != len(set(lowered)):
        problems.append("Duplicate tags remain after final normalization.")

    return (not problems), problems

def check_cache_isolation():
    """Sanity check: nothing that looks like a generated cache/temp file is
    sitting in input/ or output/, which would defeat the point of separating
    them. This only warns -- it never deletes anything.
    """
    suspicious_suffixes = {".ass", ".srt", ".json"}
    input_dir = bot_config.INPUT_DIR
    offenders = [
        p for p in input_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in suspicious_suffixes
        and not p.name.endswith((".url.txt", ".metadata.json"))
    ]
    if offenders:
        print()
        print("WARNING: possible cache/processing files found inside input/:")
        for p in offenders:
            print(f"  {p}")


def upload_video(youtube, video_path, metadata, publish_at=None):
    privacy = metadata["privacyStatus"]
    body = build_video_insert_body(metadata, publish_at)

    print()
    print(f"Uploading: {video_path.name}")
    print(f"Privacy: {privacy}")
    if publish_at:
        print(
            "Scheduled public time:",
            publish_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        )

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        progress, response = request.next_chunk()
        if progress:
            print(f"Upload progress: {progress.progress() * 100:.1f}%")

    return response


def verify_video(youtube, video_id):
    items = youtube.videos().list(
        part="status,processingDetails,snippet",
        id=video_id,
    ).execute().get("items", [])
    return items[0] if items else None



def upload_caption(youtube, video_id, srt_path):
    """Upload an SRT subtitle track without failing the video upload.

    The previous v8 file called this function but did not define it, which
    caused the automator to stop immediately after a successful video upload.
    """
    if not srt_path or not srt_path.exists():
        return False

    print("Uploading YouTube subtitle track...")

    media = MediaFileUpload(
        str(srt_path),
        mimetype="application/x-subrip",
        resumable=False,
    )

    body = build_caption_insert_body(video_id)

    try:
        youtube.captions().insert(
            part="snippet",
            body=body,
            media_body=media,
        ).execute()
        print("YouTube caption track uploaded.")
        return True
    except HttpError as error:
        # A caption failure should never undo/fail the video upload.
        print()
        print("Caption upload failed (video upload is still successful):")
        print(error)
        return False

# ============================================================
# DUPLICATE PROTECTION
# ============================================================

def load_upload_log():
    return load_json(UPLOAD_LOG, {})


def save_upload_log(data):
    atomic_save_json(UPLOAD_LOG, data)


def scheduled_videos_from_upload_log(upload_log):
    """Convert local history into scheduler input without contacting YouTube."""
    videos = []
    for entry in upload_log.values():
        if not isinstance(entry, dict):
            continue
        videos.append({
            "id": entry.get("video_id"),
            "status": {
                "privacyStatus": entry.get("privacyStatus"),
                "publishAt": entry.get("publishAt"),
            },
        })
    return videos


def save_dry_run_report(report):
    if DRY_RUN_REPORT_PATH:
        path = Path(DRY_RUN_REPORT_PATH).resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = DRY_RUN_REPORT_DIR / f"dry_run_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, report)
    return path


def load_approved_items():
    if not APPROVED_REPORT_PATH:
        return {}
    report = load_json(Path(APPROVED_REPORT_PATH), {})
    if not isinstance(report, dict) or not report.get("approved"):
        raise ValueError("The approved review report is missing or invalid.")
    items = report.get("items") or []
    approved = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("video_path"):
            continue
        request = (item.get("planned_api_requests") or {}).get("videos.insert") or {}
        body = request.get("body")
        if isinstance(body, dict):
            approved[str(Path(item["video_path"]).resolve())] = item
    if not approved:
        raise ValueError("The approved review report contains no uploadable items.")
    return approved


# ============================================================
# EXACT SHORT -> CAPTION BINDING
# ============================================================

def load_short_manifest(video_path):
    manifest_path = video_path.with_suffix(".manifest.json")
    if not manifest_path.exists():
        return None
    data = load_json(manifest_path, None)
    if not isinstance(data, dict):
        return None
    clip = data.get("clip")
    transcript_file = data.get("transcript_file")
    if not isinstance(clip, dict) or not transcript_file:
        return None
    transcript_path = Path(transcript_file)
    transcript = load_json(transcript_path, []) if transcript_path.exists() else []
    if not transcript:
        return None
    clip = dict(clip)
    if data.get("caption_file"):
        clip["rendered_caption_file"] = data["caption_file"]
    return clip, transcript, None


def resolve_clip_binding(video_path, selection_map):
    # New files: use their self-contained manifest.
    manifest = load_short_manifest(video_path)
    if manifest:
        return manifest

    # Existing files created before manifests: bind by their own output folder
    # and Short index. Never use the newest selection as a fallback.
    run_data = selection_map.get(str(video_path.parent.resolve()))
    if not run_data:
        return None, None, None
    match = re.search(r"short_(\d+)$", video_path.stem, re.I)
    index = int(match.group(1)) if match else None
    if not index or index > len(run_data["selected"]):
        return None, None, None
    clip = run_data["selected"][index - 1]
    transcript = run_data["transcript"]
    if not isinstance(clip, dict) or not transcript:
        return None, None, None
    return clip, transcript, run_data["selection_path"]


# ============================================================
# MAIN
# ============================================================

def main():
    # Lock before reading history and hold through remote insertion and local
    # persistence. OS locks are released even if the process is terminated.
    with (nullcontext() if DRY_RUN else upload_log_lock(UPLOAD_LOG)):
        return _main()


def _main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    TEMP_DIR.mkdir(exist_ok=True)

    print()
    print("=" * 70)
    print("YOUTUBE SHORTS AUTOMATOR")
    print("=" * 70)
    print(f"Schedule interval: {SCHEDULE_INTERVAL_HOURS:g} hours")
    print(f"Publishing mode: {PRIVACY_STATUS}")
    print(f"Upload confirmation: {'disabled' if AUTO_UPLOAD else 'required'}")
    if DRY_RUN:
        print("DRY RUN: YouTube OAuth and API calls are disabled.")

    check_cache_isolation()

    selection_map, fallback_data = load_all_clip_data()
    latest_selection_path, latest_selected, latest_transcript = fallback_data

    print()
    print("Selection metadata loaded for output folders:")
    for folder in sorted(selection_map):
        print(f"  {folder}")
    if latest_selection_path:
        print("Latest selection file:")
        print(latest_selection_path)

    upload_log = load_upload_log()
    approved_items = load_approved_items()
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": DRY_RUN,
        "youtube_oauth_used": False,
        "youtube_api_calls_made": False,
        "account": os.environ.get("YT_AUTO_BOT_ACCOUNT_NAME", "default"),
        "publishing_mode": PRIVACY_STATUS,
        "schedule_basis": "current YouTube channel state" if (not DRY_RUN or LIVE_SCHEDULE) else "local upload log only; remote channel changes are not queried",
        "items": [],
        "skipped": [],
    }

    if DRY_RUN:
        if LIVE_SCHEDULE:
            youtube = get_youtube_service()
            existing = get_channel_uploads(youtube)
            report["youtube_oauth_used"] = True
            report["youtube_api_calls_made"] = True
        else:
            youtube = None
            existing = scheduled_videos_from_upload_log(upload_log)
    else:
        youtube = get_youtube_service()
        report["youtube_oauth_used"] = True
        # YouTube is the source of truth. If a Short was manually deleted from
        # Studio, reopen only that local duplicate-protection entry.
        upload_log, deleted_log_entries = reconcile_upload_log(youtube, upload_log)
        if deleted_log_entries:
            print(
                f"Reopened {len(deleted_log_entries)} deleted Short(s) for scheduling."
            )

    # IMPORTANT: scan recursively. main.py creates:
    # output/<source_id>/<run_id>/short_XX.mp4
    # so glob("short_*.mp4") at OUTPUT_DIR only sees files directly in the
    # output root and misses all Shorts inside source/run subfolders.
    output_files = sorted(
        OUTPUT_DIR.rglob("short_*.mp4"),
        key=lambda p: (p.stat().st_mtime, str(p).lower()),
    )

    if SELECTED_FILES:
        before = len(output_files)
        selected_set = {str(Path(p).resolve()) for p in SELECTED_FILES}
        output_files = [p for p in output_files if str(p.resolve()) in selected_set]
        print(f"Dashboard selection active: {len(output_files)} selected Short(s) of {before} rendered Short(s).")
        if output_files:
            print("Selected files discovered:")
            for p in output_files:
                print(f"  - {p}")
        missing_selected = sorted(selected_set - {str(p.resolve()) for p in output_files})
        if missing_selected:
            print(f"WARNING: {len(missing_selected)} dashboard-selected file(s) were not found under output/:")
            for p in missing_selected:
                print(f"  - {p}")
    if not output_files:
        raise FileNotFoundError(
            f"No rendered Shorts found anywhere under {OUTPUT_DIR}. "
            "Run main.py first."
        )

    print()
    print(f"Rendered Shorts found (all folders): {len(output_files)}")

    # Live runs use the channel as the scheduling source of truth. Dry runs
    # retain the local-only estimate created above and make no API call.
    if not DRY_RUN:
        existing = get_channel_uploads(youtube)
        if PRIVACY_STATUS == "scheduled":
            reconcile_due_schedules(youtube, existing)

    pending = []

    for video_path in output_files:
        key = str(video_path.resolve())

        if approved_items and key not in approved_items:
            print(f"Skipping {video_path.name}: it is not in the approved review.")
            continue

        if key in upload_log:
            print(
                f"Skipping already-uploaded file: {video_path.name} "
                f"-> {upload_log[key].get('video_id')}"
            )
            report["skipped"].append({
                "video_path": key,
                "reason": "present in the local upload log",
                "video_id": upload_log[key].get("video_id"),
            })
            continue

        match = re.search(r"short_(\d+)$", video_path.stem, re.I)
        index = int(match.group(1)) if match else 1

        # Exact file -> exact clip/transcript. Never use the latest selection.
        clip, run_transcript, run_selection_path = resolve_clip_binding(
            video_path, selection_map
        )
        if clip is None or not run_transcript:
            print(
                f"SKIPPING {video_path.name}: no exact clip/transcript binding "
                f"was found for {video_path.parent}. Refusing to upload with "
                "another Short's captions."
            )
            report["skipped"].append({
                "video_path": key,
                "reason": "no exact clip/transcript binding",
            })
            continue

        pending.append(
            (video_path, index, clip, key, run_transcript, run_selection_path)
        )

    if not pending:
        print("Nothing new to upload.")
        if DRY_RUN:
            report_path = save_dry_run_report(report)
            print(f"Dry-run report: {report_path}")
        return

    scheduled_count = (
        len(pending)
        if PRIVACY_STATUS == "scheduled" and not approved_items
        else 0
    )
    schedule_slots = next_schedule_slots(existing, scheduled_count) if scheduled_count else []
    schedule_index = 0

    for position, (video_path, index, clip, key, transcript, selection_path) in enumerate(pending):
        approved_item = approved_items.get(key)
        approved_body = None
        if approved_item:
            approved_body = approved_item["planned_api_requests"]["videos.insert"]["body"]
            approved_status = approved_body.get("status") or {}
            privacy = approved_status.get("privacyStatus")
            publish_at = iso_to_dt(approved_status.get("publishAt"))
            if privacy not in {"private", "public"}:
                raise ValueError(f"Approved privacy is invalid for {video_path.name}.")
        else:
            privacy, needs_schedule_slot = publishing_action(
                PRIVACY_STATUS, position, len(pending)
            )
            if needs_schedule_slot:
                publish_at = schedule_slots[schedule_index]
                schedule_index += 1
            else:
                publish_at = None

        metadata = generate_metadata(
            clip=clip,
            index=index,
            transcript=transcript,
            project_dir=PROJECT_DIR,
            channel_keywords=CHANNEL_KEYWORDS,
            default_channel_keywords=DEFAULT_CHANNEL_KEYWORDS,
            privacy_status=privacy,
        )
        if approved_body:
            approved_snippet = approved_body.get("snippet") or {}
            metadata.update({
                "title": approved_snippet.get("title", ""),
                "description": approved_snippet.get("description", ""),
                "tags": approved_snippet.get("tags") or [],
                "categoryId": approved_snippet.get("categoryId", metadata["categoryId"]),
                "defaultLanguage": approved_snippet.get(
                    "defaultLanguage", metadata["defaultLanguage"]
                ),
                "privacyStatus": privacy,
            })

        metadata_key = (
            selection_path.stem
            if selection_path
            else (clip.get("clip_id") or video_path.parent.name)
        )
        metadata_path = (
            TEMP_DIR
            / f"youtube_metadata_{metadata_key}_{index:02d}.json"
        )
        save_json(metadata_path, metadata)

        print()
        print("-" * 70)
        print(f"SHORT #{index}")
        print("-" * 70)
        if publish_at:
            mode_label = "SCHEDULED -> PUBLIC"
        elif privacy == "public":
            mode_label = "PUBLIC NOW"
        else:
            mode_label = "PRIVATE"
        print(f"MODE: {mode_label}")

        # Final validation mutates only metadata["tags"] and its derived count,
        # so the report below is exactly the payload that will be uploaded.
        ok, problems = validate_before_upload(video_path, metadata)

        context = metadata.get("source_context") or {}
        series = normalize(context.get("title")) or "Unknown"
        characters = [
            normalize(x) for x in context.get("characters", [])[:4]
            if normalize(x)
        ]

        print()
        print("=" * 50)
        print("FINAL YOUTUBE METADATA")
        print("=" * 50)
        print()
        print("Clip ID:")
        print(clip.get("clip_id") or f"{selection_path.stem}_{index:02d}")
        print()
        print("Series / Source:")
        print(series)
        print()
        print("Characters:")
        print(", ".join(characters) if characters else "None confirmed")
        print()
        print("TITLE:")
        print(metadata["title"])
        print()
        print("DESCRIPTION:")
        print(metadata["description"])
        print()
        print("TAGS:")
        for tag_number, tag in enumerate(metadata.get("tags", []), 1):
            print(f"{tag_number}. {tag}")

        print()
        print(f"Tag count: {len(metadata.get('tags', []))}")
        print(f"Tag character budget: {metadata.get('tag_character_count', 0)} / 500")
        print(f"YouTube tag validation: {'PASS' if ok else 'FAIL'}")
        print("=" * 50)

        # Save the post-validation metadata so the JSON report matches the
        # exact tag list that is about to enter the API request.
        save_json(metadata_path, metadata)

        report_item = {
            "order": position + 1,
            "video_path": str(video_path.resolve()),
            "clip_id": clip.get("clip_id") or f"{video_path.parent.name}_{index:02d}",
            "metadata_path": str(metadata_path.resolve()),
            "source": {
                "title": (metadata.get("source_context") or {}).get("title"),
                "type": (metadata.get("source_context") or {}).get("source_type"),
                "confidence": (metadata.get("source_context") or {}).get("confidence"),
            },
            "planned_action": mode_label,
            "planned_publish_at": (
                publish_at.isoformat().replace("+00:00", "Z")
                if publish_at
                else None
            ),
            "validation": {"passed": ok, "problems": problems},
        }

        if not ok:
            print()
            print("VALIDATION FAILED -- skipping this Short (not uploaded):")
            for problem in problems:
                print(f"  - {problem}")
            if DRY_RUN:
                report["items"].append(report_item)
            continue

        if approved_body and build_video_insert_body(metadata, publish_at) != approved_body:
            print()
            print("APPROVAL MISMATCH: validated metadata differs from the reviewed payload.")
            print("Refusing to upload. Prepare and approve this Short again.")
            continue

        print()
        print("Validation passed:")
        print("  input file exists, output file exists and is valid,")
        print(f"  title/description generated, {len(metadata['tags'])} final tags,")
        print(f"  YouTube tag budget: {metadata['tag_character_count']} / 500,")
        print("  output file is under output/.")

        if DRY_RUN:
            clip_key = (
                clip.get("clip_id")
                or (selection_path.stem if selection_path else video_path.stem)
            )
            srt_path = None
            caption_request = None
            if UPLOAD_YOUTUBE_CAPTIONS and transcript:
                srt_path = create_srt(clip, transcript, index, clip_key=clip_key)
                caption_request = {
                    "part": "snippet",
                    "body": build_caption_insert_body("$VIDEO_ID_FROM_UPLOAD"),
                    "media_body": {
                        "path": str(srt_path.resolve()),
                        "mimetype": "application/x-subrip",
                        "resumable": False,
                    },
                }
            report_item.update({
                "caption_path": str(srt_path.resolve()) if srt_path else None,
                "metadata": metadata,
                "planned_api_requests": {
                    "videos.insert": {
                        "part": "snippet,status",
                        "body": build_video_insert_body(metadata, publish_at),
                        "media_body": {
                            "path": str(video_path.resolve()),
                            "mimetype": "video/mp4",
                            "resumable": True,
                        },
                    },
                    "captions.insert": caption_request,
                },
            })
            report["items"].append(report_item)
            print("\nDRY RUN: payload recorded; nothing was uploaded.")
            continue


        if AUTO_UPLOAD:
            print("\nUnattended upload is enabled; uploading without confirmation.")
        else:
            if not sys.stdin.isatty():
                print("\nSKIPPING: upload confirmation requires an interactive terminal.")
                print("Set YT_AUTO_BOT_AUTO_UPLOAD=1 only after reviewing the output.")
                continue
            answer = input(f"\nUpload {video_path.name} as {mode_label}? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Skipped by user.")
                continue

        response = upload_video(
            youtube,
            video_path,
            metadata,
            publish_at=publish_at,
        )
        video_id = response["id"]

        # The insertion has succeeded. Record it before any optional follow-up
        # can fail so a retry cannot publish a second copy.
        upload_log[key] = {
            "video_id": video_id,
            "title": metadata["title"],
            "privacyStatus": privacy,
            "publishAt": publish_at.isoformat().replace("+00:00", "Z") if publish_at else None,
            "caption_uploaded": False,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        save_upload_log(upload_log)

        caption_uploaded = False
        if UPLOAD_YOUTUBE_CAPTIONS and transcript:
            clip_key = (
                clip.get("clip_id")
                or (selection_path.stem if selection_path else video_path.stem)
            )
            srt_path = create_srt(clip, transcript, index, clip_key=clip_key)
            caption_uploaded = upload_caption(youtube, video_id, srt_path)

        upload_log[key]["caption_uploaded"] = caption_uploaded
        save_upload_log(upload_log)
        verified = verify_video(youtube, video_id)
        actual = verified.get("status", {}) if verified else {}

        print()
        print("YouTube verification:")
        print("Video ID:", video_id)
        print("Privacy:", actual.get("privacyStatus"))
        print("PublishAt:", actual.get("publishAt", "none"))

        if privacy == "public" and actual.get("privacyStatus") != "public":
            print()
            print("WARNING: YouTube did not leave the upload PUBLIC.")
            print("This can happen when the API project/account is restricted.")
            print("The bot cannot bypass a YouTube API restriction.")

        print()
        print("Done:")
        print(f"https://www.youtube.com/shorts/{video_id}")

    if DRY_RUN:
        report_path = save_dry_run_report(report)
        print()
        print("=" * 70)
        print("DRY RUN COMPLETE — NOTHING WAS UPLOADED")
        print("=" * 70)
        print(f"Review report: {report_path}")
        if PRIVACY_STATUS == "scheduled":
            if LIVE_SCHEDULE:
                print("Schedule times were calculated from the current YouTube queue.")
            else:
                print("Schedule times are estimates based on the local upload log.")
        return

    print()
    print("=" * 70)
    print("UPLOAD / SCHEDULING COMPLETE")
    print("=" * 70)
    print(f"Publishing mode: {PRIVACY_STATUS}")
    if PRIVACY_STATUS == "scheduled":
        print(f"Interval: {SCHEDULE_INTERVAL_HOURS:g} hours")
        print("All pending uploads: scheduled PRIVATE -> PUBLIC")
        print("New uploads begin after the latest future channel schedule")


if __name__ == "__main__":
    try:
        main()
    except HttpError as error:
        print()
        print("YouTube API error:")
        print(error)
        sys.exit(1)
    except Exception as error:
        print()
        print("ERROR:")
        print(error)
        sys.exit(1)
