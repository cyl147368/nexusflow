import time
import unittest
from unittest.mock import patch

from mail_providers.icloud_relay import ICloudRelayProvider


class ICloudRelayWaiterTest(unittest.TestCase):
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
