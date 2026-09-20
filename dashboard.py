"""Compatibility entry point for the packaged dashboard backend."""

from youtube_clipper.backend.dashboard import *  # noqa: F401,F403
from youtube_clipper.backend.dashboard import main


if __name__ == "__main__":
    main()
