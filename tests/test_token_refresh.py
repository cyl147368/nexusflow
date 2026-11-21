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

