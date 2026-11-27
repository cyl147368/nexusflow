import unittest
from unittest.mock import patch

from webui import token_refresh


class TokenRefreshTest(unittest.TestCase):
    def test_oauth_refresh_falls_back_when_session_token_fails(self):
        cred = {
            "email": "refresh@example.com",
            "session_token": "old-session",
            "refresh_token": "old-refresh",
            "extra": {},
        }
        with (
            patch.object(token_refresh.db, "get_registered", return_value=cred),
            patch.object(
                token_refresh,
                "_refresh_with_session_token",
                return_value={"ok": False, "error": "expired"},
            ),
            patch.object(
                token_refresh,
                "_refresh_with_oauth_token",
                return_value={
                    "ok": True,
                    "access_token": "new-at",
                    "refresh_token": "new-rt",
                    "method": "oauth_refresh_token",
                    "expires_in": 3600,
                },
            ),
            patch.object(
                token_refresh.db,
                "update_registered_tokens",
                return_value=True,
            ) as update_tokens,
        ):
            result = token_refresh.refresh_registered_access_token(
                "refresh@example.com",
                "socks5://proxy:1080",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "oauth_refresh_token")
        update_tokens.assert_called_once()
        kwargs = update_tokens.call_args.kwargs
        self.assertEqual(kwargs["access_token"], "new-at")
        self.assertEqual(kwargs["refresh_token"], "new-rt")

    def test_account_without_refresh_credentials_is_rejected(self):
        with patch.object(
            token_refresh.db,
            "get_registered",
            return_value={"email": "empty@example.com", "extra": {}},
        ):
            result = token_refresh.refresh_registered_access_token(
                "empty@example.com"
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "missing_credentials")

    def test_password_relogin_saves_fresh_session_without_new_otp(self):
        cred = {
            "email": "relogin@example.com",
            "password": "saved-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "extra": {"workflow_scope": "integration"},
        }

        class FakeResult:
            @staticmethod
            def to_dict():
                return {
                    "email": "relogin@example.com",
                    "access_token": "fresh-at",
                    "session_token": "fresh-st",
                    "refresh_token": "",
                    "device_id": "device",
                    "cookie_header": "oai-did=device",
                    "id_token": "id-token",
                    "csrf_token": "csrf",
                }

        created_flows = []

        class FakeFlow:
            def __init__(self, *args, **kwargs):
                self.init_kwargs = kwargs
                created_flows.append(self)

            def run_protocol_login(
                self,
                mail,
                email,
                password="",
                force_email_otp=False,
            ):
                self.login_args = (mail, email, password, force_email_otp)
                return FakeResult()

        with (
            patch.object(token_refresh.db, "get_registered", return_value=cred),
            patch.object(
                token_refresh.db,
                "get_account",
                return_value={"relay_url": "https://mail.example/relogin"},
            ),
            patch.object(token_refresh.db, "get_mail_settings", return_value={}),
            patch("mail_providers.create_mail_provider", return_value=object()),
            patch("config.Config"),
            patch("auth_flow.AuthFlow", side_effect=FakeFlow),
            patch.object(
                token_refresh.db,
                "update_registered_tokens",
                return_value=True,
            ) as update_tokens,
            patch.object(
                token_refresh,
                "validate_access_token",
                return_value={"ok": True, "status": 200},
            ),
        ):
            result = token_refresh.relogin_registered_account(
                "relogin@example.com",
            )

        flow = created_flows[0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "password_relogin")
        self.assertEqual(
            flow.login_args,
            (flow.login_args[0], "relogin@example.com", "saved-password", False),
        )
        self.assertEqual(
            flow.init_kwargs["env_overrides"]["OTP_MAX_DELIVERIES"],
            "1",
        )
        kwargs = update_tokens.call_args.kwargs
        self.assertEqual(kwargs["access_token"], "fresh-at")
        self.assertEqual(kwargs["session_token"], "fresh-st")
        self.assertEqual(kwargs["cookie_header"], "oai-did=device")
        self.assertEqual(kwargs["method"], "password_relogin")

