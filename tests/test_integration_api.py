import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
        from webui import db
        from webui.app import app
        from webui import integration

        db.DB_PATH = Path(self.temp.name) / "test.db"
        db.init_db()
        integration._job = None
        integration._stop_event.clear()
        integration._pause_event.clear()
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

    def test_continuous_config_preserves_zero_values(self):
        response = self.client.post(
            "/api/integration/config",
            json={
                "continuous": True,
                "target_count": 0,
                "cool_down_seconds": 0,
                "auto_qualify": False,
                "payment_timeout": 30,
            },
        )
        self.assertEqual(response.status_code, 200)
        config = response.json()["config"]
        self.assertEqual(config["target_count"], 0)
        self.assertEqual(config["cool_down_seconds"], 0)
        self.assertFalse(config["auto_qualify"])
        self.assertEqual(config["payment_timeout"], 30)

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

    def test_imap_password_is_masked_and_preserved(self):
        saved = self.client.post(
            "/api/integration/config",
            json={
                "receiver_mode": "imap",
                "imap_host": "imap.example.com",
                "imap_port": 993,
                "imap_email": "receiver@example.com",
                "imap_password": "authorization-code",
                "imap_folder": "INBOX",
                "imap_ssl": True,
            },
        )
        self.assertEqual(saved.status_code, 200)
        config = self.client.get("/api/integration/config").json()["config"]
        self.assertEqual(config["receiver_mode"], "imap")
        self.assertEqual(config["imap_password"], "***")
        self.assertTrue(config["imap_ssl"])

    def test_proxy_pool_is_persisted_in_database(self):
        replace = self.client.put(
            "/api/proxy/pool",
            json={"items": ["socks5://one:pass@127.0.0.1:1080", "http://two:8080"]},
        )
        self.assertEqual(replace.status_code, 200)
        self.assertEqual(replace.json()["items"][0], "socks5://one:pass@127.0.0.1:1080")
        listed = self.client.get("/api/proxy/pool")
        self.assertEqual(len(listed.json()["items"]), 2)
        deleted = self.client.post(
            "/api/proxy/pool/delete",
            json={"proxy": "http://two:8080"},
        )
        self.assertEqual(deleted.json()["items"], ["socks5://one:pass@127.0.0.1:1080"])

    def test_integration_job_can_be_loaded_after_restart(self):
        from webui import db

        db.save_integration_job({
            "id": "persisted-job",
            "status": "running",
            "stage": "waiting_registration",
            "email": "persisted@example.com",
            "relay_url": "http://127.0.0.1:8090/mail/token",
            "run_id": "old-run",
            "logs": [],
            "updated_at": 123,
        })
        job = db.get_latest_integration_job()
        self.assertEqual(job["id"], "persisted-job")
        self.assertEqual(job["email"], "persisted@example.com")

    def test_new_run_does_not_reuse_previous_cycle_identity(self):
        from webui import db, integration

        db.save_integration_config({
            "mailhub_url": "http://mail-hub:8090",
            "icloud_url": "http://icloud-prime:8081",
            "icloud_account_id": "acc_test",
            "continuous": True,
            "target_count": 0,
        })
        integration._set_job(
            id="old-job",
            status="failed",
            email="old@example.com",
            alias={"email": "old@example.com"},
            relay_url="http://mail-hub/mail/old",
            run_id="old-run",
        )
        with patch.object(integration, "_start_worker") as start_worker:
            job = integration.start_pipeline()

        self.assertNotEqual(job["id"], "old-job")
        self.assertEqual(job["email"], "")
        self.assertEqual(job["relay_url"], "")
        self.assertEqual(job["run_id"], "")
        self.assertTrue(job["continuous"])
        start_worker.assert_called_once()

    def test_continuous_loop_stops_at_success_target(self):
        from webui import db, integration

        db.save_integration_config({
            "mailhub_url": "http://mail-hub:8090",
            "icloud_url": "http://icloud-prime:8081",
            "icloud_account_id": "acc_test",
            "continuous": True,
            "target_count": 2,
            "cool_down_seconds": 0,
        })
        integration._job = None
        integration._set_job(
            id="loop-job",
            status="running",
            continuous=True,
            target_count=2,
            success_count=0,
            failure_count=0,
            consecutive_failures=0,
            cycle_no=0,
            logs=[],
        )
        results = [
            {"ok": True, "email": "one@example.com"},
            {"ok": True, "email": "two@example.com"},
        ]
        with (
            patch.object(integration, "_run_cycle", side_effect=results),
            patch.object(integration, "_start_qualification") as qualify,
        ):
            integration._run_pipeline("loop-job")

        job = integration.get_job()
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["stage"], "target_reached")
        self.assertEqual(job["success_count"], 2)
        self.assertEqual(qualify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
