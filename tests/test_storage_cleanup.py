import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from youtube_clipper.backend.storage import inventory, cleanup
from youtube_clipper.backend import dashboard


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def file(self, name, old=True):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test data")
        if old:
            timestamp = time.time() - 40 * 86400
            os.utime(path, (timestamp, timestamp))
        return path

    def test_only_disposable_files_are_removed(self):
        self.file("output/run/short_01.source.mp4")
        self.file("output/run/short_01.clean.mp4")
        self.file("cache/render/source/run/cropped_1.mp4")
        protected = [self.file(name) for name in [
            "output/run/short_01.mp4", "output/run/short_01.manifest.json",
            "cache/youtube_upload_log.json", "cache/dashboard.db", "cache/transcript.json",
            "cache/render/source/run/captions_1.ass", "cache/youtube_source/source.mp4",
            "input/video.mp4", "output/dry-run-reports/approved.json",
        ]]
        recent = self.file("output/run/short_02.source.mp4", old=False)
        snapshot = inventory(self.root)
        self.assertEqual(len(snapshot["candidates"]), 3)
        result = cleanup(self.root, snapshot["candidates"])
        self.assertEqual(result["freed_bytes"], 27)
        self.assertFalse(result["errors"])
        self.assertTrue(all(path.exists() for path in protected + [recent]))

    def test_changed_snapshot_is_rejected(self):
        path = self.file("output/run/short_01.source.mp4")
        candidates = inventory(self.root)["candidates"]
        path.write_bytes(b"replacement")
        with self.assertRaises(ValueError):
            cleanup(self.root, candidates)
        self.assertTrue(path.exists())

    def test_arbitrary_paths_and_invalid_age_rejected(self):
        for days in [-1, 3651, True, "30"]:
            with self.assertRaises(ValueError):
                inventory(self.root, days)
        for path in ["../outside.mp4", "cache/youtube_upload_log.json"]:
            with self.assertRaises(ValueError):
                cleanup(self.root, [{"path": path}])

    def test_partial_failure_reports_only_reclaimed_bytes(self):
        self.file("output/run/short_01.source.mp4")
        candidates = inventory(self.root)["candidates"]
        with patch.object(Path, "unlink", side_effect=PermissionError("in use")):
            result = cleanup(self.root, candidates)
        self.assertEqual(result["freed_bytes"], 0)
        self.assertEqual(len(result["errors"]), 1)

    def test_configured_external_storage_roots_are_scanned(self):
        data_root = self.root / "mounted-data"
        directories = {name: data_root / name for name in ("output", "cache", "input", "logs")}
        for path in directories.values():
            path.mkdir(parents=True)
        master = directories["output"] / "run" / "short_01.source.mp4"
        master.parent.mkdir(parents=True)
        master.write_bytes(b"editing master")
        timestamp = time.time() - 40 * 86400
        os.utime(master, (timestamp, timestamp))

        snapshot = inventory(self.root, directories=directories)

        self.assertEqual(snapshot["groups"]["output"]["files"], 1)
        self.assertEqual(snapshot["groups"]["output"]["bytes"], 14)
        self.assertEqual(snapshot["candidates"][0]["path"], "output/run/short_01.source.mp4")
        self.assertEqual(Path(snapshot["locations"]["output"]), directories["output"].resolve())
        cleanup(self.root, snapshot["candidates"], directories=directories)
        self.assertFalse(master.exists())

    def test_cleanup_refuses_active_jobs(self):
        handler = object.__new__(dashboard.Handler)
        handler.path = "/api/storage/cleanup"
        body = json.dumps({"files": [], "days": 30}).encode()
        handler.headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchone.return_value = (1,)
        with patch.object(dashboard, "db", return_value=connection), patch.object(dashboard, "cleanup_storage") as remove, patch.object(dashboard, "json_response") as response:
            handler.do_POST()
            self.assertEqual(response.call_args.args[2], 409)
            remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
