import os
import tempfile
import unittest
from pathlib import Path

from webui import db
from webui.team_scheduler import (
    TeamScheduler,
    get_schedule_config,
    save_schedule_config,
)


class TeamSchedulerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["NEXUSFLOW_DB_PATH"] = str(Path(self.temp.name) / "test.db")
        db.DB_PATH = Path(self.temp.name) / "test.db"
        db.init_db()
        db.set_setting("team_schedule_enabled", "1")

    def tearDown(self):
        self.temp.cleanup()
        os.environ.pop("NEXUSFLOW_DB_PATH", None)

    def test_default_schedule_is_three_hours_for_all_workflows(self):
        config = get_schedule_config()
        self.assertTrue(config["enabled"])
        self.assertEqual(config["interval_hours"], 3.0)
        self.assertEqual(config["scope"], "all")
        self.assertEqual(config["workers"], 3)

    def test_schedule_settings_are_persisted(self):
        updated = save_schedule_config({
            "enabled": False,
            "interval_hours": 4.5,
            "scope": "all",
            "batch_size": 40,
            "workers": 2,
        })
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["interval_hours"], 4.5)
        self.assertEqual(updated["scope"], "all")
        self.assertEqual(updated["batch_size"], 40)
        self.assertEqual(get_schedule_config(), updated)

    def test_cycle_checks_every_account_and_records_counts(self):
        save_schedule_config({"scope": "integration", "batch_size": 2})
        for index in range(3):
            db.save_registered({
                "email": f"team{index}@example.com",
                "access_token": f"at-{index}",
                "workflow_scope": "integration",
                "team_promo_check": {
                    "status": "team_ineligible",
                    "label": "old",
                },
            })
        calls = []

        def runner(emails, workers):
            calls.append((emails, workers))
            return {
                "results": {
                    email: {
                        "status": (
                            "team_eligible"
                            if email == "team0@example.com"
                            else "team_ineligible"
                        ),
                        "label": "checked",
                    }
                    for email in emails
                }
            }

        scheduler = TeamScheduler()
        scheduler._runner = runner
        result = scheduler._run_cycle()

        self.assertTrue(result["ok"])
        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["ineligible"], 2)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(len(call[0]) <= 2 for call in calls))

    def test_empty_scope_completes_without_runner_calls(self):
        save_schedule_config({"scope": "legacy"})
        scheduler = TeamScheduler()
        scheduler._runner = lambda *_args: self.fail("runner should not run")
        result = scheduler._run_cycle()
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 0)


if __name__ == "__main__":
    unittest.main()
