"""Dashboard-safe YouTube account connection helpers."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from youtube_clipper import config
from youtube_clipper.publishing.storage import atomic_save_json


WEB_CLIENT_FILE = config.PROJECT_ROOT / "oauth_web_client.json"
YOUTUBE_CONNECT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
ANALYTICS_SCOPES = set(YOUTUBE_CONNECT_SCOPES[-2:])
LOCAL_OAUTH_HOSTS = {"127.0.0.1", "localhost", "::1"}


def oauth_dependency_help():
    python = (
        r".\.venv\Scripts\python.exe"
        if os.name == "nt"
        else ".venv/bin/python"
    )
    return (
        "Google OAuth dependencies are missing from the Python running this dashboard. "
        f"Run 'python scripts/bootstrap.py', then restart with "
        f"'{python} dashboard.py'."
    )


@contextmanager
def oauth_transport(redirect_uri):
    """Permit OAuthLib's HTTP exception only for a validated loopback callback."""
    parsed = urlparse(str(redirect_uri or ""))
    local_http = parsed.scheme == "http" and parsed.hostname in LOCAL_OAUTH_HOSTS
    previous = os.environ.get("OAUTHLIB_INSECURE_TRANSPORT")
    if local_http:
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    try:
        yield
    finally:
        if local_http:
            if previous is None:
                os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
            else:
                os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = previous


def token_path(account):
    raw = account.get("token_file")
    if not raw:
        raise ValueError("This account has no token file configured.")
    path = (config.PROJECT_ROOT / str(raw)).resolve()
    path.relative_to(config.PROJECT_ROOT.resolve())
    if path.suffix.lower() != ".json":
        raise ValueError("The configured token file must be JSON.")
    return path


def connection_status(account):
    result = {
        "id": str(account.get("id", "")),
        "name": str(account.get("name") or account.get("id") or "Account"),
        "platform": str(account.get("platform", "")),
        "connected": False,
        "analytics_ready": False,
    }
    if result["platform"] != "youtube":
        result["status"] = account.get("status", "not_configured")
        return result
    try:
        data = json.loads(token_path(account).read_text(encoding="utf-8"))
        scopes = set(data.get("scopes") or [])
        result["connected"] = bool(data.get("refresh_token") or data.get("token"))
        result["analytics_ready"] = result["connected"] and ANALYTICS_SCOPES <= scopes
    except Exception:
        pass
    result["status"] = "connected" if result["connected"] else "not_configured"
    return result


def validate_dashboard_origin(origin):
    parsed = urlparse(str(origin or ""))
    local = parsed.hostname in LOCAL_OAUTH_HOSTS
    if not parsed.netloc or (parsed.scheme != "https" and not local):
        raise ValueError("Account connection requires the HTTPS Tailscale dashboard or localhost.")
    return f"{parsed.scheme}://{parsed.netloc}"


def youtube_oauth_flow(flow_class, *, state=None, code_verifier=None):
    kwargs = {
        "scopes": YOUTUBE_CONNECT_SCOPES,
        "autogenerate_code_verifier": code_verifier is None,
    }
    if state is not None:
        kwargs["state"] = state
    if code_verifier is not None:
        kwargs["code_verifier"] = code_verifier
    return flow_class.from_client_secrets_file(str(WEB_CLIENT_FILE), **kwargs)


def begin_youtube_connection(account, origin):
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise RuntimeError(oauth_dependency_help()) from exc
    if not WEB_CLIENT_FILE.is_file():
        raise ValueError(
            "Add a Google OAuth Web application file named oauth_web_client.json, "
            "then register this dashboard's /oauth/youtube/callback URL in Google Cloud."
        )
    base = validate_dashboard_origin(origin)
    redirect_uri = f"{base}/oauth/youtube/callback"
    flow = youtube_oauth_flow(Flow)
    flow.redirect_uri = redirect_uri
    with oauth_transport(redirect_uri):
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
    if not flow.code_verifier:
        raise RuntimeError("Google OAuth did not create the required PKCE verifier.")
    return authorization_url, state, redirect_uri, flow.code_verifier


def finish_youtube_connection(
    account, state, redirect_uri, authorization_response, code_verifier
):
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    flow = youtube_oauth_flow(
        Flow, state=state, code_verifier=code_verifier
    )
    flow.redirect_uri = redirect_uri
    with oauth_transport(redirect_uri):
        flow.fetch_token(authorization_response=authorization_response)
    credentials = flow.credentials
    channel = build("youtube", "v3", credentials=credentials).channels().list(
        part="snippet", mine=True
    ).execute().get("items", [])
    if not channel:
        raise ValueError("The selected Google account has no accessible YouTube channel.")
    destination = token_path(account)
    atomic_save_json(destination, json.loads(credentials.to_json()))
    return channel[0]["snippet"]["title"]


def disconnect_account(account):
    path = token_path(account)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed
