import time
import unittest
from unittest.mock import patch

from webui.auto_loop import AutoLoopController, AutoLoopState


class AutoLoopSchedulerTest(unittest.TestCase):
    def setUp(self):
        self.controller = AutoLoopController()
        self.controller._proxy_pool = ["bad-proxy", "good-proxy"]
        self.controller._options = {"proxy": ""}
        self.controller._resource_retry_threshold = 3

    def _record(self, mapping, key, attempts, failures):
        mapping[key] = {
            "attempts": attempts,
            "successes": attempts - failures,
            "failures": failures,
            "consecutive_failures": failures,
            "last_used_at": 0.0,
            "last_error": "",
        }

    def test_failure_scope_separates_account_and_proxy_errors(self):
        self.assertEqual(
            self.controller._failure_scope(
                "network",
                "authorize/continue HTTP 409 invalid_state",
            ),
            "account",
        )
        self.assertEqual(
            self.controller._failure_scope(
                "network",
                "SOCKS5 proxy connect timeout",
            ),
            "proxy",
        )
        self.assertEqual(
            self.controller._failure_scope("account", "wrong code"),
            "account",
        )

    def test_failed_proxy_is_moved_to_the_end(self):
        self._record(self.controller._proxy_stats, "bad-proxy", 3, 3)
        selected = self.controller._acquire_proxy(0)
        self.controller._release_proxy(selected)
        self.assertEqual(selected, "good-proxy")

    def test_failed_account_is_moved_to_the_end(self):
        accounts = [
            {"email": "bad@example.com", "imported_at": 1},
            {"email": "clean@example.com", "imported_at": 2},
        ]
        self._record(self.controller._account_stats, "bad@example.com", 3, 3)
        with (
            patch("webui.auto_loop.db.list_accounts", return_value=accounts),
            patch(
                "webui.auto_loop.db.claim_account",
                side_effect=lambda email, **_: {"email": email},
            ) as claim,
        ):
            selected = self.controller._claim_account("icloud_relay")
        self.assertEqual(selected["email"], "clean@example.com")
        claim.assert_called_once_with(
            "clean@example.com",
            workflow_scope="legacy",
        )

    def test_circuit_break_requires_all_resources_at_one_hundred_percent(self):
        accounts = [{"email": "a@example.com"}]
        self.controller._proxy_pool = ["p1", "p2"]
        for proxy in self.controller._proxy_pool:
            self._record(self.controller._proxy_stats, proxy, 3, 3)
        self._record(self.controller._account_stats, "a@example.com", 2, 2)
        with patch("webui.auto_loop.db.list_accounts", return_value=accounts):
            self.assertFalse(self.controller._resources_fully_failed())
            self._record(self.controller._account_stats, "a@example.com", 3, 3)
            self.assertTrue(self.controller._resources_fully_failed())

    def test_auto_resume_clears_pause_after_cooldown(self):
        self.controller._state = AutoLoopState.PAUSED
        self.controller._pause_event.set()
        self.controller._effective_concurrency = 2
        self.controller._auto_resume_at = time.time() - 1

        self.assertTrue(self.controller._auto_resume_if_due())
        self.assertEqual(self.controller._state, AutoLoopState.RUNNING)
        self.assertFalse(self.controller._pause_event.is_set())
        self.assertIsNone(self.controller._auto_resume_at)

    def test_circuit_break_halves_concurrency_and_schedules_auto_resume(self):
        self.controller._state = AutoLoopState.RUNNING
        self.controller._effective_concurrency = 8
        with patch.object(
            self.controller,
            "_resources_fully_failed",
            return_value=True,
        ), patch.object(self.controller, "_persist_resource_stats"):
            self.controller._record_finish(
                False,
                "network",
                "proxy timeout",
                "a@example.com",
                "bad-proxy",
            )

        self.assertEqual(self.controller._state, AutoLoopState.PAUSED)
        self.assertEqual(self.controller._effective_concurrency, 4)
        self.assertTrue(self.controller._pause_event.is_set())
        self.assertGreater(self.controller._auto_resume_at, time.time())

    def test_account_failure_does_not_penalize_proxy(self):
        self.controller._state = AutoLoopState.RUNNING
        with patch.object(
            self.controller,
            "_resources_fully_failed",
            return_value=False,
        ), patch.object(self.controller, "_persist_resource_stats"):
            self.controller._record_finish(
                False,
                "network",
                "authorize/continue HTTP 409 invalid_state",
                "a@example.com",
                "good-proxy",
            )

        account = self.controller._account_stats["a@example.com"]
        proxy = self.controller._proxy_stats["good-proxy"]
        self.assertEqual(account["failures"], 1)
        self.assertEqual(proxy["failures"], 0)


if __name__ == "__main__":
    unittest.main()
