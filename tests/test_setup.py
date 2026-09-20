import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import doctor


class SetupTests(unittest.TestCase):
    def test_json_validation_reports_missing_and_invalid_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(doctor.read_json(root / "missing.json")[1], "file is missing")
            invalid = root / "invalid.json"
            invalid.write_text("[]", encoding="utf-8")
            self.assertIn("JSON object", doctor.read_json(invalid)[1])
            valid = root / "valid.json"
            valid.write_text(json.dumps({"ready": True}), encoding="utf-8")
            self.assertEqual(doctor.read_json(valid), ({"ready": True}, None))

    def test_account_schema_requires_a_list_of_destinations(self):
        valid = {"accounts": [{"id": "youtube_main", "platform": "youtube"}]}
        invalid = {"accounts": None}
        self.assertEqual(doctor.account_destinations(valid)[0], valid["accounts"])
        self.assertIn("accounts list", doctor.account_destinations(invalid)[1])

    def test_missing_packages_use_install_names(self):
        with patch.object(doctor.importlib.util, "find_spec", return_value=None):
            missing = doctor.missing_python_packages()
        self.assertIn("google-auth-oauthlib", missing)
        self.assertIn("opencv-python", missing)


if __name__ == "__main__":
    unittest.main()
