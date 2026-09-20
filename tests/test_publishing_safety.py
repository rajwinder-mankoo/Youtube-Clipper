"""Offline regressions for dashboard requests, upload durability and captions."""

import io
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from youtube_clipper.backend import dashboard
from youtube_clipper.publishing import youtube
from youtube_clipper.publishing.storage import atomic_save_json, upload_log_lock


class PublishingSafetyTests(unittest.TestCase):
    def test_browser_request_protection(self):
        cases = [
            ({"Origin": "https://evil.example"}, 403),
            ({"Origin": "null"}, 403),
            ({"Sec-Fetch-Site": "cross-site"}, 403),
            ({"Content-Type": "text/plain"}, 415),
            ({"Origin": "http://127.0.0.1:8765"}, 200),
            ({"Host": "clipper.example", "Origin": "https://clipper.example"}, 200),
            ({}, 200),  # Non-browser JSON clients remain supported.
        ]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                handler = object.__new__(dashboard.Handler)
                handler.path = "/api/stop"
                body = b'{"job_id":"test"}'
                handler.headers = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "Content-Length": str(len(body)), **extra}
                handler.rfile = io.BytesIO(body)
                with patch.object(dashboard, "stop_job", return_value=(True, "stopped")) as stop, patch.object(dashboard, "json_response") as response:
                    handler.do_POST()
                    args = response.call_args.args
                    self.assertEqual(args[2] if len(args) > 2 else 200, expected)
                    self.assertEqual(stop.called, expected == 200)

    def test_lock_excludes_another_process_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "uploads.json"
            code = "from youtube_clipper.publishing.storage import upload_log_lock; import sys\nwith upload_log_lock(sys.argv[1]): print('acquired')"
            with upload_log_lock(path):
                result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Another upload", result.stderr)
                with upload_log_lock(Path(directory) / "other-account.json"):
                    pass
            result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_atomic_write_preserves_previous_log_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "uploads.json"
            atomic_save_json(path, {"old": 1})
            with patch("youtube_clipper.publishing.storage.os.replace", side_effect=OSError("failure")):
                with self.assertRaises(OSError):
                    atomic_save_json(path, {"new": 2})
            self.assertEqual(json.loads(path.read_text()), {"old": 1})
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_caption_times_follow_retained_audio(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(youtube, "TEMP_DIR", Path(directory)):
            clip = {"start": 10, "end": 15, "timeline": [
                {"source_start": 10, "source_end": 11.65, "output_start": 0},
                {"source_start": 14, "source_end": 15, "output_start": 1.65},
            ]}
            transcript = [{"words": [
                {"start": 10, "end": 11, "text": "Hello,"},
                {"start": 12, "end": 13, "text": "omitted"},
                {"start": 14, "end": 15, "text": "world."},
            ]}]
            result = youtube.create_srt(clip, transcript, 1).read_text()
            self.assertIn("00:00:01,650 --> 00:00:02,650", result)
            self.assertNotIn("omitted", result)

    @unittest.skipUnless(importlib.util.find_spec("pysubs2"), "pysubs2 is not installed")
    def test_existing_manifest_uses_rendered_captions(self):
        import pysubs2
        with tempfile.TemporaryDirectory() as directory, patch.object(youtube, "TEMP_DIR", Path(directory)):
            root = Path(directory)
            captions = pysubs2.SSAFile()
            captions.events.append(pysubs2.SSAEvent(start=1650, end=2650, text="world."))
            ass = root / "captions.ass"
            captions.save(str(ass))
            transcript = root / "transcript.json"
            transcript.write_text(json.dumps([{"words": [{"start": 4, "end": 5, "text": "world."}]}]))
            video = root / "short_01.mp4"
            video.with_suffix(".manifest.json").write_text(json.dumps({
                "clip": {"start": 0, "end": 5}, "transcript_file": str(transcript), "caption_file": str(ass),
            }))
            clip, words, _ = youtube.load_short_manifest(video)
            result = youtube.create_srt(clip, words, 1).read_text()
            self.assertIn("00:00:01,650 --> 00:00:02,650", result)

    def test_successful_insert_is_saved_before_verification_failure(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            video = root / "short_01.mp4"
            video.write_bytes(b"video")
            log = root / "uploads.json"
            clip = {"start": 0, "end": 5, "clip_id": "test"}
            values = {"OUTPUT_DIR": root, "TEMP_DIR": root, "UPLOAD_LOG": log,
                      "DRY_RUN": False, "AUTO_UPLOAD": True, "PRIVACY_STATUS": "private",
                      "SELECTED_FILES": set(), "UPLOAD_YOUTUBE_CAPTIONS": False}
            for name, value in values.items():
                stack.enter_context(patch.object(youtube, name, value))
            stubs = {
                "check_cache_isolation": None,
                "load_all_clip_data": ({}, (None, [], [])),
                "load_approved_items": {}, "get_youtube_service": object(),
                "get_channel_uploads": [],
                "resolve_clip_binding": (clip, [{"text": "test"}], None),
                "generate_metadata": {"title": "Test", "description": "Test", "tags": ["shorts"], "tag_character_count": 6, "privacyStatus": "private"},
                "validate_before_upload": (True, []), "upload_video": {"id": "remote-video"},
            }
            for name, value in stubs.items():
                stack.enter_context(patch.object(youtube, name, return_value=value))
            stack.enter_context(patch.object(youtube, "reconcile_upload_log", side_effect=lambda service, history: (history, [])))
            stack.enter_context(patch.object(youtube, "verify_video", side_effect=RuntimeError("network down")))
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "network down"):
                youtube.main()
            self.assertEqual(json.loads(log.read_text())[str(video.resolve())]["video_id"], "remote-video")
            with redirect_stdout(io.StringIO()):
                youtube.main()
            self.assertEqual(youtube.upload_video.call_count, 1)
            with upload_log_lock(log):
                pass  # An exception also releases the account lock.


if __name__ == "__main__":
    unittest.main()
