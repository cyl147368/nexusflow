import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import updater


class UpdaterTest(unittest.TestCase):
    def test_check_reports_commits(self):
        def fake_run(args, timeout=120):
            if args[1:3] == ["fetch", "origin"]:
                return {"ok": True, "stdout": "", "stderr": ""}
            if args[-1] == "HEAD":
                return {"ok": True, "stdout": "a" * 40, "stderr": ""}
            if args[-1] == "origin/main":
                return {"ok": True, "stdout": "b" * 40, "stderr": ""}
            return {"ok": True, "stdout": "README.md\n", "stderr": ""}

        with patch.object(updater, "_run", side_effect=fake_run):
            result = updater.check_update()
        self.assertTrue(result["ok"])
        self.assertTrue(result["behind"])
        self.assertEqual(result["changed_files"], ["README.md"])

    def test_state_file_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(updater, "STATE_FILE", str(Path(temp) / "state.json")):
                updater._save_state(status="running", stage="build")
                state = updater._state()
            self.assertEqual(state["status"], "running")
            self.assertEqual(state["stage"], "build")


if __name__ == "__main__":
    unittest.main()
