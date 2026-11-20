import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class PaymentDetectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.main_db = Path(cls.temp.name) / "webui.db"
        cls.results_db = Path(cls.temp.name) / "results.db"
        os.environ["NEXUSFLOW_DB_PATH"] = str(cls.main_db)
        os.environ["PAYMENT_RESULTS_DB"] = str(cls.results_db)
        os.environ["PAYMENT_DETECTOR_TOKEN"] = "test-token"
        os.environ["GPT_REGISTER_CONFIG_DIR"] = str(Path(cls.temp.name) / "config")

        import sqlite3

        with sqlite3.connect(cls.main_db) as con:
            con.execute(
                "CREATE TABLE registered("
                "email TEXT PRIMARY KEY, access_token TEXT, session_token TEXT,"
                "cookie_header TEXT, device_id TEXT, extra_json TEXT, created_at REAL)"
            )
            con.execute(
                "INSERT INTO registered VALUES (?,?,?,?,?,?,?)",
                (
                    "ok@example.com",
                    "token",
                    "session",
                    "__Secure-next-auth.session-token=session; oai-did=dev",
                    "dev",
                    "{}",
                    1,
                ),
            )
            con.execute(
                "INSERT INTO registered VALUES (?,?,?,?,?,?,?)",
                ("missing@example.com", "", "", "", "", "{}", 2),
            )

        from fastapi.testclient import TestClient
        from service import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_health_and_candidates(self):
        headers = {"Authorization": "Bearer test-token"}
        health = self.client.get("/api/health", headers=headers)
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["registered_accounts"], 1)
        self.assertEqual(health.json()["total_records"], 2)
        self.assertEqual(health.json()["usable_accounts"], 1)
        candidates = self.client.get("/api/candidates", headers=headers)
        self.assertEqual(candidates.status_code, 200)
        self.assertEqual(candidates.json()["total"], 1)
        self.assertEqual(candidates.json()["items"][0]["email"], "ok@example.com")
        all_candidates = self.client.get(
            "/api/candidates?include_incomplete=true",
            headers=headers,
        )
        self.assertEqual(all_candidates.json()["total"], 2)

    def test_proxy_is_required(self):
        headers = {"Authorization": "Bearer test-token"}
        response = self.client.post(
            "/api/check",
            headers=headers,
            json={"methods": ["momo"], "proxies": {}},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("momo", response.text)

    def test_socks5_proxy_is_normalized_for_remote_dns(self):
        from service import _normalize_proxy

        self.assertEqual(
            _normalize_proxy("socks5://user:pass@127.0.0.1:1080"),
            "socks5h://user:pass@127.0.0.1:1080",
        )

    def test_started_run_is_persisted_immediately(self):
        headers = {"Authorization": "Bearer test-token"}
        with patch("service._run_qualification"):
            started = self.client.post(
                "/api/check",
                headers=headers,
                json={
                    "emails": ["ok@example.com"],
                    "methods": ["momo"],
                    "proxies": {"momo": "socks5://127.0.0.1:1080"},
                },
            )
        self.assertEqual(started.status_code, 200)
        run_id = started.json()["run_id"]
        fetched = self.client.get(f"/api/runs/{run_id}", headers=headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["run"]["status"], "running")

    def test_auth_required(self):
        self.assertEqual(self.client.get("/api/candidates").status_code, 401)

    def test_vendored_payment_core_imports(self):
        from service import _prepare_upstream_config
        from sms_tool.payment.qualification import check_payment_qualification

        _prepare_upstream_config()
        self.assertTrue(callable(check_payment_qualification))


if __name__ == "__main__":
    unittest.main()
