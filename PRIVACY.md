# Privacy Policy

Effective date: September 18, 2026

YouTube Clipper is a self-hosted application. It runs on infrastructure
controlled by the person who installs it and does not send application data to
a developer-operated YouTube Clipper service.

## Data the application processes

Depending on the features used, the application may process:

- Google OAuth credentials and authorization tokens;
- YouTube video URLs and public video metadata;
- source video, audio, frames, transcripts, captions, and generated clips;
- generated titles, descriptions, tags, and publishing schedules; and
- local job logs, upload history, validation reports, and configuration.

This information is used to download or process authorized source material,
generate Shorts, prepare upload metadata, prevent duplicate uploads, and carry
out publishing actions explicitly approved by the operator.

## Storage and retention

Application data is stored on the operator's own computer or server. Retention
is controlled by that operator. YouTube Clipper does not provide centralized
cloud storage, analytics, advertising, or cross-site tracking.

OAuth token files can permit continued access to the connected YouTube account
and must be protected as sensitive credentials.

## Third-party services

Features chosen by the operator may communicate with:

- Google OAuth and the YouTube Data API for account authorization, channel
  reconciliation, captions, and video publishing;
- YouTube and `yt-dlp` for retrieving supported source material; and
- public search providers used by source detection to research source context.

Those services process requests under their own terms and privacy policies.
Network operators may also receive ordinary connection information such as IP
addresses and request timestamps.

## Data sharing

The project does not sell personal information. Data is shared with third-party
services only when required for a feature initiated by the operator, or when
the operator independently chooses to share files or logs.

## Access, deletion, and revocation

The operator can remove locally stored source files, generated media, caches,
logs, and OAuth token files at any time. Google account access can also be
revoked from the Google Account third-party connections page. Revoking access
prevents future authenticated API requests until authorization is granted
again.

## Security

Operators are responsible for securing the host, restricting dashboard access,
protecting credential files, installing updates, and maintaining appropriate
backups. The dashboard is intended for private access and should not be exposed
directly to the public internet.

## Changes

This policy may be updated when the application's data handling or integrations
change. The effective date at the top of this document identifies the current
version.

## Contact

Questions about this policy can be submitted through the repository's GitHub
issue tracker. Sensitive security reports should use GitHub private
vulnerability reporting instead of a public issue.
