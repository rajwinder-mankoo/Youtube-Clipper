# Architecture

YouTube Clipper keeps executable compatibility files at the repository root
and places application implementations in the `youtube_clipper` package. This
lets existing shell commands and the Proxmox systemd unit remain stable while
giving new code a clear owner.

## Entry points

| Command | Implementation | Responsibility |
|---|---|---|
| `python dashboard.py` | `youtube_clipper.backend.dashboard` | HTTP dashboard, job state, media streaming, and framing API |
| `python main.py` | `youtube_clipper.pipeline` | Download, transcription, moment selection, captions, and rendering |
| `python youtube_automator.py` | `youtube_clipper.publishing.youtube` | Validation, scheduling, OAuth, and YouTube publishing |
| `python run_all.py` | `youtube_clipper.cli` | Sequential command-line orchestration |

The root files should stay thin. New behavior belongs in the package module,
not in its compatibility entry point.

## Package ownership

- `backend/` owns local HTTP behavior, persistent dashboard jobs, subprocess
  control, and safe access to generated media.
- `metadata/` owns source identification and public title, description, and
  tag generation. It must not perform uploads.
- `publishing/` owns publishing policy, exact API request bodies, OAuth, and
  YouTube API calls.
- `video/` owns reusable video transformations such as manual 9:16 framing.
- `pipeline.py` coordinates the media-generation stages. Large reusable stages
  should move into `video/` or `metadata/` instead of growing the coordinator.
- `config.py` is the single source of truth for project paths and JSON-backed
  application settings.

## Non-code directories

- `dashboard/` contains only browser-delivered HTML, CSS, and JavaScript.
- `config/` contains user-editable JSON configuration, not Python modules.
- `tests/` contains deterministic regression checks.
- `docs/` contains operational and architectural documentation.
- `input/`, `output/`, `cache/`, `logs/`, and `music/` are runtime data and are
  excluded from Git.

## Dependency direction

Lower-level modules should remain independently testable:

```text
entry points -> coordinators -> metadata / video / publishing helpers
                              -> configuration
```

Metadata, policy, and payload helpers should not import the dashboard or start
subprocesses. The dashboard may launch coordinators, but coordinators should
not import the dashboard.
