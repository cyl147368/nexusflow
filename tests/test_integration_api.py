import os
import tempfile
import unittest
from pathlib import Path


class IntegrationApiTest(unittest.TestCase):
    def setUp(self):
        # SQLite WAL may briefly retain a Windows file handle after connection
        # close. The test still validates API behavior; cleanup tolerates that.
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["NEXUSFLOW_DB_PATH"] = str(Path(self.temp.name) / "test.db")
        os.environ["NEXUSFLOW_LOG_DIR"] = str(Path(self.temp.name) / "logs")
        os.environ.pop("NEXUSFLOW_BASIC_PASSWORD", None)
        os.environ["MAILHUB_ADMIN_TOKEN"] = "mail-token"
        os.environ["ICLOUD_PRIME_API_TOKEN"] = "icloud-token"

        from fastapi.testclient import TestClient
        from webui.app import app

        self.client = TestClient(app)

    def tearDown(self):
        self.temp.cleanup()

    def test_config_secrets_are_masked(self):
        response = self.client.get("/api/integration/config")
        self.assertEqual(response.status_code, 200)
        config = response.json()["config"]
        self.assertEqual(config["mailhub_token"], "***")
        self.assertEqual(config["icloud_token"], "***")

    def test_config_can_be_saved_without_overwriting_secrets(self):
        response = self.client.post(
            "/api/integration/config",
            json={
                "mailhub_url": "http://mail-hub:8090",
                "mailhub_token": "",
                "icloud_url": "http://icloud-prime:8081",
                "icloud_account_id": "acc_test",
                "otp_timeout": 180,
            },
        )
        self.assertEqual(response.status_code, 200)
        config = response.json()["config"]
        self.assertEqual(config["icloud_account_id"], "acc_test")
        self.assertEqual(config["mailhub_token"], "***")

    def test_integration_page_is_served(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("NexusFlow", response.text)

    def test_cookie_normalize_accepts_curl(self):
        response = self.client.post(
            "/api/integration/cookies/normalize",
            json={
                "text": (
                    "curl 'https://www.icloud.com/' "
                    "-H 'Cookie: X-APPLE-WEBAUTH-TOKEN=abc; "
                    "X-APPLE-WEBAUTH-USER=v%3D1%3Ad%3D42' "
                    "-H 'User-Agent: browser'"
                )
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertIn("X-APPLE-WEBAUTH-TOKEN=abc", body["text"])
        self.assertEqual(body["cookie_names"], ["X-APPLE-WEBAUTH-TOKEN", "X-APPLE-WEBAUTH-USER"])

    def test_cookie_normalize_accepts_json(self):
        response = self.client.post(
            "/api/integration/cookies/normalize",
            json={"text": '{"a":"1","b":"2=3"}'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["text"], "a=1; b=2=3")


if __name__ == "__main__":
    unittest.main()
