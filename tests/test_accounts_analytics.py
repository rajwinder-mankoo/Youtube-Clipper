import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from youtube_clipper.publishing import account_connections
from youtube_clipper.publishing.analytics import performance_note


class AccountAnalyticsTests(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("google_auth_oauthlib")
        and importlib.util.find_spec("googleapiclient"),
        "Google OAuth dependencies are not installed",
    )
    def test_oauth_runtime_dependencies_import(self):
        from google_auth_oauthlib.flow import Flow
        from googleapiclient.discovery import build

        self.assertTrue(callable(Flow.from_client_secrets_file))
        self.assertTrue(callable(build))

    def test_connection_status_does_not_expose_token_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "token.json"
            token.write_text(json.dumps({
                "refresh_token": "secret",
                "scopes": list(account_connections.ANALYTICS_SCOPES),
            }), encoding="utf-8")
            account = {"id": "main", "name": "Main", "platform": "youtube", "token_file": "token.json"}
            with patch.object(account_connections.config, "PROJECT_ROOT", root):
                result = account_connections.connection_status(account)
            self.assertTrue(result["connected"])
            self.assertTrue(result["analytics_ready"])
            self.assertNotIn("token_file", result)

    def test_remote_oauth_requires_https(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            account_connections.validate_dashboard_origin("http://clipper.example")
        self.assertEqual(
            account_connections.validate_dashboard_origin("http://127.0.0.1:8765"),
            "http://127.0.0.1:8765",
        )

    def test_local_oauth_transport_is_temporary_and_loopback_only(self):
        with patch.dict(os.environ, {}, clear=True):
            with account_connections.oauth_transport(
                "http://127.0.0.1:8765/oauth/youtube/callback"
            ):
                self.assertEqual(os.environ.get("OAUTHLIB_INSECURE_TRANSPORT"), "1")
            self.assertNotIn("OAUTHLIB_INSECURE_TRANSPORT", os.environ)

            with account_connections.oauth_transport(
                "https://youtube-clipper.example.ts.net/oauth/youtube/callback"
            ):
                self.assertNotIn("OAUTHLIB_INSECURE_TRANSPORT", os.environ)

    def test_pkce_verifier_is_reused_for_callback(self):
        class FakeFlow:
            calls = []

            @classmethod
            def from_client_secrets_file(cls, path, **kwargs):
                cls.calls.append((path, kwargs))
                return object()

        account_connections.youtube_oauth_flow(FakeFlow)
        account_connections.youtube_oauth_flow(
            FakeFlow, state="expected-state", code_verifier="expected-verifier"
        )
        start = FakeFlow.calls[0][1]
        callback = FakeFlow.calls[1][1]
        self.assertTrue(start["autogenerate_code_verifier"])
        self.assertNotIn("code_verifier", start)
        self.assertEqual(callback["state"], "expected-state")
        self.assertEqual(callback["code_verifier"], "expected-verifier")
        self.assertFalse(callback["autogenerate_code_verifier"])

    def test_missing_oauth_dependency_message_uses_project_setup(self):
        message = account_connections.oauth_dependency_help()
        self.assertIn("scripts/bootstrap.py", message)
        self.assertIn(".venv", message)
        self.assertIn("dashboard.py", message)

    def test_feedback_uses_retention_and_sample_size(self):
        self.assertIn("Early data", performance_note({"views": 4, "averageViewPercentage": 90}))
        self.assertIn("Strong retention", performance_note({"views": 100, "averageViewPercentage": 85}))
        self.assertIn("tighten", performance_note({"views": 100, "averageViewPercentage": 30}))


if __name__ == "__main__":
    unittest.main()
