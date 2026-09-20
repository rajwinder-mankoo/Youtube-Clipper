"""Compatibility entry point for the packaged generation pipeline."""

from runpy import run_module


if __name__ == "__main__":
    run_module("youtube_clipper.pipeline", run_name="__main__")
