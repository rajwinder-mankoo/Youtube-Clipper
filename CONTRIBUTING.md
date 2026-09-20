# Contributing

Thank you for helping improve YouTube Clipper. Keep changes focused, explain the
user-facing behavior, and preserve the project's publishing safeguards.

## Development setup

1. Fork and clone the repository.
2. Run `python scripts/start.py` (`python3` on Linux/macOS) for automatic setup
   and a first dashboard launch.
3. Use `.venv` for every development and validation command.
4. Create a branch from `main`.

Run the portable validation suite before opening a pull request:

```powershell
.\.venv\Scripts\python.exe scripts\check.py
```

On Linux/macOS, use `.venv/bin/python scripts/check.py`. Node.js checks are
included automatically when Node is available.

Keep new application code inside the appropriate `youtube_clipper/` package.
Root Python files are stable compatibility entry points, not implementation
modules. See `docs/ARCHITECTURE.md` before introducing a new top-level module.

## Pull requests

- Describe the problem, approach, and verification performed.
- Include tests for regressions or upload-policy changes.
- Use dry runs or private uploads when testing publishing behavior.
- Keep platform-specific paths out of application code.
- Update the README or deployment documentation when behavior changes.
- Do not weaken explicit approval, validation, or duplicate-upload protection.

## Sensitive data

Never commit or attach:

- OAuth client secrets, access tokens, or refresh tokens
- Browser cookies or exported sessions
- Private source video, generated video, captions, or transcripts
- Account identifiers or unsanitized logs

If a security issue could expose credentials or publish content unexpectedly,
use GitHub's private vulnerability reporting instead of opening a public issue.
