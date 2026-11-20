import os
import tempfile
import unittest
from pathlib import Path


class MailHubApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["MAILHUB_DB"] = str(Path(cls.temp.name) / "mailhub.db")
        os.environ["MAILHUB_ADMIN_TOKEN"] = "test-token"
        os.environ["MAILHUB_PUBLIC_URL"] = "http://example.test:8090"
        os.environ["MAILHUB_SMTP_PORT"] = "0"

        from fastapi.testclient import TestClient
        from service import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_mailbox_creation_and_relay_page(self):
        headers = {"Authorization": "Bearer test-token"}
        response = self.client.post(
            "/api/mailboxes",
            headers=headers,
            json={
                "email": "relay@icloud.com",
                "source": "icloud-prime",
                "icloud_url": "http://icloud-prime:8081",
                "icloud_token": "token",
                "icloud_account_id": "acc_1",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["email"], "relay@icloud.com")
        self.assertTrue(body["relay_url"].startswith("http://example.test:8090/mail/"))

        page = self.client.get(f"/mail/{body['token']}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("NexusFlow 邮件中转", page.text)
        self.assertIn("relay@icloud.com", page.text)

    def test_admin_endpoint_requires_token(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 401)

    def test_health_exposes_transport_configuration(self):
        response = self.client.get(
            "/api/health",
            headers={"Authorization": "Bearer test-token"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["smtp_port"], 0)
        self.assertFalse(body["smtp_enabled"])
        self.assertEqual(body["default_source"], "icloud-prime")


if __name__ == "__main__":
    unittest.main()
