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
        os.environ["NEXUSFLOW_DISABLE_SCHEDULERS"] = "1"

        from fastapi.testclient import TestClient
        from webui import db
        from webui.app import app
        from webui import integration

        db.DB_PATH = Path(self.temp.name) / "test.db"
        db.init_db()
        db.set_setting("plus_schedule_enabled", "1")
        integration._job = None
        integration._stop_event.clear()
        integration._pause_event.clear()
        self.client = TestClient(app)

    def tearDown(self):
        self.temp.cleanup()
        os.environ.pop("NEXUSFLOW_DISABLE_SCHEDULERS", None)

    def test_config_secrets_are_masked(self):
        response = self.client.get("/api/integration/config")
        self.assertEqual(response.status_code, 200)
        config = response.json()["config"]
        self.assertEqual(config["mailhub_token"], "***")
        self.assertEqual(config["icloud_token"], "***")
        self.assertTrue(config["want_password"])
        self.assertEqual(config["registration_password"], "")

    def test_plus_schedule_defaults_and_updates(self):
        response = self.client.get("/api/plus/schedule")
        self.assertEqual(response.status_code, 200)
        schedule = response.json()["schedule"]
        self.assertTrue(schedule["enabled"])
        self.assertEqual(schedule["interval_hours"], 3)
        self.assertEqual(schedule["scope"], "integration")
        self.assertEqual(schedule["egress_mode"], "direct")

        update = self.client.put(
            "/api/plus/schedule",
            json={
                "interval_hours": 4,
                "scope": "legacy",
                "egress_mode": "proxy",
                "workers": 2,
                "batch_size": 30,
            },
        )
        self.assertEqual(update.status_code, 200)
        saved = update.json()["schedule"]
        self.assertEqual(saved["interval_hours"], 4)
        self.assertEqual(saved["scope"], "legacy")
        self.assertEqual(saved["egress_mode"], "proxy")
        self.assertEqual(saved["workers"], 2)
        self.assertEqual(saved["batch_size"], 30)

    def test_team_schedule_defaults_and_updates(self):
        response = self.client.get("/api/team/schedule")
        self.assertEqual(response.status_code, 200)
        schedule = response.json()["schedule"]
        self.assertTrue(schedule["enabled"])
        self.assertEqual(schedule["interval_hours"], 3)
        self.assertEqual(schedule["scope"], "all")

        update = self.client.put(
            "/api/team/schedule",
            json={
                "interval_hours": 2,
                "scope": "all",
                "workers": 2,
                "batch_size": 10,
            },
        )
        self.assertEqual(update.status_code, 200)
        saved = update.json()["schedule"]
        self.assertEqual(saved["interval_hours"], 2)
        self.assertEqual(saved["scope"], "all")
        self.assertEqual(saved["workers"], 2)
        self.assertEqual(saved["batch_size"], 10)

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

        update = self.client.post(
            "/api/integration/config",
            json={"registration_password": "qweasd123456"},
        )
        self.assertEqual(update.status_code, 200)
        self.assertEqual(update.json()["config"]["registration_password"], "***")

    def test_alias_cleanup_config_defaults_and_persists(self):
        from webui import db

        self.assertTrue(
            db.get_integration_config()["delete_alias_after_registration"]
        )
        response = self.client.post(
            "/api/integration/config",
            json={"delete_alias_after_registration": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            response.json()["config"]["delete_alias_after_registration"]
        )

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

    def test_split_count_is_persisted_and_limited_to_two(self):
        response = self.client.post(
            "/api/integration/config",
            json={"split_count": 2},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"]["split_count"], 2)

        invalid = self.client.post(
            "/api/integration/config",
            json={"split_count": 3},
        )
        self.assertEqual(invalid.status_code, 422)

    def test_split_mailbox_addresses_are_unique_plus_aliases(self):
        from webui import integration

        addresses = integration._split_mailbox_addresses(
            "hidden.alias@icloud.com",
            2,
        )
        self.assertEqual(len(addresses), 2)
        self.assertEqual(len(set(addresses)), 2)
        for address in addresses:
            self.assertRegex(
                address,
                r"^hidden\.alias\+[a-z]{4}@icloud\.com$",
            )

    def test_safe_alias_cleanup_requires_complete_credentials(self):
        from webui import integration

        aliases = [
            {
                "email": "complete@icloud.com",
                "anonymousId": "alias-complete",
            },
            {
                "email": "missing-token@icloud.com",
                "anonymousId": "alias-missing-token",
            },
            {
                "email": "unknown@icloud.com",
                "anonymousId": "alias-unknown",
            },
        ]
        rows = {
            "complete@icloud.com": {
                "password": "qweasd123456",
                "totp_secret": "TOTP",
                "access_token": "AT",
                "session_token": "ST",
            },
            "missing-token@icloud.com": {
                "password": "qweasd123456",
                "totp_secret": "TOTP",
                "access_token": "AT",
                "session_token": "",
            },
        }
        with (
            patch.object(
                integration.db,
                "get_integration_internal_config",
                return_value={"icloud_account_id": "account"},
            ),
            patch.object(
                integration,
                "list_icloud_aliases",
                return_value={"aliases": aliases},
            ),
            patch.object(
                integration.db,
                "get_registered",
                side_effect=lambda email: rows.get(email.lower()),
            ),
        ):
            preview = integration.cleanup_safe_icloud_aliases(dry_run=True)

        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["eligible"], 1)
        self.assertEqual(preview["skipped"], 2)
        self.assertEqual(
            preview["candidates"][0]["email"],
            "complete@icloud.com",
        )

    def test_safe_alias_cleanup_deletes_only_eligible_alias(self):
        from webui import integration

        with (
            patch.object(
                integration.db,
                "get_integration_internal_config",
                return_value={"icloud_account_id": "account"},
            ),
            patch.object(
                integration,
                "list_icloud_aliases",
                return_value={
                    "aliases": [
                        {
                            "email": "complete@icloud.com",
                            "anonymousId": "alias-complete",
                        },
                        {
                            "email": "unknown@icloud.com",
                            "anonymousId": "alias-unknown",
                        },
                    ]
                },
            ),
            patch.object(
                integration.db,
                "get_registered",
                side_effect=lambda email: (
                    {
                        "password": "qweasd123456",
                        "totp_secret": "TOTP",
                        "access_token": "AT",
                        "session_token": "ST",
                    }
                    if email == "complete@icloud.com"
                    else None
                ),
            ),
            patch.object(integration, "delete_icloud_alias") as delete,
        ):
            result = integration.cleanup_safe_icloud_aliases(dry_run=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"], 1)
        delete.assert_called_once_with("account", "alias-complete")

    def test_post_success_cleanup_deletes_created_alias(self):
        from webui import integration

        with (
            patch.object(
                integration.db,
                "get_registered",
                return_value={
                    "password": "qweasd123456",
                    "totp_secret": "TOTP",
                    "access_token": "AT",
                    "session_token": "ST",
                },
            ),
            patch.object(integration, "delete_icloud_alias") as delete,
        ):
            result = integration._delete_alias_after_success(
                account_id="account",
                email="complete@icloud.com",
                created_alias={"anonymousId": "alias-complete"},
            )

        self.assertTrue(result["ok"])
        delete.assert_called_once_with("account", "alias-complete")

    def test_post_success_cleanup_requires_every_split_account_to_be_complete(self):
        from webui import integration

        complete = {
            "password": "qweasd123456",
            "totp_secret": "TOTP",
            "access_token": "AT",
            "session_token": "ST",
        }
        incomplete = {
            **complete,
            "totp_secret": "",
        }

        with (
            patch.object(
                integration.db,
                "get_registered",
                side_effect=lambda email: (
                    incomplete
                    if email == "complete+split@icloud.com"
                    else complete
                ),
            ),
            patch.object(integration, "delete_icloud_alias") as delete,
        ):
            result = integration._delete_alias_after_success(
                account_id="account",
                email="complete@icloud.com",
                created_alias={"anonymousId": "alias-complete"},
                account_emails=[
                    "complete@icloud.com",
                    "complete+split@icloud.com",
                ],
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertIn("complete+split@icloud.com", result["reason"])
        delete.assert_not_called()

    def test_cycle_registers_original_and_split_mailboxes(self):
        from webui import integration

        cfg = {
            "icloud_account_id": "acc_split",
            "mailhub_url": "http://mail-hub:8090",
            "mailhub_token": "mail-token",
            "icloud_url": "http://icloud-prime:8081",
            "icloud_token": "icloud-token",
            "receiver_mode": "imap",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_email": "receiver@example.com",
            "imap_password": "secret",
            "imap_folder": "INBOX",
            "imap_ssl": True,
            "proxy": "",
            "otp_timeout": 60,
            "want_access_token": True,
            "want_session_token": True,
            "want_refresh_token": True,
            "want_2fa": False,
            "split_count": 2,
        }
        integration._job = None
        integration._set_job(id="split-cycle", status="running", cycle_no=0, logs=[])
        mailbox_addresses = []
        run_rows = []

        def request_json(method, url, **kwargs):
            del method
            if url.endswith("/api/create"):
                return {"email": "hidden.alias@icloud.com"}
            if url.endswith("/api/mailboxes"):
                address = kwargs["payload"]["email"]
                mailbox_addresses.append(address)
                token = f"token-{len(mailbox_addresses)}"
                return {
                    "email": address,
                    "token": token,
                    "relay_url": f"https://mail.example/mail/{token}",
                }
            raise AssertionError(f"unexpected URL: {url}")

        def start_registration(account, options):
            del options
            run_id = f"split-run-{len(run_rows) + 1}"
            run_rows.append({
                "run_id": run_id,
                "status": "done",
                "error": "",
                "error_category": "",
                "email": account["email"],
            })
            return run_id

        with (
            patch.object(
                integration,
                "_select_registration_proxy",
                return_value={"proxy": "", "candidates": 1, "probed": 1},
            ),
            patch.object(integration, "_request_json", side_effect=request_json),
            patch.object(
                integration.registrar,
                "start_registration",
                side_effect=start_registration,
            ),
            patch.object(integration.db, "list_runs", side_effect=lambda limit: run_rows),
        ):
            result = integration._run_cycle("split-cycle", cfg)

        self.assertTrue(result["ok"])
        self.assertEqual(result["success_count"], 3)
        self.assertEqual(result["failure_count"], 0)
        self.assertEqual(len(mailbox_addresses), 3)
        self.assertEqual(mailbox_addresses[0], "hidden.alias@icloud.com")
        self.assertTrue(all("+", address) for address in mailbox_addresses[1:])
        self.assertEqual(
            [item["email"] for item in run_rows],
            mailbox_addresses,
        )
        self.assertEqual(
            integration.get_job()["cycle_address_total"],
            3,
        )

    def test_resume_continues_split_plan_without_creating_another_alias(self):
        from webui import db, integration

        base = "hidden.alias@icloud.com"
        split = "hidden.alias+abcd@icloud.com"
        plan = [
            {
                "email": base,
                "role": "original",
                "status": "success",
                "token": "token-base",
                "relay_url": "https://mail.example/mail/token-base",
            },
            {
                "email": split,
                "role": "split",
                "status": "prepared",
                "token": "token-split",
                "relay_url": "https://mail.example/mail/token-split",
            },
        ]
        db.import_accounts(
            f"{base}----http://mail-hub:8090/mail/token-base",
            kind="icloud_relay",
            workflow_scope="integration",
        )
        db.import_accounts(
            f"{split}----http://mail-hub:8090/mail/token-split",
            kind="icloud_relay",
            workflow_scope="integration",
        )
        integration._job = None
        integration._set_job(
            id="resume-split",
            status="running",
            stage="waiting_registration",
            cycle_no=0,
            email=split,
            base_email=base,
            split_emails=[split],
            cycle_accounts=plan,
            cycle_address_total=2,
            run_id="old-split-run",
            logs=[],
        )

        def start_registration(account, options):
            del options
            self.assertEqual(account["email"], split)
            return "new-split-run"

        with (
            patch.object(
                integration,
                "_select_registration_proxy",
                return_value={"proxy": "", "candidates": 1, "probed": 1},
            ),
            patch.object(
                integration,
                "list_icloud_aliases",
                return_value={"aliases": [{"email": base}]},
            ),
            patch.object(
                integration.db,
                "get_registered",
                side_effect=lambda email: (
                    {"access_token": "at", "session_token": "st", "refresh_token": "rt"}
                    if email == base else None
                ),
            ),
            patch.object(
                integration.registrar,
                "start_registration",
                side_effect=start_registration,
            ),
            patch.object(
                integration.db,
                "list_runs",
                return_value=[{
                    "run_id": "new-split-run",
                    "status": "done",
                    "error": "",
                    "error_category": "",
                }],
            ),
            patch.object(
                integration,
                "_request_json",
                side_effect=AssertionError("resume must not create another alias"),
            ),
        ):
            result = integration._run_cycle(
                "resume-split",
                {
                    "icloud_account_id": "acc_resume",
                    "mailhub_url": "http://mail-hub:8090",
                    "mailhub_token": "mail-token",
                    "icloud_url": "http://icloud-prime:8081",
                    "icloud_token": "icloud-token",
                    "otp_timeout": 60,
                    "want_access_token": True,
                    "want_session_token": True,
                    "want_refresh_token": True,
                    "want_2fa": False,
                },
                resuming=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["failure_count"], 0)
        self.assertEqual(set(result["processed_emails"]), {base, split})

    def test_integration_page_is_served(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("NexusFlow", response.text)
        self.assertEqual(response.headers["cache-control"], "no-cache")

    def test_hashed_assets_are_gzipped_and_cached_immutably(self):
        from webui.app import STATIC_DIR

        asset = next((STATIC_DIR / "assets").glob("index-*.js"))
        response = self.client.get(
            f"/static/assets/{asset.name}",
            headers={"Accept-Encoding": "gzip"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-encoding"], "gzip")
        self.assertIn("immutable", response.headers["cache-control"])
        self.assertIn("accept-encoding", response.headers["vary"].lower())

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

    def test_integration_proxy_selection_skips_failed_proxy(self):
        import time

        from webui import db, integration

        bad = "socks5://bad.example:1080"
        good = "socks5://good.example:1080"
        integration._proxy_cursor = 0

        def preflight(proxy):
            if proxy == bad:
                raise integration.IntegrationError(
                    "warmup HTTP 403 Cloudflare"
                )

        with (
            patch.object(
                integration.db,
                "list_proxy_pool",
                return_value=[bad, good],
            ),
            patch.object(
                integration,
                "_preflight_registration_network",
                side_effect=preflight,
            ),
            patch.object(integration, "_log"),
        ):
            selected = integration._select_registration_proxy({
                "proxy": bad,
            })

        self.assertEqual(selected["proxy"], good)
        health = db.get_proxy_health()
        self.assertGreater(health[bad]["cooldown_until"], time.time())
        self.assertEqual(health[good]["failures"], 0)

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

    def test_icloud_empty_trust_tokens_error_is_actionable(self):
        import io
        import urllib.error

        from webui import integration

        body = (
            '{"success":false,"message":"HTTP 421: '
            '{\\"success\\":false,\\"trustTokens\\":[]}"}'
        ).encode("utf-8")
        error = urllib.error.HTTPError(
            "http://icloud-prime:8081/api/aliases",
            502,
            "Bad Gateway",
            {},
            io.BytesIO(body),
        )
        with patch.object(
            integration.urllib.request,
            "urlopen",
            side_effect=error,
        ):
            with self.assertRaises(integration.IntegrationError) as raised:
                integration._request_json(
                    "GET",
                    "http://icloud-prime:8081/api/aliases",
                )

        self.assertIn("Cookie 已失效", str(raised.exception))
        self.assertIn("trustTokens 为空", str(raised.exception))
        self.assertIn("重新粘贴有效 Cookie", str(raised.exception))

    def test_integration_data_accepts_list_payloads(self):
        from webui import integration

        self.assertEqual(
            integration._data({
                "success": True,
                "data": [{"id": "acc-1"}],
            }),
            [{"id": "acc-1"}],
        )

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
                "access_token": f"at-{index}",
            })
        db.save_registered({
            "email": "checked@example.com",
            "workflow_scope": "integration",
            "access_token": "checked-at",
            "plus_check": {"status": "free", "label": "Free"},
        })
        db.save_registered({
            "email": "legacy-only@example.com",
            "workflow_scope": "legacy",
            "access_token": "legacy-at",
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

    def test_unchecked_detection_skips_accounts_without_access_token(self):
        from webui import db

        db.save_registered({
            "email": "usable@example.com",
            "workflow_scope": "integration",
            "access_token": "usable-at",
        })
        db.save_registered({
            "email": "incomplete@example.com",
            "workflow_scope": "integration",
            "access_token": "",
        })
        response = self.client.get(
            "/api/registered/unchecked_emails",
            params={"workflow_scope": "integration"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["emails"], ["usable@example.com"])
        self.assertEqual(body["skipped_no_at"], 1)

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
            row["access_token"] = f"at-{status or 'unchecked'}"
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

    def test_plus_payload_scans_all_account_nodes_for_free_trial(self):
        from webui.app import _classify_plus_payload

        payload = {
            "accounts": {
                "other": {
                    "account": {"plan_type": "free"},
                    "entitlement": {"has_active_subscription": False},
                    "eligible_promo_campaigns": {
                        "plus": {
                            "id": "plus-1-month-free",
                            "metadata": {
                                "discount": {"percentage": 100},
                                "promotion_type_label": "1-month free trial",
                            },
                        },
                    },
                },
                "default": {
                    "account": {"plan_type": "free"},
                    "entitlement": {"has_active_subscription": False},
                    "eligible_promo_campaigns": {},
                },
            },
        }
        self.assertEqual(
            _classify_plus_payload(payload)["status"],
            "plus_eligible",
        )

    def test_plus_payload_scans_all_account_nodes_for_active_plus(self):
        from webui.app import _classify_plus_payload

        payload = {
            "accounts": {
                "default": {
                    "account": {"plan_type": "free"},
                    "entitlement": {"has_active_subscription": False},
                    "eligible_promo_campaigns": {},
                },
                "workspace": {
                    "account": {"plan_type": "plus"},
                    "entitlement": {"has_active_subscription": True},
                    "eligible_promo_campaigns": {},
                },
            },
        }
        self.assertEqual(
            _classify_plus_payload(payload)["status"],
            "plus_active",
        )

    def test_plus_payload_ignores_deactivated_node_without_hiding_usable_node(self):
        from webui.app import _classify_plus_payload

        payload = {
            "accounts": {
                "disabled-workspace": {
                    "account": {
                        "plan_type": "plus",
                        "is_deactivated": True,
                    },
                    "entitlement": {"has_active_subscription": True},
                    "eligible_promo_campaigns": {},
                },
                "default": {
                    "account": {"plan_type": "free"},
                    "entitlement": {"has_active_subscription": False},
                    "eligible_promo_campaigns": {
                        "plus": {
                            "id": "plus-1-month-free",
                            "metadata": {
                                "discount": {"percentage": 100},
                                "promotion_type_label": "1-month free trial",
                            },
                        },
                    },
                },
            },
        }
        self.assertEqual(
            _classify_plus_payload(payload)["status"],
            "plus_eligible",
        )

    def test_plus_payload_marks_banned_when_every_account_node_is_deactivated(self):
        from webui.app import _classify_plus_payload

        payload = {
            "accounts": {
                "default": {
                    "account": {
                        "plan_type": "free",
                        "is_deactivated": True,
                    },
                    "entitlement": {"has_active_subscription": False},
                    "eligible_promo_campaigns": {
                        "plus": {
                            "id": "plus-1-month-free",
                            "metadata": {
                                "discount": {"percentage": 100},
                            },
                        },
                    },
                },
            },
        }
        self.assertEqual(
            _classify_plus_payload(payload)["status"],
            "banned",
        )

    def test_discounted_plus_offer_is_not_a_free_trial(self):
        from webui.app import _classify_plus_payload

        payload = {
            "accounts": {
                "default": {
                    "account": {"plan_type": "free"},
                    "entitlement": {"has_active_subscription": False},
                    "eligible_promo_campaigns": {
                        "plus": {
                            "id": "plus-1-month-50-pct-off",
                            "metadata": {
                                "discount": {"percentage": 50},
                                "promotion_type_label": "1-month 50% off",
                            },
                        },
                    },
                },
            },
        }
        self.assertEqual(_classify_plus_payload(payload)["status"], "free")

    def test_coupon_failure_does_not_confirm_a_campaign_hint(self):
        from webui.app import _confirm_plus_trial

        class FailedResponse:
            status_code = 503
            text = "unavailable"

            @staticmethod
            def json():
                return {}

        class FailedSession:
            @staticmethod
            def get(url, headers=None, timeout=0):
                del url, headers, timeout
                return FailedResponse()

        result = _confirm_plus_trial(
            FailedSession(),
            {},
            {"status": "plus_eligible", "label": "可领Plus试用"},
        )
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["trial_hint"])
        self.assertEqual(result["coupon_state"], "http_503")

    def test_fast_plus_check_classifies_and_persists_results(self):
        from webui import db

        db.save_registered({
            "email": "plus-token@example.com",
            "workflow_scope": "integration",
            "access_token": "plus-token",
        })
        db.save_registered({
            "email": "free-token@example.com",
            "workflow_scope": "integration",
            "access_token": "free-token",
        })
        db.save_registered({
            "email": "hint-token@example.com",
            "workflow_scope": "integration",
            "access_token": "hint-token",
        })
        db.save_registered({
            "email": "coupon-token@example.com",
            "workflow_scope": "integration",
            "access_token": "coupon-token",
        })

        class FakeResponse:
            status_code = 200
            text = ""

            def __init__(self, body):
                self.body = body

            def json(self):
                return self.body

        class FakeSession:
            def get(self, url, headers=None, timeout=0):
                del timeout
                token = (headers or {}).get("Authorization", "")
                if "promo_campaign/check_coupon" in url:
                    state = (
                        "eligible"
                        if "plus-token" in token or "coupon-token" in token
                        else "not_eligible"
                    )
                    return FakeResponse({
                        "state": state,
                        "redemption": {"redeemed": False},
                    })
                if "plus-token" in token:
                    promo = {
                        "plus": {
                            "id": "plus-1-month-free",
                            "metadata": {
                                "discount": {"percentage": 100},
                                "promotion_type_label": "1-month free trial",
                            },
                        },
                    }
                elif "hint-token" in token:
                    promo = {
                        "plus": {
                            "id": "plus-1-month-free",
                            "metadata": {
                                "discount": {"percentage": 100},
                                "promotion_type_label": "1-month free trial",
                            },
                        },
                    }
                else:
                    promo = {}
                return FakeResponse({
                    "accounts": {
                        "default": {
                            "account": {"plan_type": "free"},
                            "entitlement": {
                                "has_active_subscription": False,
                            },
                            "eligible_promo_campaigns": promo,
                        },
                    },
                })

            def close(self):
                return None

        with patch(
            "http_client.create_http_session",
            return_value=FakeSession(),
        ):
            response = self.client.post(
                "/api/registered/check_plus_fast",
                json={
                    "emails": [
                        "plus-token@example.com",
                        "free-token@example.com",
                        "hint-token@example.com",
                        "coupon-token@example.com",
                    ],
                    "proxy": "",
                },
            )

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(
            results["plus-token@example.com"]["status"],
            "plus_eligible",
        )
        self.assertEqual(results["free-token@example.com"]["status"], "free")
        self.assertEqual(results["hint-token@example.com"]["status"], "free")
        self.assertEqual(
            results["coupon-token@example.com"]["status"],
            "plus_eligible",
        )
        self.assertEqual(
            db.get_registered("plus-token@example.com")["extra"]["plus_check"]["status"],
            "plus_eligible",
        )

    def test_fast_plus_check_refreshes_revoked_at_and_retries(self):
        from webui import db

        email = "plus-refresh@example.com"
        db.save_registered({
            "email": email,
            "workflow_scope": "integration",
            "access_token": "revoked-at",
            "session_token": "valid-st",
            "plus_check": {"status": "token_invalid"},
            "team_promo_check": {"status": "token_invalid"},
        })

        class FakeResponse:
            def __init__(self, status_code, body, text=""):
                self.status_code = status_code
                self.body = body
                self.text = text

            def json(self):
                return self.body

        class FakeSession:
            def get(self, url, headers=None, timeout=0):
                del timeout
                token = (headers or {}).get("Authorization", "")
                if "accounts/check" in url:
                    if "revoked-at" in token:
                        return FakeResponse(
                            401,
                            {"error": {"code": "token_revoked"}},
                            '{"code":"token_revoked",'
                            '"message":"invalidated oauth token"}',
                        )
                    return FakeResponse(200, {
                        "accounts": {
                            "default": {
                                "account": {"plan_type": "free"},
                                "entitlement": {
                                    "has_active_subscription": False,
                                },
                                "eligible_promo_campaigns": {},
                            },
                        },
                    })
                if "promo_campaign/check_coupon" in url:
                    return FakeResponse(200, {
                        "coupon": "plus-1-month-free",
                        "state": "eligible",
                        "redemption": {"redeemed": False},
                    })
                raise AssertionError(f"unexpected URL: {url}")

            def close(self):
                return None

        def refresh(address, proxy):
            self.assertEqual(address, email)
            self.assertEqual(proxy, "")
            db.update_registered_tokens(
                address,
                access_token="fresh-at",
                session_token="valid-st",
                refreshed_at=1,
                method="session_token",
                expires=3600,
            )
            return {"ok": True, "email": address, "status": "success"}

        with (
            patch(
                "http_client.create_http_session",
                return_value=FakeSession(),
            ),
            patch(
                "webui.token_refresh.refresh_registered_access_token",
                side_effect=refresh,
            ),
            patch(
                "webui.token_refresh.validate_access_token",
                return_value={"ok": True, "status": 200},
            ),
        ):
            response = self.client.post(
                "/api/registered/check_plus_fast",
                json={"emails": [email], "proxy": "", "workers": 1},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["results"][email]["status"],
            "plus_eligible",
            response.json(),
        )
        saved = db.get_registered(email)
        self.assertEqual(saved["access_token"], "fresh-at")
        self.assertEqual(
            saved["extra"]["plus_check"]["status"],
            "plus_eligible",
        )
        self.assertNotIn("team_promo_check", saved["extra"])

    def test_revoked_refresh_token_falls_back_to_password_relogin(self):
        import logging

        from webui.app import _refresh_access_token_for_retry

        with (
            patch(
                "webui.token_refresh.refresh_registered_access_token",
                return_value={
                    "ok": True,
                    "email": "relogin-fallback@example.com",
                },
            ),
            patch(
                "webui.token_refresh.validate_access_token",
                return_value={"ok": False, "status": 401},
            ),
            patch(
                "webui.token_refresh.relogin_registered_account",
                return_value={"ok": True, "method": "password_relogin"},
            ) as relogin,
            patch(
                "webui.db.get_registered",
                side_effect=[
                    {
                        "email": "relogin-fallback@example.com",
                        "access_token": "revoked-fresh-at",
                    },
                    {
                        "email": "relogin-fallback@example.com",
                        "access_token": "relogin-at",
                    },
                ],
            ),
        ):
            cred, error = _refresh_access_token_for_retry(
                "relogin-fallback@example.com",
                "",
                logging.getLogger("test"),
                "test",
            )

        self.assertEqual(error, "")
        self.assertEqual(cred["access_token"], "relogin-at")
        relogin.assert_called_once_with("relogin-fallback@example.com", "")

    def test_check_plus_refreshes_revoked_at_and_retries(self):
        from webui import db

        email = "plus-slow-refresh@example.com"
        db.save_registered({
            "email": email,
            "workflow_scope": "integration",
            "access_token": "slow-revoked-at",
            "session_token": "slow-valid-st",
        })

        class FakeResponse:
            def __init__(self, status_code, body, text=""):
                self.status_code = status_code
                self.body = body
                self.text = text

            def json(self):
                return self.body

        class FakeSession:
            def get(self, url, headers=None, timeout=0):
                del timeout
                token = (headers or {}).get("Authorization", "")
                if "accounts/check" in url:
                    if "slow-revoked-at" in token:
                        return FakeResponse(
                            401,
                            {"error": {"code": "token_revoked"}},
                            '{"code":"token_revoked",'
                            '"message":"invalidated oauth token"}',
                        )
                    return FakeResponse(200, {
                        "accounts": {
                            "default": {
                                "account": {"plan_type": "free"},
                                "entitlement": {
                                    "has_active_subscription": False,
                                },
                                "eligible_promo_campaigns": {},
                            },
                        },
                    })
                if "promo_campaign/check_coupon" in url:
                    return FakeResponse(200, {
                        "coupon": "plus-1-month-free",
                        "state": "eligible",
                        "redemption": {"redeemed": False},
                    })
                raise AssertionError(f"unexpected URL: {url}")

            def close(self):
                return None

        def refresh(address, proxy):
            self.assertEqual(address, email)
            self.assertEqual(proxy, "")
            db.update_registered_tokens(
                address,
                access_token="slow-fresh-at",
                session_token="slow-valid-st",
                refreshed_at=1,
                method="session_token",
                expires=3600,
            )
            return {"ok": True, "email": address, "status": "success"}

        with (
            patch(
                "http_client.create_http_session",
                return_value=FakeSession(),
            ),
            patch(
                "webui.token_refresh.refresh_registered_access_token",
                side_effect=refresh,
            ),
            patch(
                "webui.token_refresh.validate_access_token",
                return_value={"ok": True, "status": 200},
            ),
        ):
            response = self.client.post(
                "/api/registered/check_plus",
                json={"emails": [email], "proxy": ""},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["results"][email]["status"],
            "plus_eligible",
            response.json(),
        )
        self.assertEqual(
            db.get_registered(email)["access_token"],
            "slow-fresh-at",
        )

    def test_team_check_refreshes_revoked_at_and_retries(self):
        from webui import db

        email = "team-refresh@example.com"
        db.save_registered({
            "email": email,
            "workflow_scope": "integration",
            "access_token": "revoked-team-at",
            "session_token": "valid-team-st",
            "plus_check": {"status": "token_invalid"},
            "team_promo_check": {"status": "token_invalid"},
        })

        class FakeResponse:
            def __init__(self, status_code, body, text=""):
                self.status_code = status_code
                self.body = body
                self.text = text

            def json(self):
                return self.body

        class FakeSession:
            def get(self, url, headers=None, timeout=0):
                del timeout
                token = (headers or {}).get("Authorization", "")
                if "accounts/check" in url:
                    if "revoked-team-at" in token:
                        return FakeResponse(
                            401,
                            {"error": {"code": "token_revoked"}},
                            '{"code":"token_revoked",'
                            '"message":"invalidated oauth token"}',
                        )
                    return FakeResponse(200, {
                        "accounts": {
                            "default": {
                                "account": {
                                    "account_id": "acct-personal",
                                    "structure": "personal",
                                    "plan_type": "free",
                                },
                                "entitlement": {
                                    "has_active_subscription": False,
                                },
                            },
                        },
                    })
                if "promo_campaign/check_coupon" in url:
                    return FakeResponse(200, {
                        "coupon": "team-1-month-free",
                        "state": "eligible",
                        "redemption": {"redeemed": False},
                    })
                raise AssertionError(f"unexpected URL: {url}")

            def close(self):
                return None

        def refresh(address, proxy):
            self.assertEqual(address, email)
            self.assertEqual(proxy, "")
            db.update_registered_tokens(
                address,
                access_token="fresh-team-at",
                session_token="valid-team-st",
                refreshed_at=1,
                method="session_token",
                expires=3600,
            )
            return {"ok": True, "email": address, "status": "success"}

        with (
            patch(
                "http_client.create_http_session",
                return_value=FakeSession(),
            ),
            patch(
                "webui.token_refresh.refresh_registered_access_token",
                side_effect=refresh,
            ),
            patch(
                "webui.token_refresh.validate_access_token",
                return_value={"ok": True, "status": 200},
            ),
        ):
            response = self.client.post(
                "/api/registered/check_team_promo_fast",
                json={"emails": [email], "workers": 1},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["results"][email]["status"],
            "team_eligible",
        )
        saved = db.get_registered(email)
        self.assertEqual(saved["access_token"], "fresh-team-at")
        self.assertEqual(
            saved["extra"]["team_promo_check"]["status"],
            "team_eligible",
        )
        self.assertNotIn("plus_check", saved["extra"])

    def test_team_promo_check_classifies_persists_and_filters(self):
        from webui import db

        db.save_registered({
            "email": "team-ok@example.com",
            "workflow_scope": "integration",
            "access_token": "team-ok-token",
        })
        db.save_registered({
            "email": "team-no@example.com",
            "workflow_scope": "integration",
            "access_token": "team-no-token",
        })
        db.save_registered({
            "email": "team-invalid@example.com",
            "workflow_scope": "integration",
            "access_token": "team-invalid-token",
        })

        class FakeResponse:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self.body = body
                self.text = ""

            def json(self):
                return self.body

        class FakeSession:
            def get(self, url, headers=None, timeout=0):
                del timeout
                token = (headers or {}).get("Authorization", "")
                if "team-invalid-token" in token and "accounts/check" in url:
                    return FakeResponse(401, {"error": "invalid token"})
                if "accounts/check" in url:
                    return FakeResponse(200, {
                        "accounts": {
                            "default": {
                                "account": {
                                    "account_id": "acct-personal",
                                    "structure": "personal",
                                    "name": "Personal",
                                    "plan_type": "free",
                                },
                                "entitlement": {
                                    "has_active_subscription": False,
                                },
                            }
                        }
                    })
                if "promo_campaign/check_coupon" in url:
                    expected = url.split("coupon=", 1)[1].split("&", 1)[0]
                    state = "eligible" if "team-ok-token" in token else "not_eligible"
                    return FakeResponse(200, {
                        "coupon": expected,
                        "state": state,
                        "ineligibility_reason": (
                            "" if state == "eligible" else "prior_introductory_offer_redeemed"
                        ),
                        "redemption": {"redeemed": False},
                    })
                return FakeResponse(404, {})

            def close(self):
                return None

        with patch(
            "http_client.create_http_session",
            return_value=FakeSession(),
        ):
            response = self.client.post(
                "/api/registered/check_team_promo_fast",
                json={
                    "emails": [
                        "team-ok@example.com",
                        "team-no@example.com",
                        "team-invalid@example.com",
                    ],
                },
            )

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(results["team-ok@example.com"]["status"], "team_eligible")
        self.assertEqual(results["team-no@example.com"]["status"], "team_ineligible")
        self.assertEqual(results["team-invalid@example.com"]["status"], "token_invalid")

        stored = db.get_registered("team-ok@example.com")["extra"]["team_promo_check"]
        self.assertEqual(stored["coupon"], "team-1-month-free")
        self.assertEqual(stored["account_name"], "Personal")
        filtered = self.client.get(
            "/api/registered",
            params={"filter": "team_eligible", "workflow_scope": "integration"},
        ).json()
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["items"][0]["team_promo_check"]["status"], "team_eligible")

    def test_team_promo_unchecked_emails_only_include_at_and_unchecked(self):
        from webui import db

        db.save_registered({
            "email": "team-unchecked@example.com",
            "workflow_scope": "integration",
            "access_token": "team-unchecked-token",
        })
        db.save_registered({
            "email": "team-no-at@example.com",
            "workflow_scope": "integration",
            "access_token": "",
        })
        db.save_registered({
            "email": "team-checked@example.com",
            "workflow_scope": "integration",
            "access_token": "team-checked-token",
        })
        db.update_team_promo_check(
            "team-checked@example.com",
            {"status": "team_ineligible", "label": "不符合Team"},
        )

        response = self.client.get(
            "/api/registered/team_promo_emails",
            params={"workflow_scope": "integration"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["emails"], ["team-unchecked@example.com"])
        self.assertEqual(body["skipped_no_at"], 1)

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

    def test_export_all_respects_current_registered_filter(self):
        from webui import db

        db.save_registered({
            "email": "plus-integration@example.com",
            "workflow_scope": "integration",
            "access_token": "access-plus",
            "plus_check": {"status": "plus_eligible"},
        })
        db.save_registered({
            "email": "free-integration@example.com",
            "workflow_scope": "integration",
            "access_token": "access-free",
            "plus_check": {"status": "free"},
        })
        db.save_registered({
            "email": "plus-legacy@example.com",
            "workflow_scope": "legacy",
            "access_token": "access-legacy",
            "plus_check": {"status": "plus_eligible"},
        })

        response = self.client.post(
            "/api/registered/export",
            json={
                "format": "at",
                "all": True,
                "workflow_scope": "integration",
                "filter": "plus",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(
            body["emails"],
            ["plus-integration@example.com"],
        )
        self.assertEqual(body["text"], "access-plus")

    def test_st_export_uses_backend_72_hour_marker_without_changing_text(self):
        import time

        from webui import db

        now = time.time()
        db.save_registered({
            "email": "st-recent@example.com",
            "workflow_scope": "integration",
            "session_token": "recent-st",
            "st_last_exported_at": now,
        })
        db.save_registered({
            "email": "st-old@example.com",
            "workflow_scope": "integration",
            "session_token": "old-st",
            "st_last_exported_at": now - 4 * 24 * 3600,
        })

        payload = {
            "format": "st",
            "all": True,
            "workflow_scope": "integration",
            "filter": "all",
        }
        first = self.client.post("/api/registered/export", json=payload)
        second = self.client.post("/api/registered/export", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["count"], 1)
        self.assertEqual(first.json()["emails"], ["st-old@example.com"])
        self.assertEqual(first.json()["text"], "old-st")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["count"], 0)
        self.assertEqual(second.json()["text"], "")
        saved = db.get_registered("st-old@example.com")
        self.assertGreater(
            saved["extra"]["st_last_exported_at"],
            now - 10,
        )
        self.assertEqual(saved["extra"]["st_export_count"], 1)

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
                    "anonymous_id": "",
                },
                {
                    "email": "two@example.com",
                    "relay_url": "https://mail.example/two",
                    "anonymous_id": "",
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

    def test_startup_fails_orphaned_runs_but_preserves_active_queues(self):
        import queue

        from webui import db, registrar

        db.create_run("orphan-run", "orphan@example.com", "/tmp/orphan.log")
        db.create_run("active-run", "active@example.com", "/tmp/active.log")
        with registrar._lock:
            registrar._run_queues["active-run"] = queue.Queue()
        try:
            self.assertEqual(registrar.fail_orphaned_runs(), 1)
        finally:
            with registrar._lock:
                registrar._run_queues.pop("active-run", None)

        rows = {row["run_id"]: row for row in db.list_runs(100)}
        self.assertEqual(rows["orphan-run"]["status"], "failed")
        self.assertEqual(rows["active-run"]["status"], "running")

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

    def test_recovery_stops_after_login_and_reset_fail_without_third_attempt(self):
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

        first = EmailLoginFailedFlow()
        second = ResetFailedFlow()
        with (
            patch.object(
                account_recovery,
                "create_mail_provider",
                return_value=object(),
            ),
            patch.object(
                account_recovery,
                "AuthFlow",
                side_effect=[first, second],
            ) as auth_flow,
        ):
            with self.assertRaises(account_recovery.RecoveryError) as raised:
                account_recovery.recover_account(
                    {"email": "fresh-flow@example.com"},
                    {
                        "relay_url": "https://mail.example/fresh-flow",
                        "new_password": "FreshPassword1!",
                        "want_2fa": False,
                        "workflow_scope": "integration",
                    },
                )

        self.assertEqual(auth_flow.call_count, 2)
        self.assertTrue(first.force_email_otp)
        self.assertIn("密码重置失败", str(raised.exception))
        self.assertIn("邮箱验证码登录失败", str(raised.exception))

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

    def test_recovery_reuses_existing_password_and_skips_password_reset(self):
        from webui import account_recovery

        class ExistingPasswordFlow:
            password_login_required = False

            def run_protocol_login(
                self,
                mail,
                email,
                password="",
                force_email_otp=False,
            ):
                del mail
                self.password = password
                self.force_email_otp = force_email_otp

                class Result:
                    @staticmethod
                    def to_dict():
                        return {
                            "email": email,
                            "password": "",
                            "access_token": "existing-at",
                            "session_token": "existing-st",
                            "refresh_token": "",
                        }

                return Result()

            def set_or_reset_password(self, *args, **kwargs):
                raise AssertionError("existing password must not be reset")

            def add_password_via_reauth(self, *args, **kwargs):
                raise AssertionError("existing password must not be re-added")

        fake = ExistingPasswordFlow()
        saved = {}
        with (
            patch.object(
                account_recovery,
                "create_mail_provider",
                return_value=object(),
            ),
            patch.object(account_recovery, "AuthFlow", return_value=fake),
            patch.object(
                account_recovery.db,
                "get_registered",
                return_value={
                    "password": "ExistingPassword1!",
                    "totp_secret": "",
                    "access_token": "",
                    "session_token": "",
                },
            ),
            patch.object(
                account_recovery.db,
                "save_registered",
                side_effect=lambda data: saved.update(data),
            ),
            patch.object(account_recovery.db, "mark_done"),
        ):
            result = account_recovery._recover_account_login_first(
                {"email": "existing-password@example.com"},
                {
                    "relay_url": "https://mail.example/existing",
                    "new_password": "ReplacementPassword1!",
                    "want_2fa": False,
                    "workflow_scope": "integration",
                },
            )

        self.assertEqual(fake.password, "ExistingPassword1!")
        self.assertFalse(fake.force_email_otp)
        self.assertEqual(result["password"], "ExistingPassword1!")
        self.assertEqual(saved["password"], "ExistingPassword1!")

    def test_recovery_does_not_claim_totp_is_repaired_without_secret(self):
        from webui import account_recovery
        from webui.account_recovery import RecoveryError

        class ExistingPasswordFlow:
            password_login_required = False

            def run_protocol_login(self, *args, **kwargs):
                class Result:
                    @staticmethod
                    def to_dict():
                        return {
                            "email": "server-totp@example.com",
                            "access_token": "existing-at",
                            "session_token": "existing-st",
                        }

                return Result()

        saved = {}
        with (
            patch.object(
                account_recovery,
                "create_mail_provider",
                return_value=object(),
            ),
            patch.object(
                account_recovery,
                "AuthFlow",
                return_value=ExistingPasswordFlow(),
            ),
            patch.object(
                account_recovery.db,
                "get_registered",
                return_value={
                    "email": "server-totp@example.com",
                    "password": "ExistingPassword1!",
                    "totp_secret": "",
                    "access_token": "existing-at",
                    "session_token": "existing-st",
                },
            ),
            patch.object(
                account_recovery.db,
                "save_registered",
                side_effect=lambda data: saved.update(data),
            ),
            patch.object(account_recovery.db, "mark_done"),
            patch(
                "webui.two_factor.get_totp_status",
                return_value={
                    "checked": True,
                    "enabled": True,
                    "totp": True,
                },
            ),
        ):
            with self.assertRaises(RecoveryError) as raised:
                account_recovery._recover_account_login_first(
                    {"email": "server-totp@example.com"},
                    {
                        "relay_url": "https://mail.example/totp",
                        "want_2fa": True,
                        "workflow_scope": "integration",
                    },
                )

        self.assertEqual(saved["two_fa_status"], "unrecoverable")
        self.assertIn("secret is missing locally", saved["recovery_error"])
        self.assertEqual(raised.exception.payload["two_fa_status"], "unrecoverable")

    def test_recovery_falls_back_to_slow_2fa_binding(self):
        from webui import account_recovery

        class ExistingPasswordFlow:
            password_login_required = False

            def run_protocol_login(self, *args, **kwargs):
                class Result:
                    @staticmethod
                    def to_dict():
                        return {
                            "email": "fallback-2fa@example.com",
                            "access_token": "new-at",
                            "session_token": "new-st",
                        }

                return Result()

        saved = {}
        with (
            patch.object(
                account_recovery,
                "create_mail_provider",
                return_value=object(),
            ),
            patch.object(
                account_recovery,
                "AuthFlow",
                return_value=ExistingPasswordFlow(),
            ),
            patch.object(
                account_recovery.db,
                "get_registered",
                return_value={
                    "email": "fallback-2fa@example.com",
                    "password": "ExistingPassword1!",
                    "totp_secret": "",
                },
            ),
            patch.object(
                account_recovery.db,
                "save_registered",
                side_effect=lambda data: saved.update(data),
            ),
            patch.object(account_recovery.db, "mark_done"),
            patch(
                "webui.two_factor.get_totp_status",
                return_value={
                    "checked": True,
                    "enabled": False,
                    "totp": False,
                },
            ),
            patch(
                "webui.two_factor.bind_totp_2fa_inline",
                return_value=None,
            ),
            patch(
                "webui.two_factor.bind_totp_2fa",
                return_value={
                    "secret": "JBSWY3DPEHPK3PXP",
                    "factor_id": "factor",
                },
            ) as slow_bind,
        ):
            result = account_recovery._recover_account_login_first(
                {"email": "fallback-2fa@example.com"},
                {
                    "relay_url": "https://mail.example/fallback",
                    "want_2fa": True,
                    "workflow_scope": "integration",
                },
            )

        slow_bind.assert_called_once()
        self.assertEqual(result["two_fa_status"], "created")
        self.assertEqual(saved["totp_secret"], "JBSWY3DPEHPK3PXP")

    def test_failed_recovery_deletes_alias_without_usable_credentials(self):
        from webui import integration, registrar

        result = {"ok": False, "email": "empty@example.com"}
        with (
            patch.object(registrar.db, "get_registered", return_value=None),
            patch.object(integration, "delete_icloud_alias") as delete,
        ):
            saved = registrar._delete_alias_after_failed_recovery(
                {
                    "email": "empty@example.com",
                    "anonymous_id": "alias-empty",
                },
                {"delete_alias_on_failure": True},
                result,
            )

        self.assertTrue(saved["alias_deleted"])
        delete.assert_called_once_with("", "alias-empty")

    def test_failed_recovery_keeps_alias_with_partial_credentials(self):
        from webui import integration, registrar

        result = {"ok": False, "email": "partial@example.com"}
        with (
            patch.object(
                registrar.db,
                "get_registered",
                return_value={"access_token": "partial-at"},
            ),
            patch.object(integration, "delete_icloud_alias") as delete,
        ):
            saved = registrar._delete_alias_after_failed_recovery(
                {
                    "email": "partial@example.com",
                    "anonymous_id": "alias-partial",
                },
                {"delete_alias_on_failure": True},
                result,
            )

        self.assertFalse(saved["alias_deleted"])
        self.assertEqual(saved["alias_delete_error"], "partial_credentials_present")
        delete.assert_not_called()

    def test_auth_flow_enforces_otp_delivery_budget(self):
        from auth_flow import AuthFlow

        flow = AuthFlow.__new__(AuthFlow)
        flow._env_overrides = {"OTP_MAX_DELIVERIES": "1"}
        flow._otp_delivery_count = 0

        self.assertTrue(flow._reserve_otp_delivery("first"))
        self.assertFalse(flow._reserve_otp_delivery("second"))


if __name__ == "__main__":
    unittest.main()
