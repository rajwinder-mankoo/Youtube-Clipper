# Security policy

## Reporting a vulnerability

Report vulnerabilities through GitHub's private vulnerability reporting for
this repository. Do not open a public issue for credential exposure, upload
authorization bypasses, path traversal, or unintended content publication.

Include a concise description, reproduction steps, affected revision, and the
expected impact. Remove all real tokens, cookies, channel identifiers, and
private media from reports.

## Deployment precautions

- Keep `client_secret.json`, token files, cookies, and account configuration
  readable only by the service account.
- Keep the dashboard on `127.0.0.1` and use a private proxy or VPN for remote
  access. The dashboard does not provide its own user authentication.
- Do not expose the dashboard through public port forwarding or Tailscale
  Funnel.
- Keep the review-and-approve step enabled for interactive publishing.
- Test publishing changes with dry runs and private uploads first.
- Back up OAuth tokens and upload logs in encrypted storage.

## Supported versions

Security fixes are applied to the current `main` branch. Older snapshots and
forks may not contain the latest upload-safety protections.
