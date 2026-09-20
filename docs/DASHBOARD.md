# Dashboard guide

The dashboard is a private control panel around the existing generation and
YouTube publishing pipeline. It uses Python's standard library and does not add
a web-framework dependency.

## Start the dashboard

```bash
python scripts/start.py
```

Open <http://127.0.0.1:8765>.

The starter creates or repairs the project virtual environment, checks the
installation, and always launches the dashboard with that environment. Run
`.venv/bin/python scripts/doctor.py` on Linux/macOS or
`.\.venv\Scripts\python.exe scripts\doctor.py` on Windows for diagnostics
without starting the server.

## Accounts

Account definitions live in `config/accounts.json`. Each YouTube account should
use a separate token file and upload log:

```json
{
  "accounts": [
    {
      "id": "youtube_main",
      "name": "YouTube Main",
      "platform": "youtube",
      "token_file": "youtube_token.json",
      "upload_log": "cache/youtube_upload_log_youtube_main.json"
    }
  ]
}
```

Token files and upload logs remain local and are excluded from Git.

The **Accounts** page can connect or reconnect YouTube without running the
uploader. Create a Google OAuth **Web application**, enable the YouTube Data
API and YouTube Analytics API, and save its downloaded credentials as
`oauth_web_client.json`. Add the exact callback shown by the dashboard to the
client's authorized redirect URIs. Remote connections require HTTPS; the
Tailscale Serve address satisfies this requirement. Credentials remain on the
server, and disconnecting does not remove upload history.

The callback is derived from the address used to open the dashboard. A local
clone opened at `http://127.0.0.1:8765` uses
`http://127.0.0.1:8765/oauth/youtube/callback`; a server opened through
Tailscale uses its `https://...ts.net/oauth/youtube/callback` address. Each
installation needs its own ignored `oauth_web_client.json` and token files.

## Generation

The Generate view launches `main.py` for a supported YouTube URL. Output is
grouped under the selected account. Live job progress and recent process output
are available in the Activity view.

## Review and publishing

1. Choose **Adjust frame** to open the manual crop editor. Drag the 9:16 box
   or use Left, Center, Right, and Zoom. Completed adjustments are kept as
   crop points automatically. One point holds a fixed crop; seek to another
   time and adjust the box to create smooth movement. **Previous**, **Next**,
   and the time chips jump between points. Use **Play preview** to check the
   movement, **Undo last change** to undo an edit, or **Remove point** to
   remove a movement point (the Start point is retained). Choose **Save crop**
   to render the result. The centered manual preview replaces automatic face
   tracking only when saved. Expand **Compare with saved Short** to compare.
2. Choose **Trim** to set a new start and end. Saving shortens the final video,
   full-frame master, clean editing copy, captions, framing keyframes, and
   timeline manifest together. Regenerate the Short to recover removed media.
3. Choose **Captions** to edit text and timing or change font, size, colors,
   outline, shadow, position, and safe-area margin.
4. Select one or more rendered `short_XX.mp4` files.
5. Choose **Prepare upload**.
6. Review or edit the title, description, tags, and visibility.
7. Confirm the validation result and planned publishing action.
8. Choose **Approve and upload**.

The preparation step runs without OAuth or YouTube API calls. Approval stores
the exact reviewed request, and the uploader stops if its final request differs
from the approved copy.

## Analytics

The **Analytics** page reads completed YouTube reports for the selected period.
It shows video-level views, watch time, likes, comments, average viewed
percentage, and a small editing recommendation based on retention. Existing
upload-only tokens must be reconnected once to grant the two additional
read-only permissions. The app does not request monetary analytics.

## Remote access

The dashboard does not implement user authentication. Keep it bound to
localhost and use a trusted private proxy or VPN for remote access. The
[Proxmox deployment guide](PROXMOX_DEPLOYMENT.md) uses Tailscale Serve.

## Instagram

Instagram appears as an unavailable destination. Publishing is intentionally
not simulated because the Instagram Graph API requires an eligible
professional account, permissions, and publicly accessible media delivery.
