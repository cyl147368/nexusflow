import time
import unittest
from http.cookiejar import Cookie
from unittest.mock import patch

from auth_flow import AuthFlow, BrowserFallbackResult, _is_transient_cloudflare_cookie
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
    @staticmethod
    def _cookie(name: str, value: str, domain: str = ".chatgpt.com") -> Cookie:
        return Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
            path="/",
            path_specified=True,
            secure=True,
            expires=int(time.time()) + 600,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
        )

    def test_protocol_cookie_export_reads_cookiejar_objects(self):
        cookie = self._cookie("oai-did", "device-id")
        cloudflare_cookie = self._cookie("__cf_bm", "must-not-export")
        host_cookie = self._cookie("__Host-next-auth.csrf-token", "csrf")
        secure_cookie = self._cookie("__Secure-next-auth.state", "state")

        class CookieStore:
            jar = [cookie, cloudflare_cookie, host_cookie, secure_cookie]

            def __iter__(self):
                return iter([cookie.name, cloudflare_cookie.name])

        flow = AuthFlow(Config())
        flow.session = type("Session", (), {"cookies": CookieStore()})()

        exported = flow._export_protocol_cookies()

        self.assertEqual(len(exported), 3)
        by_name = {item["name"]: item for item in exported}
        self.assertEqual(
            {key: by_name["oai-did"][key] for key in ("value", "domain", "path")},
            {
                "value": "device-id",
                "domain": ".chatgpt.com",
                "path": "/",
            },
        )
        self.assertGreater(by_name["oai-did"]["expires"], time.time())
        self.assertEqual(by_name["__Host-next-auth.csrf-token"]["url"], "https://chatgpt.com/")
        self.assertNotIn("domain", by_name["__Host-next-auth.csrf-token"])
        self.assertNotIn("path", by_name["__Host-next-auth.csrf-token"])
        self.assertTrue(by_name["__Secure-next-auth.state"]["secure"])

    def test_cloudflare_transient_cookie_detection(self):
        self.assertTrue(_is_transient_cloudflare_cookie("__cf_bm"))
        self.assertTrue(_is_transient_cloudflare_cookie("cf_clearance"))
        self.assertTrue(_is_transient_cloudflare_cookie("cf_chl_test"))
        self.assertFalse(_is_transient_cloudflare_cookie("oai-did"))

    def test_password_registration_is_enabled_with_unified_default_password(self):
        class PasswordSession:
            def __init__(self):
                self.posted = {}

            def get(self, url, **kwargs):
                del url, kwargs
                return _Response()

            def post(self, url, **kwargs):
                self.posted = {"url": url, **kwargs}
                return _Response()

        saved = []
        flow = AuthFlow(
            Config(),
            env_overrides={
                "REGISTER_SET_PASSWORD": "1",
                "REGISTER_PASSWORD": "qweasd123456",
            },
            on_password=lambda email, password: saved.append((email, password)),
        )
        flow.session = PasswordSession()
        flow.result.device_id = "device-id"
        with (
            patch.object(flow, "_auth_json_headers", return_value={}),
            patch.object(flow, "_sentinel_fp_kwargs", return_value={}),
            patch.object(flow, "_new_document_navigation"),
            patch.object(flow, "_trace_http"),
            patch("sentinel.get_sentinel_token", return_value=("token", "so")),
        ):
            self.assertTrue(flow.register_password("new@example.com"))

        self.assertEqual(flow.result.password, "qweasd123456")
        self.assertEqual(
            flow.session.posted["json"],
            {"password": "qweasd123456", "username": "new@example.com"},
        )
        self.assertEqual(saved, [("new@example.com", "qweasd123456")])

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

    def test_oauth_init_prefers_chatgpt_cookie_when_oai_did_is_duplicated(self):
        class DuplicateCookies:
            def __iter__(self):
                return iter(["oai-did"])

            def get(self, name, default="", domain=""):
                if name == "oai-did" and domain == ".chatgpt.com":
                    return "chatgpt-device-id"
                if name == "oai-did":
                    raise RuntimeError(
                        "Multiple cookies exist with name=oai-did"
                    )
                return default

        class Session:
            cookies = DuplicateCookies()

            @staticmethod
            def get(url, **kwargs):
                del url, kwargs
                return _Response()

        flow = AuthFlow(Config())
        flow.session = Session()
        with (
            patch.object(flow, "_navigation_headers", return_value={}),
            patch.object(flow, "_trace_http"),
        ):
            device_id = flow.auth_oauth_init("https://auth.test/authorize")

        self.assertEqual(device_id, "chatgpt-device-id")

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
            patch.object(flow, "get_csrf_token", return_value="csrf") as get_csrf,
            patch.object(
                flow,
                "get_auth_url",
                return_value="https://auth.openai.com/oauth/authorize?state=test",
            ) as get_auth_url,
            patch.object(flow, "auth_oauth_init", return_value="device-id") as oauth_init,
            patch.object(flow, "get_sentinel_token", return_value="login-token"),
            patch.object(flow, "authorize_continue", return_value={}) as authorize_continue,
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
                "qweasd123456",
                timeout=30,
            )

        check_proxy.assert_called_once_with()
        get_csrf.assert_not_called()
        get_auth_url.assert_not_called()
        oauth_init.assert_not_called()
        authorize_continue.assert_not_called()
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
            patch.object(flow, "signup", return_value=False),
            patch.object(flow, "kickoff_otp_delivery", return_value=True),
        ):
            with self.assertRaises(StopAfterMailWait):
                flow.run_protocol_login(
                    Mail(),
                    "alias@example.com",
                    password="",
                    force_email_otp=True,
                )

    def test_forced_login_otp_does_not_create_missing_account(self):
        flow = AuthFlow(Config(), env_overrides={"OTP_TIMEOUT": "30"})
        with (
            patch.object(flow, "check_proxy", return_value=True),
            patch.object(flow, "warmup", return_value=True),
            patch.object(flow, "get_csrf_token", return_value="csrf"),
            patch.object(flow, "get_auth_url", return_value="https://auth.test"),
            patch.object(flow, "auth_oauth_init", return_value="device"),
            patch.object(flow, "get_sentinel_token", return_value="token"),
            patch.object(flow, "signup", return_value=True),
            patch.object(flow, "register_password") as register_password,
        ):
            with self.assertRaisesRegex(RuntimeError, "不会创建新号"):
                flow.run_protocol_login(
                    object(),
                    "missing@example.com",
                    password="",
                    force_email_otp=True,
                )

        register_password.assert_not_called()

    def test_passwordless_signup_is_treated_as_new_for_recovery(self):
        flow = AuthFlow(Config())
        signup_response = {
            "page": {
                "type": "email_otp_verification",
                "payload": {"email_verification_mode": "passwordless_signup"},
            }
        }
        with patch.object(flow, "authorize_continue", return_value=signup_response):
            self.assertTrue(flow.signup("new-alias@example.com", "sentinel"))
        self.assertFalse(flow._is_existing_account)

    def test_email_otp_login_completes_totp_challenge_from_database(self):
        flow = AuthFlow(
            Config(),
            account_callback=lambda _email: {"totp_secret": "JBSWY3DPEHPK3PXP"},
        )
        challenge = {
            "continue_url": "https://auth.openai.com/mfa-challenge/challenge-123",
            "page": {"type": "mfa_challenge"},
        }
        completed = {
            "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=ok",
            "page": {"type": "external_url"},
        }
        with (
            patch("auth_flow._totp_now", return_value="123456"),
            patch.object(flow, "submit_mfa_totp", return_value=completed) as submit,
        ):
            result = flow._complete_mfa_if_needed(
                challenge,
                "alias@example.com",
            )

        submit.assert_called_once_with("123456", "challenge-123")
        self.assertEqual(result, completed)

    def test_recovery_completes_about_you_before_session_handoff(self):
        class StopAfterCreateAccount(Exception):
            pass

        class Mail:
            def wait_for_otp(self, *args, **kwargs):
                return "123456"

        flow = AuthFlow(Config(), env_overrides={"OTP_TIMEOUT": "30"})
        with (
            patch.object(flow, "check_proxy", return_value=True),
            patch.object(flow, "warmup", return_value=True),
            patch.object(flow, "get_csrf_token", return_value="csrf"),
            patch.object(flow, "get_auth_url", return_value="https://auth.test"),
            patch.object(flow, "auth_oauth_init", return_value="device"),
            patch.object(flow, "kickoff_otp_delivery", return_value=True),
            patch.object(
                flow,
                "verify_otp",
                return_value={
                    "continue_url": "https://auth.openai.com/about-you",
                    "page": {"type": "about_you"},
                },
            ),
            patch.object(flow, "fetch_client_auth_session_dump"),
            patch.object(
                flow,
                "create_account",
                side_effect=StopAfterCreateAccount(),
            ) as create_account,
        ):
            flow._is_existing_account = True
            flow._authorize_landing_url = (
                "https://auth.openai.com/email-verification"
            )
            with self.assertRaises(StopAfterCreateAccount):
                flow.run_protocol_login(
                    Mail(),
                    "alias@example.com",
                    force_email_otp=True,
                )

        create_account.assert_called_once_with()

    def test_add_password_uses_password_reauth_state(self):
        class ReauthSession:
            def __init__(self):
                self.start_url = ""
                self.form = {}

            def post(self, url, **kwargs):
                self.start_url = url
                self.form = kwargs.get("data", {})
                return type(
                    "Response",
                    (),
                    {
                        "status_code": 200,
                        "headers": {},
                        "text": "",
                        "raise_for_status": lambda self: None,
                        "json": lambda self: {
                            "url": "https://auth.openai.com/oauth/authorize?state=x"
                        },
                    },
                )()

        flow = AuthFlow(Config())
        flow.result.session_token = "session-token"
        flow.result.device_id = "device-id"
        flow.session = ReauthSession()
        completed = {"ok": True, "mode": "add"}
        with (
            patch.object(flow, "get_csrf_token", return_value="csrf-token"),
            patch.object(flow, "_remember_oauth_params"),
            patch.object(flow, "_trace_http"),
            patch.object(
                flow,
                "auth_oauth_init",
                side_effect=lambda _url: setattr(
                    flow,
                    "_authorize_landing_url",
                    "https://auth.openai.com/create-account/password",
                ) or "device-id",
            ),
            patch.object(
                flow,
                "set_or_reset_password",
                return_value=completed,
            ) as set_password,
        ):
            result = flow.add_password_via_reauth(
                "alias@example.com",
                "qweasd123456",
            )

        self.assertEqual(result, completed)
        self.assertIn("post_login_add_password=true", flow.session.start_url)
        self.assertIn("reauth=password", flow.session.start_url)
        self.assertEqual(flow.session.form["csrfToken"], "csrf-token")
        set_password.assert_called_once_with(
            "alias@example.com",
            "qweasd123456",
            has_existing_password=False,
            referer="https://auth.openai.com/create-account/password",
        )

    def test_authorize_state_is_refreshed_after_sentinel(self):
        flow = AuthFlow(Config())
        with (
            patch.object(
                flow,
                "get_auth_url",
                return_value="https://auth.test/fresh",
            ) as get_auth_url,
            patch.object(
                flow,
                "auth_oauth_init",
                return_value="device-id",
            ) as auth_oauth_init,
        ):
            result = flow._refresh_authorize_state(
                "csrf",
                "alias@example.com",
                "device-id",
                "sentinel",
            )

        self.assertEqual(
            result,
            ("https://auth.test/fresh", "device-id", "sentinel"),
        )
        get_auth_url.assert_called_once_with(
            "csrf",
            email="alias@example.com",
        )
        auth_oauth_init.assert_called_once_with("https://auth.test/fresh")

    def test_browser_home_is_not_treated_as_auth_fallback_success(self):
        flow = AuthFlow(Config())
        result = BrowserFallbackResult(
            driver="camoufox",
            final_url="https://chatgpt.com/",
            stage="chatgpt_home",
            title="ChatGPT",
            user_agent="Firefox/158",
            cookies=[
                {
                    "name": "oai-did",
                    "value": "device-id",
                    "domain": ".chatgpt.com",
                    "path": "/",
                }
            ],
        )
        with patch("auth_flow.BrowserFallback") as browser_cls:
            browser_cls.return_value.bootstrap.return_value = result
            ok = flow._recover_auth_with_browser(
                "https://auth.openai.com/authorize?state=test",
                "probe@example.com",
                reason="authorize_cloudflare",
            )

        self.assertFalse(ok)
        self.assertEqual(flow._last_browser_fallback, result)

    def test_browser_auth_landing_is_accepted_as_fallback_success(self):
        flow = AuthFlow(Config())
        result = BrowserFallbackResult(
            driver="camoufox",
            final_url="https://auth.openai.com/email-verification?x=1",
            stage="email_otp_verification",
            title="Verify email",
            user_agent="Firefox/158",
            cookies=[
                {
                    "name": "oai-did",
                    "value": "device-id",
                    "domain": ".chatgpt.com",
                    "path": "/",
                }
            ],
        )
        with patch("auth_flow.BrowserFallback") as browser_cls:
            browser_cls.return_value.bootstrap.return_value = result
            ok = flow._recover_auth_with_browser(
                "https://auth.openai.com/authorize?state=test",
                "probe@example.com",
                reason="authorize_cloudflare",
            )

        self.assertTrue(ok)

    def test_browser_callback_capture_returns_session_and_access_token(self):
        flow = AuthFlow(Config())
        result = BrowserFallbackResult(
            driver="playwright",
            final_url="https://chatgpt.com/",
            stage="chatgpt_home",
            title="ChatGPT",
            user_agent="Chrome/146",
            cookies=[
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": "session-token",
                    "domain": ".chatgpt.com",
                    "path": "/",
                }
            ],
            access_token="access-token",
        )
        with (
            patch.object(flow, "_export_protocol_cookies", return_value=[]),
            patch.object(flow, "_apply_browser_result"),
            patch("auth_flow.BrowserFallback") as browser_cls,
        ):
            browser_cls.return_value.bootstrap.return_value = result
            captured = flow._capture_callback_with_browser(
                "https://chatgpt.com/api/auth/callback/openai?code=one"
            )

        self.assertTrue(captured)
        self.assertEqual(flow.result.session_token, "session-token")
        self.assertEqual(flow.result.access_token, "access-token")
        browser_cls.return_value.bootstrap.assert_called_once()
        self.assertTrue(
            browser_cls.return_value.bootstrap.call_args.kwargs["capture_auth"]
        )

    def test_browser_login_recovers_tokens_after_callback_expires(self):
        flow = AuthFlow(Config(), env_overrides={"OTP_TIMEOUT": "30"})
        result = BrowserFallbackResult(
            driver="playwright",
            final_url="https://chatgpt.com/",
            stage="chatgpt_home",
            title="ChatGPT",
            user_agent="Chrome/146",
            cookies=[
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": "fresh-session",
                    "domain": ".chatgpt.com",
                    "path": "/",
                }
            ],
            access_token="fresh-access",
        )
        mail = type("Mail", (), {"wait_for_otp": lambda self, *a, **k: "123456"})()
        with (
            patch.object(flow, "get_csrf_token", return_value="csrf"),
            patch.object(flow, "get_auth_url", return_value="https://auth.test/fresh"),
            patch.object(flow, "_export_protocol_cookies", return_value=[]),
            patch.object(flow, "_apply_browser_result"),
            patch("auth_flow.BrowserFallback") as browser_cls,
        ):
            browser_cls.return_value.bootstrap.return_value = result
            recovered = flow._recover_session_with_browser(
                "alias@example.com",
                "qweasd123456",
                mail,
            )

        self.assertTrue(recovered)
        self.assertEqual(flow.result.session_token, "fresh-session")
        self.assertEqual(flow.result.access_token, "fresh-access")
        self.assertEqual(
            browser_cls.return_value.bootstrap.call_args.args[:3],
            ("https://auth.test/fresh", "alias@example.com", "qweasd123456"),
        )

    def test_signup_does_not_proceed_after_failed_browser_fallback(self):
        flow = AuthFlow(Config())
        with (
            patch.object(
                flow,
                "authorize_continue",
                side_effect=RuntimeError("HTTP 409 invalid_state"),
            ),
            patch.object(
                flow,
                "_recover_auth_with_browser",
                return_value=False,
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "浏览器降级没有建立有效认证状态",
            ):
                flow.signup(
                    "probe@example.com",
                    landing_url="",
                    sentinel_provider=lambda: "token",
                )


if __name__ == "__main__":
    unittest.main()
