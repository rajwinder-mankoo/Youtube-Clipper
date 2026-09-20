"""Build the exact request bodies used by the YouTube Data API."""

from __future__ import annotations

from datetime import datetime


def build_video_insert_body(metadata: dict, publish_at: datetime | None = None) -> dict:
    privacy = metadata["privacyStatus"]
    status = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": False,
    }
    if publish_at:
        if privacy != "private":
            raise ValueError("A scheduled YouTube video must be private.")
        status["publishAt"] = publish_at.isoformat().replace("+00:00", "Z")

    return {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": metadata["categoryId"],
            "defaultLanguage": metadata["defaultLanguage"],
        },
        "status": status,
    }


def build_caption_insert_body(video_id: str) -> dict:
    return {
        "snippet": {
            "videoId": video_id,
            "language": "en",
            "name": "English",
            "isDraft": False,
        }
    }
