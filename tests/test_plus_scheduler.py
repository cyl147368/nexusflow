import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import db
from webui.plus_scheduler import (
    PlusScheduler,
    get_schedule_config,
    save_schedule_config,
)


class PlusSchedulerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["NEXUSFLOW_DB_PATH"] = str(Path(self.temp.name) / "test.db")
        db.DB_PATH = Path(self.temp.name) / "test.db"
        db.init_db()
        db.set_setting("plus_schedule_enabled", "1")

    def tearDown(self):
        self.temp.cleanup()
        os.environ.pop("NEXUSFLOW_DB_PATH", None)

    def test_default_schedule_is_three_hours_for_integration(self):
        config = get_schedule_config()
        self.assertTrue(config["enabled"])
        self.assertEqual(config["interval_hours"], 3.0)
        self.assertEqual(config["scope"], "integration")
        self.assertEqual(config["egress_mode"], "direct")
        self.assertEqual(config["workers"], 3)

    def test_schedule_settings_are_persisted(self):
        updated = save_schedule_config(
            {
                "enabled": False,
                "interval_hours": 4.5,
                "scope": "all",
                "egress_mode": "proxy",
                "batch_size": 40,
                "workers": 2,
            }
        )
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["interval_hours"], 4.5)
        self.assertEqual(updated["scope"], "all")
        self.assertEqual(updated["egress_mode"], "proxy")
        self.assertEqual(get_schedule_config(), updated)

    def test_cycle_uses_proxy_and_records_counts(self):
        save_schedule_config({"egress_mode": "proxy"})
        db.replace_proxy_pool(["socks5://proxy.example:1080"])
        db.save_registered(
            {
                "email": "one@example.com",
                "access_token": "at-one",
                "session_token": "st-one",
                "workflow_scope": "integration",
            }
        )
        db.save_registered(
            {
                "email": "two@example.com",
                "access_token": "at-two",
                "session_token": "st-two",
                "workflow_scope": "integration",
            }
        )
        calls = []

        def runner(emails, proxy, workers):
            calls.append((emails, proxy, workers))
            return {
                "results": {
                    email: {"status": "plus_eligible", "label": "可领Plus试用"}
                    for email in emails
                }
            }

        scheduler = PlusScheduler()
        scheduler._runner = runner
        scheduler._proxy_selector = lambda: "socks5://proxy.example:1080"
        result = scheduler._run_cycle()

        self.assertTrue(result["ok"])
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["plus"], 2)
        self.assertEqual(calls[0][1], "socks5://proxy.example:1080")
        self.assertEqual(calls[0][2], 3)

    def test_failed_proxy_is_not_reused_in_the_same_cycle(self):
        db.replace_proxy_pool([
            "socks5://bad.example:1080",
            "socks5://good.example:1080",
        ])
        for index in range(3):
            db.save_registered(
                {
                    "email": f"user{index}@example.com",
                    "access_token": f"at-{index}",
                    "session_token": f"st-{index}",
                    "workflow_scope": "integration",
                }
            )
        save_schedule_config({
            "egress_mode": "proxy",
            "batch_size": 1,
            "workers": 1,
        })
        calls = []

        def runner(emails, proxy, workers):
            calls.append(proxy)
            if "bad" in proxy:
                return {
                    "results": {
                        email: {"status": "error", "label": "网络失败"}
                        for email in emails
                    }
                }
            return {
                "results": {
                    email: {"status": "plus_eligible", "label": "可领Plus试用"}
                    for email in emails
                }
            }

        scheduler = PlusScheduler()
        scheduler._runner = runner
        proxies = iter([
            "socks5://bad.example:1080",
            "socks5://good.example:1080",
        ])
        scheduler._proxy_selector = lambda: next(proxies)
        with patch("webui.plus_scheduler.time.sleep", return_value=None):
            result = scheduler._run_cycle()

        self.assertTrue(result["ok"])
        self.assertEqual(calls, [
            "socks5://bad.example:1080",
            "socks5://good.example:1080",
            "socks5://good.example:1080",
        ])
        self.assertGreater(
            db.get_proxy_health()["socks5://bad.example:1080"]["cooldown_until"],
            0,
        )

    def test_cycle_skips_multiple_rejected_proxies_before_succeeding(self):
        for index in range(5):
            db.save_registered(
                {
                    "email": f"retry{index}@example.com",
                    "access_token": f"at-retry-{index}",
                    "session_token": f"st-retry-{index}",
                    "workflow_scope": "integration",
                }
            )
        save_schedule_config({
            "egress_mode": "proxy",
            "batch_size": 1,
            "workers": 1,
        })
        calls = []

        def runner(emails, proxy, workers):
            calls.append(proxy)
            if "good" not in proxy:
                return {
                    "results": {
                        email: {"status": "error", "label": "网络失败"}
                        for email in emails
                    },
                    "note": "代理认证被拒（SOCKS5 97）",
                }
            return {
                "results": {
                    email: {"status": "plus_eligible", "label": "可领Plus试用"}
                    for email in emails
                }
            }

        scheduler = PlusScheduler()
        scheduler._runner = runner
        proxies = iter([
            "socks5://bad-1.example:1080",
            "socks5://bad-2.example:1080",
            "socks5://good.example:1080",
        ])
        scheduler._proxy_selector = lambda: next(proxies)
        with patch("webui.plus_scheduler.time.sleep", return_value=None):
            result = scheduler._run_cycle()

        self.assertTrue(result["ok"])
        self.assertEqual(result["checked"], 5)
        self.assertEqual(result["plus"], 3)
        self.assertEqual(calls, [
            "socks5://bad-1.example:1080",
            "socks5://bad-2.example:1080",
            "socks5://good.example:1080",
            "socks5://good.example:1080",
            "socks5://good.example:1080",
        ])

    def test_direct_cycle_does_not_use_or_require_proxies(self):
        db.save_registered(
            {
                "email": "direct@example.com",
                "access_token": "at-direct",
                "session_token": "st-direct",
                "workflow_scope": "integration",
            }
        )
        save_schedule_config({
            "egress_mode": "direct",
            "batch_size": 1,
            "workers": 1,
        })
        calls = []

        def runner(emails, proxy, workers):
            calls.append(proxy)
            return {
                "results": {
                    email: {"status": "free", "label": "Free"}
                    for email in emails
                }
            }

        scheduler = PlusScheduler()
        scheduler._runner = runner
        scheduler._proxy_selector = lambda: (_ for _ in ()).throw(
            AssertionError("direct mode must not select a proxy")
        )
        result = scheduler._run_cycle()

        self.assertTrue(result["ok"])
        self.assertEqual(calls, [""])
        self.assertEqual(result["free"], 1)


if __name__ == "__main__":
    unittest.main()
