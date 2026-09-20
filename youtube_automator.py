"""Compatibility entry point for the packaged YouTube publisher."""

from youtube_clipper.publishing.youtube import *  # noqa: F401,F403
from youtube_clipper.publishing.youtube import main


if __name__ == "__main__":
    main()
