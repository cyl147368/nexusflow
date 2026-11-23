import unittest

from browser_fallback import (
    BrowserFallback,
    BrowserFallbackResult,
    classify_auth_stage,
    playwright_proxy,
)


class BrowserFallbackTests(unittest.TestCase):
    def test_classifies_current_auth_landing_stages(self):
        self.assertEqual(
            classify_auth_stage("https://auth.openai.com/email-verification?x=1"),
            "email_otp_verification",
        )
        self.assertEqual(
            classify_auth_stage("https://auth.openai.com/create-account/password"),
            "create_account_password",
        )
        self.assertEqual(
            classify_auth_stage("https://auth.openai.com/about-you"),
            "about_you",
        )
        self.assertEqual(
            classify_auth_stage(
                "https://chatgpt.com/api/auth/callback/openai?code=one&state=two"
            ),
            "callback",
        )

    def test_proxy_conversion_uses_remote_dns_and_decodes_credentials(self):
        result = playwright_proxy(
            "socks5h://proxy%20user:p%40ss@127.0.0.1:1080"
        )
        self.assertEqual(
            result,
            {
                "server": "socks5://127.0.0.1:1080",
                "username": "proxy user",
                "password": "p@ss",
            },
        )

    def test_empty_proxy_is_supported(self):
        self.assertIsNone(playwright_proxy(""))

    def test_result_prefers_chatgpt_domain_cookie(self):
        result = BrowserFallbackResult(
            driver="camoufox",
            final_url="https://auth.openai.com/email-verification",
            stage="email_otp_verification",
            title="Verify email",
            user_agent="Firefox/144.0",
            cookies=[
                {"name": "oai-did", "value": "openai-domain", "domain": ".openai.com"},
                {"name": "oai-did", "value": "chatgpt-domain", "domain": ".chatgpt.com"},
            ],
        )
        self.assertEqual(
            result.cookie_value("oai-did", domain_hint="chatgpt.com"),
            "chatgpt-domain",
        )

    def test_fallback_can_be_disabled_without_importing_a_browser(self):
        fallback = BrowserFallback(enabled=False)
        self.assertFalse(fallback.enabled)


if __name__ == "__main__":
    unittest.main()
