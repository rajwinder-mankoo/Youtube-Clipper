import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import pysubs2
except ImportError:
    pysubs2 = None
from youtube_clipper.video import captions
from youtube_clipper.publishing import youtube


@unittest.skipUnless(pysubs2, "pysubs2 is not installed")
class CaptionTests(unittest.TestCase):
    def test_timing_and_text_validation(self):
        self.assertEqual(captions.validate_cues([{"start": 0, "end": 1, "text": "Hello"}], 2)[0]["end"], 1000)
        invalid = [[], [{"start": 1, "end": 1, "text": "Hello"}],
                   [{"start": 0, "end": 3, "text": "Hello"}],
                   [{"start": 0, "end": 1, "text": "{\\pos(0,0)}"}],
                   [{"start": float("nan"), "end": 1, "text": "Hi"}],
                   [{"start": 0, "end": 1, "text": " "}],
                   [{"start": 0, "end": 1.5, "text": "Hi"}, {"start": 1, "end": 2, "text": "Overlap"}]]
        for cues in invalid:
            with self.subTest(cues=cues), self.assertRaises(ValueError):
                captions.validate_cues(cues, 2)

    def test_render_failure_preserves_video_and_captions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "output").mkdir(); (root / "cache").mkdir()
            video = root / "output/short_01.mp4"
            video.write_bytes(b"original video")
            video.with_suffix(".clean.mp4").write_bytes(b"clean")
            ass = root / "cache/captions.ass"
            subs = pysubs2.SSAFile()
            subs.append(pysubs2.SSAEvent(start=0, end=1000, text="Original"))
            subs.save(str(ass))
            original = ass.read_bytes()
            video.with_suffix(".manifest.json").write_text(json.dumps({"caption_file": str(ass)}))
            capture = SimpleNamespace(get=lambda prop: 10 if prop == 1 else 30, release=lambda: None)
            cv2 = SimpleNamespace(VideoCapture=lambda _: capture, CAP_PROP_FPS=1, CAP_PROP_FRAME_COUNT=2)
            with patch.multiple(captions.config, PROJECT_ROOT=root, OUTPUT_DIR=root / "output", CACHE_DIR=root / "cache"), patch.dict("sys.modules", {"cv2": cv2}):
                info = captions.caption_info(video)
                with patch.object(captions.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="render failed")):
                    with self.assertRaisesRegex(RuntimeError, "render failed"):
                        captions.save_captions(video, [{"start": 0, "end": 1, "text": "Edited"}], info["revision"])
                self.assertEqual(video.read_bytes(), b"original video")
                self.assertEqual(ass.read_bytes(), original)
                with self.assertRaisesRegex(ValueError, "changed"):
                    captions.save_captions(video, [], "stale")

    def test_upload_uses_edited_ass_even_with_timeline(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(youtube, "TEMP_DIR", Path(directory)):
            ass = Path(directory) / "edited.ass"
            subs = pysubs2.SSAFile()
            subs.append(pysubs2.SSAEvent(start=200, end=900, text="Corrected words"))
            subs.save(str(ass))
            clip = {"timeline": [{"source_start": 0, "source_end": 2, "output_start": 0}], "rendered_caption_file": str(ass)}
            result = youtube.create_srt(clip, [], 1).read_text()
            self.assertIn("Corrected words", result)
            self.assertIn("00:00:00,200 --> 00:00:00,900", result)


if __name__ == "__main__":
    unittest.main()
