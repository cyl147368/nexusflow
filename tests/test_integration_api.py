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

    def test_registration_relay_uses_internal_service_url(self):
        from webui import integration

        relay = integration._internal_relay_url(
            "http://mail-hub:8090",
            {"token": "token/with spaces"},
        )
        self.assertEqual(
            relay,
            "http://mail-hub:8090/mail/token%2Fwith%20spaces",
        )

    def test_restart_while_cooling_down_starts_a_fresh_cycle(self):
        from webui import db, integration

        db.save_integration_config({
            "mailhub_url": "http://mail-hub:8090",
            "icloud_url": "http://icloud-prime:8081",
            "icloud_account_id": "acc_test",
            "continuous": False,
        })
        integration._job = None
        integration._set_job(
            id="resume-job",
            status="running",
            stage="resuming",
            _resume=True,
            _resume_stage="cooling_down",
            continuous=False,
            cycle_no=1,
            email="previous@example.com",
            success_count=0,
            failure_count=1,
            consecutive_failures=1,
            logs=[],
        )
        with (
            patch.object(
                integration,
                "_run_cycle",
                return_value={"ok": True, "email": "fresh@example.com"},
            ) as run_cycle,
            patch.object(integration, "_start_qualification"),
        ):
            integration._run_pipeline("resume-job")

        self.assertFalse(run_cycle.call_args.kwargs["resuming"])
        self.assertEqual(integration.get_job()["status"], "done")

    def test_icloud_address_limit_is_classified_as_quota_wait(self):
        from webui import integration

        self.assertTrue(integration._is_quota_wait_error(
            "HTTP 502: You have reached the limit of addresses you can create"
        ))
        self.assertTrue(integration._is_quota_wait_error("当前小时创建额度已用完"))
        self.assertTrue(integration._is_quota_wait_error(
            "Apple 暂时限制隐藏邮箱创建额度，15 秒后自动重试"
        ))
        self.assertFalse(integration._is_quota_wait_error("warmup failed"))

    def test_workflow_pools_and_results_are_isolated(self):
        from webui import db

        db.import_accounts(
            "legacy@example.com----http://mail-hub/mail/legacy",
            kind="icloud_relay",
            workflow_scope="legacy",
        )
        db.import_accounts(
            "integrated@example.com----http://mail-hub/mail/integrated",
            kind="icloud_relay",
            workflow_scope="integration",
        )

        self.assertEqual(db.stats("legacy")["total"], 1)
        self.assertEqual(db.stats("integration")["total"], 1)
        self.assertEqual(
            db.claim_next(kind="icloud_relay", workflow_scope="legacy")["email"],
            "legacy@example.com",
        )
        self.assertIsNone(
            db.claim_account("integrated@example.com", workflow_scope="legacy")
        )
        self.assertEqual(
            db.claim_account(
                "integrated@example.com",
                workflow_scope="integration",
            )["email"],
            "integrated@example.com",
        )

        db.save_registered({
            "email": "legacy@example.com",
            "password": "legacy-pass",
            "access_token": "legacy-at",
            "workflow_scope": "legacy",
        })
        db.save_registered({
            "email": "integrated@example.com",
            "password": "integrated-pass",
            "access_token": "integrated-at",
            "workflow_scope": "integration",
        })
        self.assertEqual(db.count_registered(workflow_scope="legacy"), 1)
        self.assertEqual(db.count_registered(workflow_scope="integration"), 1)
        self.assertEqual(
            db.list_registered(workflow_scope="integration")[0]["email"],
            "integrated@example.com",
        )

    def test_stats_api_returns_independent_workflow_counts(self):
        from webui import db

        db.import_accounts(
            "legacy-api@example.com----http://mail-hub/mail/legacy",
            kind="icloud_relay",
            workflow_scope="legacy",
        )
        db.import_accounts(
            "integration-api@example.com----http://mail-hub/mail/integration",
            kind="icloud_relay",
            workflow_scope="integration",
        )

        legacy = self.client.get("/api/stats?workflow_scope=legacy").json()
        integration = self.client.get(
            "/api/stats?workflow_scope=integration"
        ).json()
        self.assertEqual(legacy["stats"]["total"], 1)
        self.assertEqual(integration["stats"]["total"], 1)
        self.assertEqual(legacy["by_workflow"]["legacy"]["total"], 1)
        self.assertEqual(
            legacy["by_workflow"]["integration"]["total"],
            1,
        )

    def test_unchecked_emails_are_returned_across_pages(self):
        from webui import db

        for index in range(25):
            db.save_registered({
                "email": f"unchecked-{index}@example.com",
                "workflow_scope": "integration",
            })
        db.save_registered({
            "email": "checked@example.com",
            "workflow_scope": "integration",
            "plus_check": {"status": "free", "label": "Free"},
        })
        db.save_registered({
            "email": "legacy-only@example.com",
            "workflow_scope": "legacy",
        })

        response = self.client.get(
            "/api/registered/unchecked_emails",
            params={"workflow_scope": "integration"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 25)
        self.assertEqual(len(body["emails"]), 25)
        self.assertIn("unchecked-24@example.com", body["emails"])
        self.assertNotIn("checked@example.com", body["emails"])
        self.assertNotIn("legacy-only@example.com", body["emails"])

    def test_retest_emails_exclude_existing_plus_statuses(self):
        from webui import db

        for email, status in (
            ("unchecked@example.com", None),
            ("free@example.com", "free"),
            ("banned@example.com", "banned"),
            ("invalid@example.com", "token_invalid"),
            ("eligible@example.com", "plus_eligible"),
            ("active@example.com", "plus_active"),
        ):
            row = {"email": email, "workflow_scope": "integration"}
            if status:
                row["plus_check"] = {"status": status, "label": status}
            db.save_registered(row)

        response = self.client.get(
            "/api/registered/retest_emails",
            params={"workflow_scope": "integration"},
        )

        self.assertEqual(response.status_code, 200)
        emails = set(response.json()["emails"])
        self.assertEqual(
            emails,
            {
                "unchecked@example.com",
                "free@example.com",
                "banned@example.com",
                "invalid@example.com",
            },
        )

    def test_refresh_access_tokens_endpoint(self):
        def refresh(email, proxy):
            return {
                "ok": email != "bad@example.com",
                "email": email,
                "status": "success" if email != "bad@example.com" else "failed",
                "at_len": 10 if email != "bad@example.com" else 0,
            }

        with patch(
            "webui.token_refresh.refresh_registered_access_token",
            side_effect=refresh,
        ):
            response = self.client.post(
                "/api/registered/refresh_at",
                json={
                    "emails": [
                        "good@example.com",
                        "bad@example.com",
                        "good@example.com",
                    ],
                    "proxy": "",
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["success"], 1)
        self.assertEqual(body["failed"], 1)
        self.assertIn("good@example.com", body["results"])
        self.assertIn("bad@example.com", body["results"])

    def test_backfill_marks_persisted_integration_aliases(self):
        from webui import db

        db.import_accounts(
            "backfill@example.com----http://mail-hub/mail/backfill",
            kind="icloud_relay",
            workflow_scope="legacy",
        )
        db.save_integration_job({
            "id": "backfill-job",
            "status": "running",
            "mailbox": {"email": "backfill@example.com"},
            "logs": [],
        })
        con = db._conn()
        db._backfill_integration_scope(con)
        con.close()
        row = db.get_account("backfill@example.com")
        self.assertEqual(row["workflow_scope"], "integration")

    def test_recovery_start_accepts_manual_relay_url(self):
        with patch("webui.registrar.start_recovery", return_value="recovery-run") as start:
            response = self.client.post(
                "/api/recovery/start",
                json={
                    "email": "recover@example.com",
                    "relay_url": "https://mail.example/recover",
                    "want_2fa": True,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run_id"], "recovery-run")
        account = start.call_args.args[0]
        self.assertEqual(account["relay_url"], "https://mail.example/recover")
        self.assertEqual(account["workflow_scope"], "legacy")

    def test_recovery_batch_deduplicates_and_accepts_manual_urls(self):
        with patch(
            "webui.registrar.start_recovery_batch",
            return_value="batch-run",
        ) as start:
            response = self.client.post(
                "/api/recovery/batch/start",
                json={
                    "items": [
                        {
                            "email": "one@example.com",
                            "relay_url": "https://mail.example/one",
                        },
                        {
                            "email": "one@example.com",
                            "relay_url": "https://mail.example/duplicate",
                        },
                        {
                            "email": "two@example.com",
                            "relay_url": "https://mail.example/two",
                        },
                    ],
                    "want_2fa": True,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch_id"], "batch-run")
        items = start.call_args.args[0]
        self.assertEqual(
            items,
            [
                {
                    "email": "one@example.com",
                    "relay_url": "https://mail.example/one",
                },
                {
                    "email": "two@example.com",
                    "relay_url": "https://mail.example/two",
                },
            ],
        )

    def test_recovery_scan_icloud_starts_background_preparation(self):
        with patch(
            "webui.integration.start_icloud_recovery_scan",
            return_value={"id": "scan-batch", "status": "preparing"},
        ) as start:
            response = self.client.post(
                "/api/recovery/scan_icloud",
                json={"want_2fa": True, "otp_timeout": 180},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batch_id"], "scan-batch")
        self.assertEqual(response.json()["status"], "preparing")
        self.assertTrue(start.call_args.args[0]["want_2fa"])

    def test_icloud_recovery_prioritizes_known_accounts(self):
        from webui import integration

        aliases = [
            {"email": "unknown@example.com"},
            {"email": "known@example.com"},
        ]
        pool = {
            "unknown@example.com": {
                "email": "unknown@example.com",
                "relay_url": "http://mail-hub/mail/unknown",
            },
            "known@example.com": {
                "email": "known@example.com",
                "relay_url": "http://mail-hub/mail/known",
            },
        }
        with (
            patch.object(
                integration,
                "list_icloud_aliases",
                return_value={"ok": True, "aliases": aliases},
            ),
            patch.object(
                integration.db,
                "get_integration_internal_config",
                return_value={"icloud_account_id": "account"},
            ),
            patch.object(
                integration.db,
                "get_registered",
                side_effect=lambda email: (
                    {"email": email, "password": "saved-password"}
                    if email == "known@example.com"
                    else {}
                ),
            ),
            patch.object(
                integration.db,
                "get_account",
                side_effect=lambda email: pool.get(email),
            ),
        ):
            result = integration.prepare_icloud_recovery_items()

        self.assertEqual(
            [item["email"] for item in result["items"]],
            ["known@example.com", "unknown@example.com"],
        )
        self.assertEqual(result["known_accounts"], 1)
        self.assertEqual(result["unknown_aliases"], 1)

    def test_icloud_recovery_keeps_partial_accounts_until_all_tokens_exist(self):
        from webui import integration

        with (
            patch.object(
                integration,
                "list_icloud_aliases",
                return_value={
                    "ok": True,
                    "aliases": [
                        {"email": "partial@example.com"},
                        {"email": "complete@example.com"},
                    ],
                },
            ),
            patch.object(
                integration.db,
                "get_integration_internal_config",
                return_value={"icloud_account_id": "account"},
            ),
            patch.object(
                integration.db,
                "get_registered",
                side_effect=lambda email: (
                    {
                        "access_token": "at",
                        "session_token": "st",
                        "refresh_token": "rt",
                    }
                    if email == "complete@example.com"
                    else {"access_token": "partial-at"}
                ),
            ),
            patch.object(
                integration.db,
                "get_account",
                side_effect=lambda email: {
                    "email": email,
                    "relay_url": f"http://mail-hub/mail/{email}",
                },
            ),
        ):
            result = integration.prepare_icloud_recovery_items()

        self.assertEqual(
            [item["email"] for item in result["items"]],
            ["partial@example.com"],
        )
        self.assertEqual(result["skipped_complete"], 1)

    def test_recovery_batch_is_persisted(self):
        from webui import db

        db.save_recovery_batch({
            "id": "batch-persisted",
            "status": "running",
            "items": [{"email": "one@example.com"}],
            "results": [],
            "next_index": 0,
        })
        job = db.get_latest_recovery_batch()
        self.assertEqual(job["id"], "batch-persisted")
        self.assertEqual(job["items"][0]["email"], "one@example.com")

    def test_recovery_batch_processes_items_in_order_and_keeps_partial_results(self):
        from webui import account_recovery, db, registrar
        from webui.account_recovery import RecoveryError

        db.import_accounts(
            "one@example.com----http://mail.example/one",
            kind="icloud_relay",
            workflow_scope="legacy",
        )
        db.import_accounts(
            "two@example.com----http://mail.example/two",
            kind="icloud_relay",
            workflow_scope="legacy",
        )
        job = {
            "id": "batch-order",
            "status": "running",
            "items": [
                {"email": "one@example.com"},
                {"email": "two@example.com"},
            ],
            "options": {"want_2fa": False, "workflow_scope": "legacy"},
            "results": [],
            "next_index": 0,
            "log_path": str(Path(self.temp.name) / "batch.log"),
        }
        db.save_recovery_batch(job)
        calls = []

        def recover(account, options):
            calls.append(account["email"])
            if account["email"] == "two@example.com":
                raise RecoveryError(
                    "two failed",
                    payload={"email": account["email"], "error": "two failed"},
                )
            return {"email": account["email"], "credential_source": "recovery"}

        with patch.object(account_recovery, "recover_account", side_effect=recover):
            registrar._do_recovery_batch(
                "batch-order",
                Path(job["log_path"]),
                job,
            )

        persisted = db.get_recovery_batch("batch-order")
        self.assertEqual(calls, ["one@example.com", "two@example.com"])
        self.assertEqual(persisted["next_index"], 2)
        self.assertEqual(persisted["success_count"], 1)
        self.assertEqual(persisted["failure_count"], 1)
        self.assertEqual(persisted["status"], "partial")

    def test_rate_limit_errors_are_retryable(self):
        from webui import registrar

        self.assertTrue(registrar._is_rate_limit_error(
            "authorize/continue HTTP 429 Too many requests"
        ))
        self.assertTrue(registrar._is_rate_limit_error(
            '{"code":"rate_limit_exceeded"}'
        ))
        self.assertFalse(registrar._is_rate_limit_error("username_not_found"))
        self.assertEqual(
            registrar._recovery_retry_kind(
                "authorize/continue failed: HTTP 409 invalid_state"
            ),
            "network",
        )
        self.assertEqual(
            registrar._recovery_retry_kind("username_not_found"),
            "",
        )

    def test_recovery_uses_fresh_flow_after_reset_state_failure(self):
        from webui import account_recovery

        class EmailLoginFailedFlow:
            def run_protocol_login(
                self,
                mail,
                email,
                password="",
                force_email_otp=False,
            ):
                self.force_email_otp = force_email_otp
                raise RuntimeError("HTTP 409 invalid_state")

        class ResetFailedFlow:
            def check_proxy(self):
                return True

            def reset_password_with_email_otp(
                self,
                mail,
                email,
                new_password,
                timeout=180,
            ):
                raise RuntimeError("HTTP 409 invalid_state")

        class LoginFlow:
            def run_protocol_login(
                self,
                mail,
                email,
                password="",
                force_email_otp=False,
            ):
                self.force_email_otp = force_email_otp

                class Result:
                    @staticmethod
                    def to_dict():
                        return {
                            "email": email,
                            "access_token": "fresh-at",
                            "session_token": "fresh-st",
                            "refresh_token": "fresh-rt",
                        }

                return Result()

            def set_or_reset_password(self, email, password, **kwargs):
                return {"ok": True, "mode": "add"}

        first = EmailLoginFailedFlow()
        second = ResetFailedFlow()
        third = LoginFlow()
        with (
            patch.object(
                account_recovery,
                "create_mail_provider",
                return_value=object(),
            ),
            patch.object(
                account_recovery,
                "AuthFlow",
                side_effect=[first, second, third],
            ) as auth_flow,
        ):
            result = account_recovery.recover_account(
                {"email": "fresh-flow@example.com"},
                {
                    "relay_url": "https://mail.example/fresh-flow",
                    "new_password": "FreshPassword1!",
                    "want_2fa": False,
                    "workflow_scope": "integration",
                },
            )

        self.assertEqual(auth_flow.call_count, 3)
        self.assertTrue(first.force_email_otp)
        self.assertTrue(third.force_email_otp)
        self.assertEqual(result["access_token_len"], len("fresh-at"))
        self.assertEqual(result["credential_source"], "recovery")

    def test_recovery_marks_result_source_and_resets_password(self):
        from webui import account_recovery, db

        class FakeFlow:
            def __init__(self):
                self.password = ""
                self.force_email_otp = None
                self.password_login_required = True

            def reset_password_with_email_otp(
                self,
                mail,
                email,
                new_password,
                timeout=180,
            ):
                self.password = new_password
                return {"ok": True, "mode": "reset"}

            def run_protocol_login(self, mail, email, password="", force_email_otp=False):
                self.email = email
                self.force_email_otp = force_email_otp
                self.password = password

                class Result:
                    @staticmethod
                    def to_dict():
                        return {
                            "email": email,
                            "password": "",
                            "access_token": "recovered-at",
                            "session_token": "recovered-st",
                            "refresh_token": "",
                            "cookie_header": "oai-did=test",
                        }

                return Result()

            def set_or_reset_password(self, email, password, **kwargs):
                self.password = password
                return {"ok": True}

        fake = FakeFlow()
        with (
            patch.object(
                account_recovery,
                "create_mail_provider",
                return_value=object(),
            ),
            patch.object(account_recovery, "AuthFlow", return_value=fake),
            patch(
                "webui.two_factor.get_totp_status",
                return_value={"checked": True, "enabled": False, "totp": False},
            ),
            patch(
                "webui.two_factor.bind_totp_2fa_inline",
                return_value={"secret": "JBSWY3DPEHPK3PXP", "factor_id": "factor"},
            ),
        ):
            result = account_recovery.recover_account(
                {"email": "recovered@example.com"},
                {
                    "relay_url": "https://mail.example/recovered",
                    "new_password": "NewPassword1!",
                    "want_2fa": True,
                    "workflow_scope": "legacy",
                },
            )

        self.assertTrue(fake.force_email_otp)
        self.assertEqual(result["credential_source"], "recovery")
        self.assertEqual(result["password"], "NewPassword1!")
        saved = db.get_registered("recovered@example.com")
        self.assertEqual(saved["password"], "NewPassword1!")
        self.assertEqual(saved["totp_secret"], "JBSWY3DPEHPK3PXP")
        listed = db.list_registered(workflow_scope="legacy")
        row = next(item for item in listed if item["email"] == "recovered@example.com")
        self.assertEqual(row["credential_source"], "recovery")
        self.assertEqual(row["credential_source_label"], "邮箱验证码找回")


if __name__ == "__main__":
    unittest.main()
