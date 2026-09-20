"""YouTube publishing queue helpers shared by the CLI and dashboard."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from youtube_clipper.publishing.account_connections import token_path
from youtube_clipper.publishing.storage import atomic_save_json


def parse_youtube_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def future_scheduled_videos(videos, now=None):
    """Return normalized future private schedules in chronological order."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    scheduled = []
    for video in videos or []:
        status = video.get("status") or {}
        publish_at = parse_youtube_time(status.get("publishAt"))
        if not publish_at or publish_at <= now or status.get("privacyStatus") != "private":
            continue
        snippet = video.get("snippet") or {}
        scheduled.append({
            "video_id": str(video.get("id") or ""),
            "title": str(snippet.get("title") or "Untitled video"),
            "publish_at": publish_at.isoformat().replace("+00:00", "Z"),
            "privacy_status": "private",
            "thumbnail": ((snippet.get("thumbnails") or {}).get("medium") or {}).get("url"),
        })
    return sorted(scheduled, key=lambda item: item["publish_at"])


def next_schedule_slots(
    videos,
    count,
    *,
    interval_minutes=720,
    start_delay_minutes=10,
    now=None,
):
    """Append ``count`` slots after the channel's latest future schedule."""
    if count <= 0:
        return []
    if interval_minutes < 1:
        raise ValueError("Schedule interval must be at least one minute.")
    if start_delay_minutes < 1:
        raise ValueError("Start delay must be at least one minute.")

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    existing = [
        parse_youtube_time(item["publish_at"])
        for item in future_scheduled_videos(videos, now)
    ]
    if existing:
        cursor = max(existing) + timedelta(minutes=interval_minutes)
    else:
        cursor = now + timedelta(minutes=start_delay_minutes)
    cursor = cursor.replace(second=0, microsecond=0)
    return [cursor + timedelta(minutes=interval_minutes * index) for index in range(count)]


def _youtube_service(account):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Install the project requirements to load the YouTube schedule.") from exc

    path = token_path(account)
    if not path.is_file():
        raise ValueError("Connect this YouTube account before loading its schedule.")
    credentials = Credentials.from_authorized_user_file(str(path))
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        atomic_save_json(path, json.loads(credentials.to_json()))
    if not credentials.valid:
        raise ValueError("The YouTube connection has expired. Reconnect the account.")
    return build("youtube", "v3", credentials=credentials)


def channel_videos(account):
    """Read all videos from the connected channel's uploads playlist."""
    youtube = _youtube_service(account)
    channels = youtube.channels().list(part="contentDetails", mine=True).execute().get("items", [])
    if not channels:
        raise ValueError("The connected account has no accessible YouTube channel.")
    playlist_id = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    videos = []
    page_token = None
    while True:
        page = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        ids = [item["contentDetails"]["videoId"] for item in page.get("items", [])]
        for offset in range(0, len(ids), 50):
            videos.extend(youtube.videos().list(
                part="snippet,status",
                id=",".join(ids[offset:offset + 50]),
            ).execute().get("items", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            return videos


def schedule_snapshot(account, *, interval_hours, start_delay_minutes, projected_count=5):
    videos = channel_videos(account)
    queue = future_scheduled_videos(videos)
    slots = next_schedule_slots(
        videos,
        projected_count,
        interval_minutes=max(1, round(float(interval_hours) * 60)),
        start_delay_minutes=int(start_delay_minutes),
    )
    return {
        "source": "youtube",
        "refreshed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "queue": queue,
        "projected_slots": [value.isoformat().replace("+00:00", "Z") for value in slots],
    }
