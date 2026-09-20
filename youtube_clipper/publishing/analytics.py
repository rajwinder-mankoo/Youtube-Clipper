"""Read-only YouTube Analytics summaries for connected channels."""

from __future__ import annotations

import json
from datetime import date, timedelta

from youtube_clipper.publishing.account_connections import ANALYTICS_SCOPES, token_path
from youtube_clipper.publishing.storage import atomic_save_json


def performance_note(row):
    retention = float(row.get("averageViewPercentage") or 0)
    views = int(row.get("views") or 0)
    if views < 10:
        return "Early data — wait for more views before changing the edit."
    if retention >= 80:
        return "Strong retention — reuse this opening and pacing pattern."
    if retention >= 55:
        return "Healthy retention — test a sharper first two seconds."
    return "Low retention — tighten the opening and remove slower sections."


def youtube_analytics(account, days=28):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Install the project requirements to load analytics.") from exc
    days = max(7, min(365, int(days)))
    path = token_path(account)
    if not path.is_file():
        raise ValueError("Connect this YouTube account before loading analytics.")
    credentials = Credentials.from_authorized_user_file(str(path))
    if not credentials.has_scopes(ANALYTICS_SCOPES):
        raise ValueError("Reconnect this account to grant read-only analytics access.")
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        atomic_save_json(path, json.loads(credentials.to_json()))
    if not credentials.valid:
        raise ValueError("The YouTube connection has expired. Reconnect the account.")

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    response = build("youtubeAnalytics", "v2", credentials=credentials).reports().query(
        ids="channel==MINE",
        startDate=start.isoformat(),
        endDate=end.isoformat(),
        metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments",
        dimensions="video",
        sort="-views",
        maxResults=50,
    ).execute()
    columns = [item["name"] for item in response.get("columnHeaders", [])]
    rows = [dict(zip(columns, values)) for values in response.get("rows", [])]
    for row in rows:
        row["note"] = performance_note(row)
    return {
        "days": days,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "videos": rows,
        "summary": {
            "views": sum(int(row.get("views") or 0) for row in rows),
            "watch_minutes": round(sum(float(row.get("estimatedMinutesWatched") or 0) for row in rows), 1),
            "likes": sum(int(row.get("likes") or 0) for row in rows),
            "comments": sum(int(row.get("comments") or 0) for row in rows),
        },
    }
