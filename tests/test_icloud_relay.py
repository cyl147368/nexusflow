import time
import unittest
from unittest.mock import patch

from mail_providers.icloud_relay import (
    ICloudRelayProvider,
    _is_internal_relay_url,
    parse_relay_html,
)


class ICloudRelayWaiterTest(unittest.TestCase):
    def test_internal_mailhub_urls_bypass_external_proxy(self):
        self.assertTrue(
            _is_internal_relay_url("http://mail-hub:8090/mail/example")
        )
        self.assertTrue(
            _is_internal_relay_url("http://10.0.0.5:8090/mail/example")
        )
        self.assertTrue(
            _is_internal_relay_url("http://189.24.104.127:8090/mail/example")
        )
        self.assertFalse(
            _is_internal_relay_url("https://relay.example.com/messages/token")
        )

    def test_login_mode_ignores_password_reset_codes(self):
        provider = ICloudRelayProvider(
            "alias@icloud.com",
            "http://mail-hub:8090/mail/example",
        )
        provider.set_otp_mode("login")
        login_message = {
            "sender": "ChatGPT <noreply@example.com>",
            "subject": "Kode masuk ChatGPT sementara Anda",
        }
        reset_message = {
            "sender": "ChatGPT <noreply@example.com>",
            "subject": "Kode reset kata sandi sementara ChatGPT Anda",
        }
        self.assertTrue(provider._looks_like_openai(login_message))
        self.assertFalse(provider._looks_like_openai(reset_message))

        provider.set_otp_mode("password_reset")
        self.assertFalse(provider._looks_like_openai(login_message))
        self.assertTrue(provider._looks_like_openai(reset_message))

    def test_mailhub_epoch_time_is_used_as_message_timestamp(self):
        messages = parse_relay_html(
            "<main><article><header><b>Your verification code</b>"
            "<time>1795172802.6337237</time></header>"
            "<p>ChatGPT</p><div><span>699389</span></div>"
            "</article></main>"
        )
        self.assertTrue(messages)
        self.assertEqual(messages[0]["otp"], "699389")
        self.assertAlmostEqual(messages[0]["ts"], 1795172802.6337237)

    def test_message_arriving_during_initial_snapshot_is_not_marked_seen(self):
        provider = ICloudRelayProvider(
            "alias@icloud.com",
            "http://mail-hub:8090/mail/example",
        )
        now = time.time()
        messages = [{
            "uid": "new-message",
            "sender": "ChatGPT <noreply@example.com>",
            "subject": "Your verification code",
            "body": "",
            "otp": "654321",
            "date_str": "now",
            "ts": now,
        }]
        with patch.object(provider, "_messages", side_effect=[messages, messages]):
            code = provider.wait_for_otp(
                "alias@icloud.com",
                timeout=60,
                issued_after=now - 5,
            )
        self.assertEqual(code, "654321")


if __name__ == "__main__":
    unittest.main()
