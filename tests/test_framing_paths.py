import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from youtube_clipper.video import framing


class FramingPathTests(unittest.TestCase):
    def test_configured_external_cache_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "app"
            cache = root / "mounted-data" / "cache"
            project.mkdir()
            cache.mkdir(parents=True)
            caption = cache / "render" / "captions.ass"
            caption.parent.mkdir()
            caption.write_text("[Script Info]\n", encoding="utf-8")

            with patch.multiple(
                framing.bot_config,
                PROJECT_ROOT=project,
                CACHE_DIR=cache,
            ):
                resolved, filter_path = framing._caption_file_and_filter_path(caption)

            self.assertEqual(resolved, caption.resolve())
            self.assertIn("captions.ass", filter_path)

    def test_caption_outside_configured_cache_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "app"
            cache = root / "mounted-data" / "cache"
            project.mkdir()
            cache.mkdir(parents=True)
            outside = root / "untrusted.ass"
            outside.write_text("[Script Info]\n", encoding="utf-8")

            with patch.multiple(
                framing.bot_config,
                PROJECT_ROOT=project,
                CACHE_DIR=cache,
            ), self.assertRaisesRegex(ValueError, "configured cache"):
                framing._caption_file_and_filter_path(outside)

    def test_project_cache_symlink_uses_application_facing_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "app"
            target = root / "mounted-data" / "cache"
            project.mkdir()
            target.mkdir(parents=True)
            link = project / "cache"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Directory symlinks are unavailable: {exc}")
            caption = target / "render" / "captions.ass"
            caption.parent.mkdir()
            caption.write_text("[Script Info]\n", encoding="utf-8")

            with patch.multiple(
                framing.bot_config,
                PROJECT_ROOT=project,
                CACHE_DIR=link,
            ):
                _, filter_path = framing._caption_file_and_filter_path(caption)

            self.assertEqual(filter_path, "cache/render/captions.ass")


if __name__ == "__main__":
    unittest.main()
