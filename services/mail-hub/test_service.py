import os
import tempfile
import unittest
import email
import email.policy
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

    def test_forwarded_alias_can_be_found_outside_to_header(self):
        from service import _message_recipients

        message = email.message_from_string(
            "From: sender@example.com\n"
            "To: receiver@qq.com\n"
            "Delivered-To: hidden.alias@icloud.com\n"
            "Subject: verification\n\n"
            "Your code is 654321\n",
            policy=email.policy.default,
        )
        self.assertIn("hidden.alias@icloud.com", _message_recipients(message))

    def test_junk_folder_is_included_in_imap_scan(self):
        from service import _imap_scan_folders

        class FakeImap:
            def list(self):
                return "OK", [
                    b'(\\HasNoChildren) "/" "INBOX"',
                    b'(\\HasNoChildren \\Junk) "/" "&V4NXPpCuTvY-"',
                    b'(\\HasNoChildren) "/" "Sent"',
                ]

        self.assertEqual(
            _imap_scan_folders(FakeImap(), "INBOX"),
            ["INBOX", "&V4NXPpCuTvY-"],
        )


if __name__ == "__main__":
    unittest.main()
