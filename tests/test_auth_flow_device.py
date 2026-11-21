import time
import unittest
from unittest.mock import patch

from auth_flow import AuthFlow
from config import Config


class _Cookies:
    def get(self, name, default=""):
        return {
            "oai-did": "cookie-device-id",
        }.get(name, default)


class _Response:
    status_code = 200
    text = ""
    headers = {}

    def json(self):
        return {"url": "https://auth.openai.com/authorize?state=test"}

    def raise_for_status(self):
        return None


class _Session:
    cookies = _Cookies()

    def __init__(self):
        self.signin_url = ""

    def post(self, url, **kwargs):
        self.signin_url = url
        return _Response()


class AuthFlowDeviceTest(unittest.TestCase):
    def test_auth_url_reuses_warmup_device_id(self):
        flow = AuthFlow(Config())
        flow.session = _Session()
        with (
            patch.object(flow, "_trace_http"),
            patch.object(flow, "_remember_oauth_params"),
            patch.object(flow, "_inject_pkce_into_auth_url", side_effect=lambda url: url),
        ):
            flow.get_auth_url("csrf-token")

        self.assertEqual(flow.result.device_id, "cookie-device-id")
        self.assertIn("ext-oai-did=cookie-device-id", flow.session.signin_url)

    def test_password_reset_uses_native_email_otp_endpoints(self):
        class ResetSession:
            def __init__(self):
                self.get_urls = []
                self.post_urls = []

            def get(self, url, **kwargs):
                self.get_urls.append(url)
                return _Response()

            def post(self, url, **kwargs):
                self.post_urls.append((url, kwargs))
                return _Response()

        mail = type("Mail", (), {"wait_for_otp": lambda self, *a, **k: "123456"})()
        flow = AuthFlow(Config())
        flow.session = ResetSession()
        flow.result.device_id = "device-id"
        with (
            patch.object(flow, "check_proxy", return_value=True) as check_proxy,
            patch.object(flow, "warmup", return_value=True),
            patch.object(flow, "get_csrf_token", return_value="csrf"),
            patch.object(
                flow,
                "get_auth_url",
                return_value="https://auth.openai.com/oauth/authorize?state=test",
            ),
            patch.object(flow, "auth_oauth_init", return_value="device-id"),
            patch.object(flow, "get_sentinel_token", return_value="login-token"),
            patch.object(flow, "authorize_continue", return_value={}),
            patch.object(
                flow,
                "verify_otp",
                return_value={
                    "continue_url": "https://auth.openai.com/reset-password/new-password",
                    "page": {"type": "reset_password_new_password"},
                },
            ),
            patch.object(flow, "_trace_http"),
            patch.object(flow, "_common_headers", return_value={}),
            patch.object(flow, "_sentinel_fp_kwargs", return_value={}),
            patch("sentinel.get_sentinel_token", return_value=("token", "so")),
        ):
            result = flow.reset_password_with_email_otp(
                mail,
                "recover@example.com",
                "qweasd123",
                timeout=30,
            )

        check_proxy.assert_called_once_with()
        self.assertEqual(result["mode"], "reset")
        self.assertIn(
            "https://auth.openai.com/api/accounts/password/send-otp",
            [url for url, _ in flow.session.post_urls],
        )
        self.assertIn(
            "https://auth.openai.com/api/accounts/password/reset",
            [url for url, _ in flow.session.post_urls],
        )

    def test_login_password_page_uses_password_referer_for_otp(self):
        flow = AuthFlow(Config())
        with patch.object(flow, "send_passwordless_otp", return_value=True) as send_passwordless:
            sent = flow.start_login_password_otp(
                "https://auth.openai.com/log-in/password?state=test"
            )

        self.assertTrue(sent)
        send_passwordless.assert_called_once_with(
            "https://auth.openai.com/log-in/password?state=test"
        )

    def test_pending_passwordless_otp_keeps_previous_delivery_window(self):
        class Session:
            def post(self, url, **kwargs):
                return type(
                    "Response",
                    (),
                    {"status_code": 409, "text": '{"code":"invalid_state"}'},
                )()

        flow = AuthFlow(Config())
        flow.session = Session()
        flow.result.device_id = "device-id"
        with (
            patch("sentinel.get_sentinel_token", return_value=("token", "so")),
            patch.object(flow, "_sentinel_fp_kwargs", return_value={}),
            patch.object(flow, "resend_otp", return_value=False),
            patch.object(flow, "_trace_http"),
        ):
            sent = flow.send_passwordless_otp(
                "https://auth.openai.com/log-in/password?state=test"
            )

        self.assertTrue(sent)
        self.assertLessEqual(flow._last_otp_sent_after, time.time() - 10)

    def test_forced_login_otp_continues_to_mailbox_wait(self):
        class StopAfterMailWait(Exception):
            pass

        class Mail:
            def wait_for_otp(self, *args, **kwargs):
                raise StopAfterMailWait()

        flow = AuthFlow(Config(), env_overrides={"OTP_TIMEOUT": "30"})
        with (
            patch.object(flow, "check_proxy", return_value=True),
            patch.object(flow, "warmup", return_value=True),
            patch.object(flow, "get_csrf_token", return_value="csrf"),
            patch.object(flow, "get_auth_url", return_value="https://auth.test"),
            patch.object(flow, "auth_oauth_init", return_value="device"),
            patch.object(flow, "get_sentinel_token", return_value="token"),
            patch.object(
                flow,
                "authorize_continue",
                return_value={
                    "page": {"type": "login_password"},
                    "continue_url": "https://auth.openai.com/log-in/password",
                },
            ),
            patch.object(flow, "start_login_password_otp", return_value=True),
        ):
            with self.assertRaises(RuntimeError) as raised:
                flow.run_protocol_login(
                    Mail(),
                    "alias@example.com",
                    password="",
                    force_email_otp=True,
                )
        self.assertIsInstance(raised.exception.__cause__, StopAfterMailWait)


if __name__ == "__main__":
    unittest.main()
