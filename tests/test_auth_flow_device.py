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


if __name__ == "__main__":
    unittest.main()
