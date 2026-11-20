import os
import tempfile
import unittest
from pathlib import Path


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
        self.assertEqual(health.json()["registered_accounts"], 2)
        candidates = self.client.get("/api/candidates", headers=headers)
        self.assertEqual(candidates.status_code, 200)
        self.assertEqual(candidates.json()["total"], 2)

    def test_proxy_is_required(self):
        headers = {"Authorization": "Bearer test-token"}
        response = self.client.post(
            "/api/check",
            headers=headers,
            json={"methods": ["momo"], "proxies": {}},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("momo", response.text)

    def test_auth_required(self):
        self.assertEqual(self.client.get("/api/candidates").status_code, 401)

    def test_vendored_payment_core_imports(self):
        from service import _prepare_upstream_config
        from sms_tool.payment.qualification import check_payment_qualification

        _prepare_upstream_config()
        self.assertTrue(callable(check_payment_qualification))


if __name__ == "__main__":
    unittest.main()
